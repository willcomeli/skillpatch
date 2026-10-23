"""Benchmark-specific offline diagnosis input contract."""

from pathlib import Path
from typing import Protocol

from ..models import DiagnosisContext


class DiagnosisAdapter(Protocol):
    def build_diagnosis_context(self, run_dir: Path) -> DiagnosisContext:
        """Build inputs from a finished archive; native Runners also accept a task JSON file."""
        ...
