from pathlib import Path
from typing import Protocol

from skillpatch.models import BackendRun, Conflict, DiscoveryInput, EvaluationFeedback, PreparedTask, RunRequest
from skillpatch.patch_models import FeedbackComparison


class BenchmarkAdapter(Protocol):
    def prepare(self, request: RunRequest, destination: Path) -> PreparedTask: ...

    def evaluate(self, execution: BackendRun) -> EvaluationFeedback: ...

    def compare_feedback(self, baseline: EvaluationFeedback, candidate: EvaluationFeedback, *,
                         target: Conflict | None = None) -> FeedbackComparison:
        """Interpret improvement and regression using this benchmark's metric semantics."""
        ...

    def collect_evidence(self, run_dir: Path, *, record_path: Path | None = None) -> DiscoveryInput:
        """Translate native records into common evidence; check source provenance.

        record_path identifies a verified checkpoint when the environment is
        still alive. None selects the completed run for offline diagnosis.
        """
        ...
