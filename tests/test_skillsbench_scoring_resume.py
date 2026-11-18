from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.agentops_scoring_resume import use_recovered_score


class ScoredCandidateReplayTests(unittest.TestCase):
    def fixture(self, folder):
        task = folder / 'fix-build-agentops'
        task.mkdir()
        (task / 'task.md').write_text('fixed task')
        skills = folder / 'skills'
        (skills / 'testing-python').mkdir(parents=True)
        (skills / 'testing-python/SKILL.md').write_text('exact saved candidate')
        failed, graded = folder / 'failed', folder / 'graded'
        prepared = {'task_dir': str(task), 'skills_dir': str(skills), 'file_hashes': {'task': 'fixed'}}
        exp.write_json(failed / 'result.json', {'prepared': prepared})
        exp.write_json(graded / 'result.json', {'status': 'completed', 'feedback': {'status': 'scored'},
            'prepared': prepared, 'execution': {'summary': {'verification_recovery': {
                'source_archive': str(failed), 'additional_agent_runs': 0,
                'additional_model_calls': 0, 'verifier_timeout_seconds': 900}}}})
        request = SimpleNamespace(task_dir=task, skills_dir=skills)
        return failed, graded, request

    def test_exact_saved_candidate_uses_score_once_and_other_stages_execute_normally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            failed, graded, request = self.fixture(root)
            recovery = root / 'recovery'
            output = recovery / 'skills/testing-python/attempt/steps/step_0001/selection_eval-execution'
            with patch.object(exp, 'load_archive'), patch.object(exp, 'execute', return_value='native') as native:
                with use_recovered_score(failed, graded, recovery, 'testing-python') as audit:
                    self.assertEqual(exp.execute(request, output, {}), graded)
                    self.assertEqual(exp.execute(request, recovery / 'source_replay', {}), 'native')
                    with self.assertRaises(ValueError):
                        exp.execute(request, output, {})
                native.assert_called_once()
                self.assertEqual(audit['used'], 1)
            self.assertTrue((output / 'reused-score.json').exists())

    def test_candidate_mismatch_fails_without_replaying_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            failed, graded, request = self.fixture(root)
            other = root / 'other-skills'
            other.mkdir(); (other / 'SKILL.md').write_text('different candidate')
            with patch.object(exp, 'load_archive'), patch.object(exp, 'execute') as native:
                with use_recovered_score(failed, graded, root / 'recovery', 'testing-python'):
                    request.skills_dir = other
                    with self.assertRaisesRegex(ValueError, 'differs'):
                        exp.execute(request, root / 'recovery/skills/testing-python/attempt/selection_eval-execution', {})
                native.assert_not_called()


if __name__ == '__main__':
    unittest.main()
