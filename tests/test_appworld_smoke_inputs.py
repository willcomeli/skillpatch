"""Frozen AppWorld configurations feed all method smoke branches unchanged."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from method_input_adapters.execution import load_tasks
from set_up.appworld_smoke_inputs import prepare_inputs
from set_up.appworld_trajectories import collect, load_manifest
from set_up.native_config import load_native_config
from test_appworld_trajectories import Runner


class AppWorldSmokeInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'data'
        (self.data / 'datasets').mkdir(parents=True)
        (self.data / 'datasets/train.txt').write_text('fixture_1\nfixture_2\n', encoding='utf-8')
        (self.data / 'datasets/dev.txt').write_text('heldout_3\nheldout_1\n', encoding='utf-8')
        for task_id in ('fixture_1', 'fixture_2', 'heldout_3', 'heldout_1'):
            write_json(self.data / 'tasks' / task_id / 'specs.json', {})
        self.refs = [{'dataset': 'appworld', 'task_id': name, 'split': 'train',
                      'instruction': 'Use the exact saved task reference.'}
                     for name in ('fixture_2', 'fixture_1')]
        write_json(self.root / 'tasks.json', self.refs)
        (self.root / 'SKILL.md').write_text('Normalize dates. 保留时区。\n', encoding='utf-8')
        self.config_path = self.root / 'execution.toml'
        self.config_path.write_text(
            '[task]\nbenchmark="appworld"\npath="tasks.json"\nskill_path="SKILL.md"\n'
            '[agent]\nname="native"\nmodel="fixture-model"\n'
            'api_key_env="DEEPSEEK_API_KEY"\nbase_url_env="DEEPSEEK_BASE_URL"\n'
            'temperature=0.25\nmax_tokens=1234\nmax_retries=2\n'
            '[runtime]\nbackend="eviskill"\ntimeout_sec=76\n'
            '[runtime.runner_options]\ndata_dir="data"\nauto_server=true\n'
            'server_python="/fixture/venv/bin/python"\nmax_steps=7\nrandom_seed=42\n'
            'action_history_length=4\nenvironment_timeout=31\nparallel_workers=1\n'
            'general_skill_retrieval_threshold=0.75\n'
            '[output]\nroot="runs"\n', encoding='utf-8')
        self.environment = {'DEEPSEEK_API_KEY': 'synthetic-smoke-input-test-key',
                            'DEEPSEEK_BASE_URL': 'https://fixture.example/v1'}
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()):
            result = collect(self.config_path, self.root / 'shared', self.environment)
        self.assertEqual(result['status'], 'completed', result)
        self.manifest_path = Path(result['manifest_path'])
        self.folder = self.root / 'method/inputs'

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def test_config_roundtrip_preserves_all_execution_settings_and_budget(self):
        before = tree_hashes(self.manifest_path.parent)
        original = load_native_config(self.config_path)
        self.config_path.write_text('Original editable configuration has changed.', encoding='utf-8')
        self.folder.parent.mkdir()
        with patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            inputs = prepare_inputs(self.manifest_path, self.folder)
        expected = replace(original, base_url=self.environment['DEEPSEEK_BASE_URL'],
                           tasks_path=self.folder / 'tasks.json', skill_path=inputs['initial_skill'],
                           output_root=self.folder / 'executions')
        self.assertEqual(load_native_config(inputs['execution_config']), expected)
        self.assertEqual(tree_hashes(self.manifest_path.parent), before)
        self.assertEqual(inputs['manifest_path'], str(self.manifest_path))
        self.assertEqual(inputs['manifest_sha256'], sha256_file(self.manifest_path))
        self.assertEqual(self.read(self.folder / 'trajectory_manifest.snapshot.json'), inputs['manifest'])
        runner.assert_not_called()

    def test_method_manifests_keep_train_order_and_choose_real_dev_refs(self):
        inputs = prepare_inputs(self.manifest_path, self.folder, dev_limit=2)
        train = inputs['train_entries']
        validation = inputs['validation_entries']
        self.assertEqual([entry['task_ref'] for entry in train], self.refs)
        self.assertEqual([entry['id'] for entry in train], ['fixture_2', 'fixture_1'])
        self.assertEqual([entry['id'] for entry in validation], ['heldout_3', 'heldout_1'])
        self.assertTrue(all(entry['split'] == 'train' for entry in train))
        self.assertTrue(all(entry['split'] == 'val' and entry['task_ref']['split'] == 'dev'
                            for entry in validation))
        self.assertFalse({entry['id'] for entry in train} & {entry['id'] for entry in validation})
        self.assertEqual(load_tasks(inputs['method_manifest']), train + validation)
        self.assertEqual(load_tasks(inputs['validation_manifest']), validation)

    def test_initial_skill_folder_contains_only_exact_frozen_skill(self):
        frozen = load_manifest(self.manifest_path)
        (self.root / 'SKILL.md').write_text('Edited original Skill.', encoding='utf-8')
        inputs = prepare_inputs(self.manifest_path, self.folder)
        self.assertEqual(list(inputs['initial_skill_dir'].iterdir()), [inputs['initial_skill']])
        self.assertEqual(inputs['initial_skill'].read_bytes(), Path(frozen['initial_skill']['path']).read_bytes())
        self.assertEqual(self.read(self.folder / 'tasks.json'), self.refs)
        self.assertEqual([entry['id'] for entry in inputs['validation_entries']], ['heldout_3'])

    def test_changed_frozen_source_is_rejected_before_outputs(self):
        manifest = load_manifest(self.manifest_path)
        Path(manifest['rows'][0]['source']).write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'integrity check failed'):
            prepare_inputs(self.manifest_path, self.folder)
        self.assertFalse(self.folder.exists())

    def test_missing_pinned_endpoint_is_rejected(self):
        manifest = load_manifest(self.manifest_path)
        config_path = Path(manifest['execution_config']['path'])
        config = self.read(config_path)
        config['base_url'] = None
        write_json(config_path, config)
        manifest['execution_config']['sha256'] = sha256_file(config_path)
        write_json(self.manifest_path, manifest)
        with self.assertRaisesRegex(ValueError, 'explicit base_url'):
            prepare_inputs(self.manifest_path, self.folder)
        self.assertFalse(self.folder.exists())

    def test_overlapping_dev_task_is_rejected(self):
        (self.data / 'datasets/dev.txt').write_text('fixture_2\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'overlap'):
            prepare_inputs(self.manifest_path, self.folder)
        self.assertFalse(self.folder.exists())

    def test_output_reuse_and_nested_source_output_are_rejected(self):
        prepare_inputs(self.manifest_path, self.folder)
        before = tree_hashes(self.folder)
        with self.assertRaises(FileExistsError):
            prepare_inputs(self.manifest_path, self.folder)
        self.assertEqual(tree_hashes(self.folder), before)
        with self.assertRaisesRegex(ValueError, 'outside the shared trajectory archives'):
            prepare_inputs(self.manifest_path, self.manifest_path.parent / 'nested')

    def test_invalid_dev_limits_are_rejected(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'positive integer'):
                prepare_inputs(self.manifest_path, self.folder, dev_limit=limit)
        self.assertFalse(self.folder.exists())


if __name__ == '__main__':
    unittest.main()
