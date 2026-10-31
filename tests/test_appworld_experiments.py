"""Exercise the offline experiment with real stages and synthetic model/world boundaries."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import tree_hashes, write_json
from harness.agent_client import AgentResult
from set_up.appworld_experiments import skillpatch
from set_up.appworld_trajectories import collect
from test_appworld_patch_validation import Runner


class QueuedAgent:
    def __init__(self, payloads, after_write=None):
        self.payloads = list(payloads)
        self.calls = []
        self.after_write = after_write

    def run(self, config, prompt, materials, output, environment, **kwargs):
        self.calls.append((config, list(materials), output))
        if not self.payloads:
            raise AssertionError('Unexpected model call')
        path = output / 'workspace/report.json'
        write_json(path, self.payloads.pop(0))
        if self.after_write:
            self.after_write(len(self.calls))
        return AgentResult('completed', output, path)


def report(*quotes):
    return {'summary': 'Inspect date handling.', 'improvements': [{
        'target': {'skill_ref': 'S0001', 'quote': quote},
        'diagnosis': 'Local dates were compared.',
        'suggestion': 'Use the requested time zone.',
        'evidence': [{'evidence_id': 'T000001', 'quote': 'Compare local dates.'}],
    } for quote in quotes]}


def generation(status='candidate'):
    return {'status': status, 'reason': 'Review date handling.',
            'when_to_use': 'When comparing dates.' if status == 'candidate' else None,
            'how_to_interpret': 'Normalize time zones.' if status == 'candidate' else None}


class AppWorldExperimentTests(unittest.TestCase):
    first_quote = 'Normalize dates before comparing.'
    second_quote = 'Inspect the requested time zone.'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {'DEEPSEEK_API_KEY': 'synthetic-credential-for-tests',
                            'DEEPSEEK_BASE_URL': 'https://fixture.invalid'}
        data = self.root / 'data'
        (data / 'datasets').mkdir(parents=True)
        (data / 'datasets/train.txt').write_text('fixture_1\nfixture_2\n', encoding='utf-8')
        for task_id in ('fixture_1', 'fixture_2'):
            write_json(data / 'tasks' / task_id / 'specs.json', {})
        skill = self.root / 'SKILL.md'
        skill.write_text(f'{self.first_quote} {self.second_quote}', encoding='utf-8')
        tasks = self.root / 'tasks.json'
        write_json(tasks, [{'dataset': 'appworld', 'task_id': name, 'split': 'train'}
                           for name in ('fixture_1', 'fixture_2')])
        self.execution_config = self.root / 'execution.toml'
        self.execution_config.write_text(
            '[task]\nbenchmark = "appworld"\n'
            f'path = {json.dumps(str(tasks))}\nskill_path = {json.dumps(str(skill))}\n'
            '[agent]\nmodel = "fixture-model"\napi_key_env = "DEEPSEEK_API_KEY"\n'
            'base_url_env = "DEEPSEEK_BASE_URL"\ntemperature = 0.0\nmax_tokens = 4096\n'
            '[runtime]\nbackend = "eviskill"\ntimeout_sec = 300\n'
            '[runtime.runner_options]\n'
            f'data_dir = {json.dumps(str(data))}\nauto_server = true\n'
            'server_python = "/fixture/python"\nmax_steps = 50\nrandom_seed = 100\n',
            encoding='utf-8')
        prompt = self.root / 'prompt.md'
        prompt.write_text('Produce an evidence-bound JSON report.', encoding='utf-8')
        self.diagnosis_config = self.root / 'diagnosis.toml'
        self.diagnosis_config.write_text(
            '[diagnosis]\nmodel = "fixture-model"\nbase_url = "https://fixture.invalid"\n'
            'api_key_env = "DEEPSEEK_API_KEY"\n'
            f'prompt_path = {json.dumps(str(prompt))}\n', encoding='utf-8')
        self.stage2_config = self.root / 'stage2.toml'
        self.stage2_config.write_text('[stage2]\n'
            f'generation_config = {json.dumps(str(self.diagnosis_config))}\n', encoding='utf-8')
        self.manifest_path = self.collect_source('shared')

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8'))

    def collect_source(self, name, *, success=False):
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner(success=success)):
            state = collect(self.execution_config, self.root / name, self.environment)
        self.assertEqual(state['status'], 'completed', state)
        return Path(state['manifest_path'])

    def execute(self, output='method', **kwargs):
        return skillpatch(kwargs.pop('manifest_path', self.manifest_path), self.root / output,
                          kwargs.pop('environment', self.environment),
                          diagnosis_config=self.diagnosis_config,
                          stage2_config=self.stage2_config, **kwargs)

    def test_real_stages_replay_each_candidate_independently_and_keep_shared_input(self):
        before = tree_hashes(self.manifest_path.parent)
        client = QueuedAgent([report(self.first_quote, self.second_quote),
                              generation(), generation(), report()])
        runners = [Runner(success=True), Runner(success=False)]
        with patch('harness.agent_client.AgentClient.run', side_effect=client.run) as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner', side_effect=runners) as create:
            state = self.execute()
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual((state['candidate_count'], state['replayed_count'], state['repaired_count']), (2, 2, 1))
        self.assertEqual(agent.call_count, 4)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(state['acceptance'], 'not_decided')
        self.assertFalse(state['active_version_changed'])
        self.assertFalse(state['validation_split_used'])
        self.assertEqual(state['tasks'][1]['status'], 'no_supported_issue')
        self.assertEqual(tree_hashes(self.manifest_path.parent), before)
        baseline = self.read(self.manifest_path)['rows'][0]['source']
        for issue, runner in zip(state['tasks'][0]['issues'], runners):
            replay = Path(issue['replay'])
            binding = self.read(replay / 'binding.json')
            self.assertEqual(binding['previous_patch_count'], 0)
            self.assertEqual(binding['candidate_patch_count'], 1)
            self.assertEqual(issue['comparison']['baseline_trajectory'], baseline)
            self.assertEqual([call[0]['task_id'] for call in runner.calls], ['fixture_1'])
            self.assertTrue(runner.closed)
        self.assertEqual(state['tasks'][0]['issues'][1]['comparison']['outcome'], 'still_failed')

    def test_successful_tasks_skip_agents_and_leave_smoke_incomplete(self):
        manifest = self.collect_source('all-succeeded', success=True)
        with patch('harness.agent_client.AgentClient.run') as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            state = self.execute(manifest_path=manifest, profile='smoke')
        self.assertEqual(state['status'], 'incomplete', state)
        self.assertEqual(state['replayed_count'], 0)
        self.assertEqual([task['reason'] for task in state['tasks']], ['succeeded', 'succeeded'])
        agent.assert_not_called()
        runner.assert_not_called()

    def test_no_supported_issue_and_expected_non_candidates_are_not_failures(self):
        for outcome in ('no_issue', 'insufficient_evidence', 'unpatchable'):
            with self.subTest(outcome=outcome):
                payloads = [report()] if outcome == 'no_issue' else [report(self.first_quote), generation(outcome)]
                client = QueuedAgent(payloads)
                with patch('harness.agent_client.AgentClient.run', side_effect=client.run), \
                        patch('excutor.eviskill_runners.launcher.create_runner') as runner:
                    state = self.execute(output=outcome, profile='smoke', task_limit=1)
                self.assertEqual(state['status'], 'incomplete', state)
                self.assertEqual((state['candidate_count'], state['replayed_count']), (0, 0))
                self.assertNotIn('error', state)
                if outcome == 'no_issue':
                    self.assertEqual(state['tasks'][0]['status'], 'no_supported_issue')
                else:
                    self.assertEqual(state['tasks'][0]['issues'][0]['generation_status'], outcome)
                runner.assert_not_called()

    def test_prepare_requires_no_credentials_runner_or_agent(self):
        with patch('harness.agent_client.AgentClient.run') as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            state = self.execute(profile='smoke', environment={}, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        self.assertEqual([task['status'] for task in state['tasks']], ['prepared', 'prepared'])
        self.assertEqual(state['candidate_count'], 0)
        self.assertTrue((self.root / 'method/tasks/000001/diagnosis/input/context.json').is_file())
        self.assertFalse(self.read(self.root / 'method/plan.json')['test_split_used'])
        agent.assert_not_called()
        runner.assert_not_called()

    def test_replay_preflight_exception_is_saved_and_stops_later_work(self):
        client = QueuedAgent([report(self.first_quote, self.second_quote), generation()])
        with patch('harness.agent_client.AgentClient.run', side_effect=client.run) as agent, \
                patch('set_up.appworld_experiments.run_validation', side_effect=ValueError('Broken replay source')) as replay:
            state = self.execute()
        self.assertEqual(state['status'], 'failed', state)
        self.assertEqual(state['phase'], 'replay')
        self.assertIn('Broken replay source', state['error'])
        self.assertEqual(len(state['tasks']), 1)
        self.assertEqual(len(state['tasks'][0]['issues']), 1)
        self.assertEqual(state['tasks'][0]['issues'][0]['issue_id'], 'I000001')
        self.assertEqual((state['candidate_count'], state['replayed_count']), (1, 0))
        self.assertEqual(agent.call_count, 2)
        replay.assert_called_once()
        self.assertEqual(self.read(self.root / 'method/result.json'), state)

    def test_output_inside_shared_archive_is_rejected_without_calls(self):
        for output in ('shared/nested', 'shared/execution/nested'):
            with self.subTest(output=output), \
                    patch('harness.agent_client.AgentClient.run') as agent, \
                    patch('excutor.eviskill_runners.launcher.create_runner') as runner:
                with self.assertRaisesRegex(ValueError, 'outside the shared trajectory archives'):
                    self.execute(output=output, prepare_only=True)
                self.assertFalse((self.root / output).exists())
                agent.assert_not_called()
                runner.assert_not_called()

    def test_limits_select_first_tasks_and_first_reported_issues(self):
        client = QueuedAgent([report(self.first_quote, self.second_quote), generation()])
        with patch('harness.agent_client.AgentClient.run', side_effect=client.run) as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()) as runner:
            state = self.execute(task_limit=1, max_issues_per_task=1, profile='smoke')
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual([task['task_id'] for task in state['tasks']], ['fixture_1'])
        self.assertEqual(state['tasks'][0]['reported_issue_count'], 2)
        self.assertEqual([issue['issue_id'] for issue in state['tasks'][0]['issues']], ['I000001'])
        self.assertEqual(agent.call_count, 2)
        runner.assert_called_once()
        plan = self.read(self.root / 'method/plan.json')
        self.assertEqual(plan['selected_task_ids'], ['fixture_1'])
        self.assertEqual((plan['task_limit'], plan['max_issues_per_task']), (1, 1))

    def test_negative_limits_fail_before_output_creation(self):
        for limits in ({'task_limit': -1}, {'max_issues_per_task': -1}):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, 'nonnegative'):
                self.execute(**limits)
        self.assertFalse((self.root / 'method').exists())

    def test_tampered_initial_skill_is_rejected_before_model_calls(self):
        manifest = self.read(self.manifest_path)
        Path(manifest['initial_skill']['path']).write_text('Changed frozen Skill.', encoding='utf-8')
        with patch('harness.agent_client.AgentClient.run') as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            with self.assertRaisesRegex(ValueError, 'integrity check failed'):
                self.execute()
        self.assertFalse((self.root / 'method').exists())
        agent.assert_not_called()
        runner.assert_not_called()

    def test_agent_changing_frozen_source_fails_before_generation(self):
        source = Path(self.read(self.manifest_path)['rows'][0]['source'])

        def mutate_source(_call_count):
            value = self.read(source)
            value['instruction'] = 'Changed by the diagnosis agent.'
            write_json(source, value)

        client = QueuedAgent([report(self.first_quote)], after_write=mutate_source)
        with patch('harness.agent_client.AgentClient.run', side_effect=client.run) as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            state = self.execute()
        self.assertEqual(state['status'], 'failed', state)
        self.assertEqual(state['phase'], 'diagnosis')
        self.assertIn('integrity check failed', state['error'])
        self.assertEqual(state['candidate_count'], 0)
        self.assertEqual(state['tasks'][0]['issues'], [])
        agent.assert_called_once()
        runner.assert_not_called()
        self.assertEqual(self.read(self.root / 'method/result.json'), state)

    def test_missing_credentials_fail_before_model_calls(self):
        with patch('harness.agent_client.AgentClient.run') as agent, \
                patch('excutor.eviskill_runners.launcher.create_runner') as runner:
            with self.assertRaisesRegex(ValueError, 'Missing credential'):
                self.execute(environment={})
        self.assertFalse((self.root / 'method').exists())
        agent.assert_not_called()
        runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
