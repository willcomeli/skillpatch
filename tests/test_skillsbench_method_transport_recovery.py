import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.method_transport_recovery import recover, validate_failure


class MethodPreparationRecoveryTests(unittest.TestCase):
    def fixture(self, run, name='task'):
        archive = run / f'skillopt/{name}/skills/skill/steps/eval/result.json'
        archive.parent.mkdir(parents=True)
        raw = {'status': 'agent_error', 'finished_at': 'done', 'feedback': {'status': 'unavailable'},
               'execution': {'trajectory_events': 0, 'summary': {'n_prompts': 1, 'n_tool_calls': 0,
                   'agent_result': {'total_tokens': 0}, 'error': 'Docker compose command failed for environment task. '
                   'Request to https://example.com/browser.zip timed out after 30000ms; did not complete successfully'}}}
        archive.write_text(json.dumps(raw))
        target = {'archive': str(archive), 'archive_sha256': exp.sha256_file(archive),
                  'previous_record': {'status': 'failed', 'error': 'original'},
                  'destination': str(run / 'method_transport_recovery' / name)}
        return archive, raw, target

    def test_native_placeholder_prompt_count_does_not_imply_agent_ran(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            _, _, target = self.fixture(run)
            validate_failure(run, 'task', target, exp.read, exp.sha256_file)

    def test_lost_lease_can_reuse_nested_previous_recovery_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve()
            archive, raw, target = self.fixture(run)
            original = run / 'resume_after_cleanup/task'
            destination = original / 'source_replay/result.json'
            raw['execution']['summary']['error'] = ('Docker compose command failed for environment task. '
                'failed to solve: failed to compute cache key: lease does not exist: not found')
            exp.write_json(destination, raw)
            target.update(archive=str(destination), archive_sha256=exp.sha256_file(destination),
                          original_method_folder=str(original))
            validate_failure(run, 'task', target, exp.read, exp.sha256_file)

    def test_started_agent_or_permanent_error_or_changed_archive_rejected(self):
        for variant in ('started', 'permanent', 'changed'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                archive, raw, target = self.fixture(run)
                if variant == 'started': raw['execution']['summary']['agent_result']['total_tokens'] = 2
                if variant == 'permanent': raw['execution']['summary']['error'] += '; certificate verify failed'
                archive.write_text(json.dumps(raw) + '\n')
                if variant != 'changed': target['archive_sha256'] = exp.sha256_file(archive)
                with self.assertRaises(ValueError):
                    validate_failure(run, 'task', target, exp.read, exp.sha256_file)

    def test_completed_recovery_reused_and_other_results_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            _, _, target = self.fixture(run)
            state = {'methods': {'skillopt/task': target['previous_record'], 'skillopt/other': {'status': 'completed'}},
                     'baselines': {'task': {'status': 'completed'}}}
            destination = Path(target['destination']); destination.mkdir(parents=True)
            exp.write_json(destination / 'result.json', {'status': 'completed', 'comparison': {'score': 1}})
            save = Mock()
            with patch('scripts.skillsbench.resume_methods.recover_job') as worker:
                for _ in range(2):
                    recover(state, {'task': object()}, run, {}, object(), run / 'config',
                            {'tasks': {'task': target}}, [], save)
            worker.assert_not_called()
            save.assert_called_once()
            self.assertEqual(state['methods']['skillopt/task']['status'], 'completed')
            self.assertEqual(state['methods']['skillopt/other'], {'status': 'completed'})

    def test_independent_preparation_recoveries_share_requested_concurrency(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve()
            targets = {name: self.fixture(run, name)[2] for name in ('one', 'two')}
            state = {'methods': {'skillopt/' + name: target['previous_record'] for name,target in targets.items()},
                     'baselines': {name: {'status': 'completed'} for name in targets}}
            save = Mock()
            with patch.object(exp, '_batch_results', return_value=iter([
                    ('two', {'status': 'failed', 'error': 'isolated'}),
                    ('one', {'status': 'completed'})])) as batch:
                recover(state, {name: object() for name in targets}, run, {}, object(), run / 'config',
                        {'tasks': targets}, [], save, concurrency=10)
            jobs = batch.call_args.args[1]
            self.assertEqual({name for name, arguments in jobs}, set(targets))
            self.assertEqual(batch.call_args.args[2], 10)
            self.assertEqual(state['methods']['skillopt/one']['status'], 'completed')
            self.assertEqual(state['methods']['skillopt/two']['status'], 'failed')
            self.assertEqual(save.call_count, 2)


if __name__ == '__main__':
    unittest.main()
