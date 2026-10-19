"""Load complete execution archives for reuse as validation evidence."""

from pathlib import Path

from skillpatch.models import BackendRun, EvaluationFeedback, ExecutionResult, PreparedTask
from skillpatch.patch_sources import read_object
from skillpatch.patches import tree_hashes


def usable_execution(result):
    return (result.status == "completed" and not result.errors and result.execution is not None
            and result.execution.trajectory_complete is True and result.feedback is not None
            and result.feedback.status == "scored" and not result.feedback.issues)


def load_execution(directory: Path) -> ExecutionResult:
    directory = directory.resolve(strict=True)
    data = read_object(directory / "result.json")
    prepared = dict(data["prepared"])
    for key in ("task_dir", "task_document", "skills_dir", "manifest_path"):
        if prepared.get(key) is not None:
            prepared[key] = Path(prepared[key])
    execution = dict(data["execution"])
    for key in ("result_path", "trajectory_path", "normalized_trajectory_path", "verifier_dir"):
        if execution.get(key) is not None:
            execution[key] = Path(execution[key])
    data.update(prepared=PreparedTask(**prepared), execution=BackendRun(**execution),
                feedback=EvaluationFeedback(**data["feedback"]))
    result = ExecutionResult(**data)
    if result.run_id != directory.name or not result.finished_at or not usable_execution(result):
        raise ValueError("Validation needs complete, scored execution records")
    if not result.prepared.task_dir.is_relative_to(directory / "input"):
        raise ValueError("Prepared task is outside its execution snapshot")
    if tree_hashes(result.prepared.task_dir) != result.prepared.file_hashes:
        raise ValueError("Execution input snapshot changed")
    return result
