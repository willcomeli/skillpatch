from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.error import URLError

from excutor.eviskill_runners.appworld_runner import AppWorldRunner
from excutor.eviskill_runners.model_errors import ModelActionBudgetExceeded


class ModelLimitTests(TestCase):
    def runner(self, failure):
        runner = object.__new__(AppWorldRunner)
        runner.action_agent = Mock()
        runner.action_agent.act.side_effect = failure
        runner.max_steps = 50
        runner.action_history_length = 8
        runner.app_descriptions = []
        return runner

    def test_token_exhaustion_uses_official_verifier_without_executing_partial_code(self):
        for official_success in (False, True):
            with self.subTest(official_success=official_success):
                runner = self.runner(ModelActionBudgetExceeded('finish_reason=length'))
                env = Mock()
                env.initialize.return_value = {'instruction': 'fixture task'}
                env.evaluate.return_value = (official_success, {'num_tests': 1}, None)
                with patch.object(runner, '_retrieve', return_value=({}, 'skill', ['skill_md'])):
                    result = runner._run_task_with_env(env, {'task_id': 'fixture_1', 'split': 'test_normal'}, Mock())
                self.assertEqual(result['success'], official_success)
                self.assertEqual(result['score'], float(official_success))
                self.assertEqual(result['termination_reason'], 'model_output_limit')
                self.assertEqual(result['steps'], 0)
                env.execute.assert_not_called()
                env.evaluate.assert_called_once_with(include_requirement_labels=False)
                env.close.assert_called_once()

    def test_connection_failure_remains_unscored_infrastructure_error(self):
        runner = self.runner(URLError('503'))
        env = Mock()
        env.initialize.return_value = {'instruction': 'fixture task'}
        with patch.object(runner, '_retrieve', return_value=({}, 'skill', ['skill_md'])):
            with self.assertRaises(URLError):
                runner._run_task_with_env(env, {'task_id': 'fixture_1', 'split': 'test_normal'}, Mock())
        env.evaluate.assert_not_called()
        env.close.assert_called_once()
