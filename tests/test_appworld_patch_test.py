"""Frozen patch test deployment and interrupted-task recovery without live models."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, write_json
from excutor.excutor_runners.patch_sidecars import load_sidecars, write_sidecars
from excutor.eviskill_runners.launcher import FileSkillBank
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from set_up.appworld_patch_test import evaluate, training_entries
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.native_config import NativeRunConfig
from set_up.appworld_recover import read


class PatchTestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / 'SKILL.md'
        self.skill.write_text('Inspect API docs. Verify the final state.')
        self.manifest_path = self.root / 'manifest.json'
        self.manifest = {'initial_skill': {'path': str(self.skill)}, 'rows': [
            {'task_id': 'failed_1', 'score': 0}, {'task_id': 'passed_1', 'score': 1}]}
        write_json(self.manifest_path, self.manifest)
        self.training = self.root / 'training'
        issues = []
        for i, outcome in enumerate(['repaired', 'still_failed']):
            replay = self.root / f'replay{i}'
            bundle = replay / 'candidate_skill'
            bundle.mkdir(parents=True)
            (bundle / 'SKILL.md').write_bytes(self.skill.read_bytes())
            write_sidecars(bundle, [{'entry_id': f'P{i}', 'target': {
                'relative_path': 'SKILL.md', 'scope': 'substep', 'quote': 'Inspect API docs.',
                'version': sha256_file(self.skill)}, 'when_to_use': f'condition {i}', 'how_to_interpret': f'rule {i}'}])
            write_json(replay / 'result.json', {'status': 'completed'})
            issues.append({'issue_id': f'I{i}', 'generation_status': 'generated',
                           'replay_status': 'completed', 'replay': str(replay),
                           'comparison': {'outcome': outcome}})
        self.state = {'status': 'completed', 'candidate_count': 2,
                      'trajectory_manifest': str(self.manifest_path),
                      'tasks': [{'task_id': 'failed_1', 'status': 'completed', 'issues': issues}]}
        write_json(self.training / 'result.json', self.state)
        self.experiment = self.root / 'experiment'
        inputs = self.experiment / 'inputs'
        inputs.mkdir(parents=True)
        config = NativeRunConfig('appworld', self.root / 'unused.json', self.skill, 'execution-model',
            'EXECUTION_API_KEY', 'EXECUTION_BASE_URL', 'https://example.invalid/v1', 0, 4096, 300,
            {'data_dir': str(self.root / 'data')}, self.root / 'runs')
        (inputs / 'execution.toml').write_text(_execution_toml(config))
        self.output = self.root / 'test'
        self.refs = [{'task_id': f'unseen_{i}', 'split': 'test_normal', 'dataset': 'appworld'} for i in [1, 2]]
        self.calls = []

    def official(self, data, split):
        return self.refs if split == 'test_normal' else [{'task_id': f'{split}_1'}]

    def execute(self, config, output, environment, *, patch_skill_dir):
        self.calls.append(output)
        self.assertEqual(config.model, 'execution-model')
        bank = PatchedSkillBank(FileSkillBank(config.skill_path), patch_skill_dir)
        self.assertEqual(len(load_sidecars(patch_skill_dir)['entries']), 2)
        task = read(config.tasks_path)[0]
        archive = output / 'task.json'
        write_json(archive, {'task_id': task['task_id'], 'retrieved_memory_text': bank.format_for_prompt({}),
                            'patch_deployment': {'skill_digest': bank.digest}})
        return {'status': 'completed', 'task_results': [str(archive)]}

    def checked(self, archive, entry, expected):
        self.assertEqual(read(archive)['retrieved_memory_text'], expected)
        return {'archive': str(archive), 'task_id': entry['id'], 'success': False, 'score': 0}

    def test_all_training_patches_are_included_even_without_repair(self):
        entries, _ = training_entries(self.state, self.manifest)
        self.assertEqual([e['entry_id'] for e in entries], ['P0', 'P1'])
        self.state['status'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'must finish'):
            training_entries(self.state, self.manifest)

    def test_frozen_deployment_resume_and_valid_zero_score(self):
        def flaky(config, output, environment, **kwargs):
            if len(self.calls) == 1:
                self.calls.append(output)
                return {'status': 'backend_error', 'task_results': []}
            return self.execute(config, output, environment, **kwargs)
        with patch('set_up.appworld_patch_test.load_manifest', return_value=self.manifest), \
             patch('set_up.appworld_patch_test.load_official_appworld_split', side_effect=self.official), \
             patch('set_up.appworld_patch_test.check_execution', side_effect=self.checked), \
             patch('set_up.appworld_patch_test.run_native', side_effect=flaky):
            first = evaluate(self.training, self.experiment, self.output, {})
            self.assertEqual(first['status'], 'failed')
            second = evaluate(self.training, self.experiment, self.output, {})
            self.assertEqual(second['status'], 'completed')
            self.assertEqual(second['success_rate'], 0)
            self.assertEqual(len(self.calls), 3)
            self.assertNotEqual(self.calls[1], self.calls[2])
            self.assertEqual((self.output / 'input/skill/SKILL.md').read_bytes(), self.skill.read_bytes())
            (self.output / 'input/skill/SKILL_PATCHES.md').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'inputs changed'):
                evaluate(self.training, self.experiment, self.output, {})

    def test_test_overlap_rejected_before_model_call(self):
        self.refs[0]['task_id'] = 'train_1'
        with patch('set_up.appworld_patch_test.load_manifest', return_value=self.manifest), \
             patch('set_up.appworld_patch_test.load_official_appworld_split', side_effect=self.official), \
             patch('set_up.appworld_patch_test.run_native') as execute:
            with self.assertRaisesRegex(ValueError, 'disjoint'):
                evaluate(self.training, self.experiment, self.output, {})
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
