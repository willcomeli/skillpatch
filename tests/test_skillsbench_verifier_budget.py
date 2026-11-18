import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.skillsbench import experiment as exp
from scripts.skillsbench.reference_updates import adopt
from scripts.skillsbench.verifier_budget import budgeted_verifier


POLICY = {'tasks': {'fix-build-agentops': {'original_seconds': 240, 'seconds': 900}}}


class VerifierBudgetTests(unittest.TestCase):
    def test_only_approved_task_gets_900_and_shared_task_config_is_restored(self):
        seen = []

        async def native(env, task, paths, timing, planes, **kwargs):
            seen.append((task.name, task.config.verifier.timeout_sec))
            return 'native-score'

        wrapped = budgeted_verifier(native, POLICY)
        with tempfile.TemporaryDirectory() as temporary:
            paths = SimpleNamespace(rollout_dir=Path(temporary))
            for name in ('fix-build-agentops', 'other'):
                original = SimpleNamespace(timeout_sec=240)
                task = SimpleNamespace(name=name, config=SimpleNamespace(verifier=original))
                self.assertEqual(asyncio.run(wrapped(None, task, paths, {}, None)), 'native-score')
                self.assertIs(task.config.verifier, original)
                self.assertEqual(original.timeout_sec, 240)
        self.assertEqual(seen, [('fix-build-agentops', 900), ('other', 240)])

    def test_native_failure_still_restores_original_budget(self):
        async def native(*args, **kwargs):
            raise RuntimeError('grader failed')

        with tempfile.TemporaryDirectory() as temporary:
            original = SimpleNamespace(timeout_sec=240)
            task = SimpleNamespace(name='fix-build-agentops', config=SimpleNamespace(verifier=original))
            with self.assertRaises(RuntimeError):
                asyncio.run(budgeted_verifier(native, POLICY)(None, task,
                    SimpleNamespace(rollout_dir=Path(temporary)), {}, None))
            self.assertIs(task.config.verifier, original)

    def test_unapproved_other_task_or_budget_is_rejected(self):
        with self.assertRaises(ValueError):
            budgeted_verifier(None, {'tasks': {'other': {'original_seconds': 240, 'seconds': 900}}})


class ReferenceUpdateTests(unittest.TestCase):
    def test_adopts_only_unconsumed_reference_and_preserves_old_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve()
            previous, source = run / 'old', run / 'regraded'
            exp.write_json(previous / 'result.json', {'status': 'timeout'})
            exp.write_json(source / 'result.json', {'status': 'completed'})
            rule = {'source': str(source), 'previous_source': str(previous),
                    'previous_result_sha256': exp.sha256_file(previous / 'result.json'),
                    'hashes': exp.tree_hashes(source), 'score': 0, 'verifier_timeout_seconds': 900}
            exp.write_json(run / 'VERIFIER_BUDGET_REQUESTED.json', POLICY)
            exp.write_json(run / 'APPROVED_REFERENCE_UPDATES.json', {
                'status': 'approved', 'tasks': {'fix-build-agentops': rule}})
            old = {'status': 'failed', 'source': str(previous), 'error': '240s timeout'}
            state = {'baselines': {'fix-build-agentops': old},
                     'methods': {'skillopt/fix-build-agentops': {'status': 'blocked'}}}
            archive = SimpleNamespace(benchmark='skillsbench', task_id='fix-build-agentops', score=0)
            with patch.object(exp, 'load_archive', return_value=archive):
                adopted = adopt(state, {'fix-build-agentops': None}, run)
                self.assertEqual(adopt(state, {'fix-build-agentops': None}, run), {})
            self.assertEqual(adopted['fix-build-agentops']['score'], 0)
            self.assertEqual(state['baselines']['fix-build-agentops']['status'], 'completed')
            self.assertEqual(old['status'], 'failed')
            self.assertEqual(json.loads((previous / 'result.json').read_text())['status'], 'timeout')
            self.assertNotIn('skillopt/fix-build-agentops', state['methods'])
            state['baselines']['fix-build-agentops'] = old
            state['methods']['skillopt/fix-build-agentops'] = {'status': 'completed'}
            with patch.object(exp, 'load_archive', return_value=archive):
                with self.assertRaisesRegex(ValueError, 'already consumed'):
                    adopt(state, {'fix-build-agentops': None}, run)


if __name__ == '__main__':
    unittest.main()
