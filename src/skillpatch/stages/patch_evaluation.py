"""Evaluate the original conflict independently of patch induction."""

import json
import copy
from pathlib import Path
from typing import Mapping

from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.evidence_views import EvidenceViews
from skillpatch.evidence_references import resolve_reference
from skillpatch.model_client import DiagnosisConfig
from skillpatch.models import DiscoveryInput
from skillpatch.output_language import validate_output_language
from skillpatch.patch_models import PatchSource, TargetEvaluation
from skillpatch.stages.induction import require_behavior_references, require_displayed_references
from skillpatch.storage import jsonable, write_json


def _refs(values, context: DiscoveryInput, *, behavior=False, collection="evaluation"):
    if not isinstance(values, list):
        raise ValueError(f"{collection}: evidence must be a list")
    evidence = {item.evidence_id: item for item in context.evidence}
    for ref in values:
        if not isinstance(ref, dict) or set(ref) != {"evidence_id", "quote"}:
            raise ValueError(f"{collection}: evidence has invalid fields")
        if not isinstance(ref["evidence_id"], str):
            raise ValueError(f"{collection}: evidence identifier must be a string")
        item = evidence.get(ref["evidence_id"])
        if item is None or not isinstance(ref["quote"], str) or not ref["quote"].strip() or ref["quote"] not in item.text:
            raise ValueError(f"{collection}: evidence {ref['evidence_id']} is not an exact quote from this run")
    if behavior and not any(evidence[ref["evidence_id"]].kind == "trajectory" for ref in values):
        raise ValueError(f"{collection}: a supported behavioral judgment needs actual trajectory evidence")
    return values


