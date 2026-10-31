from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, write_json
from excutor.excutor_runners.patch_sidecars import write_sidecars
from set_up.appworld_budget_rerun import execute_variant
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.appworld_recover import read
from set_up.native_config import NativeRunConfig


class BudgetRerunTests(unittest.TestCase):
    def test_high_budget_fresh_execution_preserves_reference_and_patch_delivery_then_resumes(self):
        for name in ('trace2skill', 'skillpatch'):
            with self.subTest(method=name), TemporaryDirectory() as temporary:
                root = Path(temporary)
                experiment = root / 'experiment'
                source = experiment / 'recovery'
                folder = source / ('inputs/trace2skill' if name == 'trace2skill' else 'test/skillpatch/input/skill')
                folder.mkdir(parents=True)
                (folder / 'SKILL.md').write_text('original instruction')
                if name == 'trace2skill':
                    (folder / 'references').mkdir()
                    (folder / 'references/detail.md').write_text('reference instruction')
                else:
                    write_sidecars(folder, [{'entry_id': 'P1', 'target': {'relative_path': 'SKILL.md',
                        'scope': 'substep', 'quote': 'original instruction', 'version': sha256_file(folder / 'SKILL.md')},
                        'when_to_use': 'when relevant', 'how_to_interpret': 'supplement'}])
                cfg = NativeRunConfig('appworld', root / 'tasks.json', folder / 'SKILL.md', 'same-executor',
                    'EXECUTION_API_KEY', 'EXECUTION_BASE_URL', 'https://example.invalid/v1', 0, 4096, 300,
                    {'max_steps': 50, 'random_seed': 100, 'server_port_start': 24500}, root / 'unused', 2)
                config_path = source / 'inputs/execution.toml'
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(_execution_toml(cfg))
                refs = [{'task_ref': {'task_id': 'heldout_1', 'split': 'test_normal'}, 'limit': 'output_tokens'}]
                def execute(config, output, environment, **kwargs):
                    self.assertEqual(config.max_tokens, 32768)
                    self.assertEqual(config.runner_options['max_steps'], 300)
                    self.assertEqual(config.model, 'same-executor')
                    self.assertEqual(config.runner_options['random_seed'], 100)
                    if name == 'trace2skill':
                        self.assertIn('reference instruction', config.skill_path.read_text())
                        self.assertNotIn('patch_skill_dir', kwargs)
                    else:
                        self.assertTrue((kwargs['patch_skill_dir'] / 'SKILL_PATCHES.md').exists())
                        self.assertEqual(config.skill_path.read_text(), 'original instruction')
                    archive = output / 'tasks/task_000001.json'
                    row = {'max_steps': 300, 'patch_deployment': {}}
                    if name == 'skillpatch':
                        from excutor.eviskill_runners.launcher import FileSkillBank
                        from excutor.patch_delivery.skill_bank import PatchedSkillBank
                        row['patch_deployment']['skill_digest'] = PatchedSkillBank(FileSkillBank(config.skill_path), kwargs['patch_skill_dir']).digest
                    write_json(archive, row)
                    return {'status': 'completed', 'task_results': [str(archive)]}
                def score(archive, entry, expected):
                    return {'success': False, 'steps': 300}
                output = root / 'high-budget'
                with patch('set_up.appworld_budget_rerun.select_tasks', return_value=refs), \
                     patch('set_up.appworld_budget_rerun.run_native', side_effect=execute) as executor, \
                     patch('set_up.appworld_budget_rerun.check_execution', side_effect=score):
                    result = execute_variant(experiment, output, name, {}, 300, 32768)
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['success'], 0)
                    execute_variant(experiment, output, name, {}, 300, 32768)
                    executor.assert_called_once()
                self.assertEqual(read(output / name / 'plan.json')['max_steps'], 300)


if __name__ == '__main__':
    unittest.main()
