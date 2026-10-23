"""Map one native EviSkill task result to offline diagnosis inputs."""

import hashlib
import json
from pathlib import Path

from context import refresh_input_digest
from excutor.executor_adapter.storage import sha256_file
from .models import ContextItem, DiagnosisContext


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON number: {value}")


def read_native_record(path: Path) -> tuple[Path, dict]:
    path = path.expanduser().resolve(strict=True)
    if path.is_dir():
        path = path / "result.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a single saved task result JSON file")
    record = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if not isinstance(record, dict):
        raise ValueError("Expected one task result object, not a batch or JSONL file")
    # Also reject numeric overflow such as 1e999, parsed by JSON as infinity.
    json.dumps(record, allow_nan=False)
    return path, record


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_native_context(path: Path, benchmark: str) -> DiagnosisContext:
    path, record = read_native_record(path)
    if record.get("dataset") != benchmark:
        raise ValueError(f"Expected dataset={benchmark}")
    if record.get("status") in {"running", "in_progress", "preparing"}:
        raise ValueError("Task run has not finished")
    task_id = record.get("task_id")
    instruction = record.get("instruction")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("Task result requires a nonempty task_id")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Task result requires a nonempty instruction")
    trajectory = record.get("trajectory")
    if not isinstance(trajectory, list) or any(not isinstance(row, dict) for row in trajectory):
        raise ValueError("trajectory must be a list of step objects")
    if "steps" in record and (type(record["steps"]) is not int or record["steps"] != len(trajectory)):
        raise ValueError("steps does not match the saved trajectory length")
    version = sha256_file(path)
    items = []

    def add(identifier, kind, text, pointer, **labels):
        items.append(ContextItem(identifier, kind, text, str(path), version=version,
                                 observation={"json_pointer": pointer}, **labels))

    add("Q0001", "task", instruction, "/instruction", availability="initial")
    limitations = [
        "Native result provenance is limited to this saved JSON and its content hash; "
        "no independent task or Skill snapshot manifest is available.",
        "Saved Runner steps are not a complete model-message or tool-protocol transcript. "
        "A thought field is model-returned text, not verified internal reasoning.",
    ]
    skill_text = record.get("retrieved_memory_text")
    if skill_text is not None and not isinstance(skill_text, str):
        raise ValueError("retrieved_memory_text must be text or null")
    skill_digest = None
    if skill_text and skill_text.strip():
        add("S0001", "skill", skill_text, "/retrieved_memory_text", availability="initial",
            source_role="delivered_skill_memory")
        skill_digest = _digest(skill_text)
        limitations.append("Skill context is the delivered memory text; original Skill file "
                           "boundaries and stable patch targets cannot be inferred from it.")
    else:
        limitations.append("No delivered Skill text is saved; retrieved Skill IDs alone do not reveal its contents.")
    for number, row in enumerate(trajectory, 1):
        # Preserve all saved step fields, including native score/delta/availability fields.
        add(f"T{number:06d}", "trajectory",
            json.dumps(row, ensure_ascii=False, indent=2, allow_nan=False),
            f"/trajectory/{number - 1}")
    feedback_keys = ("success", "score", "steps", "max_steps", "task_completed",
                     "evaluation_summary", "error", "errors", "status")
    feedback = {key: record[key] for key in feedback_keys if key in record}
    add("F0001", "feedback", json.dumps(feedback, ensure_ascii=False, indent=2, allow_nan=False), "")
    complete = record.get("trajectory_complete")
    if complete is not None and type(complete) is not bool:
        raise ValueError("trajectory_complete must be boolean or null")
    if not trajectory:
        limitations.append("Saved trajectory is empty.")
    if complete is None:
        limitations.append("Trajectory completeness is not explicitly recorded; it is unknown, "
                           "even when success or done is true.")
    elif not complete:
        limitations.append("Trajectory is explicitly partial; missing actions cannot be inferred.")
    if "score" not in record:
        limitations.append("No final score is recorded; do not substitute zero.")
    if "success" not in record:
        limitations.append("No explicit success flag is recorded; do not infer it from another benchmark's scoring rule.")
    if benchmark == "scienceworld":
        limitations.append("ScienceWorld score, score_delta and score-availability flags retain "
                           "their native meanings; unavailable scores are not confirmed zero scores.")
    if benchmark == "appworld":
        limitations.append("AppWorld action text is code; next_observation is execution output. "
                           "Task completion and official evaluation success are distinct fields.")
    context = DiagnosisContext(
        run_id=str(record.get("run_id") or path.stem), benchmark=benchmark, task_id=task_id,
        task_digest=_digest(instruction), skill_digest=skill_digest,
        trajectory_complete=complete, items=items, limitations=limitations, input_digest="",
    )
    if type(record.get("success")) is bool and not record.get("error") and not record.get("errors"):
        context.task_succeeded = record["success"]
        if record["success"] and (record.get("score") == 0 or record.get("task_completed") is False):
            context.task_succeeded = None
    from .eligibility import native_eligibility
    context.evolution_eligibility, context.eligibility_reason = native_eligibility(record, benchmark)
    refresh_input_digest(context)
    return context
