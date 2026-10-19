"""Compose adapters, portable verified sessions and the Stage 1 agent."""

from copy import deepcopy
from pathlib import Path
from typing import Mapping

from skillpatch.adapters.base import BenchmarkAdapter
from skillpatch.adapters.registry import get_adapter
from skillpatch.context import refresh_input_digest
from skillpatch.environment import redact
from skillpatch.evolution_eligibility import reward_eligibility
from skillpatch.lifecycle import VerifiedCallback, VerifiedSession
from skillpatch.model_client import diagnosis_secrets, load_diagnosis_config, parse_json_object
from skillpatch.models import PreparedTask, RunRequest
from skillpatch.stages.live_discovery import diagnose_live_context
from skillpatch.storage import jsonable, now, write_json


def create_diagnosis_callback(request: RunRequest, prepared: PreparedTask,
                              run_dir: Path, environment: Mapping[str, str], *,
                              adapter: BenchmarkAdapter | None = None) -> VerifiedCallback:
    """Choose benchmark-specific collectors once, outside the generic agent loop."""
    if request.diagnosis_config is None:
        raise ValueError("Live diagnosis requires diagnosis_config")
    selected = adapter if adapter is not None else get_adapter(request.benchmark)
    if not callable(getattr(selected, "collect_evidence", None)):
        raise ValueError("This adapter does not supply Stage 1 evidence collection")
    config = load_diagnosis_config(request.diagnosis_config)
    root = run_dir.resolve()
    secrets = diagnosis_secrets(config, environment)

    async def on_verified(session: VerifiedSession) -> dict:
        record = parse_json_object((root / "result.json").read_text(encoding="utf-8"))
        if (record.get("run_id") != root.name or record.get("benchmark") != request.benchmark
                or record.get("task_id") != prepared.task_id or record.get("finished_at")):
            raise ValueError("Verified callback requires the matching active run")
        execution = deepcopy(session.execution)
        feedback = selected.evaluate(execution)
        # This is a phase checkpoint, not a completed executor run. It has a
        # verified_at timestamp and deliberately no finished_at timestamp.
        checkpoint = {
            "schema_version": 1, "run_id": root.name, "benchmark": request.benchmark,
            "task_id": prepared.task_id, "status": "verified",
            "phase": "post_verification_pre_cleanup", "verified_at": now(),
            "started_at": record["started_at"], "prepared": jsonable(prepared),
            "execution": jsonable(execution), "feedback": jsonable(feedback),
            "errors": execution.errors,
        }
        path = root / "backend" / "verified_record.json"
        write_json(path, redact(checkpoint, secrets))
        eligibility = reward_eligibility(feedback)
        if eligibility != "eligible":
            return {"status": "skipped", "reason": "task_succeeded" if eligibility == "succeeded" else "reward_unavailable",
                    "phase": "eligibility", "run_id": root.name,
                    "model_calls": 0, "tool_calls": 0, "semantic_status": "not_assessed"}
        context = selected.collect_evidence(root, record_path=path)
        if (context.run_id != root.name or context.benchmark != request.benchmark
                or context.task_id != prepared.task_id):
            raise ValueError("Collected evidence identity does not match the verified session")
        limitations = session.metadata.get("limitations", [])
        if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
            raise ValueError("Session limitations must be a list of strings")
        context.limitations.extend(limitations)
        refresh_input_digest(context)
        return await diagnose_live_context(context, config, session.inspector,
                                           root / "discovery" / "live", environment,
                                           timeout_sec=request.diagnosis_timeout_sec)

    return on_verified
