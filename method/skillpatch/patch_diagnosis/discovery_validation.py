from method_input_adapters.skillpatch.models import DiagnosisContext, ContextItem
"""Validate diagnosis shape and evidence references, not semantic correctness."""

import math
import re
from dataclasses import asdict, fields
from typing import Any
from types import SimpleNamespace
from .tool.evidence_references import resolve_reference

from .models import (
    Conflict,
    SkillImprovement,
    ImprovementReport,
    DiscoveryReport,
    EvidenceRef,
    SkillTarget,
)
from .tool.output_language import validate_output_language


def _json_value(value: Any, depth: int = 0) -> None:
    """Reject non-JSON values and non-finite numbers without printing their data."""
    if depth > 100:
        raise ValueError("payload: nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload: non-finite numbers are not permitted")
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("payload: object keys must be strings")
        for item in value.values():
            _json_value(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
        return
    raise ValueError("payload: only JSON-compatible values are permitted")


def _object(value: Any, expected: set[str], path: str, *,
            optional: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected an object")
    if not expected <= set(value) or set(value) - expected - optional:
        # Field names supplied by the model may themselves contain sensitive data.
        missing = sorted(expected - set(value))
        detail = "; missing " + ", ".join(missing) if missing else ""
        if set(value) - expected - optional:
            detail += "; unsupported fields present"
        raise ValueError(f"{path}: fields do not match the schema{detail}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: expected a non-empty string")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path}: expected a list")
    return value


def _strings(value: Any, path: str) -> list[str]:
    return [_text(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))]


