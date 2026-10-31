"""Three-method smoke acceptance with real archives and synthetic model/world boundaries."""

from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from method_input_adapters.execution import SharedExecutorBridge, load_tasks
from set_up.appworld_smoke import run_smoke
from set_up.appworld_trajectories import load_manifest
from set_up.native_config import load_native_config
import test_appworld_experiments as experiment_fixtures
from test_appworld_experiments import QueuedAgent, generation, report
from test_appworld_patch_validation import Runner


class AppWorldSmokeTests(unittest.TestCase):
    first_quote = experiment_fixtures.AppWorldExperimentTests.first_quote
    second_quote = experiment_fixtures.AppWorldExperimentTests.second_quote
    collect_source = experiment_fixtures.AppWorldExperimentTests.collect_source
    read = staticmethod(experiment_fixtures.AppWorldExperimentTests.read)

    def setUp(self):
        experiment_fixtures.AppWorldExperimentTests.setUp(self)
        data = self.root / 'data'
        (data / 'datasets/dev.txt').write_text('validation_1\nvalidation_2\n')
        for task_id in ('validation_1', 'validation_2'):
            write_json(data / 'tasks' / task_id / 'specs.json', {})
        self.initial = Path(self.read(self.manifest_path)['initial_skill']['path']).read_text()
        self.reference = 'Resolve each time-zone offset before comparing calendar dates.'
        self.optimizer_calls = {}
        self.world = Runner()
        runner_patch = patch('excutor.eviskill_runners.launcher.create_runner', return_value=self.world)
        self.runner_factory = runner_patch.start()
        self.addCleanup(runner_patch.stop)
        network_patch = patch('excutor.eviskill_runners.launcher.urlopen',
                              side_effect=AssertionError('Tests must not call a live model'))
        self.network = network_patch.start()
        self.addCleanup(network_patch.stop)

    def fake_train(self, manifest, initial_skill, output, environment, **kwargs):
        """Stand in for the optimizer, exercising the real frozen adapter and dev execution."""
        from method_input_adapters.skillopt.adapter import SharedExecutionAdapter
        self.optimizer_calls['skillopt'] = (Path(manifest), Path(initial_skill), kwargs)
        output = Path(output)
        output.mkdir()
        entries = load_tasks(manifest)
        adapter = SharedExecutionAdapter(entries, SharedExecutorBridge(environment),
            frozen_trajectory_manifest=kwargs['frozen_trajectory_manifest'], initial_skill=initial_skill)
        train = [entry for entry in entries if entry['split'] == 'train']
        validation = [entry for entry in entries if entry['split'] == 'val']
        initial = Path(initial_skill).read_text()
        candidate = initial + '\nAlways resolve the requested time zone before comparing.'
        step = output / 'steps/step_0001'
        adapter.rollout(validation, initial, output / 'selection_eval_baseline')
        adapter.rollout(train, initial, step / 'rollout', use_eval_feedback=True)
        adapter.rollout(validation, candidate, step / 'selection_eval')
        (step / 'candidate_skill.md').write_text(candidate)
        (output / 'best_skill.md').write_text(initial)
        write_json(step / 'step_record.json', {'action': 'reject', 'selection_hard': 0.0})
        write_json(output / 'history.json', [{'step': 1, 'action': 'reject'}])
        return {'best_score': 0.0}

    def fake_evolve(self, sources, initial_skill_dir, output, environment, **kwargs):
        """Only the reference changes; real bridges must still deliver its entire text."""
        from method_input_adapters.trace2skill.results import export_results
        self.optimizer_calls['trace2skill'] = (list(sources), Path(initial_skill_dir), kwargs)
        output = Path(output)
        output.mkdir()
        export_results(sources, output / 'input')
        candidate = output / 'skill'
        shutil.copytree(initial_skill_dir, candidate)
        (candidate / 'references').mkdir()
        (candidate / 'references/dates.md').write_text(self.reference)
        bridge = SharedExecutorBridge(environment)
        validation = []
        for entry in (load_tasks(kwargs['validation_manifest']) if kwargs.get('validation_manifest') else []):
            folder = output / 'validation' / entry['id']
            before = bridge.run(entry, '', folder / 'baseline', skill_dir=initial_skill_dir)
            after = bridge.run(entry, '', folder / 'candidate', skill_dir=candidate)
            validation.append({'id': entry['id'], 'baseline_archive': str(before),
                               'candidate_archive': str(after)})
        write_json(output / 'validation.json', validation)
        return {'status': 'completed', 'skill_dir': str(candidate), 'skill_changed': True,
                'analysis_records': len(sources), 'validation_status': 'executed'}

    def run_all(self, output='smoke', *, train=None, evolve=None, **kwargs):
        agent = QueuedAgent([report(self.first_quote), generation(), report()])
        with ExitStack() as stack:
            stack.enter_context(patch('method_input_adapters.skillopt.training.train',
                                      side_effect=train or self.fake_train))
            stack.enter_context(patch('method_input_adapters.trace2skill.evolution.evolve',
                                      side_effect=evolve or self.fake_evolve))
            stack.enter_context(patch('harness.agent_client.AgentClient.run', side_effect=agent.run))
            return run_smoke(self.manifest_path, self.root / output, self.environment, **kwargs)

    def test_all_methods_share_frozen_inputs_and_complete_without_score_improvement(self):
        before = tree_hashes(self.manifest_path.parent)
        state = self.run_all()
        self.assertEqual(state['status'], 'completed', state)
        self.assertTrue(state['complete_three_method_coverage'])
        self.assertFalse(state['test_split_used'])
        manifest = load_manifest(self.manifest_path)
        sources = [Path(row['source']) for row in manifest['rows']]
        trace_sources, trace_skill, _ = self.optimizer_calls['trace2skill']
        _, opt_skill, options = self.optimizer_calls['skillopt']
        self.assertEqual(trace_sources, sources)
        self.assertEqual(Path(options['frozen_trajectory_manifest']), self.manifest_path)
        self.assertEqual((trace_skill / 'SKILL.md').read_bytes(), opt_skill.read_bytes())
        self.assertEqual(sha256_file(opt_skill), manifest['initial_skill']['sha256'])
        reuse = self.read(self.root / 'smoke/skillopt/optimization/steps/step_0001/rollout/frozen_trajectory_reuse.json')
        self.assertEqual([Path(row['source']) for row in reuse['rows']], sources)
        patch_plan = self.read(self.root / 'smoke/skillpatch/optimization/plan.json')
        self.assertEqual(patch_plan['manifest_sha256'], sha256_file(self.manifest_path))
        self.assertEqual(patch_plan['selected_task_ids'], [row['task_id'] for row in manifest['rows']])
        self.assertFalse(patch_plan['validation_split_used'])
        self.assertEqual(state['methods']['skillpatch']['replayed_count'], 1)
        self.assertEqual(state['methods']['skillpatch']['repaired_count'], 0)
        for name in ('skillopt', 'trace2skill'):
            method = state['methods'][name]
            self.assertTrue(method['validation_split_used'])
            self.assertTrue(all(row['score'] == 0 and row['score_delta'] == 0
                                for row in method['source_replays']))
        self.assertEqual(tree_hashes(self.manifest_path.parent), before)
        self.network.assert_not_called()

    def test_prepare_creates_reviewable_plan_without_credentials_or_method_execution(self):
        calls = {name: Mock(side_effect=AssertionError('prepare invoked a method'))
                 for name in ('skillopt', 'trace2skill', 'skillpatch')}
        with patch.dict('set_up.appworld_smoke.RUNNERS', calls), \
             patch('harness.agent_client.AgentClient.run') as agent:
            state = run_smoke(self.manifest_path, self.root / 'prepare', {}, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        self.assertFalse(state['complete_three_method_coverage'])
        for call in calls.values():
            call.assert_not_called()
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()
        agent.assert_not_called()
        inputs = self.root / 'prepare/inputs'
        config = load_native_config(inputs / 'execution.toml')
        source = self.read(self.read(self.manifest_path)['execution_config']['path'])
        for field in ('model', 'base_url', 'max_tokens', 'max_retries', 'runner_options'):
            self.assertEqual(getattr(config, field), source[field])
        tasks = load_tasks(inputs / 'method_tasks.json')
        self.assertEqual({entry['task_ref']['split'] for entry in tasks if entry['split'] == 'val'}, {'dev'})
        self.assertFalse({entry['task_ref']['task_id'] for entry in tasks if entry['split'] == 'train'} &
                         {entry['task_ref']['task_id'] for entry in tasks if entry['split'] == 'val'})
        self.assertEqual(sha256_file(inputs / 'trajectory_manifest.snapshot.json'), sha256_file(self.manifest_path))
        plan = self.read(self.root / 'prepare/plan.json')
        self.assertIn('no dev', plan['validation_policy']['skillpatch'])
        self.assertIn('normal gate', plan['validation_policy']['skillopt'])

    def test_method_failure_is_saved_redacted_and_other_methods_continue(self):
        def fail(*args, **kwargs):
            raise RuntimeError('Optimizer failed with ' + self.environment['DEEPSEEK_API_KEY'])
        state = self.run_all(evolve=fail)
        self.assertEqual(state['status'], 'failed', state)
        self.assertFalse(state['complete_three_method_coverage'])
        self.assertEqual(state['methods']['trace2skill']['status'], 'failed')
        self.assertEqual(state['methods']['skillopt']['status'], 'completed')
        self.assertEqual(state['methods']['skillpatch']['status'], 'completed')
        saved = self.read(self.root / 'smoke/trace2skill/result.json')
        self.assertEqual(saved, state['methods']['trace2skill'])
        self.assertIn('[REDACTED]', saved['error'])
        self.assertNotIn(self.environment['DEEPSEEK_API_KEY'], json.dumps(state))

    def test_unchanged_candidate_means_incomplete_and_does_not_replay(self):
        def unchanged_train(manifest, initial_skill, output, environment, **kwargs):
            step = Path(output) / 'steps/step_0001'
            step.mkdir(parents=True)
            (step / 'candidate_skill.md').write_text(Path(initial_skill).read_text())
            write_json(Path(output) / 'history.json', [{'action': 'skip_no_patches'}])
            return {}
        def unchanged_evolve(*args, **kwargs):
            return {'status': 'completed', 'skill_changed': False}
        for name in ('skillopt', 'trace2skill'):
            with self.subTest(method=name):
                state = self.run_all(output=name, methods=(name,),
                                     train=unchanged_train, evolve=unchanged_evolve)
                self.assertEqual(state['status'], 'incomplete', state)
                self.assertEqual(state['methods'][name]['status'], 'incomplete')
                self.assertIn('No changed candidate', state['methods'][name]['reason'])
                self.assertFalse((self.root / name / name / 'source_replay').exists())
        self.runner_factory.assert_not_called()

    def test_skillopt_gate_rejection_still_covers_real_dev_and_source_execution(self):
        state = self.run_all(methods=('skillopt',))
        self.assertEqual(state['status'], 'completed', state)
        self.assertFalse(state['complete_three_method_coverage'])
        method = state['methods']['skillopt']
        self.assertEqual(method['gate_action'], 'reject')
        self.assertEqual(Path(method['selected_skill']).read_text(), self.initial)
        self.assertNotEqual(Path(method['candidate_skill']).read_text(), self.initial)
        self.assertEqual(len(method['validation']), 2)
        self.assertEqual([len(rows) for rows in method['validation']], [1, 1])
        for rows in method['validation']:
            for row in rows:
                raw = self.read(row['archive'])
                self.assertEqual(raw['split'], 'dev')
                self.assertEqual(raw['evaluation_summary']['num_tests'], 1)
        self.assertEqual([task['split'] for task, _ in self.world.calls], ['dev', 'dev', 'train', 'train'])
        self.assertEqual(len(method['source_replays']), 2)
        self.assertEqual(self.read(method['source_replays'][0]['archive'])['retrieved_memory_text'],
                         Path(method['candidate_skill']).read_text())

    def test_trace_reference_only_change_is_delivered_to_dev_and_original_tasks(self):
        state = self.run_all(methods=('trace2skill',))
        self.assertEqual(state['status'], 'completed', state)
        method = state['methods']['trace2skill']
        candidate = Path(method['candidate_skill'])
        self.assertEqual((candidate / 'SKILL.md').read_text(), self.initial)
        expected = self.initial + '\n\n--- Skill reference: references/dates.md ---\n' + self.reference
        paths = [row['candidate_archive'] for row in method['validation']]
        paths.extend(row['archive'] for row in method['source_replays'])
        for path in paths:
            raw = self.read(path)
            self.assertEqual(raw['retrieved_memory_text'], expected)
            self.assertEqual(raw['retrieved_memory_text'].count(self.reference), 1)
        for row in method['validation']:
            self.assertEqual(self.read(row['baseline_archive'])['retrieved_memory_text'], self.initial)

    def test_invalid_official_candidate_score_is_failed_not_completed(self):
        def invalid_train(*args, **kwargs):
            summary = self.fake_train(*args, **kwargs)
            output = Path(args[2])
            results = output / 'steps/step_0001/selection_eval/results.jsonl'
            row = json.loads(results.read_text().splitlines()[0])
            source = Path(row['source_archive'])
            raw = self.read(source)
            raw['evaluation_summary']['num_tests'] = 0
            write_json(source, raw)
            return summary
        state = self.run_all(methods=('skillopt',), train=invalid_train)
        self.assertEqual(state['status'], 'failed', state)
        self.assertEqual(state['methods']['skillopt']['status'], 'failed')
        self.assertFalse((self.root / 'smoke/skillopt/source_replay').exists())

    def test_modified_inputs_stop_later_methods_before_their_execution(self):
        for kind in ('settings', 'manifest', 'trajectory'):
            with self.subTest(kind=kind):
                manifest = self.collect_source('shared-' + kind)
                later = Mock(return_value={'status': 'completed'})
                def mutate(inputs, configs, output, environment, **kwargs):
                    if kind == 'settings':
                        inputs['initial_skill'].write_text('Unexpected mutation.')
                    elif kind == 'manifest':
                        path = Path(inputs['manifest_path'])
                        path.write_text(path.read_text() + '\n')
                    else:
                        source = Path(inputs['manifest']['rows'][0]['source'])
                        raw = self.read(source)
                        raw['instruction'] = 'Unexpected task mutation.'
                        write_json(source, raw)
                    return {'status': 'completed'}
                with patch.dict('set_up.appworld_smoke.RUNNERS', {'trace2skill': mutate, 'skillopt': later}):
                    state = run_smoke(manifest, self.root / kind, self.environment,
                                      methods=('trace2skill', 'skillopt'))
                self.assertEqual(state['status'], 'failed', state)
                self.assertTrue('changed' in state['error'] or 'integrity check failed' in state['error'])
                self.assertEqual(state['methods']['trace2skill']['status'], 'failed')
                saved = self.read(self.root / kind / 'trace2skill/result.json')
                self.assertEqual(saved, state['methods']['trace2skill'])
                self.assertIn('finished_at', saved)
                later.assert_not_called()
                self.assertFalse(state['complete_three_method_coverage'])

    def test_cancelled_method_is_saved_and_stops_later_methods(self):
        for mode in ('return_status', 'raise_interrupt'):
            with self.subTest(mode=mode):
                cancelled = (Mock(return_value={'status': 'cancelled', 'reason': 'Optimizer interrupted'})
                             if mode == 'return_status' else Mock(side_effect=KeyboardInterrupt))
                later = Mock(return_value={'status': 'completed'})
                output = self.root / ('cancelled-' + mode)
                with patch.dict('set_up.appworld_smoke.RUNNERS', {'trace2skill': cancelled, 'skillopt': later}):
                    state = run_smoke(self.manifest_path, output, self.environment,
                                      methods=('trace2skill', 'skillopt'))
                self.assertEqual(state['status'], 'cancelled', state)
                self.assertFalse(state['complete_three_method_coverage'])
                self.assertEqual(state['methods']['trace2skill']['status'], 'cancelled')
                saved = self.read(output / 'trace2skill/result.json')
                self.assertEqual(saved, state['methods']['trace2skill'])
                self.assertIn('finished_at', saved)
                self.assertEqual(self.read(output / 'result.json')['status'], 'cancelled')
                self.assertNotIn('skillopt', state['methods'])
                cancelled.assert_called_once()
                later.assert_not_called()
                self.assertFalse((output / 'skillopt').exists())

    def test_cancelled_cli_returns_interrupt_exit_code(self):
        from set_up.appworld_smoke import main
        with patch('set_up.appworld_smoke.load_environment', return_value=self.environment), \
             patch('set_up.appworld_smoke.run_smoke', return_value={'status': 'cancelled'}):
            code = main(['--trajectory-manifest', str(self.manifest_path),
                         '--output', str(self.root / 'cli-cancelled')])
        self.assertEqual(code, 130)

    def test_invalid_output_token_budget_is_rejected_before_creating_output(self):
        for index, budget in enumerate((0, -1, True, False)):
            with self.subTest(budget=budget):
                output = self.root / f'invalid-budget-{index}'
                with self.assertRaisesRegex(ValueError, 'max_output_tokens must be a positive integer'):
                    run_smoke(self.manifest_path, output, self.environment, max_output_tokens=budget)
                self.assertFalse(output.exists())
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()

    def test_nested_and_reused_output_are_rejected_without_running_methods(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            run_smoke(self.manifest_path, self.manifest_path.parent / 'nested', self.environment)
        reused = self.root / 'reused'
        reused.mkdir()
        marker = reused / 'keep.txt'
        marker.write_text('Keep existing artifacts.')
        with self.assertRaises(FileExistsError):
            run_smoke(self.manifest_path, reused, self.environment)
        self.assertEqual(marker.read_text(), 'Keep existing artifacts.')
        self.runner_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
