"""Experiment invariants: cohort identity, recovery, and unchanged task limits."""

from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.scienceworld import experiment as exp
from scripts.scienceworld import splits
from scripts.scienceworld.training import (RetryingCompletions, apply_score_overrides,
    check_optional_final_evaluation, OptionalEvaluationCancelled)
from set_up.native_config import load_native_config


class ScienceWorldExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cohorts(self):
        recipe = runpy.run_path(str(splits.ROOT / 'method/eviskill/scripts/build_scienceworld_split.py'))
        raw = {'train': [['unused', i] for i in range(1483)],
               'valid': [['unused', i] for i in range(194)],
               'test': [['task-1-boil', i + 1000] for i in range(211)]}
        for name, indices, count, offset in (
                ('train', recipe['MAIN_TRAIN_INDICES'], 4, 0),
                ('valid', recipe['MAIN_VAL_INDICES'], 2, 100)):
            for j, index in enumerate(indices):
                raw[name][index] = [f'task-1-family{j // count}', offset + j % count]
        return raw

    def test_released_cohorts_and_disjoint_identities(self):
        cohorts = splits.select_cohorts(self.cohorts(), 'easy')
        self.assertEqual({k: len(v) for k, v in cohorts.items()}, {'train': 96, 'val': 48, 'test': 211})
        self.assertEqual(cohorts['train'][0]['skillnet_index'], 1386)
        self.assertEqual(cohorts['val'][0]['skillnet_index'], 154)
        self.assertTrue(all(row['simplification'] == 'easy' for rows in cohorts.values() for row in rows))
        raw = self.cohorts()
        raw['test'][0] = raw['train'][1386]
        with self.assertRaisesRegex(ValueError, 'Duplicate task identity'):
            splits.select_cohorts(raw)

    def test_scienceworld_limits_are_existing_defaults(self):
        tasks, skill, config = [self.root / name for name in ('tasks.json', 'skill.md', 'run.toml')]
        tasks.write_text('[]')
        skill.write_text('')
        exp.write_native(config, tasks, skill, {}, self.root / 'scienceworld')
        native = load_native_config(config)
        self.assertIsNone(native.max_tokens)
        self.assertEqual(native.max_retries, 0)
        for key in ('max_steps', 'scienceworld_env_step_limit', 'remote_timeout', 'recommended_max_steps'):
            self.assertNotIn(key, native.runner_options)
        self.assertEqual(native.timeout_sec, 300)  # Existing single-request timeout, not task runtime.
        self.assertEqual(native.runner_options['scienceworld_server_python'], sys.executable)

    def test_user_can_cancel_only_optional_last_skill_test_without_scoring_partial_rows(self):
        trainer = self.root / 'trainer'
        policy = self.root / 'final_skill_test_policy.json'
        splits.save(policy, {'enabled': False, 'source': 'user_instruction'})
        for stage in ['rollout', 'selection_eval', 'final_selection_eval', 'test_eval_baseline', 'test_eval']:
            check_optional_final_evaluation(trainer / stage, trainer, policy)
            self.assertFalse((trainer / stage / 'cancellation.json').exists())
        partial = trainer / 'test_eval_final-executions/fixture/task-1/completed.json'
        splits.save(partial, {'existing': 'preserved'})
        with self.assertRaises(OptionalEvaluationCancelled):
            check_optional_final_evaluation(trainer / 'test_eval_final', trainer, policy)
        self.assertEqual(splits.read(partial), {'existing': 'preserved'})
        self.assertEqual(splits.read(trainer / 'test_eval_final/cancellation.json')['status'],
                         'cancelled_by_user')
        self.assertFalse((trainer / 'test_eval_final/results.jsonl').exists())

    def test_optional_last_skill_test_default_still_runs(self):
        check_optional_final_evaluation(self.root / 'trainer/test_eval_final',
                                        self.root / 'trainer', self.root / 'absent.json')
        self.assertFalse((self.root / 'trainer/test_eval_final').exists())

    def execute_fixture(self):
        tasks, skill, config = [self.root / name for name in ('tasks.json', 'skill.md', 'run.toml')]
        tasks.write_text('[]')
        skill.write_text('original')
        exp.write_native(config, tasks, skill, {}, self.root / 'scienceworld')
        entry = {'id': 'train-1', 'split': 'train', 'config': str(config),
                 'task_ref': {'task_id': 'boil--0', 'task': 'boil', 'variation_idx': 0}}
        calls = []

        def native(cfg, output, environment, **kwargs):
            output.mkdir(parents=True)
            calls.append(output)
            if len(calls) == 1:
                state = {'status': 'backend_error', 'errors': ['ConnectionError: connection reset'], 'task_results': []}
            else:
                source = output / 'tasks/task_000001.json'
                splits.save(source, {'dataset': 'scienceworld', 'task_id': 'boil--0', 'instruction': 'Boil water',
                                     'task_family': 'boil', 'success': False, 'score': 0.25, 'steps': 1,
                                     'trajectory': [{'step': 0, 'action': 'look around', 'observation': 'room',
                                                     'next_observation': 'room', 'done': False}],
                                     'retrieved_memory_text': cfg.skill_path.read_text()})
                state = {'status': 'completed', 'errors': [], 'task_results': [str(source)]}
            splits.save(output / 'result.json', state)
            return state
        return entry, calls, native

    def test_connection_retry_reuses_completed_task_and_checks_skill_binding(self):
        entry, calls, native = self.execute_fixture()
        folder = self.root / 'execution'
        with patch('excutor.runner_common.launcher.run_native', side_effect=native), patch.object(exp, 'backoff'):
            first = exp.execute(entry, 'original', folder, {})
            second = exp.execute(entry, 'original', folder, {})
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(list(folder.glob('attempt-*'))), 2)
            with self.assertRaisesRegex(ValueError, 'binding changed'):
                exp.execute(entry, 'different', folder, {})

    def test_completed_task_recovers_after_process_dies_before_receipt(self):
        entry, calls, native = self.execute_fixture()
        folder = self.root / 'execution'
        with patch('excutor.runner_common.launcher.run_native', side_effect=native), patch.object(exp, 'backoff'):
            first = exp.execute(entry, 'original', folder, {})
            (folder / 'completed.json').unlink()
            self.assertEqual(exp.execute(entry, 'original', folder, {}), first)
            self.assertEqual(len(calls), 2)

    def test_task_skips_after_five_retries_and_resume_does_not_repeat(self):
        entry, _, _ = self.execute_fixture()
        folder = self.root / 'execution'

        def fail(cfg, output, environment, **kwargs):
            output.mkdir(parents=True)
            state = {'status': 'backend_error', 'errors': ['ConnectionError: connection reset'],
                     'task_results': []}
            splits.save(output / 'result.json', state)
            return state

        with patch('excutor.runner_common.launcher.run_native', side_effect=fail) as run, \
                patch.object(exp, 'backoff'):
            self.assertIsNone(exp.execute(entry, 'original', folder, {}))
            self.assertEqual(run.call_count, 6)
            self.assertIsNone(exp.execute(entry, 'original', folder, {}))
            self.assertEqual(run.call_count, 6)
        self.assertTrue(splits.read(folder / 'skipped.json')['excluded_from_scoring'])

    def test_skipped_tasks_are_excluded_from_scoring(self):
        row = SimpleNamespace(success=True, score=0.5)
        with patch('method_input_adapters.archive.load_archive', return_value=row):
            result = exp.summarize([Path('scored.json'), None])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['requested_count'], 2)
        self.assertEqual(result['skipped_count'], 1)
        self.assertEqual(result['success_rate'], 1.0)
        self.assertEqual(result['mean_score'], 0.5)
        self.assertIsNone(exp.summarize([None])['mean_score'])

    def test_unlimited_task_retries_can_be_paused_and_resumed_after_old_cap(self):
        entry, calls, native = self.execute_fixture()
        folder = self.root / 'unlimited'
        count = 0

        def outage(cfg, output, environment, **kwargs):
            nonlocal count
            count += 1
            if count == 9:
                raise KeyboardInterrupt
            output.mkdir(parents=True)
            state = {'status': 'backend_error', 'errors': ['ConnectionError: reset'], 'task_results': []}
            splits.save(output / 'result.json', state)
            return state

        environment = {'SCIENCEWORLD_TASK_MAX_RETRIES': 'unlimited'}
        with patch('excutor.runner_common.launcher.run_native', side_effect=outage), \
                patch.object(exp, 'backoff'), self.assertRaises(KeyboardInterrupt):
            exp.execute(entry, 'original', folder, environment)
        self.assertEqual(count, 9)
        self.assertFalse((folder / 'skipped.json').exists())
        calls.append(None)  # The restored fixture backend succeeds on its next call.
        with patch('excutor.runner_common.launcher.run_native', side_effect=native), \
                patch.object(exp, 'backoff'):
            self.assertIsNotNone(exp.execute(entry, 'original', folder, environment))
        self.assertEqual(len(list(folder.glob('attempt-*'))), 10)
        self.assertEqual(len(list(folder.glob('attempt-*/retry_failure.json'))), 8)

    def test_unlimited_policy_reopens_exhausted_task_and_preserves_skip_receipt(self):
        entry, calls, native = self.execute_fixture()
        folder = self.root / 'exhausted'

        def outage(cfg, output, environment, **kwargs):
            output.mkdir(parents=True)
            state = {'status': 'backend_error', 'errors': ['ConnectionError: reset'], 'task_results': []}
            splits.save(output / 'result.json', state)
            return state

        with patch('excutor.runner_common.launcher.run_native', side_effect=outage), patch.object(exp, 'backoff'):
            self.assertIsNone(exp.execute(entry, 'original', folder, {}))
        old_skip = splits.read(folder / 'skipped.json')
        calls.append(None)
        with patch('excutor.runner_common.launcher.run_native', side_effect=native), patch.object(exp, 'backoff'):
            self.assertIsNotNone(exp.execute(entry, 'original', folder,
                                            {'SCIENCEWORLD_TASK_MAX_RETRIES': 'unlimited'}))
        self.assertFalse((folder / 'skipped.json').exists())
        self.assertEqual(splits.read(folder / 'skipped.previous-000001.json'), old_skip)
        self.assertEqual(len(list(folder.glob('attempt-*'))), 7)

    def test_retry_policy_validation(self):
        self.assertEqual(exp.task_retry_limit({}), 5)
        self.assertIsNone(exp.task_retry_limit({'SCIENCEWORLD_TASK_MAX_RETRIES': 'unlimited'}))
        self.assertEqual(exp.task_retry_limit({'SCIENCEWORLD_TASK_MAX_RETRIES': '0'}), 0)
        for invalid in ('-1', 'false', '5.5', ''):
            with self.assertRaises(ValueError):
                exp.task_retry_limit({'SCIENCEWORLD_TASK_MAX_RETRIES': invalid})
        self.assertEqual(exp.request_retry_limit({}), 0)
        self.assertIsNone(exp.request_retry_limit({'SCIENCEWORLD_REQUEST_MAX_RETRIES': 'unlimited'}))

    def test_unlimited_request_policy_reaches_native_executor(self):
        entry, _, native = self.execute_fixture()
        environment = {'SCIENCEWORLD_REQUEST_MAX_RETRIES': 'unlimited'}
        with patch('excutor.runner_common.launcher.run_native', side_effect=native) as run, \
                patch.object(exp, 'backoff'):
            self.assertIsNotNone(exp.execute(entry, 'original', self.root / 'execution', environment))
        self.assertTrue(all(item.args[0].max_retries is None for item in run.call_args_list))
        config = self.root / 'unlimited.toml'
        exp.write_native(config, self.root / 'tasks.json', self.root / 'skill.md',
                         environment, self.root / 'scienceworld')
        self.assertIsNone(load_native_config(config).max_retries)

    def test_explicit_zero_override_is_counted_without_fabricating_official_archive(self):
        entry = {'id': 'val-1', 'task_ref': {'task_id': 'power-component--11'}}
        cache, output = self.root / 'cache', self.root / 'output'
        output.mkdir()
        splits.save(cache / 'val-1/skipped.json', {'scoring_override': {
            'score': 0, 'success': False, 'source': 'user_instruction'}})
        rows, identifiers = apply_score_overrides([entry], [None], cache, output, [])
        self.assertEqual(identifiers, ['val-1'])
        self.assertEqual(rows[0]['soft'], 0)
        self.assertEqual(rows[0]['hard'], 0)
        self.assertIsNone(rows[0]['source_archive'])
        self.assertEqual(rows[0]['score_origin'], 'user_instruction')

    def test_stage_retry_preserves_failure_and_never_repeats_success(self):
        calls = []

        def operation(path):
            path.mkdir()
            calls.append(path)
            result = ({'status': 'failed', 'error': 'connection reset'} if len(calls) == 1
                      else {'status': 'generated', 'patch_path': 'fixture'})
            splits.save(path / 'result.json', result)
            return result
        with patch.object(exp, 'backoff'):
            result, path = exp.attempt_stage(self.root / 'stage', operation, {'generated'})
            self.assertEqual(result['status'], 'generated')
            self.assertEqual(exp.attempt_stage(self.root / 'stage', operation, {'generated'}), (result, path))
            self.assertEqual(len(calls), 2)
            (path / 'result.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'modified'):
                exp.attempt_stage(self.root / 'stage', operation, {'generated'})

    def test_schema_and_configuration_failures_are_terminal(self):
        self.assertFalse(exp.transient(ValueError('Invalid sidecar schema')))
        self.assertFalse(exp.transient(ValueError('Missing API key')))
        self.assertFalse(exp.transient(ValueError('Model HTTP request failed: 401')))
        self.assertTrue(exp.transient(TimeoutError()))
        self.assertTrue(exp.transient(RuntimeError(
            '{"errors": ["IncompleteRead: IncompleteRead(0 bytes read)"]}')))
        self.assertTrue(exp.transient(RuntimeError(
            '{"errors": ["SSLError: [SSL] record layer failure (_ssl.c:2580)"]}')))
        self.assertTrue(exp.transient(RuntimeError(
            "IncompleteModelResponse: Model returned an incomplete or empty action: finish_reason='stop', content_chars=0")))
        self.assertTrue(exp.transient(RuntimeError(
            'URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol>')))
        self.assertTrue(exp.transient(ValueError('Model HTTP request failed: 503')))
        self.assertTrue(exp.transient(RuntimeError(
            "ModelActionBudgetExceeded: Model returned an incomplete or empty action: finish_reason='length'")))

    def test_optimizer_retries_same_request_without_losing_training_state(self):
        class Completion:
            def __init__(self):
                self.calls = []

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    raise ConnectionError('disconnected')
                return 'response'

        target = Completion()
        with patch('scripts.scienceworld.training.backoff'):
            self.assertEqual(RetryingCompletions(target).create(
                model='method', messages=['same'], max_tokens=8192,
                max_completion_tokens=16384), 'response')
        self.assertEqual(target.calls[0], target.calls[1])
        self.assertEqual(target.calls[0], {'model': 'method', 'messages': ['same']})

    def test_full_pipeline_accepts_all_patches_uses_train_only_and_reuses_trainer_test(self):
        # Import before patching its configuration loader so module-level imports
        # cannot retain a mock and affect later tests in the same process.
        from method.skillpatch.patch_generation import pipeline  # noqa: F401
        inputs = self.root / 'inputs'
        inputs.mkdir()
        for stage in ('diagnosis', 'generation'):
            (inputs / f'{stage}.prompt.md').write_text('Fixture prompt')
        manifest = [{'id': f'{split}-1', 'split': split} for split in ('train', 'val', 'test')]
        splits.save(inputs / 'manifest.json', manifest)
        skill = 'Check the temperature.\nUse a clean container.\n'
        test_source = self.root / 'trainer-test.json'
        row = {'dataset': 'scienceworld', 'task_id': 'boil--21', 'instruction': 'Boil water',
               'success': True, 'score': 1.0, 'steps': 1,
               'trajectory': [{'action': 'look around', 'observation': 'room',
                               'next_observation': 'done', 'done': True}]}
        splits.save(test_source, row)
        calls = []

        def train(_, output, environment):
            path = output / 'trainer'
            path.mkdir(parents=True)
            (path / 'best_skill.md').write_text(skill)
            splits.save(path / 'test_eval/orchestration_receipt.json', {
                'binding': {'skill': splits.digest(skill)},
                'rows': [{'id': 'test-1', 'source_archive': str(test_source)}]})
            return {'status': 'completed'}, output

        def execute(entry, content, output, environment, patch_dir=None):
            calls.append((entry['split'], patch_dir))
            self.assertEqual(content, skill)
            if patch_dir:
                from excutor.excutor_runners.patch_sidecars import load_sidecars
                self.assertEqual(len(load_sidecars(patch_dir)['entries']), 2)
            return test_source

        def discovery(source, config, output, environment, **kwargs):
            output.mkdir()
            result = {'status': 'completed'}
            splits.save(output / 'result.json', result)
            return result

        def generation(diagnosis, identifier, config, output, environment, **kwargs):
            output.mkdir()
            path = output / 'generation/patch.json'
            splits.save(path, {'id': identifier})
            result = {'status': 'generated', 'patch_path': str(path)}
            splits.save(output / 'result.json', result)
            return result

        def load_patch(path):
            target = SimpleNamespace(skill_ref='S1', quote='Check the temperature.', scope='substep')
            patch = SimpleNamespace(patch_id=read_patch_id(path), target=target,
                                    when_to_use='When heating.', how_to_interpret='Measure before heating.')
            source = SimpleNamespace(context=SimpleNamespace(items=[SimpleNamespace(context_id='S1', text=skill)]))
            return patch, source, None

        def read_patch_id(path):
            return splits.read(path)['id']

        with patch('scripts.scienceworld.training.train', side_effect=train), \
                patch.object(exp, 'execute', side_effect=execute), \
                patch('method.skillpatch.patch_diagnosis.config.load_discovery_config', return_value=None), \
                patch('method.skillpatch.patch_diagnosis.discovery.run_discovery', side_effect=discovery), \
                patch('set_up.appworld_experiments.issue_ids', return_value=['I000001', 'I000002']), \
                patch('method.skillpatch.patch_generation.pipeline.run_stage2', side_effect=generation), \
                patch('method.skillpatch.patch_consolidation.source.load_patch', side_effect=load_patch):
            result = exp.run_pipeline(self.root, {})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['patch_count'], 2)
        self.assertFalse(result['skillpatch_validation_used'])
        self.assertEqual([split for split, _ in calls], ['train', 'test'])
        self.assertEqual(result['scores']['skillopt']['count'], 1)
        self.assertEqual(result['scores']['skillpatch']['success_rate'], 1)


if __name__ == '__main__':
    unittest.main()
