"""The execution boundary excludes diagnosis and feedback comparison."""

from pathlib import Path
from typing import Protocol

from .models import BackendRun, EvaluationFeedback, PreparedTask, RunRequest


class ExecutionAdapter(Protocol):
    def prepare(self, request: RunRequest, destination: Path) -> PreparedTask:
        """Prepare an independent task and Skill snapshot."""
        ...

    def evaluate(self, execution: BackendRun) -> EvaluationFeedback:
        """Read and normalize official verifier feedback."""
        ...
