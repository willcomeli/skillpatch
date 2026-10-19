"""Import accepted Stage2 evidence without rewriting historical reports."""

from dataclasses import dataclass, replace
from pathlib import Path
import tempfile

from skillpatch.adapters.registry import get_adapter
from skillpatch.evidence_views import normalize_evidence_item
from skillpatch.execution_records import load_execution
from skillpatch.patch_models import PatchSource, SkillPatch
from skillpatch.patch_sources import load_patch_source, load_request, read_object
from skillpatch.patches import render_patch, tree_hashes, validate_generation
from skillpatch.skill_invocation import check_patch_reads, task_messages_equal
from skillpatch.stages.patch_evaluation import decide_validation, validate_evaluation
from skillpatch.stages.patch_validation import _protected_inputs_equal
from skillpatch.storage import hash_manifest, jsonable, sha256_file, write_json


@dataclass
class AcceptedPatch:
    queue_path: Path
    queue_digest: str
    patch: SkillPatch
    source: PatchSource
    candidate_pack: Path
    receipt: dict
    cases: list[dict]
    presentation_mode: str = "inline_v1"


def _inside(root: Path, value, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(label + " must be an archived path")
    path = Path(value)
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
        raise ValueError(label + " resolves outside its Stage2 episode")
    return path.resolve(strict=True)


def _find_run(episode: Path, identifier: str, *, baseline: bool) -> Path:
    if not isinstance(identifier, str) or Path(identifier).name != identifier or identifier in {".", ".."}:
        raise ValueError("Invalid archived run identifier")
    directories = [episode / "baselines"] if baseline else [episode / "runs", *episode.glob("attempts/*/runs")]
    found = [p / identifier for p in directories if (p / identifier / "result.json").is_file()]
    if len(found) != 1:
        raise ValueError("Cannot resolve unique archived validation run")
    return _inside(episode, str(found[0]), "validation run")


def _saved_evaluation(payload):
    """Ignore the retired field only when reading existing evaluation archives."""
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key != "uncertainties"}


