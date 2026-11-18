from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.retry_queue import active_layout, freeze_at_checkpoint, retain_transport_provenance, worker
from scripts.skillsbench.method_transport_recovery import recover


class RetryQueueOwnershipTests(unittest.TestCase):
    def test_later_retry_or_inherited_result_does_not_restart_prior_recovery(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary).resolve()
                destination = run / 'method_transport_recovery/task'
                target = {'destination': str(destination),
                          'previous_record': {'status': 'failed', 'error': 'preparation'}}
                policy = {'tasks': {'task': target}}
                exp.write_json(run / 'METHOD_TRANSPORT_RECOVERY_REQUESTED.json', policy)
                exp.write_json(destination / 'result.json', {'status': 'failed', 'error': 'first replay'})
                latest = {'status': status, 'recovery': {'original_method_folder': 'latest-checkpoint'}}
                result = retain_transport_provenance(run, 'task', latest)
                state = {'methods': {'skillopt/task': result}}
                with patch('scripts.skillsbench.resume_methods.recover_job') as execute, \
                     patch.object(exp, '_batch_results', return_value=iter(())) as batch:
                    recover(state, {}, run, {}, object(), run / 'config', policy, [], lambda: None)
                execute.assert_not_called()
                self.assertEqual(batch.call_args.args[1], [])
                self.assertEqual(state['methods']['skillopt/task']['status'], status)
                self.assertEqual(result['recovery'], latest['recovery'])
                self.assertNotIn('transport_recovery', latest)

    def test_unattempted_preparation_is_not_marked_as_recovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve()
            exp.write_json(run / 'METHOD_TRANSPORT_RECOVERY_REQUESTED.json', {'tasks': {
                'task': {'destination': str(run / 'method_transport_recovery/task'),
                         'previous_record': {'status': 'failed'}}}})
            latest = {'status': 'failed'}
            self.assertIs(retain_transport_provenance(run, 'task', latest), latest)

    def test_preparation_recovery_uses_checkpoint_worker(self):
        with patch('scripts.skillsbench.retry_queue.recover_job', return_value='recovered') as recover, \
             patch('scripts.skillsbench.retry_queue.exp._method_job') as fresh:
            self.assertEqual(worker('transport_retry', ('task', 'checkpoint')), 'recovered')
        recover.assert_called_once_with('task', 'checkpoint')
        fresh.assert_not_called()

    def test_handoff_accepts_nine_inherited_jobs_to_fill_free_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            folders = {f'skillopt/task{i}': run / f'task{i}' for i in range(9)}
            for folder in folders.values(): folder.mkdir()
            launch = {'pid': 123, 'phase_owner': 'skillopt_retry_queue'}
            state = {'status': 'running', 'phase': 'skillopt', 'methods': {}}
            exp.write_json(run.with_suffix('.launcher.json'), launch)
            exp.write_json(run / 'result.json', state)
            with patch('scripts.skillsbench.retry_queue.process', return_value='S'), \
                 patch('scripts.skillsbench.retry_queue.os.kill') as kill, \
                 patch('scripts.skillsbench.retry_queue.active_layout', return_value=(
                     {'legacy_active': list(folders), 'new_active': []}, folders, {})):
                self.assertEqual(freeze_at_checkpoint(run)[0], 123)
                self.assertEqual(kill.call_count, 1)

    def test_second_recovery_keeps_checkpoints_and_owners_of_three_live_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            round_dir = run / 'round-one'
            exp.write_json(run / 'METHOD_FAILURE_RETRY_REQUESTED.json', {'round_dir': str(round_dir)})
            exp.write_json(round_dir / 'previous-launcher.json', {'pid': 20, 'additional_live_roots': [10]})
            exp.write_json(run / 'queue_handoff/progress.json',
                           {'new_active': ['skillopt/normal'], 'legacy_active': ['skillopt/resumed']})
            exp.write_json(round_dir / 'progress.json', {'legacy_active': ['skillopt/normal', 'skillopt/resumed'],
                                                      'new_active': ['skillopt/retry', 'skillopt/fresh']})
            exp.write_json(run / 'METHOD_RETRY_OUTPUTS.json', {'tasks': {'retry': str(round_dir / 'retry')}})
            launch = {'pid': 30, 'phase_owner': 'skillopt_retry_queue', 'additional_live_roots': [20, 10]}
            _, folders, parents = active_layout(run, launch)
            self.assertEqual(folders['skillopt/normal'], run / 'skillopt/normal')
            self.assertEqual(folders['skillopt/resumed'], run / 'resume_after_docker_cleanup/resumed')
            self.assertEqual(folders['skillopt/retry'], round_dir / 'retry')
            self.assertEqual(folders['skillopt/fresh'], run / 'skillopt/fresh')
            self.assertEqual(parents[10]['keys'], ['skillopt/resumed'])
            self.assertEqual(parents[20]['keys'], ['skillopt/normal'])
            self.assertEqual(parents[30]['keys'], ['skillopt/retry', 'skillopt/fresh'])

    def test_persisted_parent_bindings_reject_orphan_active_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            round_dir = run / 'round'
            exp.write_json(round_dir / 'progress.json', {'legacy_active': ['skillopt/lost'], 'new_active': []})
            exp.write_json(round_dir / 'parents.json', {'10': {'module': 'scripts.skillsbench.retry_queue', 'keys': []}})
            launch = {'pid': 20, 'phase_owner': 'skillopt_retry_queue', 'additional_live_roots': [10],
                      'active_retry_round': str(round_dir)}
            with self.assertRaisesRegex(ValueError, 'unique owners'):
                active_layout(run, launch)


if __name__ == '__main__':
    unittest.main()
