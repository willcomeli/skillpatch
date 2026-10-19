import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skillpatch.cli import main
from skillpatch.models import EvaluationFeedback, ExecutionResult, RunRequest


class LiveDiagnosisExitTests(unittest.TestCase):
    def invoke(self, diagnosis, feedback, *, status="completed"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = RunRequest("fixture", root / "task", "agent", "model", ("bench",),
                                 diagnosis_config=root / "diagnosis.toml")
            result = ExecutionResult("run", "fixture", "task", status, "start",
                                     feedback=feedback, diagnosis=diagnosis)
            with (patch("skillpatch.cli.load_environment", return_value={}),
                  patch("skillpatch.cli.load_config", return_value=(request, root)),
                  patch("skillpatch.cli.get_adapter"),
                  patch("skillpatch.cli.Executor") as executor,
                  redirect_stdout(io.StringIO())):
                executor.return_value.run.return_value = result
                return main(["run", "--config", str(root / "run.toml")])

    def test_successful_task_with_recorded_success_skip_exits_zero(self):
        self.assertEqual(self.invoke({"status": "skipped", "reason": "task_succeeded"},
                                     EvaluationFeedback("scored", {"reward": 1}, True)), 0)

    def test_reward_one_with_unknown_passed_skips_and_partial_reward_does_not(self):
        diagnosis = {"status": "skipped", "reason": "task_succeeded"}
        self.assertEqual(self.invoke(diagnosis, EvaluationFeedback("scored", {"reward": 1}, None)), 0)
        self.assertEqual(self.invoke(diagnosis, EvaluationFeedback("scored", {"reward": 0.85}, True)), 2)

    def test_missing_reward_has_its_own_skip_reason(self):
        diagnosis = {"status": "skipped", "reason": "reward_unavailable"}
        self.assertEqual(self.invoke(diagnosis, EvaluationFeedback("scored", {}, False)), 0)
        self.assertEqual(self.invoke(diagnosis, EvaluationFeedback("scored", {"reward": 0}, False)), 2)

    def test_unjustified_skip_does_not_exit_zero(self):
        for reason, feedback in [
            ("unsupported", EvaluationFeedback("scored", {}, True)),
            ("task_succeeded", EvaluationFeedback("scored", {}, False)),
            ("task_succeeded", EvaluationFeedback("scored", {}, None)),
            ("task_succeeded", EvaluationFeedback("error", {}, True)),
            ("task_succeeded", None),
        ]:
            with self.subTest(reason=reason, feedback=feedback):
                self.assertEqual(self.invoke({"status": "skipped", "reason": reason}, feedback), 2)

    def test_skip_does_not_mask_execution_failure(self):
        self.assertEqual(self.invoke({"status": "skipped", "reason": "task_succeeded"},
                                     EvaluationFeedback("scored", {}, True), status="artifact_error"), 2)

    def test_failed_task_with_completed_diagnosis_still_exits_zero(self):
        self.assertEqual(self.invoke({"status": "completed"}, EvaluationFeedback("scored", {}, False)), 0)

    def test_failed_diagnosis_still_exits_two(self):
        self.assertEqual(self.invoke({"status": "failed"}, EvaluationFeedback("scored", {}, False)), 2)
