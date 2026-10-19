"""Verify adapter/session/agent composition with a non-SkillsBench record format."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from skillpatch.integration import create_diagnosis_callback
from skillpatch.lifecycle import VerifiedSession, invoke_verified_callback
from skillpatch.models import BackendRun, DiscoveryInput, EvidenceItem, EvaluationFeedback, PreparedTask, RunRequest
from skillpatch.storage import write_json


class ForeignAdapter:
    def evaluate(self, execution):
        return EvaluationFeedback("scored", {"distance": execution.summary["distance"], "reward": 0}, False)

    def collect_evidence(self, run_dir, *, record_path=None):
        self.checkpoint = json.loads(record_path.read_text())
        # This collector does not need task.md, ACP or a verifier/ directory.
        return DiscoveryInput(run_dir.name, "foreign", "table", "task-digest", None, True,
                              [EvidenceItem("Q0001", "task", "Preserve rows.", "instructions.csv")], [], "")


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()
        prompt = self.root / "prompt.md"
        prompt.write_text("Review evidence.")
        config = self.root / "diagnosis.toml"
        config.write_text('[diagnosis]\nmodel="fixture"\nbase_url="https://not-called.invalid"\n'
                          'api_key_env="TEST_API_KEY"\nprompt_path="prompt.md"\n')
        self.request = RunRequest("foreign", self.root, "agent", "model", (), backend="foreign",
                                  diagnosis_config=config)
        self.task = PreparedTask("foreign", "table", self.root, self.root / "instructions.csv",
                                 None, self.root / "foreign-manifest.json", "task-digest", None)
        self.record = {"run_id": self.root.name, "benchmark": "foreign", "task_id": "table",
                       "status": "running", "started_at": "2026-01-01T00:00:00Z", "finished_at": None}
        write_json(self.root / "result.json", self.record)
        self.adapter = ForeignAdapter()
        self.inspector = object()
        self.session = VerifiedSession(BackendRun("completed", summary={"distance": 12.5, "reward": 0}), self.inspector,
                                       {"limitations": ["Fixture inspector exposes only table output."]})

    async def test_foreign_adapter_supplies_generic_agent_without_native_layout(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        with patch("skillpatch.integration.diagnose_live_context", new_callable=AsyncMock,
                   return_value={"status": "completed"}) as agent:
            result = await callback(self.session)
        self.assertEqual(result["status"], "completed")
        context, _, inspector, output, _ = agent.await_args.args
        self.assertEqual(context.benchmark, "foreign")
        self.assertIs(inspector, self.inspector)
        self.assertEqual(output, self.root / "discovery/live")
        self.assertTrue(context.input_digest.startswith("sha256:"))
        self.assertIn("Fixture inspector", context.limitations[-1])
        self.assertEqual(self.adapter.checkpoint["status"], "verified")
        self.assertNotIn("finished_at", self.adapter.checkpoint)
        self.assertEqual(self.adapter.checkpoint["feedback"]["metrics"], {"distance": 12.5, "reward": 0})
        self.assertEqual(json.loads((self.root / "result.json").read_text()), self.record)

    async def test_success_skips_collection_inspection_and_model_and_preserves_checkpoint(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        # Reward controls eligibility independently of the adapter passed flag.
        feedback = EvaluationFeedback("scored", {"distance": 0.0, "reward": 1}, True)
        output = self.root / "backend/live_discovery.json"
        with patch.object(self.adapter, "evaluate", return_value=feedback), \
             patch.object(self.adapter, "collect_evidence") as collect, \
             patch("skillpatch.integration.diagnose_live_context", new_callable=AsyncMock) as agent:
            result = await invoke_verified_callback(callback, self.session, output, timeout_sec=10)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "task_succeeded")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["tool_calls"], 0)
        collect.assert_not_called()
        agent.assert_not_awaited()
        self.assertFalse((self.root / "discovery").exists())
        self.assertEqual(json.loads(output.read_text()), result)
        checkpoint = json.loads((self.root / "backend/verified_record.json").read_text())
        self.assertTrue(checkpoint["feedback"]["passed"])
        self.assertNotIn("finished_at", checkpoint)
        self.assertEqual(json.loads((self.root / "result.json").read_text()), self.record)

    async def test_partial_and_zero_rewards_enter_diagnosis_independently_of_passed(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        for reward, passed in ((0, False), (0.85, None), (0.5, True)):
            feedback = EvaluationFeedback("scored", {"reward": reward}, passed)
            with self.subTest(reward=reward, passed=passed), \
                 patch.object(self.adapter, "evaluate", return_value=feedback), \
                 patch("skillpatch.integration.diagnose_live_context", new_callable=AsyncMock,
                       return_value={"status": "completed"}) as agent:
                self.assertEqual((await callback(self.session))["status"], "completed")
                agent.assert_awaited_once()

    async def test_invalid_rewards_skip_without_collecting_evidence_or_calling_agent(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        for feedback in (
            EvaluationFeedback("scored", {}),
            EvaluationFeedback("scored", {"reward": None}),
            EvaluationFeedback("scored", {"reward": True}),
            EvaluationFeedback("scored", {"reward": "0"}),
            EvaluationFeedback("unavailable"),
            EvaluationFeedback("error", {"reward": 0}),
        ):
            with self.subTest(feedback=feedback), \
                 patch.object(self.adapter, "evaluate", return_value=feedback), \
                 patch.object(self.adapter, "collect_evidence") as collect, \
                 patch("skillpatch.integration.diagnose_live_context", new_callable=AsyncMock) as agent:
                result = await callback(self.session)
                self.assertEqual(result["reason"], "reward_unavailable")
                collect.assert_not_called()
                agent.assert_not_awaited()

    async def test_nonfinite_feedback_is_rejected_before_model_or_collection(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        for reward in (float("nan"), float("inf")):
            with self.subTest(reward=reward), \
                 patch.object(self.adapter, "evaluate", return_value=EvaluationFeedback("scored", {"reward": reward})), \
                 patch.object(self.adapter, "collect_evidence") as collect, \
                 patch("skillpatch.integration.diagnose_live_context", new_callable=AsyncMock) as agent:
                with self.assertRaises(ValueError):
                    await callback(self.session)
                collect.assert_not_called()
                agent.assert_not_awaited()

    async def test_reward_one_skips_even_when_passed_unknown(self):
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        with patch.object(self.adapter, "evaluate", return_value=EvaluationFeedback("scored", {"reward": 1}, None)), \
             patch.object(self.adapter, "collect_evidence") as collect:
            result = await callback(self.session)
        self.assertEqual(result["reason"], "task_succeeded")
        collect.assert_not_called()

    async def test_finished_run_is_not_misrepresented_as_live(self):
        self.record["finished_at"] = "2026-01-01T00:01:00Z"
        write_json(self.root / "result.json", self.record)
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        with self.assertRaisesRegex(ValueError, "active run"):
            await callback(self.session)

    async def test_collector_must_return_the_same_task_identity(self):
        original = self.adapter.collect_evidence
        def mismatched(*args, **kwargs):
            context = original(*args, **kwargs)
            context.task_id = "other-task"
            return context
        self.adapter.collect_evidence = mismatched
        callback = create_diagnosis_callback(self.request, self.task, self.root, {}, adapter=self.adapter)
        with self.assertRaisesRegex(ValueError, "identity"):
            await callback(self.session)


if __name__ == "__main__":
    unittest.main()