def _choice(value: Any, choices: set[str], path: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{path}: expected one of {', '.join(sorted(choices))}")
    return value


def _references(value: Any, evidence: dict[str, ContextItem], allowed_kinds: set[str],
                path: str, *, required: bool = False) -> list[EvidenceRef]:
    entries = _list(value, path)
    references = []
    for index, entry in enumerate(entries):
        location = f"{path}[{index}]"
        if isinstance(entry, dict) and "quote" not in entry:
            entry = resolve_reference({"collection": "source", **entry},
                                      SimpleNamespace(items=list(evidence.values())), "source")
        entry = _object(entry, {"evidence_id", "quote"}, location)
        identifier = _text(entry["evidence_id"], location + ".evidence_id")
        quote = _text(entry["quote"], location + ".quote")
        item = evidence.get(identifier)
        if item is None:
            raise ValueError(f"{location}.evidence_id: reference is absent from this input")
        if quote not in item.text:
            raise ValueError(f"{location}.quote: not an exact substring of the referenced evidence")
        references.append(EvidenceRef(evidence_id=identifier, quote=quote))
    return references


def _target(value: Any, evidence: dict[str, ContextItem], path: str, *, legacy: bool) -> SkillTarget:
    if isinstance(value, dict) and "quote" not in value:
        allowed = {"scope", "skill_ref", "start_char", "end_char"}
        if not set(value) <= allowed:
            unexpected = sorted(set(value) - allowed)
            raise ValueError(
                f"{path}: invalid target selector fields; unexpected={unexpected}. "
                'Use {"scope":"substep","skill_ref":"<existing Skill ID>",'
                '"start_char":0,"end_char":100} with both offsets for the selected displayed passage, '
                'or {"scope":"substep","skill_ref":"<existing Skill ID>","quote":"<exact displayed passage>"}. '
                "These are schema examples, not evidence or target choices. "
                "Remove unsupported fields: collection and evidence_id belong to evidence tools/citations, "
                "not this Skill target. Keep the chosen skill_ref and supported passage; "
                "do not change the source, path or version binding.")
        ref = resolve_reference({"collection": "source", "evidence_id": value.get("skill_ref"),
                                 **{k: value[k] for k in ("start_char", "end_char") if k in value}},
                                SimpleNamespace(items=list(evidence.values())), "source")
        value = {"scope": value.get("scope"), "skill_ref": value.get("skill_ref"), "quote": ref["quote"]}
    keys = {"scope", "skill_ref", "quote"}
    value = _object(value, keys | {"skill_id"} if legacy else keys, path)
    scope = _choice(value["scope"], {"whole_skill", "substep"}, path + ".scope")
    skill_ref = _text(value["skill_ref"], path + ".skill_ref")
    quote = _text(value["quote"], path + ".quote")
    item = evidence.get(skill_ref)
    if item is None or item.kind != "skill":
        raise ValueError(f"{path}.skill_ref: expected a Skill evidence item from this input")
    skill_id = _text(item.skill_id, path + ".bound_skill_id")
    if legacy and value["skill_id"] != skill_id:
        raise ValueError(f"{path}.skill_id: does not match the referenced Skill")
    start = item.text.find(quote)
    if start < 0:
        raise ValueError(f"{path}.quote: not an exact substring of the referenced Skill")
    if item.text.find(quote, start + 1) >= 0:
        raise ValueError(f"{path}.quote: target is ambiguous within the referenced Skill")
    return SkillTarget(
        scope=scope, skill_id=skill_id, skill_ref=skill_ref, quote=quote,
        relative_path=_text(item.relative_path, path + ".bound_relative_path"),
        version=_text(item.version, path + ".bound_version"),
        source_path=_text(item.source_path, path + ".bound_source_path"),
    )


def _trajectory_span(value: Any, positions: dict[str, int], behavior: list[EvidenceRef],
                     path: str) -> dict[str, str]:
    value = _object(value, {"start", "end"}, path)
    if value == {"start": "", "end": ""} and not any(ref.evidence_id in positions for ref in behavior):
        return value
    start = _text(value["start"], path + ".start")
    end = _text(value["end"], path + ".end")
    if start not in positions or end not in positions:
        raise ValueError(f"{path}: endpoints must reference trajectory evidence in this input")
    if positions[start] > positions[end]:
        raise ValueError(f"{path}: start follows end")
    if any(not positions[start] <= positions[ref.evidence_id] <= positions[end]
           for ref in behavior if ref.evidence_id in positions):
        raise ValueError(f"{path}: must contain every trajectory behavior evidence reference")
    return {"start": start, "end": end}


LEGACY_DESCRIPTIONS = {
    "applicability_reason": "适用性",
    "observed_behavior": "观察行为",
    "expected_behavior": "预期行为",
    "feedback_assessment": "反馈关系",
}
LEGACY_CONFLICT_FIELDS = {
    "conflict_id", "type", "target", *LEGACY_DESCRIPTIONS,
    "decision_evidence", "behavior_evidence", "feedback_evidence", "trajectory_range", "uncertainty",
}
REPORT_CONTENT_FIELDS = {"status", "summary", "conflicts", "other_findings", "coverage_notes"}


def validate_report(payload: dict, context: DiagnosisContext, *, require_english: bool = False) -> DiscoveryReport | ImprovementReport:
    """Validate compact or legacy model output and bind it to a v2 candidate report."""
    _json_value(payload)
    if isinstance(payload, dict) and "improvements" in payload:
        return validate_improvements(payload, context)
    payload = _object(payload, {"status", "summary", "conflicts"}, "payload",
                      optional={"other_findings", "coverage_notes"})
    _text(context.run_id, "context.run_id")
    _text(context.input_digest, "context.input_digest")
    evidence: dict[str, ContextItem] = {}
    positions: dict[str, int] = {}
    last_trajectory_number = 0
    for index, item in enumerate(context.items):
        identifier = _text(item.context_id, f"context.items[{index}].evidence_id")
        if identifier in evidence:
            raise ValueError("context.items: evidence identifiers must be unique")
        if not isinstance(item.text, str):
            raise ValueError(f"context.items[{index}].text: expected a string")
        evidence[identifier] = item
        if item.kind == "trajectory":
            if not re.fullmatch(r"T[0-9]{6,}", identifier):
                raise ValueError("context.items: trajectory identifiers must use the T000001 format")
            number = int(identifier[1:])
            if number <= last_trajectory_number:
                raise ValueError("context.items: trajectory identifiers must be in increasing order")
            positions[identifier] = len(positions)
            last_trajectory_number = number
        elif item.kind == "artifact":
            if not re.fullmatch(r"A[0-9]{6,}", identifier):
                raise ValueError("context.items: artifact identifiers must use the A000001 format")
            if item.availability is not None:
                _choice(item.availability, {"initial", "post_execution", "unknown"},
                        f"context.items[{index}].availability")

    status = _choice(payload["status"], {"conflicts_found", "no_supported_conflict", "insufficient_evidence"}, "status")
    summary = _text(payload["summary"], "summary")
    coverage_notes = _strings(payload.get("coverage_notes", []), "coverage_notes")

    conflicts: list[Conflict] = []
    conflict_ids: set[str] = set()
    for index, raw in enumerate(_list(payload["conflicts"], "conflicts")):
        path = f"conflicts[{index}]"
        legacy = isinstance(raw, dict) and "diagnosis" not in raw
        if legacy:
            raw = _object(raw, LEGACY_CONFLICT_FIELDS, path)
            conflict_id = _text(raw["conflict_id"], path + ".conflict_id")
            labels = ({key: key.replace("_", " ").capitalize() for key in LEGACY_DESCRIPTIONS}
                      if require_english else LEGACY_DESCRIPTIONS)
            separator = ": " if require_english else "："
            diagnosis = "\n\n".join(f"{label}{separator}{_text(raw[key], path + '.' + key)}"
                                      for key, label in labels.items())
        else:
            raw = _object(raw, {"type", "target", "diagnosis", "decision_evidence", "behavior_evidence"},
                          path, optional={"feedback_evidence", "uncertainty"})
            conflict_id = f"C{index + 1:06d}"
            diagnosis = _text(raw["diagnosis"], path + ".diagnosis")
        if conflict_id in conflict_ids:
            raise ValueError(f"{path}.conflict_id: duplicate conflict identifier")
        conflict_ids.add(conflict_id)
        conflict_type = _choice(raw["type"], {"under_use", "misuse"}, path + ".type")
        target = _target(raw["target"], evidence, path + ".target", legacy=legacy)
        decision = _references(raw["decision_evidence"], evidence, {"task", "skill", "trajectory", "artifact"},
                               path + ".decision_evidence", required=True)
        behavior = _references(raw["behavior_evidence"], evidence, {"trajectory", "artifact"},
                               path + ".behavior_evidence", required=True)
        feedback = _references(raw.get("feedback_evidence", []), evidence, {"feedback"}, path + ".feedback_evidence")
        if legacy:
            span = _trajectory_span(raw["trajectory_range"], positions, behavior, path + ".trajectory_range")
        else:
            # This bounds cited events only; it does not certify examination of the intervening trajectory.
            cited = {ref.evidence_id for ref in decision + behavior if ref.evidence_id in positions}
            span = ({"start": min(cited, key=positions.__getitem__), "end": max(cited, key=positions.__getitem__)}
                    if cited else {"start": "", "end": ""})
        conflicts.append(Conflict(
            conflict_id=conflict_id, type=conflict_type, target=target,
            diagnosis=diagnosis,
            decision_evidence=decision, behavior_evidence=behavior, feedback_evidence=feedback,
            trajectory_range=span,
            uncertainty=_strings(raw.get("uncertainty", []), path + ".uncertainty"),
        ))

    if (status == "conflicts_found") != bool(conflicts):
        raise ValueError("conflicts: conflicts_found requires a non-empty list; other statuses require an empty list")

    other_findings = []
    for index, raw in enumerate(_list(payload.get("other_findings", []), "other_findings")):
        path = f"other_findings[{index}]"
        raw = _object(raw, {"category", "description", "evidence"}, path)
        category = _choice(raw["category"], {"implementation_error", "skill_content_issue", "evaluator_issue", "environment_issue", "uncertain"},
                           path + ".category")
        refs = _references(raw["evidence"], evidence, {"task", "skill", "trajectory", "feedback", "artifact"},
                           path + ".evidence", required=True)
        other_findings.append({"category": category,
                               "description": _text(raw["description"], path + ".description"),
                               "evidence": [{"evidence_id": ref.evidence_id, "quote": ref.quote} for ref in refs]})

    return DiscoveryReport(run_id=context.run_id, input_digest=context.input_digest,
                           status=status, summary=summary, conflicts=conflicts,
                           other_findings=other_findings, coverage_notes=coverage_notes,
                           semantic_status="candidate_diagnosis")


def validate_saved_report(value: dict, context: DiagnosisContext) -> DiscoveryReport | ImprovementReport:
    """Read v1/v2 archives, preserving old IDs and checking program-bound metadata again."""
    _json_value(value)
    if isinstance(value, dict) and value.get("schema_version") == 3:
        return validate_saved_improvements(value, context)
    value = _object(value, {field.name for field in fields(DiscoveryReport)}, "report")
    version = value["schema_version"]
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("Unsupported Stage 1 report schema_version")
    if value["run_id"] != context.run_id or value["input_digest"] != context.input_digest:
        raise ValueError("Stage 1 report is bound to another input")
    if value["semantic_status"] != "candidate_diagnosis":
        raise ValueError("Stage 1 semantic_status must remain candidate_diagnosis")
    payload = {key: value[key] for key in REPORT_CONTENT_FIELDS}
    payload["conflicts"] = []
    bindings = []
    for index, entry in enumerate(_list(value["conflicts"], "report.conflicts")):
        path = f"report.conflicts[{index}]"
        expected = LEGACY_CONFLICT_FIELDS if version == 1 else {field.name for field in fields(Conflict)}
        entry = _object(entry, expected, path)
        target = _object(entry["target"], {field.name for field in fields(SkillTarget)}, path + ".target")
        bindings.append(target)
        raw = dict(entry)
        keys = {"scope", "skill_ref", "quote"}
        if version == 1:
            keys.add("skill_id")
        else:
            raw.pop("conflict_id")
            raw.pop("trajectory_range")
        raw["target"] = {key: target[key] for key in keys}
        payload["conflicts"].append(raw)
    report = validate_report(payload, context)
    if any(target != asdict(conflict.target) for target, conflict in zip(bindings, report.conflicts)):
        raise ValueError("Stage 1 target metadata differs from its evidence-bound location or version")
    if version == 2:
        # IDs are stable archive identities, including reports converted from v1. Never renumber them.
        positions = {item.context_id: index for index, item in enumerate(context.items)
                     if item.kind == "trajectory"}
        identifiers = set()
        for index, (entry, conflict) in enumerate(zip(value["conflicts"], report.conflicts)):
            path = f"report.conflicts[{index}]"
            identifier = _text(entry["conflict_id"], path + ".conflict_id")
            if identifier in identifiers:
                raise ValueError(f"{path}.conflict_id: duplicate conflict identifier")
            identifiers.add(identifier)
            conflict.conflict_id = identifier
            conflict.trajectory_range = _trajectory_span(entry["trajectory_range"], positions,
                                                          conflict.behavior_evidence, path + ".trajectory_range")
    return report


def validate_improvements(payload: dict, context: DiagnosisContext) -> ImprovementReport:
    """Check basic structure, unique attachment anchors and source references only."""
    _json_value(payload)
    payload = _object(payload, {"summary", "improvements"}, "payload")
    _text(context.run_id, "context.run_id")
    _text(context.input_digest, "context.input_digest")
    evidence = {item.context_id: item for item in context.items}
    if len(evidence) != len(context.items):
        raise ValueError("context.items: evidence identifiers must be unique")
    improvements = []
    for index, raw in enumerate(_list(payload["improvements"], "improvements")):
        path = f"improvements[{index}]"
        raw = _object(raw, {"target", "diagnosis", "suggestion", "evidence"}, path)
        target = _object(raw["target"], {"skill_ref"}, path + ".target",
                         optional={"quote", "start_char", "end_char", "scope"})
        selectors = set(target) - {"scope", "skill_ref"}
        if selectors not in ({"quote"}, {"start_char", "end_char"}):
            raise ValueError(f"{path}.target: provide quote or both start_char/end_char")
        target = _target({"scope": "substep", **target}, evidence, path + ".target", legacy=False)
        refs = _references(raw["evidence"], evidence, set(), path + ".evidence")
        if not refs:
            raise ValueError(f"{path}.evidence: provide at least one source reference")
        improvements.append(SkillImprovement(
            improvement_id=f"I{index + 1:06d}", target=target,
            diagnosis=_text(raw["diagnosis"], path + ".diagnosis"),
            suggestion=_text(raw["suggestion"], path + ".suggestion"), evidence=refs))
    return ImprovementReport(context.run_id, context.input_digest,
                             _text(payload["summary"], "summary"), improvements)


def validate_saved_improvements(value: dict, context: DiagnosisContext) -> ImprovementReport:
    value = _object(value, {field.name for field in fields(ImprovementReport)}, "report")
    if type(value["schema_version"]) is not int or value["schema_version"] != 3:
        raise ValueError("Unsupported Stage 1 report schema_version")
    if value["run_id"] != context.run_id or value["input_digest"] != context.input_digest:
        raise ValueError("Stage 1 report is bound to another input")
    if value["semantic_status"] != "candidate_diagnosis":
        raise ValueError("Stage 1 semantic_status must remain candidate_diagnosis")
    payload = {"summary": value["summary"], "improvements": []}
    originals = _list(value["improvements"], "report.improvements")
    identifiers = set()
    for index, entry in enumerate(originals):
        path = f"report.improvements[{index}]"
        entry = _object(entry, {field.name for field in fields(SkillImprovement)}, path)
        identifier = _text(entry["improvement_id"], path + ".improvement_id")
        if identifier in identifiers:
            raise ValueError("Duplicate improvement identifier")
        identifiers.add(identifier)
        target = _object(entry["target"], {field.name for field in fields(SkillTarget)}, path + ".target")
        payload["improvements"].append({
            "target": {key: target[key] for key in ("scope", "skill_ref", "quote")},
            **{key: entry[key] for key in ("diagnosis", "suggestion", "evidence")}})
    report = validate_improvements(payload, context)
    for original, improvement in zip(originals, report.improvements):
        if original["target"] != asdict(improvement.target):
            raise ValueError("Stage 1 target metadata differs from its evidence-bound location or version")
        improvement.improvement_id = original["improvement_id"]
    return report
