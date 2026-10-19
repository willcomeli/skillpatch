"""Lifecycle behavior with an unrelated backend, without Docker or a model API."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from skillpatch.lifecycle import VerifiedSession, invoke_verified_callback
from skillpatch.models import BackendRun


class MemoryInspector:
    def __init__(self):
        self.alive = True

    async def prepare(self):
        if not self.alive:
            raise RuntimeError("Environment is closed")
        return {"resources": [{"resource_id": "output-table"}], "limitations": []}

    def definitions(self):
        return []

    async def call(self, name, arguments):
        if not self.alive:
            raise RuntimeError("Environment is closed")
        return {"text": "rows: 12", "evidence": [], "images": []}


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "diagnosis.json"
        self.events = []
        self.inspector = MemoryInspector()
        self.execution = BackendRun("completed", summary={"metrics": {"distance": 12.5}})

    async def run_backend(self, callback, timeout=10):
        try:
            self.events.extend(["execute", "verify"])
            state = await invoke_verified_callback(
                callback, VerifiedSession(self.execution, self.inspector), self.output,
                timeout_sec=timeout,
            )
            return self.execution, state
        finally:
            self.inspector.alive = False
            self.events.append("cleanup")

    async def test_independent_backend_calls_diagnosis_before_cleanup(self):
        async def diagnose(session):
            self.assertEqual(self.events, ["execute", "verify"])
            self.assertEqual((await session.inspector.call("read", {}))["text"], "rows: 12")
            self.events.append("diagnose")
            return {"status": "completed", "conflict_count": 0}

        execution, state = await self.run_backend(diagnose)
        self.assertEqual(self.events, ["execute", "verify", "diagnose", "cleanup"])
        self.assertEqual(execution.summary, {"metrics": {"distance": 12.5}})
        self.assertEqual(state["status"], "completed")
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await self.inspector.prepare()

    async def test_callback_exception_keeps_score_and_cleans_environment(self):
        async def diagnose(session):
            raise ValueError("bad diagnostic response")
        execution, state = await self.run_backend(diagnose)
        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.errors, [])
        self.assertEqual(state["status"], "failed")
        self.assertFalse(self.inspector.alive)
        self.assertEqual(json.loads(self.output.read_text())["status"], "failed")

    async def test_success_skip_is_persisted_and_environment_is_cleaned(self):
        async def eligibility(session):
            return {"status": "skipped", "reason": "task_succeeded", "model_calls": 0, "tool_calls": 0}
        execution, state = await self.run_backend(eligibility)
        self.assertEqual(self.events, ["execute", "verify", "cleanup"])
        self.assertEqual(state["status"], "skipped")
        self.assertEqual(state["reason"], "task_succeeded")
        self.assertEqual(json.loads(self.output.read_text()), state)
        self.assertEqual(execution.summary, {"metrics": {"distance": 12.5}})
        self.assertFalse(self.inspector.alive)

    async def test_failed_agent_result_is_not_wrapped_as_success(self):
        async def diagnose(session):
            return {"status": "failed", "error": "insufficient context budget"}
        _, state = await self.run_backend(diagnose)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(self.inspector.alive)

    async def test_timeout_cancels_callback_and_then_cleans_environment(self):
        cancelled = asyncio.Event()
        async def diagnose(session):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        _, state = await self.run_backend(diagnose, timeout=1)
        self.assertEqual(state["status"], "timeout")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.inspector.alive)

    async def test_user_cancellation_propagates_after_persisting_state(self):
        entered = asyncio.Event()
        async def diagnose(session):
            entered.set()
            await asyncio.Future()
        task = asyncio.create_task(self.run_backend(diagnose))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.inspector.alive)
        self.assertEqual(json.loads(self.output.read_text())["status"], "cancelled")

    async def test_unknown_result_status_is_reported_as_failure(self):
        async def diagnose(session):
            return {"status": "running"}
        _, state = await self.run_backend(diagnose)
        self.assertEqual(state["status"], "failed")


if __name__ == "__main__":
    unittest.main()
