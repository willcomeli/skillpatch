"""Benchmark-independent diagnosis context rendering and digests."""

import hashlib
import json
from pathlib import Path
from typing import Any

from method_input_adapters.skillpatch.models import DiagnosisContext
from excutor.executor_adapter.storage import jsonable


def refresh_input_digest(discovery: DiagnosisContext) -> str:
    payload = jsonable(discovery)
    del payload["input_digest"]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    discovery.input_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return discovery.input_digest


def _field_dump(value: Any, prefix: str = "") -> str:
    """Flatten field labels, retaining the exact newlines of textual values."""
    if isinstance(value, dict) and value:
        return "\n\n".join(_field_dump(value[key], f"{prefix}.{key}" if prefix else key) for key in sorted(value))
    if isinstance(value, list) and value:
        return "\n\n".join(_field_dump(item, f"{prefix}[{index}]") for index, item in enumerate(value))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    return f"{prefix}:\n{text}" if prefix else text



def render_diagnosis_context(discovery: DiagnosisContext) -> str:
    """Render all available text without exposing local absolute source paths."""
    metadata = {
        "run_id": discovery.run_id, "benchmark": discovery.benchmark, "task_id": discovery.task_id,
        "input_digest": discovery.input_digest, "task_digest": discovery.task_digest,
        "skill_digest": discovery.skill_digest, "trajectory_complete": discovery.trajectory_complete,
        "relative_path_convention": ("relative to the prepared skills root; skill_id identifies <package>/SKILL.md"
                                     if discovery.benchmark == "skillsbench" else
                                     "native result items use source JSON pointers; original Skill file boundaries may be unavailable"),
        "limitations": discovery.limitations,
        "evolution_eligibility": discovery.evolution_eligibility,
        "eligibility_reason": discovery.eligibility_reason,
    }
    parts = ["DIAGNOSIS CONTEXT METADATA\n" + _field_dump(metadata)]
    for item in discovery.items:
        labels = {"context_id": item.context_id, "kind": item.kind}
        for key in ("skill_id", "relative_path", "version", "availability", "source_role"):
            value = getattr(item, key)
            if value is not None:
                labels[key] = value
        parts.append("=== CONTEXT ITEM ===\n" + _field_dump(labels) + "\n--- BEGIN TEXT ---\n" + item.text + "\n--- END TEXT ---")
    return "\n\n".join(parts) + "\n"
