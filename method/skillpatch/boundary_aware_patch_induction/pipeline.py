"""Induce a candidate and revise it against grounded boundary cases."""

from dataclasses import replace
from pathlib import Path

from excutor.executor_adapter.storage import jsonable, write_json
from method.skillpatch.config import DiscoveryConfig
from method.skillpatch.patch_diagnosis.discovery_validation import _object, _text
from method.skillpatch.patch_generation.models import NOTE_EXAMPLE_FORMAT, FindingIssue, PatchSource
from method.skillpatch.patch_generation.patches import validate_generation
from method.skillpatch.session import run_session

from .cases import CASE_BUDGET, validate_boundary_cases


def run_boundary_induction(source: PatchSource, generation: DiscoveryConfig, output: Path, environment, *,
                           prompts: dict[str, Path], client=None, progress=None) -> tuple[dict, int]:
    """Return the final generation response and revision for a bound Stage1 issue."""
    target = source.conflict.target
    if isinstance(source.conflict, FindingIssue) or target is None:
        raise ValueError("Boundary-aware induction requires a Stage1 target")

    context_paths = {"source": source.diagnosis_dir / "input/context.json"}
    intermediate = output / "boundary_induction"
    final = output / "generation"
    request = {"anchor": target.quote, "issue": jsonable(source.conflict)}
    emit = progress or (lambda _: None)

    def run(prompt_path, data, folder):
        return run_session(replace(generation, prompt_path=prompt_path),
                           data, {"source": source.context}, folder, environment,
                           context_paths=context_paths, client=client, progress=emit)

    emit("Inducing the initial anchored patch")
    initial = run(prompts["initial"], {**request, "mode": "INITIALIZE"}, intermediate / "initial")
    _object(initial, {"decision", "patch"}, "initialization")
    p0 = initial["patch"]
    candidate = validate_generation(p0, source, revision=0, patch_format=NOTE_EXAMPLE_FORMAT)
    decision = initial["decision"]

    if candidate is not None:
        _object(decision, {"target_step", "decision_ref"}, "decision")
        _text(decision["target_step"], "decision.target_step")
        decision_ref = _text(decision["decision_ref"], "decision.decision_ref")
        if not any(item.context_id == decision_ref and item.kind == "trajectory" for item in source.context.items):
            raise ValueError("decision.decision_ref must name a trajectory item in the Stage1 context")

        current = {**request, **decision, "current_patch": {"note": candidate.note, "example": candidate.example}}
        emit("Proposing grounded INV and CON cases")
        proposal = run(prompts["cases"], {**current, "test_budget": CASE_BUDGET}, intermediate / "cases")
        validate_boundary_cases(proposal, {item.context_id for item in source.context.items})
        if proposal["boundary_cases"]:
            emit("Revising the patch against the proposed cases")
            response = run(prompts["initial"], {**current, **proposal, "mode": "REVISE"}, final)
            return response, 1
    elif decision is not None:
        raise ValueError("A non-candidate initialization must have a null decision")

    write_json(final / "response.json", p0)
    return p0, 0
