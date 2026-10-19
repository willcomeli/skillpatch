import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from skillpatch.lifecycle import VerifiedSession
from skillpatch.models import BackendRun, PreparedTask, RunRequest
from skillpatch.runners.benchflow import BenchFlowBackend, build_config
from skillpatch.runners.benchflow_worker import (
    _create_session, _snapshot_files, freeze_verified_execution, live_rollout_class, rollout_config_values,
)


class FakeInspector:
    def __init__(self, events):
        self.events = events

    async def prepare(self):
        self.events.append("inspect")
        return {"resources": []}

    def definitions(self):
        return []

    async def call(self, name, arguments):
        return {"text": "observed", "evidence": [], "images": []}


class FakeRollout:
    def __init__(self):
        self.events = []
        self.alive = True
        self.rewards = {"reward": 0}

    async def install_agent(self):
        self.events.append("install")

    async def verify(self):
        self.events.append("verify")
        return self.rewards

    @property
    def result(self):
        self.events.append("persist")
        return {"rewards": self.rewards}

    async def cleanup(self):
        self.events.append("cleanup")
        self.alive = False

    async def run(self):
        try:
            await self.install_agent()
            return await self.verify()
        finally:
            await self.cleanup()


class WorkerHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "backend").mkdir()
        self.request = RunRequest("other-benchmark", self.root, "agent", "model", ("bench",))
        self.task = PreparedTask("other-benchmark", "task", self.root, self.root / "task.md",
                                 None, self.root / "manifest.json", "hash", None)
        self.rollout = live_rollout_class(FakeRollout)()
        self.execution = BackendRun("completed", summary={"rewards": {"reward": 0}})
        self.session = VerifiedSession(self.execution, FakeInspector(self.rollout.events), {})
        patches = [patch("skillpatch.runners.benchflow_worker._snapshot_files",
                         new=AsyncMock(return_value={"hashes": {}, "limitations": ["scan bounded"]})),
                   patch("skillpatch.runners.benchflow_worker.freeze_verified_execution", return_value=self.execution),
                   patch("skillpatch.runners.benchflow_worker._create_session", return_value=self.session)]
        for mocked in patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def configure(self, callback, request=None):
        self.rollout.configure_diagnosis(self.root, request or self.request, self.task,
                                         {}, "0.6.3", on_verified=callback)

    def state(self):
        return json.loads((self.root / "backend" / "live_discovery.json").read_text())

    async def test_callback_receives_common_session_after_verify_before_cleanup(self):
        async def callback(session):
            self.assertIs(session, self.session)
            self.assertTrue(self.rollout.alive)
            self.assertEqual(self.rollout.events, ["install", "verify", "persist"])
            await session.inspector.prepare()
            self.rollout.events.append("diagnose")
            return {"status": "completed", "report": "report.json"}
        self.configure(callback)
        self.assertEqual(await self.rollout.run(), {"reward": 0})
        self.assertEqual(self.rollout.events, ["install", "verify", "persist", "inspect", "diagnose", "cleanup"])
        self.assertEqual(self.state()["status"], "completed")
        self.assertEqual(self.execution.summary, {"rewards": {"reward": 0}})
        self.assertFalse(self.rollout.alive)

    async def test_callback_failure_cleans_up_and_retains_original_score(self):
        async def callback(session):
            raise RuntimeError("diagnostic provider failed")
        self.configure(callback)
        self.assertEqual(await self.rollout.run(), {"reward": 0})
        self.assertEqual(self.state()["status"], "failed")
        self.assertFalse(self.rollout.alive)

    async def test_failed_callback_status_is_not_misreported_as_completed(self):
        async def callback(session):
            return {"status": "failed", "error": "invalid evidence reference"}
        self.configure(callback)
        await self.rollout.run()
        self.assertEqual(self.state()["status"], "failed")

    async def test_timeout_cleans_up_without_overwriting_score(self):
        async def callback(session):
            await asyncio.sleep(60)
        self.configure(callback, replace(self.request, diagnosis_timeout_sec=1))
        self.assertEqual(await self.rollout.run(), {"reward": 0})
        self.assertEqual(self.state()["status"], "timeout")
        self.assertFalse(self.rollout.alive)

    async def test_cancellation_is_recorded_and_cleanup_still_runs(self):
        async def callback(session):
            raise asyncio.CancelledError()
        self.configure(callback)
        with self.assertRaises(asyncio.CancelledError):
            await self.rollout.run()
        self.assertEqual(self.state()["status"], "cancelled")
        self.assertFalse(self.rollout.alive)

    async def test_initial_scan_limitations_are_preserved(self):
        self.configure(AsyncMock(return_value={"status": "completed"}))
        await self.rollout.run()
        initial = json.loads((self.root / "backend" / "initial_files.json").read_text())
        self.assertEqual(initial["limitations"], ["scan bounded"])
        self.assertEqual(initial["phase"], "post_install_pre_execution")

    async def test_wrapper_redacts_explicit_custom_named_diagnosis_credential(self):
        prompt = self.root / "prompt.md"
        prompt.write_text("Diagnose the recorded task.")
        config = self.root / "diagnosis.toml"
        config.write_text('[diagnosis]\nmodel="test-model"\nbase_url="https://example.com/v1"\n'
                          'api_key_env="CUSTOMDIAG"\nprompt_path="prompt.md"\n')
        secret = "custom-provider-value-123"
        async def callback(session):
            raise RuntimeError(secret)
        self.rollout.configure_diagnosis(self.root, replace(self.request, diagnosis_config=config),
            self.task, {"CUSTOMDIAG": secret}, "0.6.3", on_verified=callback)
        await self.rollout.run()
        self.assertEqual(self.state()["status"], "failed")
        self.assertNotIn(secret, (self.root / "backend" / "live_discovery.json").read_text())


class InspectionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_transport_is_agent_and_post_verify_transport_is_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            initial = {"hashes": {"/work/input.pdf": "sha256:original"}, "limitations": []}
            (root / "backend" / "initial_files.json").write_text(json.dumps(initial))
            request = RunRequest("fixture", root, "agent", "model", ("bench",), sandbox_user="agent")
            sandbox = SimpleNamespace(exec=AsyncMock(return_value={
                "return_code": 0, "stdout": json.dumps({"resources": [], "limitations": []})}))
            rollout = SimpleNamespace(env=sandbox, _agent_cwd="/work", _effective_skills_sandbox_dir="/skills",
                skillpatch_run_dir=root, skillpatch_request=request, skillpatch_environment={},
                skillpatch_secrets=("explicit-custom-credential",))
            await _snapshot_files(rollout, request)
            session = _create_session(rollout, BackendRun("completed"), "verified-timestamp")
            await session.inspector.prepare()
            self.assertEqual([call.kwargs["user"] for call in sandbox.exec.await_args_list], ["agent", "root"])
            self.assertEqual(session.metadata["inspection_user"], "root")
            self.assertEqual(session.metadata["initial_inspection_user"], "agent")
            self.assertIn("hardening", session.metadata["inspection_reason"])
            self.assertEqual(session.inspector.secrets, ("explicit-custom-credential",))
            self.assertEqual(session.inspector.initial_hashes, initial["hashes"])
            for call in sandbox.exec.await_args_list:
                self.assertTrue(call.args[0].startswith("python3 -I -c "))
                self.assertNotIn("chmod", call.args[0])


class WorkerEvidenceTests(unittest.TestCase):
    def test_cli_and_live_worker_consume_the_same_prepared_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir = root / "input/task"
            skills = task_dir / "environment/skills"
            (skills / "fixture").mkdir(parents=True)
            (skills / "fixture/SKILL.md").write_text("---\nname: fixture\n---\nRead relevant inputs.\n")
            document = task_dir / "task.md"
            document.write_text("Complete the original task.\n")
            task = PreparedTask("skillsbench", "task", task_dir, document, skills,
                                root / "input/manifest.json", "task-hash", "skill-hash")
            request = RunRequest("skillsbench", task_dir, "openhands", "model", ("bench",))
            BenchFlowBackend().prepare_prompt(request, task, root)
            cli = build_config(request, task, root)
            worker = rollout_config_values(request, task, root)
            effective = (root / "prompt/effective.md").read_text()
            self.assertEqual(cli["prompts"], [effective])
            self.assertEqual(worker["prompts"], [effective])
            self.assertEqual(effective.count("Before solving the task"), 1)

    def test_worker_import_does_not_import_optional_benchflow_sdk(self):
        before = {key for key in sys.modules if key == "benchflow" or key.startswith("benchflow.")}
        importlib.reload(sys.modules["skillpatch.runners.benchflow_worker"])
        after = {key for key in sys.modules if key == "benchflow" or key.startswith("benchflow.")}
        self.assertEqual(before, after)

    def test_freeze_copies_original_artifacts_without_changing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = root / "backend"
            backend.mkdir()
            trial = root / "jobs" / "job" / "example__1234abcd"
            (trial / "trajectory").mkdir(parents=True)
            (trial / "verifier").mkdir()
            summary = {"task_name": "example", "rewards": {"reward": 0}, "partial_trajectory": False}
            result_path = trial / "result.json"
            result_path.write_text(json.dumps(summary))
            trace = trial / "trajectory" / "acp_trajectory.jsonl"
            trace.write_text(json.dumps({"type": "user_message", "text": "Original task."}) + "\n")
            reward = trial / "verifier" / "reward.txt"
            reward.write_text("0")
            before = {path: path.read_bytes() for path in (result_path, trace, reward)}
            task = PreparedTask("fixture", "example", root, root / "task.md", None,
                                 root / "manifest.json", "hash", None)
            execution = freeze_verified_execution(root, task, "0.6.3")
            self.assertEqual(execution.summary, summary)
            self.assertEqual(execution.trajectory_events, 1)
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            manifest = json.loads((backend / "evidence_freeze.json").read_text())
            for item in manifest["files"]:
                self.assertEqual(Path(item["source_path"]).read_bytes(), Path(item["frozen_path"]).read_bytes())
            self.assertEqual(len(list((root / "jobs").rglob("result.json"))), 1)
