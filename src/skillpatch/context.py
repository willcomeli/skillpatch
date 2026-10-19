"""Benchmark-independent evidence rendering, digests and collector dispatch."""

import hashlib
import json
from pathlib import Path
from typing import Any

from skillpatch.models import DiscoveryInput
from skillpatch.storage import jsonable


def build_discovery_input(run_dir: Path, *, record_path: Path | None = None) -> DiscoveryInput:
    """Select an adapter at the boundary; collectors own their native layouts."""
    from skillpatch.adapters.registry import get_adapter
    from skillpatch.model_client import parse_json_object

    root = run_dir.resolve(strict=True)
    path = (record_path or root / "result.json").resolve(strict=True)
    allowed = root / ("backend/verified_record.json" if record_path is not None else "result.json")
    if path != allowed:
        raise ValueError("Evidence record is outside this run or its approved checkpoint")
    record = parse_json_object(path.read_text(encoding="utf-8"))
    if record.get("run_id") != root.name:
        raise ValueError("Executor run_id does not match this run directory")
    return get_adapter(record.get("benchmark")).collect_evidence(root, record_path=record_path)


def refresh_input_digest(discovery: DiscoveryInput) -> str:
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



def render_evidence(discovery: DiscoveryInput) -> str:
    """Render all available text without exposing local absolute source paths."""
    metadata = {
        "run_id": discovery.run_id, "benchmark": discovery.benchmark, "task_id": discovery.task_id,
        "input_digest": discovery.input_digest, "task_digest": discovery.task_digest,
        "skill_digest": discovery.skill_digest, "trajectory_complete": discovery.trajectory_complete,
        "relative_path_convention": "relative to the prepared skills root; skill_id identifies <package>/SKILL.md",
        "limitations": discovery.limitations,
    }
    parts = ["DISCOVERY INPUT METADATA\n" + _field_dump(metadata)]
    for item in discovery.evidence:
        labels = {"evidence_id": item.evidence_id, "kind": item.kind}
        for key in ("skill_id", "relative_path", "version", "availability", "source_role"):
            value = getattr(item, key)
            if value is not None:
                labels[key] = value
        parts.append("=== EVIDENCE ===\n" + _field_dump(labels) + "\n--- BEGIN TEXT ---\n" + item.text + "\n--- END TEXT ---")
    return "\n\n".join(parts) + "\n"
