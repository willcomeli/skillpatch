"""Offline diagnosis inputs from a native ScienceWorld Runner result."""

from pathlib import Path

from ..models import DiagnosisContext
from ..shared_context_builder import build_native_context


class ScienceWorldDiagnosisAdapter:
    def build_diagnosis_context(self, run_dir: Path) -> DiagnosisContext:
        """Accept a task-result JSON file or a directory containing result.json."""
        return build_native_context(run_dir, "scienceworld")
