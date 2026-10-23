"""Original evaluation contract and objective-first acceptance policy."""
import copy
from method_input_adapters.skillpatch.models import DiagnosisContext
from method.skillpatch.patch_diagnosis.tool.evidence_references import resolve_reference
from .models import TargetEvaluation

def _refs(values, context: DiagnosisContext, *, behavior=False, collection="evaluation"):
    if not isinstance(values, list):
        raise ValueError(f"{collection}: evidence must be a list")
    evidence = {item.context_id: item for item in context.items}
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


def validate_evaluation(payload: dict, baseline: DiagnosisContext, candidate: DiagnosisContext, *,
                        views: dict | None = None,
                        require_english: bool = False, basic: bool = False,
                        require_behavior: bool = True) -> TargetEvaluation:
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
    _refs(payload["baseline_evidence"], baseline, behavior=require_behavior and not basic and payload["baseline_status"] != "uncertain",
          collection="baseline")
    _refs(payload["candidate_evidence"], candidate, behavior=require_behavior and not basic and payload["target_status"] in {"repaired", "not_repaired"},
          collection="candidate")
    if not basic and not require_behavior:
        for name, supported in (("baseline", payload["baseline_status"] != "uncertain"),
                                ("candidate", payload["target_status"] in {"repaired", "not_repaired"})):
            if supported and not payload[name + "_evidence"]:
                raise ValueError(f"{name}: a supported finding judgment needs execution evidence")
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
        by_id = {e.context_id: e for e in candidate.items}
        if not basic and not any(by_id[r["evidence_id"]].kind == "skill" for r in refs):
            raise ValueError("Reverse conflict needs a Skill clause as well as behavior")
    if not basic and baseline.trajectory_complete is not True and payload["baseline_status"] != "uncertain":
        raise ValueError("baseline: incomplete execution requires baseline_status=uncertain")
    if not basic and candidate.trajectory_complete is not True and payload["target_status"] in {"repaired", "not_repaired"}:
        raise ValueError("candidate: incomplete execution requires target_status=uncertain")
    return TargetEvaluation(**payload)

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


def evaluate_target(source, baseline, candidate, config, output, environment, *, client=None, progress=None):
    from excutor.executor_adapter.storage import write_json, jsonable
    from method.skillpatch.patch_generation.session import run_session
    payload = run_session(config, {'issue':jsonable(source.conflict),'execution_roles':{
        'source_run_id':source.context.run_id,'baseline_run_id':baseline.run_id,
        'candidate_run_id':candidate.run_id,
        'baseline_is_source_execution':baseline.run_id == source.context.run_id}},
        {'source':source.context,'baseline':baseline,'candidate':candidate},output,environment,
        client=client,progress=progress)
    result = validate_evaluation(payload,baseline,candidate,basic=True)
    write_json(output/'evaluation.json',{'evaluation':result,
        'validation_scope':'basic_schema_and_source_references','semantic_correctness':'not_verified',
        'access_tracking':'harness_read_ranges_not_verified','original_input_digests':{
            'source':source.context.input_digest,'baseline':baseline.input_digest,'candidate':candidate.input_digest}})
    return result
