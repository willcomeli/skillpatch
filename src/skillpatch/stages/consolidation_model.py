"""Propose local patch consolidation without changing or activating a Skill."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Mapping

from skillpatch.context import refresh_input_digest
from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.model_client import DiagnosisConfig
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.output_language import validate_output_language
from skillpatch.patch_models import PatchSource
from skillpatch.patches import tree_hashes
from skillpatch.stages.induction import require_displayed_references
from skillpatch.storage import hash_manifest, jsonable, write_json


def validate_proposal(payload: dict) -> dict:
    """Validate the small model contract, not its semantic correctness."""
    if not isinstance(payload, dict) or set(payload) != {
            "action", "reason", "when_to_use", "how_to_interpret"}:
        raise ValueError("Consolidation fields do not match the schema")
    if not isinstance(payload["action"], str) or payload["action"] not in {"keep", "merge", "defer"}:
        raise ValueError("Invalid consolidation action")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Consolidation reason is required")
    for name in ("when_to_use", "how_to_interpret"):
        value = payload[name]
        if payload["action"] != "merge":
            if value is not None:
                raise ValueError("Only a merge proposal may contain replacement patch text")
        elif (not isinstance(value, str) or not value.strip() or len(value) > 16000
              or "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value or "\x00" in value):
            raise ValueError("Merged patch sections must be nonempty bounded text without reserved markers")
    return payload


def _entry_target(entry: dict) -> tuple[str, str, str]:
    if not isinstance(entry, dict) or not isinstance(entry.get("target"), dict):
        raise ValueError("Consolidation entries must have a registered target")
    binding = tuple(entry["target"].get(name) for name in ("target_id", "skill_key", "base_digest"))
    if any(not isinstance(value, str) or not value.strip() for value in binding):
        raise ValueError("Consolidation entries need a stable target_id, skill_key and base_digest")
    for name in ("when_to_use", "how_to_interpret"):
        if not isinstance(entry.get(name), str) or not entry[name].strip():
            raise ValueError("Consolidation entry has no patch body")
    return binding


def original_skill_context_items(context, target):
    """Read the target and the main declaration identified by source metadata.

    The deployment may contain earlier, marked supplements. Do not infer common
    Skill identity from its task-local filename or hash; registered bindings own
    that identity, while each archive retains its own provenance.
    """
    return [item for item in context.evidence if item.kind == "skill" and (
        item.relative_path == target.relative_path or (
            item.skill_id == target.skill_id
            and PurePosixPath(item.relative_path or "").name == "SKILL.md"))]


def _source_bundle(source: PatchSource, sources: list[PatchSource] | None):
    """Namespace archives in one read-only collection; never concatenate run tails."""
    selected = []
    keys = set()
    for item in [source, *(sources or [])]:
        key = (item.context.run_id, item.context.input_digest, item.conflict.conflict_id)
        if key not in keys:
            selected.append(item)
            keys.add(key)
    evidence, records, limitations = [], [], []
    for index, item in enumerate(selected):
        prefix = f"R{index}__"
        identifiers = {e.evidence_id: prefix + e.evidence_id for e in item.context.evidence}
        if len(identifiers) != len(item.context.evidence):
            raise ValueError("Support archive contains duplicate evidence identifiers")
        evidence.extend(replace(e, evidence_id=identifiers[e.evidence_id],
                                observation={**copy.deepcopy(e.observation),
                                  "consolidation_source": {"index": index,
                                      "run_id": item.context.run_id,
                                      "input_digest": item.context.input_digest,
                                      "evidence_id": e.evidence_id}})
                        for e in item.context.evidence)

        def remap(value):
            if isinstance(value, list):
                return [remap(part) for part in value]
            if isinstance(value, dict):
                return {key: identifiers.get(part, part)
                        if key in {"evidence_id", "skill_ref", "start", "end"} and isinstance(part, str)
                        else remap(part) for key, part in value.items()}
            return value

        records.append({"index": index, "prefix": prefix, "run_id": item.context.run_id,
                        "benchmark": item.context.benchmark, "task_id": item.context.task_id,
                        "original_input_digest": item.context.input_digest,
                        "trajectory_complete": item.context.trajectory_complete,
                        "evidence_ids": identifiers, "conflict": remap(jsonable(item.conflict))})
        limitations.extend(f"{prefix}{note}" for note in item.context.limitations)
    identity = hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    combined = DiscoveryInput("stage3-supports-" + identity[:16], "support-archives", "same-target",
        identity, source.context.skill_digest,
        all(item.context.trajectory_complete is True for item in selected), evidence, limitations, "")
    refresh_input_digest(combined)
    return combined, records


def _require_support_coverage(view, records):
    """Each source's original Skill and relevant behavior must actually be shown."""
    for record in records:
        prefix = record["prefix"]
        target = record["conflict"]["target"]
        label = "source:" + prefix
        require_displayed_references([{"evidence_id": target["skill_ref"], "quote": target["quote"]}],
                                     view, label)
        original_skill = [item for item in view.context.evidence
                          if item.evidence_id.startswith(prefix) and item.kind == "skill"
                          and (item.relative_path == target["relative_path"] or (
                              item.skill_id == target["skill_id"]
                              and PurePosixPath(item.relative_path or "").name == "SKILL.md"))]
        if not original_skill or any(not view.fully_presented(item) for item in original_skill):
            raise ValueError(f"{label}: read the complete original Skill and target context before consolidation")
        if record["trajectory_complete"] is not True:
            raise ValueError(f"{label}: incomplete execution requires deferring consolidation")
        refs = record["conflict"]["behavior_evidence"]
        require_displayed_references(refs, view, label)
        trajectory = [item for item in view.context.evidence
                      if item.evidence_id.startswith(prefix) and item.kind == "trajectory"]
        cited = {ref["evidence_id"] for ref in refs}
        if not any(item.evidence_id in cited for item in trajectory):
            raise ValueError(f"{label}: consolidation requires source trajectory evidence")