def load_accepted_queue(path: Path, *, adapter_factory=get_adapter) -> AcceptedPatch:
    path = path.absolute()
    if path.is_symlink():
        raise ValueError("Queue cannot be a symbolic link")
    path = path.resolve(strict=True)
    queue = read_object(path)
    episode = path.parent
    validation_path = _inside(episode, queue.get("validation_path"), "validation")
    if sha256_file(validation_path) != queue.get("validation_sha256"):
        raise ValueError("Stage2 validation digest changed")
    validation = read_object(validation_path)
    if validation.get("decision") != "accepted" or validation.get("ready_for_stage3") is not True:
        raise ValueError("Stage3 only imports accepted Stage2 candidates")
    # Missing metadata identifies the historical inline protocol. Reconstruct
    # exactly what was evaluated rather than granting a sidecar a past result.
    presentation_mode = queue.get("presentation_mode", "inline_v1")
    if presentation_mode not in {"inline_v1", "sidecar_v1"}:
        raise ValueError("Unknown accepted patch presentation mode")
    if validation.get("presentation_mode", "inline_v1") != presentation_mode:
        raise ValueError("Validated and queued presentation modes differ")
    source_info = read_object(episode / "source.json") if (episode / "source.json").is_file() else read_object(episode / "plan.json")
    diagnosis_dir = source_info.get("diagnosis_dir", source_info.get("source_diagnosis_dir"))
    if not isinstance(diagnosis_dir, str):
        raise ValueError("Queue has no bound source diagnosis")
    benchmark = read_object(Path(diagnosis_dir).parent.parent / "result.json")["benchmark"]
    adapter = adapter_factory(benchmark)
    raw_patch = queue["patch"]
    source = load_patch_source(Path(diagnosis_dir), raw_patch["conflict_id"], adapter=adapter)
    patch = validate_generation({"status": "candidate", "reason": "Revalidate the archived candidate binding.",
        "when_to_use": raw_patch["when_to_use"], "how_to_interpret": raw_patch["how_to_interpret"]},
        source, raw_patch["revision"])
    if jsonable(patch) != raw_patch:
        raise ValueError("Queue patch does not match its source diagnosis")
    if "patch" in validation:
        if validation["patch"] != raw_patch:
            raise ValueError("Validated and queued patches differ")
    else:
        # A saved validate-existing-patch episode has a separately hashed patch.
        archived_patch = Path(validation.get("source_patch", ""))
        if (validation.get("patch_id") != patch.patch_id or not archived_patch.is_file()
                or sha256_file(archived_patch) != validation.get("source_patch_sha256")
                or read_object(archived_patch) != raw_patch):
            raise ValueError("Existing-patch validation has no exact patch binding")
    replay_archives = []
    patch_origin = queue.get("patch_origin", "generated")
    if patch_origin not in {"generated", "existing"} or validation.get("patch_origin", "generated") != patch_origin:
        raise ValueError("Validated and queued patch origins differ")
    if patch_origin == "existing":
        # New replay episodes retain an independent, exact parsed patch copy.
        # The external input path is informational and need not remain present.
        archived_patch = _inside(episode, validation.get("source_patch"), "archived replay patch")
        if (archived_patch != episode / "source_patch.json"
                or sha256_file(archived_patch) != validation.get("archived_patch_sha256")
                or read_object(archived_patch) != raw_patch):
            raise ValueError("Existing-patch replay archive does not match the validated patch")
        generation_path = _inside(episode, str(validation_path.parent / "generation/generation.json"),
                                   "replay generation record")
        generation = read_object(generation_path)
        if generation.get("patch_origin") != "existing" or generation.get("patch") != raw_patch:
            raise ValueError("Existing-patch generation record does not match the validated patch")
        replay_archives = [archived_patch, generation_path]
    candidate_pack = _inside(episode, queue.get("candidate_pack"), "candidate pack")
    candidate_digest = hash_manifest(tree_hashes(candidate_pack))
    if candidate_digest != queue.get("candidate_pack_digest"):
        raise ValueError("Validated candidate pack changed")
    if hash_manifest(tree_hashes(source.prepared.skills_dir)) != source.context.skill_digest:
        raise ValueError("Source Skill pack changed")
    with tempfile.TemporaryDirectory(prefix="skillpatch-queue-") as temporary:
        rendered = render_patch(source.prepared.skills_dir, patch, Path(temporary) / "candidate",
                                presentation_mode=presentation_mode)
        if rendered["candidate_digest"] != candidate_digest:
            raise ValueError("Queued pack is not the exact source plus archived patch")
    items = validation.get("cases")
    if items is None:
        baseline_dir = _inside(episode, validation["baseline"]["run_dir"], "baseline run")
        candidate_dir = _inside(episode, validation["candidate"]["run_dir"], "candidate run")
        items = [{"case_id": "source", "role": "source", "repetition": 0,
                  "baseline_run_id": baseline_dir.name, "candidate_run_id": candidate_dir.name,
                  "evaluation": validation["evaluation"], "comparison": validation["comparison"]}]
    if not isinstance(items, list) or not items:
        raise ValueError("Accepted validation has no checked cases")
    cases, evaluations, comparisons, regressions, regression_evaluations = [], [], [], [], []
    archive_files = {str(path): sha256_file(path), str(validation_path): sha256_file(validation_path)}
    archive_files.update({str(file): sha256_file(file) for file in replay_archives})
    archive_trees = {}
    for folder in (source.prepared.task_dir, source.diagnosis_dir, candidate_pack):
        archive_trees[str(folder)] = tree_hashes(folder)
        for relative, digest in tree_hashes(folder).items():
            archive_files[str(folder / relative)] = digest
    for file in (source.run_dir / "result.json", source.run_dir / "request.json"):
        archive_files[str(file)] = sha256_file(file)
    for evidence in source.context.evidence:
        for name in (evidence.source_path, evidence.observation.get("local_source")):
            if name and Path(name).is_file():
                archive_files[str(Path(name))] = sha256_file(Path(name))
    seen = set()
    for item in items:
        role = item.get("role")
        case_id, repetition = item.get("case_id"), item.get("repetition")
        if role not in {"source", "regression"} or not isinstance(case_id, str) or Path(case_id).name != case_id:
            raise ValueError("Invalid Stage2 case identity")
        if type(repetition) is not int or repetition < 0 or (case_id, repetition) in seen:
            raise ValueError("Invalid or repeated Stage2 repetition")
        seen.add((case_id, repetition))
        baseline_origin = item.get("baseline_origin", "fresh")
        if not isinstance(baseline_origin, str) or baseline_origin not in {"source", "fresh"}:
            raise ValueError("Invalid Stage2 baseline origin")
        if baseline_origin == "source":
            if role != "source" or item["baseline_run_id"] != source.context.run_id:
                raise ValueError("Reused baseline must be the bound source execution")
            baseline_dir = source.run_dir
            if item.get("baseline_result_sha256") != sha256_file(baseline_dir / "result.json"):
                raise ValueError("Reused source baseline result digest changed")
        else:
            # Historical queues have no origin field and retain the original
            # episode-local baseline lookup and execution-condition checks.
            baseline_dir = _find_run(episode, item["baseline_run_id"], baseline=True)
        candidate_dir = _find_run(episode, item["candidate_run_id"], baseline=False)
        baseline, candidate = load_execution(baseline_dir), load_execution(candidate_dir)
        baseline_request, candidate_request = load_request(baseline_dir / "request.json"), load_request(candidate_dir / "request.json")
        if presentation_mode == "sidecar_v1":
            audit = check_patch_reads(candidate_request, candidate.prepared,
                                      candidate.execution.normalized_trajectory_path)
            if audit["status"] == "unconfirmed" and validation.get("reason") != "objective_improvement":
                raise ValueError("Stage2 sidecar acceptance has no confirmed Skill and patch reads")
            audit_path = candidate_dir / "patch_read_audit.json"
            if (item.get("patch_read_audit") != audit or not audit_path.is_file()
                    or read_object(audit_path) != audit):
                raise ValueError("Stage2 archived patch read audit differs from execution evidence")
            archive_files[str(audit_path)] = sha256_file(audit_path)
        # Reusing the original run also changes post-task diagnosis and the
        # administrative condition label; task execution settings stay equal.
        expected_candidate = replace(baseline_request, task_dir=candidate_request.task_dir,
                                     skills_dir=candidate_request.skills_dir)
        if baseline_origin == "source":
            expected_candidate = replace(expected_candidate, diagnosis_config=None,
                                         skill_condition="stage2-validation")
        if expected_candidate != candidate_request:
            raise ValueError("Stage2 execution model, harness or budgets differ")
        if baseline.execution.backend_version != candidate.execution.backend_version:
            raise ValueError("Stage2 backend versions differ")
        if role == "source" and baseline_origin == "fresh" and replace(source.request, task_dir=baseline_request.task_dir,
                skills_dir=baseline_request.skills_dir, diagnosis_config=None,
                skill_condition="stage2-validation") != baseline_request:
            raise ValueError("Stage2 baseline differs from the source execution configuration")
        runtime_before = baseline_dir / "runtime_environment.json"
        runtime_after = candidate_dir / "runtime_environment.json"
        if read_object(runtime_before) != read_object(runtime_after):
            raise ValueError("Stage2 recorded provider settings differ")
        selected = adapter_factory(baseline.benchmark)
        if baseline.benchmark != candidate.benchmark or not _protected_inputs_equal(
                baseline, candidate, patch, presentation_mode=presentation_mode):
            raise ValueError("Stage2 protected inputs differ")
        if role == "source" and (baseline.prepared.skill_digest != source.context.skill_digest
                                  or candidate.prepared.skill_digest != candidate_digest):
            raise ValueError("Source validation deployed another Skill pack")
        if role == "regression" and baseline.feedback.passed is not True:
            raise ValueError("Regression baseline was not successful")
        # The verified source context includes any archived live observations
        # and is exactly the baseline evidence presented during Stage2.
        before = source.context if baseline_origin == "source" else selected.collect_evidence(baseline_dir)
        after = selected.collect_evidence(candidate_dir)
        if not task_messages_equal(before, after, baseline_dir, candidate_dir):
            raise ValueError("Stage2 task messages differ")
        comparison = selected.compare_feedback(baseline.feedback, candidate.feedback,
                                                target=source.conflict if role == "source" else None)
        normalized_before = replace(before, evidence=[normalize_evidence_item(e) for e in before.evidence])
        normalized_after = replace(after, evidence=[normalize_evidence_item(e) for e in after.evidence])
        evaluation = validate_evaluation(_saved_evaluation(item["evaluation"]),
                                         normalized_before, normalized_after, basic=True)
        evaluation_path = validation_path.parent / "evaluation" / case_id / f"{repetition:03d}" / "evaluation.json"
        archived = read_object(evaluation_path)
        if _saved_evaluation(archived.get("evaluation")) != jsonable(evaluation):
            raise ValueError("Independent evaluation and accepted record disagree")
        digests = archived.get("original_input_digests")
        if digests is not None and digests != {"source": source.context.input_digest,
                "baseline": before.input_digest, "candidate": after.input_digest}:
            raise ValueError("Evaluation is bound to different execution evidence")
        if jsonable(comparison) != item["comparison"]:
            raise ValueError("Archived verifier comparison cannot be reproduced")
        for file in (baseline_dir / "result.json", candidate_dir / "result.json", evaluation_path):
            archive_files[str(file)] = sha256_file(file)
        for directory, result, context in ((baseline_dir, baseline, before), (candidate_dir, candidate, after)):
            archive_trees[str(result.prepared.task_dir)] = tree_hashes(result.prepared.task_dir)
            for relative, digest in tree_hashes(result.prepared.task_dir).items():
                archive_files[str(result.prepared.task_dir / relative)] = digest
            # Sidecar runs have an intentionally different invocation prefix.
            # Its verified derivation is part of acceptance provenance too.
            prompt_dir = directory / "prompt"
            if prompt_dir.exists():
                prompt_hashes = tree_hashes(prompt_dir)
                archive_trees[str(prompt_dir)] = prompt_hashes
                archive_files.update({str(prompt_dir / name): digest
                                      for name, digest in prompt_hashes.items()})
            for file in (directory / "request.json", directory / "runtime_environment.json", result.prepared.manifest_path,
                         result.execution.normalized_trajectory_path, result.execution.trajectory_path,
                         result.execution.result_path):
                if file is not None:
                    archive_files[str(file)] = sha256_file(file)
            for evidence in context.evidence:
                if Path(evidence.source_path).is_file():
                    archive_files[evidence.source_path] = sha256_file(Path(evidence.source_path))
        cases.append({"case_id": case_id, "role": role, "repetition": repetition,
                      "baseline_origin": baseline_origin,
                      "baseline_dir": str(baseline_dir), "candidate_dir": str(candidate_dir),
                      "request_path": str(baseline_dir / "request.json")})
        if role == "source":
            evaluations.append(evaluation); comparisons.append(comparison)
        else:
            regression_evaluations.append(evaluation); regressions.append(comparison)
    if validation.get("reason") != "objective_improvement" and any(e.revision_action != "none"
           for e in [*evaluations, *regression_evaluations]):
        raise ValueError("Stage2 evaluation retains a requested revision; "
                         "revalidate the patch in Stage2 or revisit its diagnosis before Stage3 inheritance")
    decision = decide_validation(evaluations, comparisons, regressions,
                                  regression_evaluations=regression_evaluations)
    if decision["decision"] != "accepted":
        raise ValueError("Archived evidence no longer supports Stage2 acceptance")
    receipt = {"schema_version": 1, "presentation_mode": presentation_mode, "patch_origin": patch_origin,
               "queue_path": str(path), "queue_sha256": sha256_file(path),
               "validation_path": str(validation_path), "validation_sha256": sha256_file(validation_path),
               "diagnosis_dir": str(source.diagnosis_dir), "conflict_id": patch.conflict_id,
               "source_input_digest": source.context.input_digest, "patch_id": patch.patch_id,
               "candidate_pack_digest": candidate_digest, "archive_files": archive_files, "archive_trees": archive_trees,
               "coverage_notes": queue.get("coverage_notes", []),
               "regression_coverage": queue.get("regression_coverage", {}), "cases": cases}
    return AcceptedPatch(path, sha256_file(path), patch, source, candidate_pack, receipt, cases,
                         presentation_mode)


def save_receipt(accepted: AcceptedPatch, path: Path) -> dict:
    write_json(path, accepted.receipt)
    return {"path": str(path.absolute()), "sha256": sha256_file(path)}


def read_receipt(reference: dict) -> dict:
    path = Path(reference["path"])
    if not path.is_file() or sha256_file(path) != reference["sha256"]:
        raise ValueError("Patch support receipt changed")
    receipt = read_object(path)
    for name, digest in receipt["archive_files"].items():
        if not Path(name).is_file() or sha256_file(Path(name)) != digest:
            raise ValueError("Patch support archive changed")
    for directory, hashes in receipt.get("archive_trees", {}).items():
        if tree_hashes(Path(directory)) != hashes:
            raise ValueError("Patch support resource inventory changed")
    return receipt
