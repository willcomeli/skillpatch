"""Resume failed collection using real archives and a synthetic AppWorld boundary."""

import fcntl
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import sha256_file, tree_hashes, write_json
from method_input_adapters.archive import load_archive
from set_up.appworld_trajectories import load_manifest
import test_appworld_trajectories as trajectory_fixtures
from test_appworld_trajectories import Runner


class AppWorldResumeTests(unittest.TestCase):
    run_collection = trajectory_fixtures.SharedAppWorldTrajectoriesTests.run_collection

    def setUp(self):
        trajectory_fixtures.SharedAppWorldTrajectoriesTests.setUp(self)
        self.shared = self.failed_collection('failed')
        self.prefix = self.shared / 'execution/tasks/task_000001.json'
        self.prefix_hash = sha256_file(self.prefix)
        self.original_collection = self.read(self.shared / 'result.json')
        self.original_execution = self.read(self.shared / 'execution/result.json')
        network = patch('excutor.eviskill_runners.launcher.urlopen',
                        side_effect=AssertionError('Resume tests must not call a model'))
        self.network = network.start()
        self.addCleanup(network.stop)

    @staticmethod
    def read(path):
        return json.loads(Path(path).read_text())

    def failed_collection(self, name):
        result, runner = self.run_collection(output=name, runner=Runner(fail_at=2))
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(runner.closed)
        shared = self.root / name
        self.assertEqual(len(self.read(shared / 'execution/result.json')['task_results']), 1)
        self.assertFalse((shared / 'trajectory_manifest.json').exists())
        return shared

    def resume(self, *, shared=None, runner=None, prepare_only=False, environment=None):
        from set_up.appworld_resume import resume_collection
        runner = runner or Runner()
        with patch('set_up.appworld_resume.create_runner', return_value=runner) as factory:
            result = resume_collection(shared or self.shared,
                                       self.environment if environment is None else environment,
                                       prepare_only=prepare_only)
        return result, runner, factory

    def assert_saved_history(self, shared, before_collection, before_execution):
        history = shared / 'execution/resume_attempts'
        collection_records = [self.read(path) for path in history.glob('*/before/collection_result.json')]
        execution_records = [self.read(path) for path in history.glob('*/before/execution_result.json')]
        self.assertIn(before_collection, collection_records)
        self.assertIn(before_execution, execution_records)

    def test_only_remaining_task_runs_and_completed_prefix_and_failure_history_survive(self):
        result, runner, factory = self.resume()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual([task['task_id'] for task, _ in runner.calls], ['fixture_2'])
        self.assertEqual(sha256_file(self.prefix), self.prefix_hash)
        factory.assert_called_once()
        self.assertTrue(runner.closed)
        self.assert_saved_history(self.shared, self.original_collection, self.original_execution)
        manifest = load_manifest(self.shared / 'trajectory_manifest.json')
        self.assertEqual([row['task_id'] for row in manifest['rows']], ['fixture_1', 'fixture_2'])
        self.assertEqual([row['success'] for row in manifest['rows']], [True, False])
        self.assertEqual(manifest['rows'][0]['sha256'], self.prefix_hash)
        self.assertEqual(len(self.read(self.shared / 'execution/result.json')['task_results']), 2)
        self.network.assert_not_called()

    def test_inspection_and_prepare_are_read_only_and_need_no_credentials(self):
        from set_up.appworld_resume import inspect_resume
        before = tree_hashes(self.shared)
        preview = inspect_resume(self.shared)
        self.assertEqual(preview['status'], 'prepared')
        self.assertEqual((preview['completed_count'], preview['remaining_count'], preview['total_count']), (1, 1, 2))
        self.assertEqual(preview['next_task_id'], 'fixture_2')
        result, runner, factory = self.resume(prepare_only=True, environment={})
        self.assertEqual(result['status'], 'prepared', result)
        self.assertEqual(result['remaining_count'], 1)
        factory.assert_not_called()
        self.assertEqual(runner.calls, [])
        self.assertEqual(tree_hashes(self.shared), before)
        self.network.assert_not_called()

    def test_already_completed_resume_is_idempotent(self):
        result, _, _ = self.resume()
        self.assertEqual(result['status'], 'completed', result)
        before = tree_hashes(self.shared)
        repeated, runner, factory = self.resume(environment={})
        self.assertEqual(repeated['status'], 'completed', repeated)
        factory.assert_not_called()
        self.assertEqual(runner.calls, [])
        self.assertEqual(tree_hashes(self.shared), before)
        self.assertEqual(len(load_manifest(self.shared / 'trajectory_manifest.json')['rows']), 2)

    def test_invalid_archived_inputs_scores_and_registered_paths_are_rejected_before_runner(self):
        from set_up.appworld_resume import inspect_resume, resume_collection
        for kind in ('initial_skill', 'task_order', 'step_budget', 'invalid_score', 'source_path'):
            with self.subTest(kind=kind):
                shared = self.failed_collection('tampered-' + kind)
                execution = shared / 'execution'
                if kind == 'initial_skill':
                    (execution / 'input/SKILL.md').write_text('Different instructions.')
                elif kind == 'task_order':
                    path = execution / 'input/tasks.json'
                    write_json(path, list(reversed(self.read(path))))
                elif kind == 'step_budget':
                    path = execution / 'config.json'
                    value = self.read(path)
                    value['runner_options']['max_steps'] += 1
                    write_json(path, value)
                elif kind == 'invalid_score':
                    path = execution / 'tasks/task_000001.json'
                    value = self.read(path)
                    value['score'] = 0.0  # Contradicts explicit success and official tests.
                    write_json(path, value)
                else:
                    path = execution / 'result.json'
                    value = self.read(path)
                    duplicate = self.root / ('copied-' + kind + '.json')
                    duplicate.write_bytes(Path(value['task_results'][0]).read_bytes())
                    value['task_results'][0] = str(duplicate)
                    write_json(path, value)
                before = tree_hashes(shared)
                with patch('set_up.appworld_resume.create_runner') as factory:
                    with self.assertRaises(ValueError):
                        inspect_resume(shared)
                    with self.assertRaises(ValueError):
                        resume_collection(shared, self.environment)
                factory.assert_not_called()
                self.assertEqual(tree_hashes(shared), before)

    def test_second_failure_keeps_prefix_and_can_be_resumed_again(self):
        from set_up.appworld_resume import inspect_resume
        failed, runner, _ = self.resume(runner=Runner(fail_at=1))
        self.assertNotEqual(failed['status'], 'completed', failed)
        self.assertTrue(runner.closed)
        self.assertEqual(sha256_file(self.prefix), self.prefix_hash)
        self.assertFalse((self.shared / 'trajectory_manifest.json').exists())
        preview = inspect_resume(self.shared)
        self.assertEqual((preview['completed_count'], preview['remaining_count']), (1, 1))
        second_collection = self.read(self.shared / 'result.json')
        second_execution = self.read(self.shared / 'execution/result.json')
        completed, resumed_runner, _ = self.resume()
        self.assertEqual(completed['status'], 'completed', completed)
        self.assertEqual([task['task_id'] for task, _ in resumed_runner.calls], ['fixture_2'])
        self.assertEqual(sha256_file(self.prefix), self.prefix_hash)
        self.assert_saved_history(self.shared, self.original_collection, self.original_execution)
        self.assert_saved_history(self.shared, second_collection, second_execution)
        self.assertEqual(len(load_manifest(self.shared / 'trajectory_manifest.json')['rows']), 2)

    def test_checkpoint_detects_subsequent_configuration_and_prefix_changes(self):
        from set_up.appworld_resume import inspect_resume
        for kind in ('model_budget', 'self_consistent_score'):
            with self.subTest(kind=kind):
                shared = self.failed_collection('checkpoint-' + kind)
                failed, _, _ = self.resume(shared=shared, runner=Runner(fail_at=1))
                self.assertNotEqual(failed['status'], 'completed', failed)
                if kind == 'model_budget':
                    path = shared / 'execution/config.json'
                    value = self.read(path)
                    value['max_tokens'] += 1024
                else:
                    path = shared / 'execution/tasks/task_000001.json'
                    value = self.read(path)
                    value.update(success=False, score=0, task_completed=False)
                    value['evaluation_summary'].update(passed_tests=0, failed_tests=1)
                write_json(path, value)
                with self.assertRaises(ValueError):
                    inspect_resume(shared)

    def test_concurrent_resume_lock_prevents_runner_and_archive_changes(self):
        from set_up.appworld_resume import resume_collection
        lock = self.shared / '.resume.lock'
        with lock.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = tree_hashes(self.shared)
            with patch('set_up.appworld_resume.create_runner') as factory:
                with self.assertRaises((ValueError, RuntimeError, BlockingIOError)):
                    resume_collection(self.shared, self.environment)
            factory.assert_not_called()
            self.assertEqual(tree_hashes(self.shared), before)

    def test_running_collection_state_is_rejected_without_overwriting_it(self):
        from set_up.appworld_resume import inspect_resume, resume_collection
        for location in ('result.json', 'execution/result.json'):
            with self.subTest(location=location):
                shared = self.failed_collection('running-' + location.replace('/', '-'))
                path = shared / location
                value = self.read(path)
                value['status'] = 'running'
                value.pop('finished_at', None)
                write_json(path, value)
                before = tree_hashes(shared)
                with patch('set_up.appworld_resume.create_runner') as factory:
                    with self.assertRaises(ValueError):
                        inspect_resume(shared)
                    with self.assertRaises(ValueError):
                        resume_collection(shared, self.environment)
                factory.assert_not_called()
                self.assertEqual(tree_hashes(shared), before)

    def test_unscored_partial_task_is_not_committed_as_a_completed_trajectory(self):
        def partial(row):
            for key in ('success', 'score', 'evaluation_summary'):
                row.pop(key, None)
        result, runner, _ = self.resume(runner=Runner(alter=partial))
        self.assertNotEqual(result['status'], 'completed', result)
        self.assertTrue(runner.closed)
        self.assertEqual(sha256_file(self.prefix), self.prefix_hash)
        native = self.read(self.shared / 'execution/result.json')
        self.assertEqual(native['task_results'], [str(self.prefix)])
        self.assertFalse((self.shared / 'trajectory_manifest.json').exists())
        pending = self.shared / 'execution/tasks/task_000002.json'
        if pending.exists():
            with self.assertRaises(ValueError):
                load_archive(pending)

    def test_partial_runtime_log_does_not_skip_unscored_task(self):
        path = self.shared / 'execution/appworld_runtime/experiments/outputs/interrupted/tasks/fixture_2/logs/environment_io.md'
        path.parent.mkdir(parents=True)
        path.write_text('### Environment Interaction 1\nPartial execution, no official evaluation.\n')
        before = path.read_bytes()
        result, runner, _ = self.resume()
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual([task['task_id'] for task, _ in runner.calls], ['fixture_2'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(load_manifest(self.shared / 'trajectory_manifest.json')['rows']), 2)

    def test_cancellation_and_cleanup_failure_do_not_publish_false_completion(self):
        class CancelledRunner(Runner):
            def run_task(self, task, bank, **kwargs):
                self.calls.append((task, kwargs))
                raise KeyboardInterrupt

        class CloseFailureRunner(Runner):
            def close(self):
                self.closed = True
                raise RuntimeError('fixture cleanup failed')

        for kind, runner in (('cancelled', CancelledRunner()), ('close_failure', CloseFailureRunner())):
            with self.subTest(kind=kind):
                shared = self.failed_collection(kind)
                prefix = shared / 'execution/tasks/task_000001.json'
                before = sha256_file(prefix)
                result, runner, _ = self.resume(shared=shared, runner=runner)
                self.assertNotEqual(result['status'], 'completed', result)
                if kind == 'cancelled':
                    self.assertEqual(result['status'], 'cancelled')
                self.assertTrue(runner.closed)
                self.assertEqual(sha256_file(prefix), before)
                self.assertFalse((shared / 'trajectory_manifest.json').exists())
                if kind == 'close_failure':
                    from set_up.appworld_resume import resume_collection
                    native = self.read(shared / 'execution/result.json')
                    self.assertEqual(len(native['task_results']), 2)
                    self.assertTrue(native['errors'])
                    failed_artifacts = tree_hashes(shared)
                    with patch('set_up.appworld_resume.create_runner') as factory:
                        with self.assertRaises(ValueError):
                            resume_collection(shared, self.environment)
                    factory.assert_not_called()
                    self.assertEqual(tree_hashes(shared), failed_artifacts)
                    self.assertFalse((shared / 'trajectory_manifest.json').exists())


if __name__ == '__main__':
    unittest.main()
