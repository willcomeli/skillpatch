import json
import io
from contextlib import redirect_stdout
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from excutor.excutors import Executor
from excutor.executor_adapter.models import BackendRun, EvaluationFeedback, PreparedTask, RunRequest
from excutor.executor_adapter.storage import sha256_file
from excutor.excutor_runners.benchflow import BenchFlowBackend, build_config


class OtherBenchmark:
    """A test adapter with no SkillsBench directories or reward convention."""

    def prepare(self, request, destination):
        destination.mkdir()
        document = destination / "instructions.txt"
        document.write_text("An unrelated task format")
        return PreparedTask(request.benchmark, "foreign-task", destination, document,
                            None, destination / "manifest.json", "digest", None,
                            {"instructions.txt": sha256_file(document)})

    def evaluate(self, execution):
        return EvaluationFeedback("scored", {"distance": 12.5}, False)


class Backend:
    def __init__(self, action=None):
        self.calls = 0
        self.action = action

    def run(self, request, task, run_dir, environment):
        self.calls += 1
        if self.action:
            self.action(task, run_dir)
        return BackendRun("completed")


class PromptedBenchmark(OtherBenchmark):
    def prepare(self, request, destination):
        task = super().prepare(request, destination)
        skills = destination / "environment/skills"
        (skills / "fixture").mkdir(parents=True)
        skill = skills / "fixture/SKILL.md"
        skill.write_text("---\nname: fixture\n---\nInspect relevant inputs.\n")
        task.skills_dir = skills
        task.skill_digest = "fixture-skill-digest"
        task.file_hashes[skill.relative_to(destination).as_posix()] = sha256_file(skill)
        return task


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.request = RunRequest("another-benchmark", self.root / "source", "agent", "model", ("bench",))

    def test_other_benchmark_uses_same_executor_and_task_failure_is_completed(self):
        backend = Backend()
        result = Executor(OtherBenchmark(), backend).run(self.request, self.root / "run", {})
        self.assertEqual(result.status, "completed")
        self.assertFalse(result.feedback.passed)
        self.assertEqual(result.feedback.metrics, {"distance": 12.5})
        self.assertEqual(backend.calls, 1)

    def test_existing_run_is_never_resumed_or_overwritten(self):
        folder = self.root / "old"
        folder.mkdir()
        marker = folder / "result.json"
        marker.write_text("historical data")
        backend = Backend()
        with self.assertRaises(FileExistsError):
            Executor(OtherBenchmark(), backend).run(self.request, folder, {})
        self.assertEqual(marker.read_text(), "historical data")
        self.assertEqual(backend.calls, 0)



    def test_prepare_does_not_invoke_backend_or_claim_execution(self):
        backend = Backend()
        result = Executor(OtherBenchmark(), backend).run(self.request, self.root / "plan", {}, prepare_only=True)
        self.assertEqual(result.status, "prepared")
        self.assertIsNone(result.feedback)
        self.assertEqual(backend.calls, 0)

    def test_prepare_previews_the_same_prompt_as_the_run_path_without_launching(self):
        backend = BenchFlowBackend()
        request = replace(self.request, agent="openhands")
        preview_root = self.root / "preview"
        run_root = self.root / "actual-run"
        with patch.object(backend, "run") as run:
            preview = Executor(PromptedBenchmark(), backend).run(
                request, preview_root, {}, prepare_only=True)
            run.assert_not_called()
        self.assertEqual(preview.status, "prepared")
        self.assertIsNone(preview.execution)
        self.assertFalse((preview_root / "backend").exists())
        self.assertFalse((preview_root / "jobs").exists())

        def consume_prompt(request, task, root, environment):
            # The execution hook must see the preview before launching anything.
            effective = (root / "prompt/effective.md").read_text()
            self.assertEqual(build_config(request, task, root)["prompts"], [effective])
            return BackendRun("completed")
        with patch.object(backend, "run", side_effect=consume_prompt) as run:
            result = Executor(PromptedBenchmark(), backend).run(request, run_root, {})
            run.assert_called_once()
        self.assertEqual(result.status, "completed")
        for name in ("original.md", "added.md", "effective.md"):
            self.assertEqual((preview_root / "prompt" / name).read_bytes(),
                             (run_root / "prompt" / name).read_bytes())
        for record in (preview, result):
            self.assertEqual(record.prepared.task_document.read_text(), "An unrelated task format")
            self.assertTrue(all(sha256_file(record.prepared.task_dir / name) == digest
                                for name, digest in record.prepared.file_hashes.items()))

    def test_unsupported_harness_is_reported_before_executor_starts_backend(self):
        backend = BenchFlowBackend()
        with patch.object(backend, "run") as run:
            result = Executor(PromptedBenchmark(), backend).run(
                replace(self.request, agent="unsupported-harness"), self.root / "unsupported-preview", {})
            run.assert_not_called()
        self.assertEqual(result.status, "backend_error")
        self.assertIsNone(result.execution)
        self.assertTrue(result.errors)







    def test_missing_evaluation_is_not_a_completed_zero_score(self):
        class Missing(OtherBenchmark):
            def evaluate(self, execution):
                return EvaluationFeedback("unavailable")
        result = Executor(Missing(), Backend()).run(self.request, self.root / "run", {})
        self.assertEqual(result.status, "artifact_error")
        self.assertEqual(result.feedback.metrics, {})
        self.assertIsNone(result.feedback.passed)

    def test_modified_snapshot_is_reported(self):
        backend = Backend(lambda task, _: task.task_document.write_text("changed"))
        result = Executor(OtherBenchmark(), backend).run(self.request, self.root / "run", {})
        self.assertEqual(result.status, "artifact_error")
        self.assertEqual(result.errors[-1]["stage"], "provenance")

    def test_backend_failure_is_saved_and_known_secret_is_redacted(self):
        secret = "test-credential-value-123"
        def fail(task, folder):
            (folder / "backend.log").write_text("provider said " + secret)
            raise RuntimeError("failure with " + secret)
        folder = self.root / "failed"
        result = Executor(OtherBenchmark(), Backend(fail)).run(self.request, folder, {"LLM_API_KEY": secret})
        self.assertEqual(result.status, "backend_error")
        self.assertNotIn(secret, (folder / "result.json").read_text())
        self.assertNotIn(secret, (folder / "backend.log").read_text())
        self.assertIsNotNone(json.loads((folder / "result.json").read_text())["finished_at"])

    def test_preparation_error_remains_inspectable(self):
        class Broken(OtherBenchmark):
            def prepare(self, request, destination):
                raise ValueError("invalid task")
        folder = self.root / "failed"
        backend = Backend()
        result = Executor(Broken(), backend).run(self.request, folder, {})
        self.assertEqual(result.status, "preparation_error")
        self.assertEqual(backend.calls, 0)
        self.assertTrue((folder / "result.json").is_file())


if __name__ == "__main__":
    unittest.main()
