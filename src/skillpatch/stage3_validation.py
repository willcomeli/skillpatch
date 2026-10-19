"""Fresh paired task runs for patch combinations and preservation after merging."""

from dataclasses import replace
from pathlib import Path
import json
import shutil

from skillpatch.adapters.registry import get_adapter
from skillpatch.config import load_config
from skillpatch.executor import Executor
from skillpatch.model_client import load_diagnosis_config
from skillpatch.patch_sources import load_request
from skillpatch.patch_targets import locate_skill_root
from skillpatch.patches import tree_hashes
from skillpatch.skill_invocation import check_patch_reads, task_messages_equal
from skillpatch.stage3_sources import load_accepted_queue, read_receipt
from skillpatch.stages.patch_evaluation import evaluate_target, decide_validation
from skillpatch.stages.patch_validation import _usable, default_backend
from skillpatch.storage import hash_manifest, jsonable, write_json


def collect_cases(references, incoming, config, output, *, adapter_factory=get_adapter):
    """Reopen original evidence; source and configured preservation tasks stay explicit."""
    accepted = {incoming.queue_digest: incoming}
    for reference in references:
        receipt = read_receipt(reference)
        if receipt["queue_sha256"] not in accepted:
            item = load_accepted_queue(Path(receipt["queue_path"]), adapter_factory=adapter_factory)
            if item.queue_digest != receipt["queue_sha256"]:
                raise ValueError("Support queue changed")
            accepted[item.queue_digest] = item
    cases, seen = [], set()
    for item in accepted.values():
        source = item.source
        key = (source.context.input_digest, source.conflict.conflict_id)
        if key not in seen:
            seen.add(key)
            request = replace(source.request, task_dir=source.prepared.task_dir,
                              skills_dir=source.prepared.skills_dir, diagnosis_config=None,
                              skill_condition="stage3-validation")
            cases.append({"id": f"source-{len(cases):03d}", "role": "source", "source": source,
                          "request": request, "pack": source.prepared.skills_dir,
                          "target_relative_path": item.patch.target.relative_path,
                          "incoming": key == (incoming.source.context.input_digest, incoming.patch.conflict_id)})
        for archived in item.cases:
            if archived["role"] != "regression":
                continue
            request = load_request(Path(archived["request_path"]))
            request = replace(request, diagnosis_config=None, skill_condition="stage3-validation")
            key = (json.dumps(jsonable(request), sort_keys=True), source.context.input_digest, source.conflict.conflict_id)
            if key not in seen:
                seen.add(key)
                cases.append({"id": f"regression-{len(cases):03d}", "role": "regression", "source": source,
                    "request": request, "pack": request.skills_dir,
                    "target_relative_path": item.patch.target.relative_path, "incoming": False})
    for index, path in enumerate(config.regression_configs):
        request, _ = load_config(path)
        selected = adapter_factory(request.benchmark)
        prepared = selected.prepare(request, output / "regression-inputs" / str(index))
        if prepared.skills_dir is None or request.skill_mode != "with-skill":
            raise ValueError("Stage3 regressions need task-provided Skills")
        cases.append({"id": f"regression-{len(cases):03d}", "role": "regression", "source": incoming.source,
            "request": replace(request, task_dir=prepared.task_dir, skills_dir=prepared.skills_dir,
                               diagnosis_config=None, skill_condition="stage3-validation"),
            "pack": prepared.skills_dir, "target_relative_path": incoming.patch.target.relative_path,
            "incoming": False})
    if len(cases) > config.max_validation_cases:
        raise ValueError("Source union exceeds configured max_validation_cases; no task was started")
    return cases


