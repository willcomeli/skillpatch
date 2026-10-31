from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from excutor.executor_adapter.storage import write_json
from set_up.appworld_watchdog import check, restart_decision


class WatchdogTests(unittest.TestCase):
    def test_known_failures_bounded_and_unknown_errors_require_review(self):
        state = {'status': 'failed', 'error': 'Model returned an incomplete or empty action'}
        self.assertEqual(restart_decision(state, 0), 'restart')
        for status in (520, 525):
            self.assertEqual(restart_decision({'error': f'Model HTTP request failed: {status}'}, 0), 'restart')
        self.assertEqual(restart_decision(state, 3), 'needs_review')
        self.assertEqual(restart_decision({'error': 'Frozen inputs changed'}, 0), 'needs_review')
        self.assertEqual(restart_decision({'error': 'INSUFFICIENT_BALANCE'}, 0), 'needs_configuration')
        self.assertEqual(restart_decision({'error': 'INSUFFICIENT_BALANCE'}, 3, True), 'restart')

    def test_completed_experiments_are_never_restarted(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / '.env').write_text('EXECUTION_MODEL=fixture\n')
            for name in ('initial', 'trace2skill', 'skillopt', 'skillpatch'):
                write_json(base / 'recovery/test' / name / 'result.json', {'status': 'completed', 'completed': 168})
            with patch('set_up.appworld_watchdog.ROOT', base), \
                 patch('set_up.appworld_watchdog.recovery_process_running', return_value=False), \
                 patch('set_up.appworld_watchdog.subprocess.Popen') as spawn:
                state = check(base, {})
            self.assertEqual(state['status'], 'completed')
            spawn.assert_not_called()

    def test_active_workers_are_observed_without_duplicate_launch(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / '.env').write_text('EXECUTION_MODEL=fixture\n')
            write_json(base / 'recovery/test/skillpatch/result.json', {'status': 'completed'})
            with patch('set_up.appworld_watchdog.ROOT', base), \
                 patch('set_up.appworld_watchdog.locked', return_value=True), \
                 patch('set_up.appworld_watchdog.recovery_process_running', return_value=True), \
                 patch('set_up.appworld_watchdog.subprocess.Popen') as spawn:
                state = check(base, {})
            self.assertEqual(state['experiments']['initial']['monitor_action'], 'observe')
            spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
