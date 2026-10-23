"""Native Runner delivery checks without models or environment services."""

import unittest
from pathlib import Path
from unittest.mock import Mock

from excutor.eviskill_runners import AlfWorldRunner, ScienceWorldRunner, AppWorldRunner
from excutor.eviskill_runners import scienceworld_runner


class EviSkillRunnerTests(unittest.TestCase):
    def test_all_native_entry_points_are_available(self):
        for runner in (AlfWorldRunner, ScienceWorldRunner, AppWorldRunner):
            self.assertTrue(callable(runner.run_task))
            self.assertTrue(callable(runner.run_task_set))
        script = Path(scienceworld_runner.__file__).parent / 'scripts/serve_scienceworld_env.py'
        self.assertTrue(script.is_file())

    def test_appworld_delivers_skill_text_executes_action_and_records_feedback(self):
        runner = AppWorldRunner.__new__(AppWorldRunner)
        runner.action_agent = Mock()
        runner.action_agent.act.return_value = {'thought': 'fixture', 'action': 'print(42)'}
        runner.app_descriptions = []
        runner.max_steps = 2
        runner.action_history_length = 8
        runner._retrieve = Mock(return_value=({'skills': []}, 'original skill + patch supplement', ['skill-1']))
        env = Mock()
        env.initialize.return_value = {'instruction': 'fixture task'}
        env.execute.return_value = '42'
        env.task_completed.return_value = True
        env.evaluate.return_value = (True, {'passed': True}, [])
        result = runner._run_task_with_env(env, {'task_id': 'fixture_1', 'split': 'test_normal'}, Mock())
        env.execute.assert_called_once_with('print(42)')
        env.close.assert_called_once()
        self.assertEqual(runner.action_agent.act.call_args.args[3], 'original skill + patch supplement')
        self.assertTrue(result['success'])
        self.assertEqual(result['steps'], 1)
        self.assertEqual(result['trajectory'][0]['next_observation'], '42')
        self.assertEqual(result['retrieved_skill_ids'], ['skill-1'])