def validate_evaluation(payload: dict, baseline: DiscoveryInput, candidate: DiscoveryInput, *,
                        views: Mapping[str, EvidenceViews] | None = None,
                        require_english: bool = False, basic: bool = False) -> TargetEvaluation:
    required = {"baseline_status", "target_status", "explanation", "baseline_evidence", "candidate_evidence",
                "reverse_conflicts", "revision_action"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ValueError("Evaluation fields do not match the schema")
    if not isinstance(payload["baseline_status"], str) or payload["baseline_status"] not in {"present", "absent", "uncertain"}:
        raise ValueError("Invalid baseline conflict status")
    if not isinstance(payload["target_status"], str) or payload["target_status"] not in {"repaired", "not_repaired", "uncertain", "invalid_diagnosis", "unpatchable"}:
        raise ValueError("Invalid target repair status")
    if not isinstance(payload["revision_action"], str) or payload["revision_action"] not in {"revise_patch", "revisit_diagnosis", "stop", "none"}:
        raise ValueError("Invalid revision action")
    if not isinstance(payload["explanation"], str) or not payload["explanation"].strip():
        raise ValueError("Evaluation explanation is required")
    payload = copy.deepcopy(payload)
    def resolve(values, context, name):
        if not isinstance(values, list):
            raise ValueError(f"{name}: evidence must be a list")
        return [resolve_reference(ref, context, name, views[name] if views and not basic else None)
                for ref in values]
    payload["baseline_evidence"] = resolve(payload["baseline_evidence"], baseline, "baseline")
    payload["candidate_evidence"] = resolve(payload["candidate_evidence"], candidate, "candidate")
    _refs(payload["baseline_evidence"], baseline, behavior=not basic and payload["baseline_status"] != "uncertain",
          collection="baseline")
    _refs(payload["candidate_evidence"], candidate, behavior=not basic and payload["target_status"] in {"repaired", "not_repaired"},
          collection="candidate")
    reverse = payload["reverse_conflicts"]
    if not isinstance(reverse, list):
        raise ValueError("reverse_conflicts must be a list")
    for finding in reverse:
        if not isinstance(finding, dict) or set(finding) != {"type", "explanation", "evidence"}:
            raise ValueError("Reverse conflict fields do not match the schema")
        if not isinstance(finding["type"], str) or finding["type"] not in {"under_use", "misuse"} or not isinstance(finding["explanation"], str) or not finding["explanation"].strip():
            raise ValueError("Reverse conflict requires a type and explanation")
        finding["evidence"] = resolve(finding["evidence"], candidate, "candidate")
        refs = _refs(finding["evidence"], candidate, behavior=not basic, collection="candidate.reverse_conflicts")
        by_id = {e.evidence_id: e for e in candidate.evidence}
        if not basic and not any(by_id[r["evidence_id"]].kind == "skill" for r in refs):
            raise ValueError("Reverse conflict needs a Skill clause as well as behavior")
    if not basic and baseline.trajectory_complete is not True and payload["baseline_status"] != "uncertain":
        raise ValueError("baseline: incomplete execution requires baseline_status=uncertain")
    if not basic and candidate.trajectory_complete is not True and payload["target_status"] in {"repaired", "not_repaired"}:
        raise ValueError("candidate: incomplete execution requires target_status=uncertain")
    if views is not None and not basic:
        for name, refs in (("baseline", payload["baseline_evidence"]),
                           ("candidate", payload["candidate_evidence"])):
            require_displayed_references(refs, views[name], name)
        if payload["baseline_status"] != "uncertain":
            require_behavior_references(payload["baseline_evidence"], views["baseline"], "baseline")
        if payload["target_status"] in {"repaired", "not_repaired"}:
            require_behavior_references(payload["candidate_evidence"], views["candidate"], "candidate")
        for finding in reverse:
            require_displayed_references(finding["evidence"], views["candidate"], "candidate")
            require_behavior_references(finding["evidence"], views["candidate"], "candidate")
    if require_english and not basic:
        validate_output_language(payload, "evaluation")
    return TargetEvaluation(**payload)


def evaluate_target(source: PatchSource, baseline: DiscoveryInput, candidate: DiscoveryInput,
                    config: DiagnosisConfig, output: Path, environment: Mapping[str, str], *, client=None,
                    session_timeout_sec: int = 900):
    data = "ORIGINAL TARGET AND CRITERIA\n" + json.dumps(jsonable(source.conflict), ensure_ascii=False)
    data += "\nEXECUTION ROLES\n" + json.dumps({
        "source_run_id": source.context.run_id, "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "baseline_is_source_execution": baseline.run_id == source.context.run_id,
        "baseline_uses_source_context": baseline.input_digest == source.context.input_digest,
    })
    contexts = {"source": source.context, "baseline": baseline, "candidate": candidate}
    priority = {name: [item.evidence_id for item in context.evidence
                      if item.kind == "skill" and item.relative_path == source.conflict.target.relative_path]
                for name, context in contexts.items()}
    checked = {}

    def validate(payload, views):
        checked["evaluation"] = validate_evaluation(payload, views["baseline"].context,
                                                    views["candidate"].context, views=views,
                                                    basic=True)
        checked["digests"] = {name: view.context.input_digest for name, view in views.items()}

    def fallback(reason):
        return {"baseline_status": "uncertain", "target_status": "uncertain",
                "explanation": "Repair of the original issue cannot yet be determined. Evidence inspection is incomplete: " + reason,
                "baseline_evidence": [], "candidate_evidence": [], "reverse_conflicts": [],
                "revision_action": "none"}

    recorded_evidence_session(config, data, contexts, output, environment, client=client,
                              priority_ids=priority, validate=validate, budget_fallback=fallback,
                              session_timeout_sec=session_timeout_sec)
    result = checked["evaluation"]
    write_json(output / "evaluation.json", {"evaluation": result,
                                           "validation_scope": "basic_schema_and_source_references",
                                           "semantic_correctness": "not_verified",
                                           "source_digest": checked["digests"]["source"],
                                           "baseline_digest": checked["digests"]["baseline"],
                                           "candidate_digest": checked["digests"]["candidate"],
                                           "original_input_digests": {name: context.input_digest
                                                                     for name, context in contexts.items()}})
    return result


def decide_validation(evaluations, comparisons, regression_comparisons, *, executions_ok=True,
                      regression_evaluations=()):
    """Program-owned acceptance. Model repair claims alone cannot accept a patch."""
    # Objective gain retains the patch even when the model disagrees or other
    # tests regress. All comparisons/evaluations remain in the validation record.
    improved = any(c.status == "improved" or any(
        change.get("direction") == "improved" for change in c.changes)
        for c in comparisons)
    if executions_ok and improved:
        return {"decision": "accepted", "reason": "objective_improvement", "ready_for_stage3": True}
    if not executions_ok or not evaluations or len(evaluations) != len(comparisons):
        return {"decision": "pending", "reason": "execution_or_evidence_unavailable", "ready_for_stage3": False}
    if any(c.status == "regressed" for c in [*comparisons, *regression_comparisons]):
        return {"decision": "rejected", "reason": "verifier_regression", "ready_for_stage3": False}
    if any(e.reverse_conflicts for e in [*evaluations, *regression_evaluations]):
        return {"decision": "rejected", "reason": "opposite_use_conflict", "ready_for_stage3": False}
    if any(e.target_status in {"invalid_diagnosis", "unpatchable"} for e in evaluations):
        return {"decision": "rejected", "reason": "diagnosis_or_patch_method_invalid", "ready_for_stage3": False}
    if any(e.target_status == "not_repaired" for e in evaluations):
        return {"decision": "rejected", "reason": "target_not_repaired", "ready_for_stage3": False}
    if any(e.target_status == "not_repaired" for e in regression_evaluations):
        return {"decision": "rejected", "reason": "related_behavior_regressed", "ready_for_stage3": False}
    if (any(e.baseline_status != "present" or e.target_status != "repaired" for e in evaluations)
            or any(c.status == "unavailable" for c in [*comparisons, *regression_comparisons])
            or any(e.target_status != "repaired" or e.baseline_status == "uncertain" for e in regression_evaluations)):
        return {"decision": "pending", "reason": "insufficient_repair_or_verifier_evidence", "ready_for_stage3": False}
    if not all(c.status == "improved" for c in comparisons):
        return {"decision": "pending", "reason": "no_consistent_objective_improvement", "ready_for_stage3": False}
    return {"decision": "accepted", "reason": "repair_and_objective_improvement", "ready_for_stage3": True}
