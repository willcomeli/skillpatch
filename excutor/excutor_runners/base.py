"""Interface for running a prepared task."""

from pathlib import Path
from typing import Mapping, Protocol

from excutor.executor_adapter.models import BackendRun, PreparedTask, RunRequest


class RunBackend(Protocol):
    def run(self, request: RunRequest, task: PreparedTask, run_dir: Path,
            environment: Mapping[str, str]) -> BackendRun:
        """Execute a prepared task and return its execution record."""
        ...
