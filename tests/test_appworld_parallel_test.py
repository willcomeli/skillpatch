from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import tree_hashes, write_json
from set_up.appworld_parallel_test import evaluate_variant
from set_up.appworld_recover import CachedBridge, read
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.native_config import NativeRunConfig, load_native_config


class ParallelTestTests(unittest.TestCase):
    def test_isolated_port_and_shared_cache_preserve_frozen_executor(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / 'inputs'
            for name in ['initial_skill', 'trace2skill']:
                (inputs / name).mkdir(parents=True)
                (inputs / name / 'SKILL.md').write_text(name)
            refs = [{'task_id': 'heldout_1', 'split': 'test_normal'}]
            write_json(inputs / 'test_tasks.json', refs)
            config = NativeRunConfig('appworld', inputs / 'test_tasks.json', inputs / 'initial_skill/SKILL.md',
                'executor', 'EXECUTION_API_KEY', 'EXECUTION_BASE_URL', 'https://example.invalid', 0, 4096, 300,
                {'server_port_start': 24500, 'random_seed': 100}, root / 'unused')
            config_path = inputs / 'execution.toml'
            config_path.write_text(_execution_toml(config))
            write_json(root / 'plan.json', {'test_tasks': refs})
            write_json(root / 'input_hashes.json', tree_hashes(inputs))
            before = tree_hashes(inputs)
            def execute(entry, content, output, *, skill_dir):
                actual = load_native_config(Path(entry['config']))
                self.assertEqual(actual.runner_options, {'server_port_start': 24600, 'random_seed': 100})
                self.assertEqual((actual.model, actual.max_tokens), ('executor', 4096))
                archive = output / 'execution/tasks/task_000001.json'
                write_json(archive, {'text': (skill_dir / 'SKILL.md').read_text()})
                write_json(archive.parent.parent / 'config.json', {'port': 24600})
                return archive
            def checked(archive, entry, expected):
                self.assertEqual(read(archive)['text'], expected)
                return {'archive': str(archive), 'task_id': entry['id'], 'success': True, 'score': 1}
            with patch('method_input_adapters.execution.SharedExecutorBridge.run', side_effect=execute) as execute_mock, \
                 patch('set_up.appworld_parallel_test.check_execution', side_effect=checked), \
                 patch('set_up.appworld_recover.check_execution', side_effect=checked):
                result = evaluate_variant(root, 'initial', 24600, {})
                self.assertEqual(result['status'], 'completed')
                entry = {'id': 'heldout_1', 'split': 'test', 'task_ref': refs[0], 'config': str(config_path)}
                CachedBridge({}, root / 'executions').run(entry, '', root / 'unused', skill_dir=inputs / 'initial_skill')
                execute_mock.assert_called_once()
            self.assertEqual(tree_hashes(inputs), before)
            (inputs / 'trace2skill/SKILL.md').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'inputs changed'):
                evaluate_variant(root, 'initial', 24600, {})


if __name__ == '__main__':
    unittest.main()
