"""SkillsBench/BenchFlow evidence collection with strict snapshot provenance."""

import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.storage import hash_manifest, sha256_file
from skillpatch.context import refresh_input_digest


_RAW_FEEDBACK = ("test-stdout.txt", "test-stderr.txt", "reward.txt", "ctrf.json")
_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".mp3", ".mp4",
    ".wav", ".ogg", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf", ".pyc", ".so",
}
_DATA_URI = re.compile(r"data:[^\s\"'<>]*;base64,[A-Za-z0-9+/=]+")
_LONG_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/])")
_IMAGE_PLACEHOLDER = re.compile(r"\[Image:\s*\d+\s+URLs?\]", re.IGNORECASE)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite JSON number: {value}")
    return number


def _loads(text: str) -> Any:
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def _inside(root: Path, value: Any, label: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must identify a path within this run")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"{label} points outside this run: {value}")
    if directory and not resolved.is_dir():
        raise ValueError(f"{label} directory is missing: {value}")
    if not directory and not resolved.is_file():
        raise ValueError(f"{label} file is missing: {value}")
    return resolved


def _hash_snapshot(task_dir: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(task_dir.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Snapshot must not contain symlinks: {path}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Snapshot contains a non-regular file: {path}")
        # Oracle and verifier source bytes are hashed solely for snapshot
        # integrity. Their bodies are never opened as evidence or executed.
        hashes[path.relative_to(task_dir).as_posix()] = sha256_file(path)
    return hashes


def _sanitize(value: Any, limitations: list[str], evidence_id: str, field: str = "") -> Any:
    if isinstance(value, str):
        if _IMAGE_PLACEHOLDER.search(value):
            limitations.append(
                f"{evidence_id} {field}: image placeholder records an image-view result, "
                "but the underlying image content is not available in this text evidence."
            )
            limitations.append(
                "trajectory_complete refers to recorded event coverage, "
                "not complete observability of underlying artifacts."
            )
        clean, count = _DATA_URI.subn("[non-text data URI omitted]", value)
        clean, encoded_count = _LONG_BASE64.subn("[encoded binary payload omitted]", clean)
        if count or encoded_count:
            limitations.append(f"{evidence_id} {field}: encoded non-text payload omitted; consult the raw artifact.")
        return clean
    if isinstance(value, list):
        return [_sanitize(item, limitations, evidence_id, f"{field}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        media_type = str(value.get("type", "")).lower()
        mime_type = str(value.get("mimeType", value.get("mime_type", ""))).lower()
        media = media_type in {"image", "audio", "video", "image_url", "audio_url", "input_image", "input_audio"}
        media = media or mime_type.startswith(("image/", "audio/", "video/"))
        clean = {}
        for key in sorted(value):
            item = value[key]
            child = f"{field}.{key}" if field else key
            if key == "blob" or (media and key in {"data", "image_url", "audio_url", "url", "bytes", "base64"}):
                clean[key] = "[non-text payload omitted; raw artifact retained]"
                limitations.append(f"{evidence_id} {child}: non-text payload unavailable to the text diagnosis model.")
            else:
                clean[key] = _sanitize(item, limitations, evidence_id, child)
        return clean
    return value


def _field_dump(value: Any, prefix: str = "") -> str:
    """Flatten field labels, retaining the exact newlines of textual values."""
    if isinstance(value, dict) and value:
        return "\n\n".join(_field_dump(value[key], f"{prefix}.{key}" if prefix else key) for key in sorted(value))
    if isinstance(value, list) and value:
        return "\n\n".join(_field_dump(item, f"{prefix}[{index}]") for index, item in enumerate(value))
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    return f"{prefix}:\n{text}" if prefix else text


def _text_file(path: Path, limitations: list[str], label: str) -> str | None:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        limitations.append(f"{label}: non-text file {path.name} was not presented to the text diagnosis model.")
        return None
    try:
        content = path.read_bytes().decode("utf-8")
    except UnicodeError:
        limitations.append(f"{label}: {path.name} is not UTF-8 text and was not presented to the text diagnosis model.")
        return None
    if "\x00" in content:
        limitations.append(f"{label}: binary content in {path.name} was not presented to the text diagnosis model.")
        return None
    return _sanitize(content, limitations, label)


def _json_lines(path: Path, *, limitations: list[str] | None = None) -> dict[int, dict[str, Any]]:
    records = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = _object(_loads(line), f"{path.name}:{number}")
        except ValueError as exc:
            if limitations is None:
                raise ValueError(f"Invalid normalized event at {path.name}:{number}: {exc}") from exc
            limitations.append(f"Canonical trajectory line {number} is malformed; the trajectory is partial.")
            continue
        records[number] = record
    return records


def build_discovery_input(run_dir: Path, *, record_path: Path | None = None) -> DiscoveryInput:
    root = run_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("run_dir must be an executor run directory")
    if record_path is None:
        result_path = _inside(root, "result.json", "executor result")
    else:
        result_path = _inside(root, str(record_path), "verified checkpoint")
        if result_path != root / "backend" / "verified_record.json":
            raise ValueError("Live evidence must use this run's verified checkpoint")
    result = _object(_loads(result_path.read_text(encoding="utf-8")), "executor result")
    if result.get("schema_version") != 1 or result.get("run_id") != root.name:
        raise ValueError("Executor schema_version or run_id does not match this run directory")
    for key in ("benchmark", "task_id", "status", "started_at"):
        if not isinstance(result.get(key), str) or not result[key]:
            raise ValueError(f"Executor result is missing {key}")
    if record_path is None:
        if not result.get("finished_at"):
            raise ValueError("Executor run has not finished")
    elif (result.get("status") != "verified"
          or result.get("phase") != "post_verification_pre_cleanup"
          or not isinstance(result.get("verified_at"), str) or not result["verified_at"]):
        raise ValueError("Live checkpoint must identify completed verification before cleanup")
    prepared = _object(result.get("prepared"), "prepared task")
    execution = _object(result.get("execution"), "execution")
    feedback = _object(result.get("feedback"), "feedback")
    if prepared.get("benchmark") != result["benchmark"] or prepared.get("task_id") != result["task_id"]:
        raise ValueError("Prepared task identity does not match the executor result")

    task_dir = _inside(root, prepared.get("task_dir"), "prepared task", directory=True)
    if not task_dir.is_relative_to(root / "input" / "tasks"):
        raise ValueError("Prepared task must be an executor input/tasks snapshot")
    task_document = _inside(root, prepared.get("task_document"), "task document")
    if task_document != task_dir / "task.md":
        raise ValueError("Task document must be the snapshot task.md, not another artifact")
    manifest_path = _inside(root, prepared.get("manifest_path"), "input manifest")
    if manifest_path != root / "input" / "manifest.json":
        raise ValueError("Input manifest must be this executor run's input/manifest.json")
    manifest = _object(_loads(manifest_path.read_text(encoding="utf-8")), "input manifest")
    if manifest.get("schema_version") != 1 or manifest.get("task_id") != result["task_id"] or manifest.get("benchmark") != result["benchmark"]:
        raise ValueError("Input manifest identity does not match this executor run")
    snapshot = _object(manifest.get("prepared"), "manifest prepared snapshot")
    if _inside(root, snapshot.get("task_dir"), "manifest prepared task", directory=True) != task_dir:
        raise ValueError("Manifest and result refer to different prepared snapshots")
    hashes = _hash_snapshot(task_dir)
    if hashes != prepared.get("file_hashes") or hashes != snapshot.get("file_hashes"):
        raise ValueError("Prepared snapshot file hashes do not match the recorded manifest")
    digest = hash_manifest(hashes)
    if digest != prepared.get("task_digest") or digest != snapshot.get("task_digest"):
        raise ValueError("Task digest does not match the prepared snapshot")

    limitations: list[str] = []
    evidence: list[EvidenceItem] = []
    skill_manifest = _object(manifest.get("skills"), "manifest skills")
    skills_dir = None
    skill_digest = prepared.get("skill_digest")
    if prepared.get("skills_dir") is not None:
        skills_dir = _inside(root, prepared["skills_dir"], "prepared skills", directory=True)
        if skills_dir != task_dir / "environment" / "skills":
            raise ValueError("Prepared skills must be the snapshot environment/skills pack")
        if _inside(root, skill_manifest.get("prepared_dir"), "manifest skills", directory=True) != skills_dir:
            raise ValueError("Manifest and result refer to different skill packs")
        skill_hashes = {(task_dir / name).relative_to(skills_dir).as_posix(): value for name, value in hashes.items()
                        if (task_dir / name).is_relative_to(skills_dir)}
        if hash_manifest(skill_hashes) != skill_digest or skill_manifest.get("skill_digest") != skill_digest:
            raise ValueError("Skill digest does not match the prepared skill pack")
    else:
        if skill_digest is not None or skill_manifest.get("skill_digest") is not None:
            raise ValueError("A skill digest is present without a prepared skill pack")
        skill_hashes = {}
        limitations.append("No deployed task skill pack is recorded; skill applicability may be unassessable.")

    for relative in sorted(hashes):
        path = task_dir / relative
        if path.suffix.lower() in _BINARY_EXTENSIONS and PurePosixPath(relative).parts[0] == "environment":
            if skills_dir is None or not path.is_relative_to(skills_dir):
                limitations.append(
                    f"Task asset {relative} is non-text and is not directly presented; "
                    "only its recorded textual observations are available to this diagnosis."
                )

    canonical_path = _inside(root, execution.get("trajectory_path"), "canonical trajectory")
    if not canonical_path.is_relative_to(root / "jobs") or canonical_path.parent.name != "trajectory":
        raise ValueError("Canonical trajectory must belong to this run's jobs trial artifacts")
    events_path = _inside(root, execution.get("normalized_trajectory_path"), "normalized trajectory")
    if events_path != root / "backend" / "events.jsonl":
        raise ValueError("Normalized trajectory must be this executor run's backend/events.jsonl")
    if execution.get("result_path") is not None:
        backend_result = _inside(root, execution["result_path"], "backend result")
        if backend_result != canonical_path.parent.parent / "result.json":
            raise ValueError("Backend result does not belong to this trajectory's trial")
    if execution.get("verifier_dir") is not None:
        verifier_outputs = _inside(root, execution["verifier_dir"], "verifier outputs", directory=True)
        if verifier_outputs != canonical_path.parent.parent / "verifier":
            raise ValueError("Verifier outputs do not belong to this trajectory's trial")
    before_malformed = len(limitations)
    canonical = _json_lines(canonical_path, limitations=limitations)
    malformed = len(limitations) != before_malformed
    normalized = list(_json_lines(events_path).values())
    event_count = execution.get("trajectory_events")
    if type(event_count) is not int or event_count <= 0 or len(normalized) != event_count or len(canonical) != event_count:
        raise ValueError("Trajectory event count does not match execution.trajectory_events")
    summary = _object(execution.get("summary", {}), "backend summary")
    trajectory_summary = _object(summary.get("trajectory_summary", {}), "backend trajectory summary")
    if "steps" in trajectory_summary and trajectory_summary["steps"] != event_count:
        raise ValueError("Backend trajectory step count does not match the artifact events")
    ids: set[str] = set()
    task_number = 0
    trajectory_items = []
    for index, (record, canonical_line) in enumerate(zip(normalized, canonical), 1):
        event_id = record.get("event_id")
        if not isinstance(event_id, str) or event_id in ids or event_id != f"T{index:06d}":
            raise ValueError("Trajectory event IDs must be unique and retain canonical sequential IDs")
        ids.add(event_id)
        source = _object(record.get("source"), f"{event_id} source")
        if _inside(root, source.get("path"), f"{event_id} source") != canonical_path:
            raise ValueError(f"{event_id} source is not this run's canonical trajectory")
        if type(source.get("line")) is not int or source["line"] != canonical_line:
            raise ValueError(f"{event_id} source line does not identify its canonical event")
        event = _object(record.get("event"), f"{event_id} event")
        if event != canonical[canonical_line]:
            raise ValueError(f"{event_id} normalized payload differs from the canonical event")
        text = _field_dump(_sanitize(event, limitations, event_id))
        trajectory_items.append(EvidenceItem(event_id, "trajectory", text, str(canonical_path), canonical_line))
        if event.get("type") == "user_message":
            task_number += 1
            evidence.append(EvidenceItem(f"Q{task_number:04d}", "task", text, str(canonical_path), canonical_line))
    if not task_number:
        limitations.append("No actual user_message was found; the task snapshot may not fully describe the execution prompt.")
    task_number += 1
    task_text = _text_file(task_document, limitations, f"Q{task_number:04d}")
    if task_text is None:
        raise ValueError("The task document must be readable UTF-8 text")
    evidence.append(EvidenceItem(
        f"Q{task_number:04d}", "task",
        "Task snapshot metadata and full task document (not an additional user message):\n" + task_text,
        str(task_document), 1, version=hashes[task_document.relative_to(task_dir).as_posix()],
    ))

    if skills_dir is not None:
        skill_number = 0
        for relative, version in sorted(skill_hashes.items()):
            path = skills_dir / relative
            label = f"Skill file {relative}"
            content = _text_file(path, limitations, label)
            if content is None:
                continue
            skill_number += 1
            parts = PurePosixPath(relative).parts
            package_id = f"{parts[0]}/SKILL.md" if len(parts) > 1 and f"{parts[0]}/SKILL.md" in skill_hashes else None
            evidence.append(EvidenceItem(
                f"S{skill_number:04d}", "skill", content, str(path), 1,
                skill_id=package_id, relative_path=relative, version=version,
            ))
            if package_id is None:
                limitations.append(f"S{skill_number:04d} is shared skill-root material without a unique package owner; relative_path is relative to the skills root.")
    evidence.extend(trajectory_items)

    raw_paths = _object(feedback.get("raw_paths", {}), "feedback raw_paths")
    verified_raw = {}
    for name, value in raw_paths.items():
        verified_raw[name] = _inside(root, value, f"feedback {name}", directory=name == "verifier_dir")
    feedback_text = {key: value for key, value in feedback.items() if key != "raw_paths"}
    evidence.append(EvidenceItem(
        "F0001", "feedback", _field_dump(_sanitize(feedback_text, limitations, "F0001")),
        str(result_path), version=sha256_file(result_path),
    ))
    feedback_number = 1
    for name in _RAW_FEEDBACK:
        path = verified_raw.get(name)
        if path is None:
            continue
        if path.name != name:
            raise ValueError(f"Feedback {name} does not refer to the named verifier output")
        verifier_dir = execution.get("verifier_dir")
        if verifier_dir is None or path.parent != _inside(root, verifier_dir, "verifier outputs", directory=True):
            raise ValueError(f"Feedback {name} is not in this execution's verifier output directory")
        feedback_number += 1
        evidence_id = f"F{feedback_number:04d}"
        text = _text_file(path, limitations, evidence_id)
        if text is None:
            continue
        if name == "ctrf.json":
            text = _field_dump(_sanitize(_loads(text), limitations, evidence_id))
        evidence.append(EvidenceItem(evidence_id, "feedback", text, str(path), 1, version=sha256_file(path)))

    complete = execution.get("trajectory_complete")
    if complete is not None and type(complete) is not bool:
        raise ValueError("trajectory_complete must be a boolean or null")
    if malformed or summary.get("partial_trajectory") is True or trajectory_summary.get("partial_trajectory") is True:
        complete = False
    if complete is False:
        limitations.append("Trajectory is partial; absence of an observed step does not establish that it never occurred.")
    elif complete is None:
        limitations.append("Trajectory completeness is unknown; missing-step claims require caution.")
    if result["status"] not in {"completed", "verified"} or execution.get("status") != "completed":
        limitations.append(f"Run/execution status is {result['status']}/{execution.get('status')}; this alone is not a skill conflict.")
    if result.get("errors") or execution.get("errors") or summary.get("error"):
        limitations.append("Execution reports errors; infrastructure or execution failures must not be relabeled as skill conflicts.")
    if feedback.get("status") != "scored" or feedback.get("issues") or summary.get("verifier_error"):
        limitations.append(f"Verifier feedback status is {feedback.get('status')} and may be incomplete or erroneous; retain uncertainty.")
    if not isinstance(feedback.get("metrics"), dict) or "reward" not in feedback["metrics"]:
        limitations.append("No verifier reward is recorded; do not infer reward zero or task failure from its absence.")
    usage = summary.get("usage_tracking") or {}
    agent_result = summary.get("agent_result") or {}
    if not isinstance(usage, dict) or usage.get("status") in {None, "off", "unavailable"} or (
        isinstance(agent_result, dict) and agent_result.get("usage_source") in {"unavailable", "unknown"}
    ):
        limitations.append("Model token/cost telemetry is unavailable or disabled; zero counters do not establish zero model usage.")
    if not any(record["event"].get("model") or record["event"].get("llm") for record in normalized):
        limitations.append("No per-call model metadata is present in the supplied trajectory; configured model identity is not independently verified here.")
    limitations = list(dict.fromkeys(limitations))
    discovery = DiscoveryInput(
        run_id=root.name, benchmark=result["benchmark"], task_id=result["task_id"],
        task_digest=digest, skill_digest=skill_digest, trajectory_complete=complete,
        evidence=evidence, limitations=limitations, input_digest="",
    )
    refresh_input_digest(discovery)
    return discovery
