"""Full AppWorld experiment orchestration without paid model or environment calls."""

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_generation.config import load_stage2_config
from method_input_adapters.execution import load_tasks
from set_up.appworld_trajectories import collect, load_manifest
from set_up.native_config import load_native_config
import test_appworld_experiments as experiment_fixtures
from test_appworld_patch_validation import Runner


class AppWorldFullTests(unittest.TestCase):
    first_quote = experiment_fixtures.AppWorldExperimentTests.first_quote
    second_quote = experiment_fixtures.AppWorldExperimentTests.second_quote
    collect_source = experiment_fixtures.AppWorldExperimentTests.collect_source
    read = staticmethod(experiment_fixtures.AppWorldExperimentTests.read)

    def setUp(self):
        experiment_fixtures.AppWorldExperimentTests.setUp(self)
        self.train_ids = ['fixture_1', 'fixture_2']
        self.dev_ids = ['validation_1', 'validation_2']
        data = self.root / 'data'
        (data / 'datasets/dev.txt').write_text('\n'.join(self.dev_ids) + '\n')
        for task_id in self.dev_ids:
            write_json(data / 'tasks' / task_id / 'specs.json', {})
        # Permit dry preparation without any credential/environment configuration.
        text = self.execution_config.read_text()
        self.execution_config.write_text(text.replace(
            'base_url_env = "DEEPSEEK_BASE_URL"',
            'base_url_env = "DEEPSEEK_BASE_URL"\nbase_url = "https://fixture.invalid"'))
        self.inputs_seen = []
        self.world = Runner()
        runner_patch = patch('excutor.eviskill_runners.launcher.create_runner', return_value=self.world)
        self.runner_factory = runner_patch.start()
        self.addCleanup(runner_patch.stop)
        network_patch = patch('excutor.eviskill_runners.launcher.urlopen',
                              side_effect=AssertionError('No live model requests in full-run tests'))
        self.network = network_patch.start()
        self.addCleanup(network_patch.stop)

    def method(self, name):
        def execute(inputs, configs, output, environment, **kwargs):
            self.inputs_seen.append((name, inputs, configs, dict(environment), kwargs))
            write_json(Path(output) / 'boundary.json', {
                'train_ids': [entry['task_ref']['task_id'] for entry in inputs['train_entries']],
                'dev_ids': [entry['task_ref']['task_id'] for entry in inputs['validation_entries']],
                'profile': kwargs.get('profile'),
            })
            return {'status': 'completed', 'validation_split_used': name != 'skillpatch',
                    'candidate_count': 0, 'skill_changed': False}
        return execute

    def invoke(self, output='full', *, existing=True, environment=None, runners=None, **kwargs):
        from set_up.appworld_full import run_full
        methods = {name: self.method(name) for name in ('trace2skill', 'skillopt', 'skillpatch')}
        methods.update(runners or {})
        options = ({'manifest_path': self.manifest_path} if existing
                   else {'config_path': self.execution_config})
        options.update(kwargs)
        with patch.dict('set_up.appworld_full.RUNNERS', methods):
            return run_full(self.root / output, self.environment if environment is None else environment,
                            **options)

    def assert_full_plan(self, state):
        self.assertEqual(state['profile'], 'experiment')
        self.assertEqual(state['train_task_ids'], self.train_ids)
        self.assertEqual(state['validation_task_ids'], self.dev_ids)
        self.assertTrue(state['full_train_coverage'])
        self.assertFalse(state['test_split_used'])

    def test_existing_full_manifest_runs_all_train_and_dev_without_recollection(self):
        before = tree_hashes(self.manifest_path.parent)
        with patch('set_up.appworld_full.collect', side_effect=AssertionError('Recollected frozen evidence')) as collector:
            state = self.invoke(max_output_tokens=12345)
        self.assertEqual(state['status'], 'completed', state)
        self.assert_full_plan(state)
        self.assertTrue(state['complete_three_method_coverage'])
        self.assertEqual({name for name, *_ in self.inputs_seen}, {'trace2skill', 'skillopt', 'skillpatch'})
        manifest = load_manifest(self.manifest_path)
        for name, inputs, configs, environment, options in self.inputs_seen:
            self.assertEqual(Path(inputs['manifest_path']), self.manifest_path)
            self.assertEqual(inputs['manifest'], manifest)
            self.assertEqual(sha256_file(inputs['initial_skill']), manifest['initial_skill']['sha256'])
            self.assertEqual([row['task_ref']['task_id'] for row in inputs['train_entries']], self.train_ids)
            self.assertEqual([row['task_ref']['task_id'] for row in inputs['validation_entries']], self.dev_ids)
            self.assertEqual({row['task_ref']['split'] for row in inputs['validation_entries']}, {'dev'})
            self.assertEqual(options['profile'], 'experiment')
            self.assertEqual(options['max_output_tokens'], 12345)
            self.assertEqual(state['methods'][name]['status'], 'completed')
        collector.assert_not_called()
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(tree_hashes(self.manifest_path.parent), before)

    def test_without_manifest_collects_all_training_tasks_once_for_all_methods(self):
        with patch('set_up.appworld_full.collect', wraps=collect) as collector:
            state = self.invoke(existing=False)
        self.assertEqual(state['status'], 'completed', state)
        self.assert_full_plan(state)
        collector.assert_called_once()
        self.assertEqual([task['task_id'] for task, _ in self.world.calls], self.train_ids)
        self.runner_factory.assert_called_once()
        manifests = {Path(inputs['manifest_path']) for _, inputs, *_ in self.inputs_seen}
        self.assertEqual(len(manifests), 1)
        manifest_path = manifests.pop()
        self.assertTrue(manifest_path.is_relative_to(self.root / 'full'))
        self.assertNotEqual(manifest_path, self.manifest_path)
        self.assertEqual([row['task_id'] for row in load_manifest(manifest_path)['rows']], self.train_ids)
        self.assertTrue(state['complete_three_method_coverage'])
        self.network.assert_not_called()

    def test_frozen_execution_model_endpoint_key_name_and_initial_skill_override_later_edits(self):
        frozen = load_manifest(self.manifest_path)
        original = self.read(frozen['execution_config']['path'])
        (self.root / 'SKILL.md').write_text('Later edit must not become the shared initial Skill.')
        self.execution_config.write_text(self.execution_config.read_text().replace('fixture-model', 'later-model'))
        environment = {**self.environment, 'DEEPSEEK_BASE_URL': 'https://later.invalid'}
        state = self.invoke(environment=environment)
        self.assertEqual(state['status'], 'completed', state)
        for _, inputs, configs, passed_environment, _ in self.inputs_seen:
            native = load_native_config(inputs['execution_config'])
            for key in ('model', 'base_url', 'api_key_env', 'base_url_env', 'temperature',
                        'max_tokens', 'max_retries', 'timeout_sec', 'runner_options'):
                self.assertEqual(getattr(native, key), original[key])
            self.assertEqual(passed_environment[native.base_url_env], original['base_url'])
            self.assertEqual(passed_environment[native.api_key_env], self.environment[native.api_key_env])
            self.assertEqual(sha256_file(inputs['initial_skill']), frozen['initial_skill']['sha256'])
            diagnosis = load_discovery_config(configs['diagnosis'])
            generation = load_discovery_config(load_stage2_config(configs['stage2']).generation_config)
            for agent in (diagnosis.agent, generation.agent):
                self.assertEqual(agent.model, 'openai/' + original['model'])
                self.assertEqual(agent.base_url, original['base_url'])
                self.assertEqual(agent.api_key_env, original['api_key_env'])
        self.assertEqual(environment['DEEPSEEK_BASE_URL'], 'https://later.invalid')
        for path in (self.root / 'full/inputs').rglob('*'):
            if path.is_file():
                self.assertNotIn(self.environment['DEEPSEEK_API_KEY'], path.read_text())

    def test_reuse_ignores_deleted_or_changed_original_collection_input_paths(self):
        for mode in ('deleted', 'changed'):
            with self.subTest(mode=mode):
                manifest_path = self.collect_source('shared-' + mode)
                manifest = load_manifest(manifest_path)
                raw = self.read(manifest['execution_config']['path'])
                original_skill, original_tasks = Path(raw['skill_path']), Path(raw['tasks_path'])
                self.assertNotEqual(original_skill, Path(manifest['initial_skill']['path']))
                self.assertNotEqual(original_tasks, Path(manifest['tasks_snapshot']['path']))
                if mode == 'deleted':
                    original_skill.unlink()
                    original_tasks.unlink()
                else:
                    original_skill.write_text('Changed original collection Skill.')
                    original_tasks.write_text('No longer a valid JSON task list.')
                self.assertEqual(load_manifest(manifest_path), manifest)
                state = self.invoke(output=mode, manifest_path=manifest_path, prepare_only=True,
                                    environment={})
                self.assertEqual(state['status'], 'prepared', state)
                plan = self.read(self.root / mode / 'plan.json')
                self.assertEqual(plan['initial_skill_sha256'], manifest['initial_skill']['sha256'])
                self.assertEqual(sha256_file(self.root / mode / 'inputs/initial_skill/SKILL.md'),
                                 manifest['initial_skill']['sha256'])
                self.assertEqual(self.read(self.root / mode / 'inputs/tasks.json'),
                                 self.read(manifest['tasks_snapshot']['path']))
        self.assertEqual(self.inputs_seen, [])
        self.runner_factory.assert_not_called()

    def test_subset_manifest_is_rejected_before_any_method_or_collection(self):
        with patch('excutor.eviskill_runners.launcher.create_runner', return_value=Runner()):
            collected = collect(self.execution_config, self.root / 'subset', self.environment, limit=1)
        subset = Path(collected['manifest_path'])
        self.assertEqual(len(load_manifest(subset)['rows']), 1)
        with patch('set_up.appworld_full.collect', side_effect=AssertionError('Unexpected recollection')) as collector:
            with self.assertRaisesRegex(ValueError, '(?i)(full|all|complete|official|coverage)'):
                self.invoke(manifest_path=subset)
        collector.assert_not_called()
        self.assertEqual(self.inputs_seen, [])
        self.runner_factory.assert_not_called()
        self.assertFalse((self.root / 'full').exists())

    def test_prepare_with_existing_manifest_plans_every_task_without_credentials_or_calls(self):
        with patch('set_up.appworld_full.collect', side_effect=AssertionError('Unexpected collection')) as collector, \
             patch('harness.agent_client.AgentClient.run') as agent:
            state = self.invoke(environment={}, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        self.assert_full_plan(state)
        self.assertFalse(state['complete_three_method_coverage'])
        entries = load_tasks(self.root / 'full/inputs/method_tasks.json')
        self.assertEqual([row['task_ref']['task_id'] for row in entries if row['split'] == 'train'], self.train_ids)
        self.assertEqual([row['task_ref']['task_id'] for row in entries if row['split'] == 'val'], self.dev_ids)
        self.assertEqual(self.inputs_seen, [])
        collector.assert_not_called()
        agent.assert_not_called()
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()

    def test_prepare_without_manifest_plans_full_collection_without_publishing_evidence(self):
        with patch('harness.agent_client.AgentClient.run') as agent:
            state = self.invoke(existing=False, environment={}, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        self.assert_full_plan(state)
        self.assertFalse(state['complete_three_method_coverage'])
        self.assertTrue(state['method_configs_prepared'])
        folder = self.root / 'full/collection_inputs'
        diagnosis = load_discovery_config(folder / 'skillpatch-diagnosis/agent.toml')
        stage2 = load_stage2_config(folder / 'skillpatch-stage2.toml')
        generation = load_discovery_config(stage2.generation_config)
        native = load_native_config(folder / 'execution.toml')
        for agent_config in (diagnosis.agent, generation.agent):
            self.assertEqual(agent_config.model, 'openai/' + native.model)
            self.assertEqual(agent_config.base_url, native.base_url)
            self.assertEqual(agent_config.api_key_env, native.api_key_env)
        from skillopt.config import flatten_config, load_config
        skillopt = flatten_config(load_config(str(folder / 'skillopt.yaml')))
        self.assertEqual(skillopt['batch_size'], len(self.train_ids))
        self.assertEqual(skillopt['minibatch_size'], 1)
        self.assertEqual(skillopt['num_epochs'], 1)
        self.assertTrue(skillopt['use_gate'])
        self.assertFalse(skillopt['eval_test'])
        self.assertFalse(list((self.root / 'full').rglob('trajectory_manifest.json')))
        self.assertEqual(self.inputs_seen, [])
        agent.assert_not_called()
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()

    def test_invalid_method_configuration_stops_before_collecting_training_tasks(self):
        with patch('set_up.appworld_full.prepare_method_configs',
                   side_effect=ValueError('Fixture invalid method configuration')) as configure, \
             patch('set_up.appworld_full.collect', wraps=collect) as collector:
            state = self.invoke(existing=False)
        self.assertEqual(state['status'], 'failed', state)
        self.assertIn('Fixture invalid method configuration', state['error'])
        self.assertEqual(self.read(self.root / 'full/result.json')['status'], 'failed')
        configure.assert_called_once()
        collector.assert_not_called()
        self.runner_factory.assert_not_called()
        self.network.assert_not_called()
        self.assertEqual(self.inputs_seen, [])

    def test_method_configuration_is_frozen_before_collection_and_checked_afterwards(self):
        def collect_then_mutate(*args, **kwargs):
            folder = Path(args[0]).parent
            config = load_stage2_config(folder / 'skillpatch-stage2.toml')
            generation = load_discovery_config(config.generation_config)
            result = collect(*args, **kwargs)
            self.assertEqual(result['status'], 'completed', result)
            generation.prompt_path.write_text('Unexpected change during collection.')
            return result
        with patch('set_up.appworld_full.collect', side_effect=collect_then_mutate) as collector:
            state = self.invoke(existing=False)
        self.assertEqual(state['status'], 'failed', state)
        self.assertIn('changed', state['error'])
        collector.assert_called_once()
        self.assertEqual(self.inputs_seen, [])
        self.assertFalse(state['complete_three_method_coverage'])

    def test_method_failure_is_persisted_and_remaining_methods_continue(self):
        def fail(*args, **kwargs):
            raise RuntimeError('fixture error: ' + self.environment['DEEPSEEK_API_KEY'])
        state = self.invoke(runners={'trace2skill': fail})
        self.assertEqual(state['status'], 'failed', state)
        self.assertEqual(state['methods']['trace2skill']['status'], 'failed')
        self.assertEqual(state['methods']['skillopt']['status'], 'completed')
        self.assertEqual(state['methods']['skillpatch']['status'], 'completed')
        self.assertFalse(state['complete_three_method_coverage'])
        self.assertEqual(self.read(self.root / 'full/trace2skill/result.json'), state['methods']['trace2skill'])
        self.assertIn('[REDACTED]', state['methods']['trace2skill']['error'])
        self.assertEqual([row[0] for row in self.inputs_seen], ['skillopt', 'skillpatch'])

    def test_cancellation_records_current_method_and_stops_later_methods(self):
        for mode in ('returned', 'interrupted'):
            with self.subTest(mode=mode):
                cancelled = (Mock(return_value={'status': 'cancelled'}) if mode == 'returned'
                             else Mock(side_effect=KeyboardInterrupt))
                later = Mock(return_value={'status': 'completed'})
                state = self.invoke(output=mode, runners={'trace2skill': cancelled, 'skillopt': later})
                self.assertEqual(state['status'], 'cancelled', state)
                self.assertEqual(state['methods']['trace2skill']['status'], 'cancelled')
                self.assertEqual(self.read(self.root / mode / 'trace2skill/result.json'),
                                 state['methods']['trace2skill'])
                self.assertNotIn('skillopt', state['methods'])
                self.assertFalse(state['complete_three_method_coverage'])
                later.assert_not_called()
                self.assertFalse((self.root / mode / 'skillopt').exists())

    def test_input_integrity_failure_is_terminal_and_persisted(self):
        for kind in ('settings', 'manifest', 'trajectory'):
            with self.subTest(kind=kind):
                manifest = self.collect_source('shared-' + kind)
                def mutate(inputs, configs, output, environment, **kwargs):
                    if kind == 'settings':
                        inputs['initial_skill'].write_text('Unexpected mutation.')
                    elif kind == 'manifest':
                        path = Path(inputs['manifest_path'])
                        path.write_text(path.read_text() + '\n')
                    else:
                        source = Path(inputs['manifest']['rows'][0]['source'])
                        raw = self.read(source)
                        raw['instruction'] = 'Unexpected source mutation.'
                        write_json(source, raw)
                    return {'status': 'completed'}
                later = Mock(return_value={'status': 'completed'})
                state = self.invoke(output=kind, manifest_path=manifest,
                                    runners={'trace2skill': mutate, 'skillopt': later})
                self.assertEqual(state['status'], 'failed', state)
                self.assertEqual(state['methods']['trace2skill']['status'], 'failed')
                self.assertEqual(self.read(self.root / kind / 'trace2skill/result.json'),
                                 state['methods']['trace2skill'])
                self.assertFalse(state['complete_three_method_coverage'])
                later.assert_not_called()
                self.assertNotIn('skillopt', state['methods'])

    def test_output_cannot_reuse_or_be_ancestor_or_descendant_of_frozen_inputs(self):
        from set_up.appworld_full import run_full
        before = tree_hashes(self.manifest_path.parent)
        outputs = (self.root, self.manifest_path.parent, self.manifest_path.parent / 'nested',
                   self.manifest_path.parent / 'execution/tasks/nested')
        for output in outputs:
            with self.subTest(output=output), self.assertRaises((ValueError, FileExistsError)):
                run_full(output, self.environment, manifest_path=self.manifest_path)
        reused = self.root / 'reused'
        reused.mkdir()
        marker = reused / 'keep.txt'
        marker.write_text('Keep existing output intact.')
        with self.assertRaises((ValueError, FileExistsError)):
            run_full(reused, self.environment, manifest_path=self.manifest_path)
        self.assertEqual(marker.read_text(), 'Keep existing output intact.')
        self.assertEqual(tree_hashes(self.manifest_path.parent), before)
        self.runner_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
