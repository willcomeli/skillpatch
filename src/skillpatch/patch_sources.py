"""Revalidate an archived Stage 1 candidate against its original execution."""

import hashlib
import json
from pathlib import Path

from skillpatch.adapters.registry import get_adapter
from skillpatch.discovery_validation import validate_saved_report
from skillpatch.evidence_views import normalize_evidence_item
from skillpatch.evolution_eligibility import reward_eligibility
from skillpatch.model_client import parse_json_object
from skillpatch.models import DiscoveryInput, EvidenceItem, PreparedTask, RunRequest
from skillpatch.patch_models import PatchSource
from skillpatch.storage import jsonable, sha256_file


def read_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Stage 2 input must be a regular JSON file")
    return parse_json_object(path.read_text(encoding="utf-8"))


def load_request(path: Path) -> RunRequest:
    values = read_object(path)
    for name in ("task_dir", "skills_dir", "bench_python", "diagnosis_config"):
        if values.get(name) is not None:
            values[name] = Path(values[name])
    values["bench_command"] = tuple(values["bench_command"])
    return RunRequest(**values)


def _same_evidence(actual: EvidenceItem | None, expected: EvidenceItem) -> bool:
    # JSON comparison also distinguishes forged bool/float offsets from integers.
    return json.dumps(jsonable(actual), sort_keys=True, allow_nan=False) == json.dumps(
        jsonable(expected), sort_keys=True, allow_nan=False)


def load_patch_source(diagnosis_dir: Path, conflict_id: str, *, adapter=None) -> PatchSource:
    directory = diagnosis_dir.resolve(strict=True)
    if directory.parent.name != "discovery":
        raise ValueError("Stage 2 requires a diagnosis under its run's discovery directory")
    root = directory.parent.parent
    record = read_object(root / "result.json")
    state = read_object(directory / "result.json")
    raw_report = read_object(directory / "report.json")
    raw_input = read_object(directory / "input.json")
    if (record.get("run_id") != root.name or not record.get("finished_at")
            or record.get("status") != "completed"
            or state.get("status") != "completed" or state.get("run_id") != root.name):
        raise ValueError("Stage 2 needs a completed execution and completed diagnosis from the same run")
    # Partial rewards remain eligible even when the adapter cannot define passed.
    feedback = record.get("feedback") or {}
    if reward_eligibility(feedback) != "eligible":
        raise ValueError("Stage 2 requires a valid scored reward != 1")
    selected = adapter or get_adapter(record["benchmark"])
    checkpoint = root / "backend/verified_record.json" if state.get("mode") == "live_post_verification" else None
    original = selected.collect_evidence(root, record_path=checkpoint)
    claimed_digest = raw_input.get("input_digest")
    digest_payload = {key: value for key, value in raw_input.items() if key != "input_digest"}
    actual_digest = "sha256:" + hashlib.sha256(json.dumps(digest_payload, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if actual_digest != claimed_digest:
        raise ValueError("Stage 1 input digest does not match its contents")
    # Verify the archived schema before filling newer optional observation fields.
    # Earlier text-only inputs legitimately omit those fields.
    values = dict(raw_input)
    values["evidence"] = [EvidenceItem(**item) for item in values["evidence"]]
    context = DiscoveryInput(**values)
    if (context.run_id != original.run_id or context.benchmark != original.benchmark
            or context.task_id != original.task_id or context.task_digest != original.task_digest
            or context.skill_digest != original.skill_digest
            or context.trajectory_complete != original.trajectory_complete):
        raise ValueError("Stage 1 input differs from its original execution")
    archived = {e.evidence_id: e for e in context.evidence}
    canonical = {e.evidence_id: e for e in original.evidence}
    for key, item in canonical.items():
        # Accept historical raw evidence or the exact view derived from that raw
        # evidence. Never trust an archived text_view marker, hash, or range.
        if not (_same_evidence(archived.get(key), item)
                or _same_evidence(archived.get(key), normalize_evidence_item(item))):
            raise ValueError("Stage 1 Q/S/T/F evidence has changed")
    for key, item in archived.items():
        if key not in canonical:
            if item.kind != "artifact":
                raise ValueError("Only archived artifact observations may supplement execution evidence")
            source = Path(item.observation.get("local_source", ""))
            if (source.is_symlink() or not source.resolve().is_relative_to(directory)
                    or not source.is_file() or sha256_file(source) != item.version):
                raise ValueError("Archived observation source or version is unavailable")
    if raw_report.get("run_id") != context.run_id or raw_report.get("input_digest") != claimed_digest:
        raise ValueError("Stage 1 report is bound to another input")
    report = validate_saved_report(raw_report, context)
    matches = [c for c in report.conflicts if c.conflict_id == conflict_id]
    if len(matches) != 1:
        raise ValueError("Select one existing Stage 1 conflict; zero-conflict reports cannot induce patches")
    request = load_request(root / "request.json")
    values = dict(record["prepared"])
    for key in ("task_dir", "task_document", "skills_dir", "manifest_path"):
        if values.get(key) is not None:
            value = Path(values[key])
            if not value.resolve().is_relative_to(root / "input"):
                raise ValueError("Prepared source resolves outside the execution snapshot")
            values[key] = value
    prepared = PreparedTask(**values)
    if (request.benchmark != context.benchmark or prepared.benchmark != context.benchmark
            or prepared.task_id != context.task_id):
        raise ValueError("Source benchmark identity differs")
    return PatchSource(root, directory, request, prepared, context, matches[0])
