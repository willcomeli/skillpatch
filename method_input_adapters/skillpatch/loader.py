"""Choose a diagnosis adapter using the archived execution identity."""

from pathlib import Path

from .models import DiagnosisContext
from .registry import get_diagnosis_adapter


def build_diagnosis_context(run_dir: Path) -> DiagnosisContext:
    from .shared_context_builder import read_native_record

    root = run_dir.expanduser().resolve(strict=True)
    path, record = read_native_record(root)
    if "dataset" in record:
        return get_diagnosis_adapter(record["dataset"]).build_diagnosis_context(path)
    if not root.is_dir():
        raise ValueError("Executor archives must be supplied as a run directory")
    if record.get("run_id") != root.name:
        raise ValueError("Executor run_id does not match this run directory")
    if not record.get("finished_at"):
        raise ValueError("Executor run has not finished")
    return get_diagnosis_adapter(record.get("benchmark")).build_diagnosis_context(root)
