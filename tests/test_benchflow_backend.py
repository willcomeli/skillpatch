import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from excutor.executor_adapter.models import BackendRun, PreparedTask, RunRequest
from excutor.excutor_runners.benchflow import BenchFlowBackend


FAKE_BENCH = r'''
import json, os, pathlib, sys, time
if "--version" in sys.argv:
    print("bench, version " + os.environ.get("FAKE_VERSION", "0.6.3"))
    raise SystemExit(0)
config = json.loads(pathlib.Path(sys.argv[sys.argv.index("--config") + 1]).read_text())
mode = os.environ.get("FAKE_MODE", "ok")
task = config["include"][0]
root = pathlib.Path(config["jobs_dir"])
for index in range(2 if mode == "ambiguous" else 1):
    trial = root / "new-job" / (task + "__" + str(index))
    (trial / "trajectory").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "verifier" / "reward.txt").write_text("0")
    summary = {"task_name": "different" if mode == "wrong-task" else task,
               "rewards": {"reward": 0}, "partial_trajectory": False,
               "observed_debug": os.environ.get("DEBUG")}
    if mode == "unknown": summary.pop("partial_trajectory")
    if mode == "partial": summary["partial_trajectory"] = True
    if mode == "count-mismatch": summary["trajectory_summary"] = {"steps": 3}
    if mode in ("agent-error", "agent-error-exit"): summary.update(error="agent failed", error_category="api_error")
    if mode == "env-error": summary.update(error="setup failed", error_category="sandbox_startup")
    if mode == "verifier-error": summary["verifier_error"] = "test process failed"
    if mode != "timeout-no-result":
        (trial / "result.json").write_text(json.dumps(summary))
    if mode == "result-overflow":
        (trial / "result.json").write_text('{"task_name":"task-a", "rewards":{"reward":1e999}}')
    events = [{"type": "user_message", "text": "task"},
              {"type": "tool_call", "status": "completed", "extra": {"untouched": [1, 2]}}]
    content = "".join(json.dumps(event) + "\n" for event in events)
    if mode == "malformed": content += "{broken\n"
    if mode == "event-overflow": content += '{"type":"tool_call", "number":1e999}\n'
    if mode == "empty": content = ""
    (trial / "trajectory" / "acp_trajectory.jsonl").write_text(content)
if mode.startswith("timeout"):
    time.sleep(60)
if mode == "agent-error-exit": raise SystemExit(1)
'''


FAKE_DOCKER = r'''
import json, os, pathlib, sys
path = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
if args[0] == "ps":
    print("\n".join(state["containers"]))
elif args[:2] == ["container", "inspect"]:
    print(json.dumps(state["records"][args[-1]]))
elif args[:2] == ["rm", "-f"]:
    state["containers"] = [cid for cid in state["containers"] if cid not in args[2:]]
else:
    raise SystemExit(1)
path.write_text(json.dumps(state))
'''


class BenchFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        fake = self.root / "fake_bench.py"
        fake.write_text(textwrap.dedent(FAKE_BENCH))
        task_dir = self.root / "task-a"
        skill_dir = task_dir / "environment" / "skills"
        skill_dir.mkdir(parents=True)
        (task_dir / "task.md").write_text("Complete the fixture task.\n")
        (skill_dir / "fixture").mkdir()
        (skill_dir / "fixture" / "SKILL.md").write_text("---\nname: fixture\n---\nInspect relevant inputs.\n")
        self.task = PreparedTask("skillsbench", "task-a", task_dir, task_dir / "task.md",
                                 skill_dir, self.root / "manifest.json", "task-hash", "skill-hash")
        self.request = RunRequest("skillsbench", task_dir, "openhands", "provider/model",
                                  (sys.executable, str(fake)), expected_backend_version="0.6.3")
        self.run_dir = self.root / "run"
        self.backend = BenchFlowBackend(termination_grace_sec=0.05)

    def run_fake(self, mode="ok", request=None):
        return self.backend.run(request or self.request, self.task, self.run_dir,
                                {**os.environ, "FAKE_MODE": mode})

    def test_new_rollout_and_lossless_events(self):
        result = self.run_fake()
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.backend_version, "0.6.3")
        self.assertTrue(result.trajectory_complete)
        self.assertEqual(result.summary["rewards"]["reward"], 0)
        events = [json.loads(line) for line in result.normalized_trajectory_path.read_text().splitlines()]
        self.assertEqual(events[1]["event_id"], "T000002")
        self.assertEqual(events[1]["source"]["line"], 2)
        self.assertEqual(events[1]["event"]["extra"], {"untouched": [1, 2]})
        config = json.loads((self.run_dir / "backend" / "config.json").read_text())
        self.assertEqual(config["max_retries"], 0)
        self.assertEqual(config["loop_strategy"], "single-shot")
        self.assertEqual(config["build_concurrency"], 1)
        self.assertIsNone(config["skills_dir"])
        self.assertIn("Before solving the task, invoke and read these task-provided Skills: `fixture`.",
                      config["prompts"][0])
        self.assertEqual(config["prompts"][0], (self.run_dir / "prompt/effective.md").read_text())
        self.assertTrue((self.run_dir / "backend" / "skill_reads.json").is_file())

    def test_direct_backend_preserves_task_and_saves_prompt_before_launch(self):
        original = self.task.task_document.read_bytes()
        result = self.run_fake()
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.task.task_document.read_bytes(), original)
        self.assertEqual((self.run_dir / "prompt/original.md").read_bytes(), original)
        self.assertTrue((self.run_dir / "prompt/added.md").is_file())
        self.assertTrue((self.run_dir / "prompt/manifest.json").is_file())

    def test_unsupported_explicit_harness_fails_before_launch(self):
        with patch.object(self.backend, "_execute") as execute:
            result = self.run_fake(request=replace(self.request, agent="unsupported-harness"))
        self.assertEqual(result.status, "backend_error")
        execute.assert_not_called()
        self.assertFalse((self.run_dir / "backend").exists())

    def test_prepared_prompt_cannot_drift_before_backend_launch(self):
        self.backend.prepare_prompt(self.request, self.task, self.run_dir)
        path = self.run_dir / "prompt/effective.md"
        path.write_text("Different execution instruction")
        with patch.object(self.backend, "_execute") as execute:
            result = self.run_fake()
        self.assertEqual(result.status, "backend_error")
        execute.assert_not_called()
        self.assertEqual(path.read_text(), "Different execution instruction")



    def test_unknown_partial_is_unknown(self):
        self.assertIsNone(self.run_fake("unknown").trajectory_complete)

    def test_invalid_debug_is_removed_only_from_child_environment(self):
        with patch.dict(os.environ, {"DEBUG": "release"}):
            environment = dict(os.environ)
            result = self.backend.run(self.request, self.task, self.run_dir, environment)
            self.assertEqual(os.environ["DEBUG"], "release")
            self.assertEqual(environment["DEBUG"], "release")
        self.assertEqual(result.status, "completed")
        self.assertIsNone(result.summary["observed_debug"])
        self.assertFalse(result.errors)
        report_text = Path(result.artifacts["environment_filter"]).read_text()
        self.assertNotIn("release", report_text)
        self.assertEqual(json.loads(report_text)["removed"][0]["name"], "DEBUG")

    def test_boolean_debug_values_are_preserved(self):
        for index, value in enumerate(("0", "1", "f", "false", "n", "no", "off", "on", "t", "true", "y", "yes", "", "TRUE")):
            with self.subTest(value=value):
                result = self.backend.run(self.request, self.task, self.root / f"debug-{index}",
                                          {**os.environ, "DEBUG": value})
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.summary["observed_debug"], value)
                report = json.loads(Path(result.artifacts["environment_filter"]).read_text())
                self.assertEqual(report["removed"], [])

    def test_explicit_partial(self):
        self.assertFalse(self.run_fake("partial").trajectory_complete)

    def test_multiple_results_rejected(self):
        result = self.run_fake("ambiguous")
        self.assertEqual(result.status, "artifact_error")
        self.assertIsNone(result.result_path)
        self.assertEqual(result.errors[0]["code"], "ambiguous_results")

    def test_wrong_task_rejected(self):
        result = self.run_fake("wrong-task")
        self.assertEqual(result.status, "artifact_error")
        self.assertIsNone(result.trajectory_path)

    def test_malformed_trajectory_preserves_valid_events(self):
        result = self.run_fake("malformed")
        self.assertEqual(result.status, "artifact_error")
        self.assertFalse(result.trajectory_complete)
        self.assertEqual(result.trajectory_events, 2)

    def test_empty_trajectory_not_complete(self):
        result = self.run_fake("empty")
        self.assertEqual(result.status, "artifact_error")
        self.assertFalse(result.trajectory_complete)

    def test_summary_event_count_mismatch_not_complete(self):
        result = self.run_fake("count-mismatch")
        self.assertEqual(result.status, "artifact_error")
        self.assertFalse(result.trajectory_complete)
        self.assertTrue(any(error["code"] == "trajectory_count_mismatch" for error in result.errors))

    def test_nonfinite_json_is_reported_instead_of_raising(self):
        for mode in ("result-overflow", "event-overflow"):
            with self.subTest(mode=mode):
                self.run_dir = self.root / mode
                result = self.run_fake(mode)
                self.assertEqual(result.status, "artifact_error")
                self.assertIsNot(result.trajectory_complete, True)

    def run_interrupted_timeout(self, mode):
        # Simulate a subprocess timeout signal to test partial collection/cleanup;
        # normal rollouts no longer have an outer wall-clock timer.
        original_wait = subprocess.Popen.wait
        interrupted = False
        def wait(process, *args, **kwargs):
            nonlocal interrupted
            if "eval" in process.args and not interrupted:
                interrupted = True
                import time
                time.sleep(0.3)
                raise subprocess.TimeoutExpired(process.args, 0.3)
            return original_wait(process, *args, **kwargs)
        with patch("subprocess.Popen.wait", new=wait):
            return self.run_fake(mode)

    def test_rollout_waits_without_an_outer_timeout(self):
        original_wait = subprocess.Popen.wait
        observed = []
        def wait(process, *args, **kwargs):
            if "eval" in process.args:
                observed.append(kwargs.get('timeout'))
            return original_wait(process, *args, **kwargs)
        with patch("subprocess.Popen.wait", new=wait):
            result = self.run_fake()
        self.assertEqual(result.status, "completed")
        self.assertEqual(observed, [None])

    def test_timeout_collects_partial_without_result(self):
        result = self.run_interrupted_timeout("timeout-no-result")
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.trajectory_complete)
        self.assertEqual(result.trajectory_events, 2)
        self.assertTrue(any(error["code"] == "cleanup_unconfirmed" for error in result.errors))

    def test_timeout_cannot_be_reported_complete(self):
        result = self.run_interrupted_timeout("timeout")
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.trajectory_complete)

    def test_keyboard_interrupt_is_cancelled(self):
        original_wait = subprocess.Popen.wait
        interrupted = False

        def wait(process, *args, **kwargs):
            nonlocal interrupted
            if "eval" in process.args and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(process, *args, **kwargs)

        with patch("subprocess.Popen.wait", new=wait):
            result = self.run_fake()
        self.assertEqual(result.status, "cancelled")
        self.assertIsNot(result.trajectory_complete, True)

    def test_reused_jobs_directory_rejected(self):
        (self.run_dir / "jobs").mkdir(parents=True)
        self.assertEqual(self.run_fake().status, "backend_error")

    def test_process_signals_allow_asyncio_cleanup_first(self):
        process = Mock(pid=123456)
        with patch("os.killpg") as killpg:
            BenchFlowBackend(termination_grace_sec=0)._stop(process, BackendRun("cancelled"))
        self.assertEqual([call.args[1] for call in killpg.call_args_list],
                         [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])

    def cleanup_fixture(self, wrong_mount=False):
        jobs = self.run_dir / "jobs"
        backend = self.run_dir / "backend"
        trial = jobs / "new-job" / "task-a__1234abcd"
        trial.mkdir(parents=True)
        backend.mkdir()
        (trial / "config.json").write_text(json.dumps({"task_path": "task-a", "environment": "docker"}))
        container_id = "a" * 64
        state_path = self.root / "docker-state.json"
        record = {"id": container_id, "project": trial.name, "owned": "true",
                  "working_dir": "/temporary/task-a/environment", "config_files": "/installed/compose.yaml",
                  "mounts": [{"Source": str(self.root / "old-run" if wrong_mount else trial / "agent")}]}
        state_path.write_text(json.dumps({"containers": [container_id], "records": {container_id: record}}))
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        binary = binary_dir / "docker"
        binary.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_DOCKER))
        binary.chmod(0o755)
        environment = {**os.environ, "PATH": str(binary_dir) + os.pathsep + os.environ.get("PATH", ""),
                       "FAKE_DOCKER_STATE": str(state_path)}
        return jobs, backend, environment, state_path, container_id

    def test_cleanup_only_verified_current_run_container(self):
        jobs, backend, environment, state_path, container_id = self.cleanup_fixture()
        result = BackendRun("cancelled")
        self.backend._cleanup_docker(self.task, jobs, backend, environment, result)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["containers"], [])
        self.assertIn(["rm", "-f", container_id], state["calls"])
        self.assertEqual(json.loads((backend / "cleanup.json").read_text())["status"], "containers_absent")
        self.assertFalse(result.errors)

    def test_cleanup_refuses_old_container_with_matching_project_label(self):
        jobs, backend, environment, state_path, container_id = self.cleanup_fixture(wrong_mount=True)
        result = BackendRun("cancelled")
        self.backend._cleanup_docker(self.task, jobs, backend, environment, result)
        state = json.loads(state_path.read_text())
        self.assertEqual(state["containers"], [container_id])
        self.assertFalse(any(call[:2] == ["rm", "-f"] for call in state["calls"]))
        self.assertEqual(result.errors[0]["code"], "cleanup_unconfirmed")

    def test_version_mismatch_does_not_execute(self):
        request = replace(self.request, expected_backend_version="0.6.4")
        result = self.run_fake(request=request)
        self.assertEqual(result.status, "backend_error")
        self.assertEqual(list((self.run_dir / "jobs").iterdir()), [])

    def test_error_categories(self):
        for mode, expected in (("agent-error", "agent_error"), ("env-error", "environment_error"),
                               ("verifier-error", "verifier_error"), ("agent-error-exit", "agent_error")):
            with self.subTest(mode=mode):
                self.run_dir = self.root / mode
                self.assertEqual(self.run_fake(mode).status, expected)


if __name__ == "__main__":
    unittest.main()