def _add_original_snapshot(context, binding, original_skill_dir):
    if original_skill_dir is None:
        return None
    hashes = tree_hashes(original_skill_dir)
    if hash_manifest(hashes) != binding["base_digest"]:
        raise ValueError("Canonical original Skill does not match the registered base_digest")
    relative = binding.get("relative_path")
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or PurePosixPath(relative).as_posix() != relative):
        raise ValueError("Canonical target must be a relative original Skill path")
    records = []
    for index, path in enumerate(dict.fromkeys(["SKILL.md", relative])):
        if path not in hashes:
            raise ValueError("Canonical original Skill is missing its declaration or target file")
        original_file = original_skill_dir / path
        identifier = f"ORIGINAL__S{index + 1:06d}"
        context.evidence.append(EvidenceItem(identifier, "skill", original_file.read_bytes().decode("utf-8"),
            str(original_file), skill_id=binding["skill_key"], relative_path=path, version=hashes[path],
            availability="initial", source_role="canonical_original_skill",
            observation={"origin": "immutable_original_snapshot", "base_digest": binding["base_digest"]}))
        records.append({"evidence_id": identifier, "relative_path": path, "sha256": hashes[path]})
    if tree_hashes(original_skill_dir) != hashes:
        raise ValueError("Canonical original Skill changed while being read")
    refresh_input_digest(context)
    return {"skill_key": binding["skill_key"], "base_digest": binding["base_digest"], "files": records}


def propose_consolidation(source: PatchSource, existing: list[dict], incoming: dict,
                          config: DiagnosisConfig, output: Path, environment: Mapping[str, str], *,
                          sources: list[PatchSource] | None = None, client=None,
                          session_timeout_sec: int = 900, original_skill_dir: Path | None = None) -> dict:
    """Keep every entry, merge all entries for this object, or defer; never activate."""
    binding = _entry_target(incoming)
    if not isinstance(existing, list) or not existing:
        raise ValueError("A semantic consolidation proposal requires existing patches")
    if any(_entry_target(entry) != binding for entry in existing):
        raise ValueError("Only patches for the same target_id, skill_key and base_digest may be consolidated")
    context, records = _source_bundle(source, sources)
    original = _add_original_snapshot(context, incoming["target"], original_skill_dir)
    data = "CONSOLIDATION INPUT\n" + json.dumps(jsonable({
        "target_id": binding[0], "existing": existing, "incoming": incoming,
        "support_archives": records, "canonical_original": original}), ensure_ascii=False)
    checked = {}

    def validate(payload, views):
        validate_proposal(payload)
        validate_output_language(payload, "consolidation")
        if payload["action"] in {"keep", "merge"}:
            _require_support_coverage(views["source"], records)
            if original is not None:
                ids = {item["evidence_id"] for item in original["files"]}
                if any(not views["source"].fully_presented(item) for item in views["source"].context.evidence
                       if item.evidence_id in ids):
                    raise ValueError("source: read the complete canonical original Skill and target before consolidation")
        checked["source_digest"] = views["source"].context.input_digest

    priority = [item["evidence_id"] for item in original["files"]] if original is not None else []
    priority.extend(record["conflict"]["target"]["skill_ref"] for record in records)
    payload = recorded_evidence_session(config, data, {"source": context}, output, environment,
        client=client, validate=validate, session_timeout_sec=session_timeout_sec,
        priority_ids={"source": priority},
        budget_fallback=lambda reason: {"action": "defer", "reason": "Evidence inspection is incomplete: " + reason,
                                        "when_to_use": None, "how_to_interpret": None})
    write_json(output / "source_map.json", records)
    write_json(output / "original_skill.json", original)
    write_json(output / "proposal.json", {"proposal": payload,
        "semantic_correctness": "not_verified", "ready_for_activation": False,
        "source_digest": checked["source_digest"], "original_bundle_digest": context.input_digest})
    return payload
