"""Separate optimizer credentials/models from real frozen executor inputs."""

from pathlib import Path
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_generation.config import load_stage2_config
from set_up.native_config import load_native_config
from set_up.optimizer_config import resolve_optimizer, UNCONFIGURED_URL
import test_appworld_smoke as smoke_fixtures
import test_appworld_full as full_fixtures


class AppWorldModelRoleTests(unittest.TestCase):
    first_quote = smoke_fixtures.AppWorldSmokeTests.first_quote
    second_quote = smoke_fixtures.AppWorldSmokeTests.second_quote
    collect_source = smoke_fixtures.AppWorldSmokeTests.collect_source
    read = staticmethod(smoke_fixtures.AppWorldSmokeTests.read)
    fake_train = smoke_fixtures.AppWorldSmokeTests.fake_train
    fake_evolve = smoke_fixtures.AppWorldSmokeTests.fake_evolve
    run_all = smoke_fixtures.AppWorldSmokeTests.run_all

    def setUp(self):
        smoke_fixtures.AppWorldSmokeTests.setUp(self)
        self.optimizer = self.root / 'optimizer.toml'
        self.optimizer.write_text('[optimizer]\nmodel = "gpt-5.5"\n'
                                  'api_key_env = "GPT_API_KEY"\nbase_url_env = "GPT_BASE_URL"\n')
        self.gpt_environment = {'GPT_API_KEY': 'synthetic-gpt-optimizer-key',
                                'GPT_BASE_URL': 'https://optimizer.invalid/v1'}

    def test_all_three_methods_use_gpt_while_every_execution_keeps_frozen_ds_settings(self):
        self.environment.update(self.gpt_environment)
        state = self.run_all(optimizer_config_path=self.optimizer)
        self.assertEqual(state['status'], 'completed', state)
        self.assertTrue(state['complete_three_method_coverage'])
        self.assertEqual(state['optimizer']['model'], 'gpt-5.5')
        for name in ('trace2skill', 'skillopt'):
            options = self.optimizer_calls[name][-1]
            self.assertEqual(options['model'], 'gpt-5.5')
            self.assertEqual(options['api_key_env'], 'GPT_API_KEY')
            self.assertEqual(options['base_url_env'], 'GPT_BASE_URL')
        folder = self.root / 'smoke/inputs'
        for path in (folder / 'skillpatch-diagnosis/agent.toml',
                     load_stage2_config(folder / 'skillpatch-stage2.toml').generation_config):
            agent = load_discovery_config(path).agent
            self.assertEqual(agent.model, 'openai/gpt-5.5')
            self.assertEqual(agent.api_key_env, 'GPT_API_KEY')
            self.assertEqual(agent.base_url, self.gpt_environment['GPT_BASE_URL'])
        native = load_native_config(folder / 'execution.toml')
        for call in self.runner_factory.call_args_list:
            executed = call.args[0]
            self.assertEqual(executed.model, native.model)
            self.assertEqual(executed.api_key_env, 'DEEPSEEK_API_KEY')
            self.assertEqual(executed.base_url, self.environment['DEEPSEEK_BASE_URL'])
            self.assertEqual(call.args[2]['DEEPSEEK_API_KEY'], self.environment['DEEPSEEK_API_KEY'])
        self.assertFalse(state['methods']['skillpatch']['validation_split_used'])
        for path in folder.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.gpt_environment['GPT_API_KEY'], path.read_text())
        self.network.assert_not_called()

    def test_empty_gpt_slots_prepare_safely_but_real_method_run_is_rejected(self):
        state = self.run_all(output='preview', optimizer_config_path=self.optimizer, prepare_only=True)
        self.assertEqual(state['status'], 'prepared', state)
        self.assertEqual(state['optimizer']['missing_environment'], ('GPT_BASE_URL', 'GPT_API_KEY'))
        self.assertFalse(state['optimizer']['ready'])
        self.assertEqual(state['optimizer']['base_url'], UNCONFIGURED_URL)
        self.runner_factory.assert_not_called()
        state = self.run_all(output='blocked', optimizer_config_path=self.optimizer)
        self.assertEqual(state['status'], 'failed', state)
        self.assertIn('GPT_BASE_URL', state['error'])
        self.assertIn('GPT_API_KEY', state['error'])
        self.assertEqual(state['methods'], {})
        self.assertEqual(self.optimizer_calls, {})

    def test_independent_url_cannot_overwrite_the_executor_environment_name(self):
        native = load_native_config(self.root / 'execution.toml')
        self.optimizer.write_text('[optimizer]\nmodel = "gpt-5.5"\n'
                                  'api_key_env = "GPT_API_KEY"\n'
                                  'base_url_env = "DEEPSEEK_BASE_URL"\n'
                                  'base_url = "https://optimizer.invalid/v1"\n')
        with self.assertRaisesRegex(ValueError, 'different base_url_env'):
            resolve_optimizer(self.optimizer, native, self.environment)

    def test_optimizer_urls_reject_embedded_credentials_before_snapshots(self):
        native = load_native_config(self.root / 'execution.toml')
        for endpoint in ('https://credential@example.invalid/v1', 'not-an-api-url',
                         'https://api.invalid/v1?api_key=secret'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                resolve_optimizer(self.optimizer, native, {'GPT_BASE_URL': endpoint})

    def test_full_pipeline_prepares_all_dev_with_gpt_and_keeps_frozen_executor(self):
        fixture = full_fixtures.AppWorldFullTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        environment = {**fixture.environment, **self.gpt_environment}
        state = fixture.invoke(environment=environment, optimizer_config_path=self.optimizer)
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['optimizer']['model'], 'gpt-5.5')
        for _, inputs, configs, env, _ in fixture.inputs_seen:
            native = load_native_config(inputs['execution_config'])
            self.assertEqual(native.model, 'fixture-model')
            self.assertEqual(env['DEEPSEEK_BASE_URL'], 'https://fixture.invalid')
            self.assertEqual(env['GPT_BASE_URL'], self.gpt_environment['GPT_BASE_URL'])
            self.assertEqual(len(inputs['validation_entries']), 2)
            self.assertEqual(self.read(configs['optimizer'])['model'], 'gpt-5.5')

    def test_full_pipeline_missing_gpt_configuration_blocks_before_paid_collection(self):
        fixture = full_fixtures.AppWorldFullTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with self.assertRaisesRegex(ValueError, 'Missing optimizer'):
            fixture.invoke(existing=False, optimizer_config_path=self.optimizer)
        fixture.runner_factory.assert_not_called()
        self.assertFalse((fixture.root / 'full').exists())

    def test_role_configuration_accepts_independent_model_names_without_brand_binding(self):
        self.optimizer.write_text('[optimizer]\nmodel_env = "METHOD_MODEL"\n'
                                  'api_key_env = "METHOD_API_KEY"\n'
                                  'base_url_env = "METHOD_BASE_URL"\n')
        original = self.execution_config.read_text()
        self.execution_config.write_text(original.replace('model = "fixture-model"',
            'model_env = "EXECUTION_MODEL"').replace('DEEPSEEK_API_KEY', 'EXECUTION_API_KEY')
            .replace('DEEPSEEK_BASE_URL', 'EXECUTION_BASE_URL'))
        for index, (executor, method) in enumerate((('gpt-5.5', 'arbitrary-method'),
                                                   ('another-executor', 'another-method'))):
            with self.subTest(executor=executor, method=method):
                self.environment = {'EXECUTION_MODEL': executor, 'EXECUTION_API_KEY': 'synthetic-execution-key',
                    'EXECUTION_BASE_URL': 'https://execution.invalid/v1', 'METHOD_MODEL': method,
                    'METHOD_API_KEY': 'synthetic-method-key', 'METHOD_BASE_URL': 'https://method.invalid/v1'}
                self.manifest_path = self.collect_source('generic-source-' + str(index))
                self.runner_factory.reset_mock()
                state = self.run_all(output='generic-' + str(index), optimizer_config_path=self.optimizer)
                self.assertEqual(state['status'], 'completed', state)
                self.assertEqual(state['optimizer']['model'], method)
                for name in ('skillopt', 'trace2skill'):
                    self.assertEqual(self.optimizer_calls[name][-1]['model'], method)
                    self.assertEqual(self.optimizer_calls[name][-1]['api_key_env'], 'METHOD_API_KEY')
                for call in self.runner_factory.call_args_list:
                    self.assertEqual(call.args[0].model, executor)
                    self.assertEqual(call.args[0].api_key_env, 'EXECUTION_API_KEY')
                agent = load_discovery_config(self.root / ('generic-' + str(index)) /
                                               'inputs/skillpatch-diagnosis/agent.toml').agent
                self.assertEqual(agent.model, 'openai/' + method)
                self.assertEqual(agent.api_key_env, 'METHOD_API_KEY')

    def test_execution_model_is_resolved_before_freezing_and_not_rebound_by_later_env(self):
        original = self.execution_config.read_text()
        self.execution_config.write_text(original.replace('model = "fixture-model"',
                                                         'model_env = "EXECUTION_MODEL"'))
        with self.assertRaisesRegex(ValueError, 'EXECUTION_MODEL'):
            load_native_config(self.execution_config, {})
        config = load_native_config(self.execution_config, {'EXECUTION_MODEL': 'selected-executor'})
        self.assertEqual(config.model, 'selected-executor')
        self.environment['EXECUTION_MODEL'] = 'selected-executor'
        manifest_path = self.collect_source('frozen-role')
        manifest = self.read(manifest_path)
        raw = self.read(manifest['execution_config']['path'])
        self.assertEqual(raw['model'], 'selected-executor')
        self.assertNotIn('model_env', raw)

    def test_env_role_credentials_preserve_compatibility_with_old_frozen_archives(self):
        from set_up.environment import load_environment
        env_file = self.root / '.env'
        env_file.write_text('EXECUTION_API_KEY=synthetic-exec-key\n'
                            'EXECUTION_BASE_URL=https://execution.invalid/v1\n'
                            'METHOD_API_KEY=synthetic-method-key\n'
                            'METHOD_BASE_URL=https://method.invalid/v1\n')
        with patch.dict('os.environ', {}, clear=True):
            env = load_environment(env_file)
        self.assertEqual(env['DEEPSEEK_API_KEY'], env['EXECUTION_API_KEY'])
        self.assertEqual(env['GPT_API_KEY'], env['METHOD_API_KEY'])

    def test_harness_can_change_independently_of_the_method_model(self):
        self.environment.update(self.gpt_environment)
        self.optimizer.write_text(self.optimizer.read_text() + 'harness_env = "SKILLPATCH_HARNESS"\n')
        for harness in ('codex', 'openhands'):
            with self.subTest(harness=harness):
                self.environment['SKILLPATCH_HARNESS'] = harness
                state = self.run_all(output=harness, optimizer_config_path=self.optimizer)
                self.assertEqual(state['status'], 'completed', state)
                self.assertEqual(state['optimizer']['harness'], harness)
                folder = self.root / harness / 'inputs'
                for path in (folder / 'skillpatch-diagnosis/agent.toml',
                             load_stage2_config(folder / 'skillpatch-stage2.toml').generation_config):
                    agent = load_discovery_config(path).agent
                    self.assertEqual(agent.harness, harness)
                    self.assertEqual(agent.model, 'gpt-5.5' if harness == 'codex' else 'openai/gpt-5.5')
                    self.assertEqual(agent.api_key_env, 'GPT_API_KEY')
                self.assertFalse(state['methods']['skillpatch']['validation_split_used'])

    def test_unknown_harness_is_rejected_during_preflight(self):
        native = load_native_config(self.execution_config)
        self.optimizer.write_text(self.optimizer.read_text() + 'harness_env = "SKILLPATCH_HARNESS"\n')
        with self.assertRaisesRegex(ValueError, 'openhands or codex'):
            resolve_optimizer(self.optimizer, native, {'SKILLPATCH_HARNESS': 'not-a-harness'})
