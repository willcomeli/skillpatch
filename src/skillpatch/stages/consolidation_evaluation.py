"""Independent checks that patch consolidation preserves previously fixed behavior."""

import json
from pathlib import Path
from typing import Mapping

from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.model_client import DiagnosisConfig
from skillpatch.models import DiscoveryInput
from skillpatch.patch_models import PatchSource, TargetEvaluation
from skillpatch.stages.consolidation_model import original_skill_context_items
from skillpatch.stages.induction import require_displayed_references
from skillpatch.stages.patch_evaluation import validate_evaluation
from skillpatch.storage import jsonable, write_json


def evaluate_preservation(source: PatchSource, baseline: DiscoveryInput, candidate: DiscoveryInput,
                          config: DiagnosisConfig, output: Path, environment: Mapping[str, str], *,
                          client=None, session_timeout_sec: int = 900) -> TargetEvaluation:
    """Compare an unmerged patch collection with a proposed consolidated collection."""
    data = ("PRESERVATION CHECK: baseline already contains the accepted, unmerged patches.\n"
            "The candidate contains the proposed consolidated collection.\n"
            "ORIGINAL TARGET AND CRITERIA\n" + json.dumps(jsonable(source.conflict), ensure_ascii=False))
    contexts = {"source": source.context, "baseline": baseline, "candidate": candidate}
    priority = {name: [item.evidence_id for item in original_skill_context_items(context, source.conflict.target)]
                for name, context in contexts.items()}
    checked = {}

    def validate(payload, views):
        result = validate_evaluation(payload, views["baseline"].context,
                                     views["candidate"].context, views=views, require_english=True)
        if payload["baseline_status"] != "uncertain" or payload["target_status"] != "uncertain":
            target = source.conflict.target
            view = views["source"]
            require_displayed_references([{"evidence_id": target.skill_ref, "quote": target.quote}], view, "source")
            original_skill = original_skill_context_items(view.context, target)
            if not original_skill or any(not view.fully_presented(item) for item in original_skill):
                raise ValueError("source: read the complete original Skill and target context before preservation judgment")
        checked["evaluation"] = result
        checked["digests"] = {name: view.context.input_digest for name, view in views.items()}

    def fallback(reason):
        return {"baseline_status": "uncertain", "target_status": "uncertain",
                "explanation": "Preservation of previous repairs cannot yet be determined. Evidence inspection is incomplete: " + reason,
                "baseline_evidence": [], "candidate_evidence": [], "reverse_conflicts": [],
                "revision_action": "none"}

    recorded_evidence_session(config, data, contexts, output, environment, client=client,
        priority_ids=priority, validate=validate, budget_fallback=fallback,
        session_timeout_sec=session_timeout_sec)
    write_json(output / "evaluation.json", {"evaluation": checked["evaluation"],
        "mode": "preservation", "semantic_correctness": "not_verified",
        "validation_scope": "schema_and_exact_displayed_references_and_complete_execution",
        "input_digests": checked["digests"],
        "original_input_digests": {name: context.input_digest for name, context in contexts.items()}})
    return checked["evaluation"]


def decide_preservation(evaluations, comparisons, regression_comparisons=(), *,
                        executions_ok=True, regression_evaluations=()) -> dict:
    """Allow unchanged scores; require supported preservation on every checked pair."""
    def decision(value, reason):
        return {"decision": value, "reason": reason, "ready_for_activation": value == "accepted"}

    if (not executions_ok or not evaluations or len(evaluations) != len(comparisons)
            or len(regression_evaluations) != len(regression_comparisons)):
        return decision("pending", "execution_or_evidence_unavailable")
    all_evaluations = [*evaluations, *regression_evaluations]
    all_comparisons = [*comparisons, *regression_comparisons]
    if any(item.status == "regressed" for item in all_comparisons):
        return decision("rejected", "verifier_regression")
    if any(item.reverse_conflicts for item in all_evaluations):
        return decision("rejected", "new_use_conflict")
    if any(item.target_status in {"invalid_diagnosis", "unpatchable"} for item in all_evaluations):
        return decision("rejected", "diagnosis_or_patch_method_invalid")
    if any(item.target_status == "not_repaired" for item in all_evaluations):
        return decision("rejected", "repair_not_preserved")
    if any(item.baseline_status == "present" for item in all_evaluations):
        return decision("pending", "unmerged_baseline_did_not_preserve_repair")
    if (any(item.baseline_status != "absent" or item.target_status != "repaired"
            or item.revision_action != "none" for item in all_evaluations)
            or any(item.status not in {"unchanged", "improved"} for item in all_comparisons)):
        return decision("pending", "insufficient_preservation_or_verifier_evidence")
    return decision("accepted", "repairs_preserved_without_observed_regression")