def _compose(case, skill_dir, original_dir, destination, allowed_source_digests):
    pack = case["pack"]
    if pack is None:
        raise ValueError("Validation case has no Skill pack")
    unit, prefix = locate_skill_root(pack, case["target_relative_path"])
    # Same filenames cannot establish the same original. Only exact registered
    # original/version snapshots may supply this Skill in another task.
    origin_hashes, case_hashes = tree_hashes(original_dir), tree_hashes(unit)
    if hash_manifest(case_hashes) not in {hash_manifest(origin_hashes), *allowed_source_digests}:
        raise ValueError("Validation task uses another Skill resource version")
    before = tree_hashes(pack)
    shutil.copytree(pack, destination)
    target = destination / prefix if prefix else destination
    if prefix:
        shutil.rmtree(target)
        shutil.copytree(skill_dir, target)
    else:
        shutil.rmtree(destination)
        shutil.copytree(skill_dir, destination)
    if tree_hashes(pack) != before:
        raise ValueError("Validation source changed during preparation")
    return destination, prefix


def validate_patch_sets(cases, baseline_skill: Path, candidate_skill: Path, original_skill: Path,
                        config, output: Path, environment, *, mode="preservation", client=None,
                        adapter_factory=get_adapter, backend_factory=default_backend, progress=None,
                        allowed_source_digests=()):
    from skillpatch.stages.consolidation_evaluation import evaluate_preservation, decide_preservation
    if mode not in {"preservation", "combination"}:
        raise ValueError("Unknown Stage3 validation mode")
    emit = progress or (lambda _: None)
    preservation, comparisons, repair, repair_comparisons, records = [], [], [], [], []
    executions_ok = True
    prepared_cases = []
    # Finish all resource checks before starting any charged task/model call.
    for case in cases:
        baseline_pack, prefix = _compose(case, baseline_skill, original_skill, output / "packs" / case["id"] / "baseline", allowed_source_digests)
        candidate_pack, _ = _compose(case, candidate_skill, original_skill, output / "packs" / case["id"] / "candidate", allowed_source_digests)
        prepared_cases.append((case, baseline_pack, candidate_pack, prefix))
    write_json(output / "plan.json", {"mode": mode, "cases": [{"id": c[0]["id"], "role": c[0]["role"],
        "source_run_id": c[0]["source"].context.run_id, "conflict_id": c[0]["source"].conflict.conflict_id,
        "request": c[0]["request"]} for c in prepared_cases], "repetitions": config.repetitions,
        "max_backend_runs": len(cases) * config.repetitions * 2,
        "max_evaluation_sessions": len(cases) * config.repetitions,
        "session_timeout_sec": config.model_session_timeout_sec})
    for case, before_pack, after_pack, prefix in prepared_cases:
        adapter = adapter_factory(case["request"].benchmark)
        for repetition in range(config.repetitions):
            record = {"case_id": case["id"], "role": case["role"], "repetition": repetition}
            records.append(record)
            baseline_dir = output / "runs" / f"{case['id']}-baseline-{repetition:03d}"
            candidate_dir = output / "runs" / f"{case['id']}-candidate-{repetition:03d}"
            request = case["request"]
            emit(f"Stage3 {mode}: baseline {case['id']} {repetition + 1}")
            baseline = Executor(adapter, backend_factory(request.backend)).run(
                replace(request, skills_dir=before_pack), baseline_dir, environment, progress=emit,
                expected_skill_digest=hash_manifest(tree_hashes(before_pack)))
            if baseline.status == "cancelled":
                raise KeyboardInterrupt
            record["baseline_dir"] = str(baseline_dir)
            if not _usable(baseline) or (case["role"] == "regression" and baseline.feedback.passed is not True):
                executions_ok = False
                record["error"] = "Baseline unavailable or regression baseline did not pass"
                continue
            emit(f"Stage3 {mode}: candidate {case['id']} {repetition + 1}")
            candidate = Executor(adapter, backend_factory(request.backend)).run(
                replace(request, skills_dir=after_pack), candidate_dir, environment, progress=emit,
                expected_skill_digest=hash_manifest(tree_hashes(after_pack)))
            if candidate.status == "cancelled":
                raise KeyboardInterrupt
            record["candidate_dir"] = str(candidate_dir)
            if not _usable(candidate):
                executions_ok = False
                record["error"] = "Candidate execution unavailable"
                continue
            for name, result in (("baseline", baseline), ("candidate", candidate)):
                audit = check_patch_reads(request, result.prepared, result.execution.normalized_trajectory_path)
                record[name + "_patch_read_audit"] = audit
                if audit["status"] == "unconfirmed":
                    executions_ok = False
                    record["error"] = "Required Skill and patch reads were not confirmed: " + name
            if "error" in record:
                continue
            if baseline.execution.backend_version != candidate.execution.backend_version:
                executions_ok = False
                record["error"] = "Baseline and candidate backend versions differ"
                continue
            # Exclude only changed Skill files, never task prompts/scripts/assets.
            before_hashes, after_hashes = tree_hashes(before_pack), tree_hashes(after_pack)
            changed = {k for k in before_hashes.keys() | after_hashes.keys() if before_hashes.get(k) != after_hashes.get(k)}
            protected = []
            for result in (baseline, candidate):
                task = result.prepared
                excluded = {(task.skills_dir / k).relative_to(task.task_dir).as_posix() for k in changed}
                protected.append({k: v for k, v in task.file_hashes.items() if k not in excluded})
            if (protected[0] != protected[1]
                    or baseline.prepared.skill_digest != hash_manifest(before_hashes)
                    or candidate.prepared.skill_digest != hash_manifest(after_hashes)):
                executions_ok = False
                record["error"] = "Protected inputs or deployed Skill digests differ"
                continue
            before, after = adapter.collect_evidence(baseline_dir), adapter.collect_evidence(candidate_dir)
            if not task_messages_equal(before, after, baseline_dir, candidate_dir):
                executions_ok = False
                record["error"] = "Actual task prompts differ"
                continue
            is_repair = mode == "combination" and case["incoming"] and case["role"] == "source"
            comparison = adapter.compare_feedback(baseline.feedback, candidate.feedback,
                target=case["source"].conflict if is_repair else None)
            evaluator = evaluate_target if is_repair else evaluate_preservation
            model_config = load_diagnosis_config(config.repair_evaluation_config if is_repair else config.evaluation_config)
            evaluation = evaluator(case["source"], before, after, model_config,
                output / "evaluation" / case["id"] / f"{repetition:03d}", environment,
                client=client, session_timeout_sec=config.model_session_timeout_sec)
            record.update(comparison=jsonable(comparison), evaluation=jsonable(evaluation), mode="repair" if is_repair else "preservation")
            if is_repair:
                repair.append(evaluation); repair_comparisons.append(comparison)
            else:
                preservation.append(evaluation); comparisons.append(comparison)
            write_json(output / "cases.json", records)
    decisions = []
    if mode == "combination":
        repair_decision = decide_validation(repair, repair_comparisons, [], executions_ok=executions_ok)
        if repair_decision["decision"] == "accepted" and any(e.revision_action != "none" for e in repair):
            repair_decision = {"decision": "pending", "reason": "unresolved_combination_evidence"}
        decisions.append(repair_decision)
    if preservation or mode == "preservation":
        decisions.append(decide_preservation(preservation, comparisons, executions_ok=executions_ok))
    status = "rejected" if any(d["decision"] == "rejected" for d in decisions) else (
        "accepted" if executions_ok and decisions and all(d["decision"] == "accepted" for d in decisions) else "pending")
    regression_tasks = sum(c["role"] == "regression" for c in cases)
    regression_pairs = sum(r.get("evaluation") is not None and r["role"] == "regression" for r in records)
    result = {"decision": status, "mode": mode, "cases": records, "checks": decisions,
              "reason": "repair_preserved" if status == "accepted" else "validation_did_not_establish_acceptance",
              "regression_coverage": {"configured_tasks": regression_tasks, "checked_pairs": regression_pairs,
                  "status": "not_configured" if not regression_tasks else (
                      "complete" if regression_pairs == regression_tasks * config.repetitions else "incomplete")}}
    write_json(output / "validation.json", result)
    return result
