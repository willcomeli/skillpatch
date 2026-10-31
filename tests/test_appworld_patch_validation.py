"""Real diagnosis/generation/sidecars/launcher with a synthetic AppWorld runner."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json, tree_hashes
from excutor.eviskill_runners.launcher import run_native
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from harness.agent_client import AgentResult
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import Stage2Config
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_validation.appworld_patch_validation.pipeline import run_validation, read
from set_up.native_config import NativeRunConfig
from set_up.cli import main


class Client:
    def __init__(self, payload):
        self.payload = payload

    def run(self, config, prompt, materials, output, environment, **kwargs):
        path = output / 'workspace/report.json'
        write_json(path, self.payload)
        return AgentResult('completed', output, path)


class Runner:
    def __init__(self, success=False, invalid=False):
        self.success, self.invalid = success, invalid
        self.calls = []
        self.closed = False

    def run_task(self, task, bank, **kwargs):
        self.calls.append((task, kwargs))
        if kwargs.get('patch_skill_dir'):
            bank = PatchedSkillBank(bank, kwargs['patch_skill_dir'])
        row = {'dataset': 'appworld', 'task_id': task['task_id'], 'split': task['split'],
               'instruction': 'Compare dates.', 'success': self.success, 'score': int(self.success),
               'task_completed': self.success, 'steps': 1,
               'retrieved_memory_text': bank.format_for_prompt({}),
               'trajectory': [{'action': 'Compare local dates.', 'next_observation': 'Wrong day.'}],
               'evaluation_summary': {'num_tests': 1, 'passed_tests': int(self.success),
                                      'failed_tests': int(not self.success)}}
        if kwargs.get('patch_skill_dir'):
            row['patch_deployment'] = {'skill_dir': str(kwargs['patch_skill_dir'])}
        if self.invalid:
            row['evaluation_summary']['num_tests'] = 0
        return row

    def close(self):
        self.closed = True


class AppWorldReplayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.skill = self.root / 'initial.md'
        self.skill.write_text('Normalize dates before comparing. Inspect the requested time zone.')
        tasks = self.root / 'tasks.json'
        write_json(tasks, [{'task_id': 'fixture_1', 'split': 'train'},
                           {'task_id': 'fixture_2', 'split': 'train'}])
        self.config = NativeRunConfig('appworld', tasks, self.skill, 'fixture',
            'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', None, 0.0, 4096, 300,
            {'data_dir': str(self.root), 'auto_server': True, 'server_python': '/fixture/python',
             'max_steps': 50, 'random_seed': 100}, self.root)
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()):
            state = run_native(self.config, self.root / 'baseline', {})
        self.assertEqual(state['status'], 'completed', state)
        # Deliberately diagnose the second task, to verify task selection.
        self.source = Path(state['task_results'][1])
        self.patch_file = self.generate(self.source, 'first')

    def generate(self, source, name, quote='Normalize dates before comparing.'):
        project = Path(__file__).resolve().parents[1]
        gen = project / 'configs/skillpatch-generation.toml'
        diagnosis = self.root / f'{name}-diagnosis'
        report = {'summary': 'Normalize dates.', 'improvements': [{
            'target': {'skill_ref': 'S0001', 'quote': quote},
            'diagnosis': 'Local dates were compared.', 'suggestion': 'Normalize time zones.',
            'evidence': [{'evidence_id': 'T000001', 'quote': 'Compare local dates.'}]}]}
        state = run_discovery(source, load_discovery_config(gen), diagnosis, {}, client=Client(report))
        self.assertEqual(state['status'], 'completed', state)
        state = run_stage2(diagnosis, 'I000001', Stage2Config(gen), self.root / f'{name}-generation', {},
            client=Client({'status': 'candidate', 'reason': 'Clarify comparison.',
                'when_to_use': 'When comparing dates.', 'how_to_interpret': 'Normalize time zones.'}))
        self.assertEqual(state['status'], 'generated', state)
        return Path(state['patch_path'])

    def test_replays_only_source_task_with_patch_and_keeps_originals(self):
        before = tree_hashes(self.root / 'baseline')
        self.skill.write_text('Original file has since changed.')
        runner = Runner(success=True)
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner) as create:
            state = run_validation(self.patch_file, self.root / 'out', {})
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['comparison']['outcome'], 'repaired')
        self.assertEqual(state['comparison']['score_delta'], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0]['task_id'], 'fixture_2')
        cfg = create.call_args.args[0]
        self.assertEqual(cfg.model, self.config.model)
        self.assertEqual(cfg.runner_options, self.config.runner_options)
        self.assertEqual(tree_hashes(self.root / 'baseline'), before)
        self.assertTrue(runner.closed)
        self.assertEqual(state['acceptance'], 'not_decided')

    def test_runtime_dataset_symlink_allows_replay_without_changing_source(self):
        baseline = self.root / 'baseline'
        before = {path.relative_to(baseline): path.read_bytes()
                  for path in baseline.rglob('*') if path.is_file()}
        dataset = self.root / 'external-dataset'
        dataset.mkdir()
        marker = dataset / 'fixture.txt'
        marker.write_text('External dataset contents must stay unchanged.')
        runtime = baseline / 'appworld_runtime'
        runtime.mkdir()
        link = runtime / 'data'
        link.symlink_to(dataset, target_is_directory=True)
        original_target = link.readlink()
        runner = Runner(success=True)
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=runner):
            state = run_validation(self.patch_file, self.root / 'out', {})
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['comparison']['outcome'], 'repaired')
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.readlink(), original_target)
        after = {path.relative_to(baseline): path.read_bytes()
                 for path in baseline.rglob('*') if path.is_file() and not path.is_symlink()}
        self.assertEqual(after, before)
        self.assertEqual(marker.read_text(), 'External dataset contents must stay unchanged.')
        self.assertTrue(runner.closed)

    def test_runtime_dataset_symlink_retargeting_is_detected_after_replay(self):
        runtime = self.root / 'baseline/appworld_runtime'
        runtime.mkdir()
        first, second = self.root / 'dataset-first', self.root / 'dataset-second'
        first.mkdir()
        second.mkdir()
        # Equal contents ensure that the archived link identity itself is checked.
        for dataset in (first, second):
            (dataset / 'fixture.txt').write_text('Identical dataset contents.')
        link = runtime / 'data'
        link.symlink_to(first, target_is_directory=True)

        def replay_then_retarget(*args, **kwargs):
            result = run_native(*args, **kwargs)
            self.assertEqual(result['status'], 'completed', result)
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
            return result

        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner(success=True)), \
             patch('method.skillpatch.patch_validation.appworld_patch_validation.pipeline.run_native',
                   side_effect=replay_then_retarget) as replay:
            state = run_validation(self.patch_file, self.root / 'out', {})
        replay.assert_called_once()
        self.assertEqual(state['status'], 'failed', state)
        self.assertIn('Historical input archives changed', state['error'])
        self.assertEqual(read(self.root / 'out/result.json')['status'], 'failed')

    def test_prepare_cli_does_not_start_runner(self):
        with patch('excutor.eviskill_runners.launcher.create_runner') as create:
            code = main(['validate-appworld-patch', '--patch', str(self.patch_file),
                         '--output', str(self.root / 'prepare'), '--prepare-only'])
        self.assertEqual(code, 0)
        create.assert_not_called()
        self.assertEqual(read(self.root / 'prepare/result.json')['status'], 'prepared')

    def test_still_failed_is_valid_completed_replay(self):
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()):
            state = run_validation(self.patch_file, self.root / 'out', {})
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['comparison']['outcome'], 'still_failed')

    def test_invalid_official_evaluation_is_not_scored_as_failure(self):
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner(invalid=True)):
            state = run_validation(self.patch_file, self.root / 'out', {})
        self.assertEqual(state['status'], 'failed', state)
        self.assertNotIn('comparison', state)

    def test_tampered_source_rejected_before_execution(self):
        row = read(self.source)
        row['instruction'] = 'Changed task.'
        write_json(self.source, row)
        with patch('excutor.eviskill_runners.launcher.create_runner') as create:
            with self.assertRaises(ValueError):
                run_validation(self.patch_file, self.root / 'out', {})
        create.assert_not_called()

    def test_previous_patches_preserved_when_next_candidate_added(self):
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()):
            first = run_validation(self.patch_file, self.root / 'first-replay', {})
        self.assertEqual(first['status'], 'completed', first)
        second_patch = self.generate(Path(first['comparison']['candidate_trajectory']), 'second',
                                     quote='Inspect the requested time zone.')
        second = run_validation(second_patch, self.root / 'second-replay', {}, prepare_only=True)
        self.assertEqual(second['status'], 'prepared', second)
        binding = read(self.root / 'second-replay/binding.json')
        self.assertEqual(binding['previous_patch_count'], 1)
        self.assertEqual(binding['candidate_patch_count'], 2)

    def test_output_reuse_and_nested_output_rejected(self):
        for output in (self.root / 'baseline', self.root / 'baseline/nested'):
            with self.assertRaises(ValueError):
                run_validation(self.patch_file, output, {}, prepare_only=True)


if __name__ == '__main__':
    unittest.main()
