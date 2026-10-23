"""Offline diagnosis inputs from a native AppWorld Runner result."""

from pathlib import Path

from ..models import DiagnosisContext
from ..shared_context_builder import build_native_context


class AppWorldDiagnosisAdapter:
    def build_diagnosis_context(self, run_dir: Path) -> DiagnosisContext:
        """Accept a task-result JSON file or a directory containing result.json."""
        return build_native_context(run_dir, "appworld")
