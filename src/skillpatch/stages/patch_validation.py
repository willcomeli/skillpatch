"""Reuse the source baseline, run candidates and bound Stage 2 revisions.

Benchmark-specific preparation, evidence and scoring remain in adapters. Model
generation and evaluation have separate sessions; only Skill supplements go
to the execution Agent. Accepted candidates are queued, never made active here.
"""

from dataclasses import replace
from pathlib import Path
from typing import Mapping

from skillpatch.adapters.registry import get_adapter
from skillpatch.config import load_config
from skillpatch.environment import redact, secret_values
from skillpatch.execution_records import load_execution, usable_execution as _usable
from skillpatch.executor import Executor
from skillpatch.model_client import diagnosis_secrets, load_diagnosis_config
from skillpatch.patch_models import Stage2Config, SkillPatch, TargetEvaluation
from skillpatch.patch_budget import PatchBudget
from skillpatch.patch_sources import load_patch_source, read_object
from skillpatch.patches import render_patch, target_file, tree_hashes, validate_generation
from skillpatch.stages.induction import generate_patch
from skillpatch.stages.patch_evaluation import decide_validation, evaluate_target
from skillpatch.storage import hash_manifest, jsonable, now, sha256_file, write_json


def default_backend(name):
    if name != "benchflow":
        raise ValueError("No Stage 2 backend is registered for this execution framework")
    from skillpatch.runners.benchflow import BenchFlowBackend
    return BenchFlowBackend()


def _protected_inputs_equal(baseline, candidate, patch, *, presentation_mode="sidecar_v1"):
    a, b = baseline.prepared, candidate.prepared
    if a is None or b is None or a.skills_dir is None or b.skills_dir is None:
        return False
    if presentation_mode == "sidecar_v1":
        from skillpatch.patch_sidecars import load_sidecars, SIDECAR_MARKDOWN, SIDECAR_MANIFEST
        from skillpatch.patch_targets import locate_skill_root
        # Every original file, including the target and its YAML description,
        # stays identical. Existing supplements are preserved in order, with
        # exactly this candidate appended to its own Skill's companion pair.
        unit_a, _ = locate_skill_root(a.skills_dir, patch.target.relative_path)
        unit_b, _ = locate_skill_root(b.skills_dir, patch.target.relative_path)
        before = load_sidecars(unit_a)
        after = load_sidecars(unit_b)
        expected = {"entry_id": patch.patch_id,
            "target": {"relative_path": (b.skills_dir / patch.target.relative_path).relative_to(unit_b).as_posix(),
                       "scope": patch.target.scope, "quote": patch.target.quote.replace("\r\n", "\n"),
                       "version": patch.target.version},
            "when_to_use": patch.when_to_use, "how_to_interpret": patch.how_to_interpret}
        if after is None or after["entries"] != ([*before["entries"]] if before else []) + [expected]:
            return False
        allowed_a = {(unit_a / name).relative_to(a.task_dir).as_posix()
                     for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)}
        allowed_b = {(unit_b / name).relative_to(b.task_dir).as_posix()
                     for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)}
        return ({k: v for k, v in a.file_hashes.items() if k not in allowed_a}
                == {k: v for k, v in b.file_hashes.items() if k not in allowed_b})
    if presentation_mode != "inline_v1":
        raise ValueError("Unknown patch presentation mode")
    def relative_target(prepared):
        path = prepared.skills_dir / patch.target.relative_path
        return path.relative_to(prepared.task_dir).as_posix() if path.is_relative_to(prepared.task_dir) else None
    target_a, target_b = relative_target(a), relative_target(b)
    return ({k: v for k, v in a.file_hashes.items() if k != target_a}
            == {k: v for k, v in b.file_hashes.items() if k != target_b})


def run_stage2(diagnosis_dir: Path, conflict_id: str, config: Stage2Config, output: Path,
               environment: Mapping[str, str], *, prepare_only=False, generate_only=False,
               patch_file: Path | None = None, client=None, adapter_factory=get_adapter,
               backend_factory=default_backend, progress=None):
    """One isolated research episode. Existing directories are never resumed."""
    output = output.absolute()
    if output.is_symlink() or output.exists():
        raise ValueError("Stage 2 output must be a new directory")
    diagnosis_dir = diagnosis_dir.resolve(strict=True)
    # Never create output inside any original input/Skill snapshot.
    run_root = diagnosis_dir.parent.parent
    if output.resolve().is_relative_to(run_root / "input"):
        raise ValueError("Stage 2 output cannot modify the original input snapshot")
    output.mkdir(parents=True, mode=0o700)
    state = {"schema_version": 1, "status": "running", "phase": "preflight", "started_at": now(),
             "output_dir": str(output), "ready_for_stage3": False, "attempts": [], "coverage_notes": []}
    secrets = secret_values(environment)
    budget = None
    emit = progress or (lambda _: None)
    def save():
        write_json(output / "result.json", redact(state, secrets))
    try:
        save()
        if (type(config.max_revisions) is not int or not 0 <= config.max_revisions <= 2
                or type(config.repetitions) is not int or config.repetitions < 1
                or type(config.model_session_timeout_sec) is not int
                or config.model_session_timeout_sec <= 0):
            raise ValueError("Stage 2 requires explicit valid revision and repetition budgets")
        generation_timeout = config.generation_session_timeout_sec
        if generation_timeout is None:
            generation_timeout = config.model_session_timeout_sec
        if type(generation_timeout) is not int or generation_timeout <= 0:
            raise ValueError("generation_session_timeout_sec must be a positive integer")
        generation = load_diagnosis_config(config.generation_config)
        evaluation = load_diagnosis_config(config.evaluation_config)
        secrets = tuple(set(secrets) | set(diagnosis_secrets(generation, environment))
                        | set(diagnosis_secrets(evaluation, environment)))
        source_record = read_object(run_root / "result.json")
        adapter = adapter_factory(source_record["benchmark"])
        source = load_patch_source(diagnosis_dir, conflict_id, adapter=adapter)
        replay_patch = None
        if patch_file is not None:
            patch_file = patch_file.resolve(strict=True)
            raw_patch = read_object(patch_file)
            revision = raw_patch.get("revision")
            if type(revision) is not int or revision < 0:
                raise ValueError("Existing patch revision must be a nonnegative integer")
            replay_patch = validate_generation({"status": "candidate", "reason": "Revalidate the exact archived patch.",
                "when_to_use": raw_patch.get("when_to_use"), "how_to_interpret": raw_patch.get("how_to_interpret")},
                source, revision)
            if jsonable(replay_patch) != raw_patch:
                raise ValueError("Existing patch does not match this source diagnosis")
            write_json(output / "source_patch.json", raw_patch)
            state.update(patch_origin="existing", source_patch=str(patch_file),
                         source_patch_sha256=sha256_file(patch_file))
        source_baseline = load_execution(source.run_dir)
        source_result_digest = sha256_file(source.run_dir / "result.json")
        state.update(source_run_id=source.context.run_id, conflict_id=conflict_id,
                     source_input_digest=source.context.input_digest, baseline_policy="source_execution")
        if source.prepared.skills_dir is None:
            raise ValueError("Source task has no current Skill pack")
        # Validate full pack version before any model request or fresh execution.
        if hash_manifest(tree_hashes(source.prepared.skills_dir)) != source.context.skill_digest:
            raise ValueError("Source Skill pack changed")
        binding = SkillPatch("preflight", source.context.run_id, source.context.input_digest,
                             conflict_id, source.conflict.type, 0, source.conflict.target, "", "")
        target_file(source.prepared.skills_dir, binding)
        cases = [{"id": "source", "adapter": adapter, "request": replace(source.request,
                  task_dir=source.prepared.task_dir, skills_dir=source.prepared.skills_dir,
                  diagnosis_config=None, skill_condition="stage2-validation"),
                  "pack": source.prepared.skills_dir, "role": "source", "baseline_origin": "source"}]
        for index, path in enumerate(config.regression_configs, 1):
            request, _ = load_config(path)
            selected = adapter_factory(request.benchmark)
            prepared = selected.prepare(request, output / "cases" / f"regression-{index:03d}" / "input")
            if prepared.skills_dir is None or request.skill_mode != "with-skill":
                raise ValueError("Regression tasks must use the same current targeted Skill")
            cases.append({"id": f"regression-{index:03d}", "adapter": selected,
                          "request": replace(request, task_dir=prepared.task_dir, skills_dir=prepared.skills_dir,
                                             diagnosis_config=None, skill_condition="stage2-validation"),
                          "pack": prepared.skills_dir, "role": "regression", "baseline_origin": "fresh"})
        for case in cases:
            if not callable(getattr(case["adapter"], "compare_feedback", None)):
                raise ValueError("Stage 2 adapter must define benchmark-specific feedback comparison")
            backend_factory(case["request"].backend)
            tree_hashes(case["pack"])
            target_file(case["pack"], binding)
        if not config.regression_configs:
            state["coverage_notes"].append("No related-success task was configured; preservation is untested outside the source task.")
        state["regression_coverage"] = {"configured_tasks": len(config.regression_configs),
            "checked_pairs": 0, "status": "not_configured" if not config.regression_configs else "not_checked"}
        state["coverage_notes"].append("Evaluation uses archived textual evidence. Objective source-task improvements retain patches even with uncertain model judgments or observed regressions; acceptance does not establish causal repair or stability.")
        state["coverage_notes"].append("Source-task candidates are compared with the original source execution. "
            "Candidate repetitions share this one baseline and do not establish independent baseline reproducibility.")
        write_json(output / "config.json", config)
        write_json(output / "source.json", redact({"conflict": source.conflict, "context": source.context,
                                                  "diagnosis_dir": diagnosis_dir}, secrets))
        max_candidates = 1 if replay_patch is not None else config.max_revisions + 1
        generation_sessions = 0 if replay_patch is not None else max_candidates
        evaluation_sessions = len(cases) * config.repetitions * max_candidates
        fresh_baseline_runs = len(config.regression_configs) * config.repetitions
        candidate_runs = len(cases) * config.repetitions * max_candidates
        def call_limit(sessions, model, per_session):
            return 0 if sessions == 0 else None if model.time_budget_only else sessions * per_session

        def session_budget(model, timeout):
            return {
                "time_budget_only": model.time_budget_only,
                "session_timeout_sec": timeout,
                "max_rounds": None if model.time_budget_only else model.max_rounds,
                "max_tool_calls": None if model.time_budget_only else model.max_tool_calls,
                "max_report_repairs": None if model.time_budget_only else model.max_report_repairs,
                "max_input_chars": model.max_input_chars,
            }

        write_json(output / "plan.json", redact({
            "cases": [{"id": c["id"], "role": c["role"], "request": c["request"],
                       "baseline_origin": c["baseline_origin"],
                       "pack_digest": hash_manifest(tree_hashes(c["pack"]))} for c in cases],
            "source_baseline": {"run_id": source_baseline.run_id, "run_dir": source.run_dir,
                                "result_sha256": source_result_digest, "input_digest": source.context.input_digest},
            "repetitions": config.repetitions, "max_candidates": max_candidates,
            "patch_origin": "existing" if replay_patch is not None else "generated",
            "max_backend_runs": fresh_baseline_runs + candidate_runs,
            "max_fresh_baseline_runs": fresh_baseline_runs,
            "max_candidate_runs": candidate_runs,
            "max_generation_sessions": generation_sessions,
            "max_evaluation_sessions": evaluation_sessions,
            "max_generation_calls": call_limit(generation_sessions, generation,
                                               generation.max_rounds + generation.max_report_repairs),
            "max_evaluation_calls": call_limit(evaluation_sessions, evaluation,
                                               evaluation.max_rounds + evaluation.max_report_repairs),
            "max_generation_tool_calls": call_limit(generation_sessions, generation, generation.max_tool_calls),
            "max_evaluation_tool_calls": call_limit(evaluation_sessions, evaluation, evaluation.max_tool_calls),
            "model_session_timeout_sec": config.model_session_timeout_sec,
            "generation_session_timeout_sec": generation_timeout,
            "evaluation_session_timeout_sec": config.model_session_timeout_sec,
            "generation_session_budget": session_budget(generation, generation_timeout),
            "evaluation_session_budget": session_budget(evaluation, config.model_session_timeout_sec),
        }, secrets))
        if prepare_only:
            state.update(status="prepared", phase="preflight")
            return state
        models = ([] if replay_patch is not None else [generation]) + ([] if generate_only else [evaluation])
        for model in models:
            if not environment.get(model.api_key_env):
                raise ValueError("Stage 2 model credential is missing: " + model.api_key_env)
        if not generate_only:
            provider_settings = {name: environment[name] for name in (
                "DEEPSEEK_THINKING", "DEEPSEEK_BASE_URL", "LLM_BASE_URL", "OPENAI_BASE_URL"
            ) if name in environment}
            if read_object(source.run_dir / "runtime_environment.json").get("provider_settings") != provider_settings:
                raise ValueError("Stage 2 provider settings differ from the source baseline")
        baselines = {}
        budget = PatchBudget(source, config.max_revisions + 1)
        budget.acquire()
        prior_attempts = []
        for entry in budget.entries:
            prior = Path(entry["episode_dir"]) / "attempts" / f"{entry['revision']:03d}"
            evidence_path = prior / "validation.json"
            if not evidence_path.is_file():
                evidence_path = prior / "generation/generation.json"
            if evidence_path.is_file():
                prior_attempts.append(read_object(evidence_path))
        if replay_patch is None and len(budget.entries) >= budget.limit:
            state.update(status="completed", phase="finished", decision="rejected", reason="source_conflict_budget_exhausted")
            return state
        remaining = 1 if replay_patch is not None else budget.limit - len(budget.entries)
        for _ in range(remaining):
            revision = replay_patch.revision if replay_patch is not None else budget.reserve(output)
            attempt_dir = output / "attempts" / f"{revision:03d}"
            state.update(phase="generation", revision=revision)
            save()
            if replay_patch is None:
                emit(f"Stage 2: generating candidate revision {revision}")
                patch, generated = generate_patch(source, generation, attempt_dir / "generation", environment,
                                                  revision=revision, history=[*prior_attempts, *state["attempts"]], client=client,
                                                  session_timeout_sec=generation_timeout)
            else:
                emit(f"Stage 2: reuse archived patch {replay_patch.patch_id}; no generation call")
                patch = replay_patch
                generated = {"status": "candidate", "reason": "Reuse the exact archived patch; revalidate its delivery and effect."}
                write_json(attempt_dir / "generation" / "patch.json", patch)
                write_json(attempt_dir / "generation" / "generation.json", {**generated, "patch": patch,
                    "patch_origin": "existing", "source_patch_sha256": state["source_patch_sha256"],
                    "semantic_correctness": "not_verified", "original_source_digest": source.context.input_digest})
            attempt = {"revision": revision, "generation_status": generated["status"],
                       "generation_reason": generated["reason"], "patch": jsonable(patch), "cases": []}
            if replay_patch is not None:
                attempt.update(patch_origin="existing", source_patch=str(output / "source_patch.json"),
                               archived_patch_sha256=sha256_file(output / "source_patch.json"))
            state["attempts"].append(attempt)
            if patch is None:
                state.update(status="completed", decision="pending" if generated["status"] == "insufficient_evidence" else "rejected",
                             reason=generated["status"])
                break
            for case in cases:
                candidate_pack = attempt_dir / "packs" / case["id"]
                rendered = render_patch(case["pack"], patch, candidate_pack)
                diff = rendered.pop("diff")
                write_json(attempt_dir / "presentation" / (case["id"] + ".json"), rendered)
                (attempt_dir / "presentation" / (case["id"] + ".diff")).write_text(diff, encoding="utf-8")
                case["candidate_pack"] = candidate_pack
                case["presentation_mode"] = rendered["presentation_mode"]
            attempt["presentation_mode"] = cases[0]["presentation_mode"]
            state["presentation_mode"] = attempt["presentation_mode"]
            if generate_only:
                state.update(status="completed", phase="generation", decision="unvalidated", reason="generation_only")
                break
            state["phase"] = "validation"
            save()
            target_evals, source_comparisons, regression_comparisons, regression_evals = [], [], [], []
            executions_ok = True
            for case in cases:
                for repetition in range(config.repetitions):
                    key = (case["id"], repetition)
                    if key not in baselines:
                        if case["role"] == "source":
                            emit(f"Stage 2: reuse source execution {source_baseline.run_id} as baseline")
                            baselines[key] = (source_baseline, source.context, source.run_dir)
                        else:
                            emit(f"Stage 2: fresh baseline {case['id']} repetition {repetition + 1}")
                            baseline_dir = output / "baselines" / f"{case['id']}-baseline-{repetition:03d}"
                            baseline_result = Executor(case["adapter"], backend_factory(case["request"].backend)).run(
                                case["request"], baseline_dir, environment, progress=emit)
                            if baseline_result.status == "cancelled":
                                raise KeyboardInterrupt
                            baseline_context = case["adapter"].collect_evidence(baseline_dir) if _usable(baseline_result) else None
                            baselines[key] = (baseline_result, baseline_context, baseline_dir)
                    baseline_result, baseline_context, baseline_dir = baselines[key]
                    item = {"case_id": case["id"], "role": case["role"], "repetition": repetition,
                            "baseline_run_id": baseline_result.run_id, "baseline_origin": case["baseline_origin"],
                            "baseline_result_sha256": sha256_file(baseline_dir / "result.json")}
                    attempt["cases"].append(item)
                    if baseline_context is None or (case["role"] == "regression" and baseline_result.feedback.passed is not True):
                        executions_ok = False
                        item["error"] = "Baseline is unusable or the selected regression task did not pass"
                        continue
                    emit(f"Stage 2: fresh candidate {case['id']} repetition {repetition + 1}")
                    candidate_dir = attempt_dir / "runs" / f"{case['id']}-candidate-r{revision}-{repetition:03d}"
                    request = replace(case["request"], skills_dir=case["candidate_pack"])
                    candidate_result = Executor(case["adapter"], backend_factory(request.backend)).run(
                        request, candidate_dir, environment, progress=emit)
                    if candidate_result.status == "cancelled":
                        raise KeyboardInterrupt
                    item["candidate_run_id"] = candidate_result.run_id
                    if (case["role"] == "source"
                            and sha256_file(source.run_dir / "result.json") != source_result_digest):
                        executions_ok = False
                        item["error"] = "Source baseline record changed during validation"
                        continue
                    if (not _usable(candidate_result) or not _protected_inputs_equal(baseline_result, candidate_result, patch,
                                                                                   presentation_mode=case["presentation_mode"])
                            or candidate_result.prepared.skill_digest != hash_manifest(tree_hashes(case["candidate_pack"]))
                            or baseline_result.prepared.skill_digest != hash_manifest(tree_hashes(case["pack"]))):
                        executions_ok = False
                        item["error"] = "Candidate execution/evidence unavailable or protected inputs differ"
                        continue
                    if (baseline_result.execution.backend_version != candidate_result.execution.backend_version
                            or read_object(baseline_dir / "runtime_environment.json")
                            != read_object(candidate_dir / "runtime_environment.json")):
                        executions_ok = False
                        item["error"] = "Execution backend versions or recorded provider settings differ"
                        continue
                    candidate_context = case["adapter"].collect_evidence(candidate_dir)
                    # Some harnesses augment the submitted prompt after preparing
                    # task files. Check actual Q evidence, not just file hashes.
                    from skillpatch.skill_invocation import task_messages_equal, check_patch_reads
                    if not task_messages_equal(baseline_context, candidate_context, baseline_dir, candidate_dir):
                        executions_ok = False
                        item["error"] = "Actual task messages differ between baseline and candidate"
                        continue
                    if case["presentation_mode"] == "sidecar_v1":
                        read_audit = check_patch_reads(request, candidate_result.prepared,
                                                      candidate_result.execution.normalized_trajectory_path)
                        item["patch_read_audit"] = read_audit
                        write_json(candidate_dir / "patch_read_audit.json", read_audit)
                        if read_audit["status"] == "unconfirmed":
                            item["read_warning"] = "Candidate patch reading is unconfirmed; objective evaluation continues"
                    comparison = case["adapter"].compare_feedback(baseline_result.feedback, candidate_result.feedback,
                                                                  target=source.conflict if case["role"] == "source" else None)
                    try:
                        evaluated = evaluate_target(source, baseline_context, candidate_context, evaluation,
                                                    attempt_dir / "evaluation" / case["id"] / f"{repetition:03d}",
                                                    environment, client=client,
                                                    session_timeout_sec=config.model_session_timeout_sec)
                    except (ValueError, OSError, TimeoutError) as exc:
                        item["evaluation_warning"] = redact(str(exc), secrets)
                        evaluated = TargetEvaluation("uncertain", "uncertain",
                            "Evaluation unavailable; retain the objective verifier comparison.", [], [], [], "none")
                        write_json(attempt_dir / "evaluation" / case["id"] / f"{repetition:03d}" / "evaluation.json",
                                   {"evaluation": evaluated, "status": "unavailable",
                                    "error": item["evaluation_warning"], "original_input_digests": {
                                        "source": source.context.input_digest,
                                        "baseline": baseline_context.input_digest,
                                        "candidate": candidate_context.input_digest}})
                    item.update(comparison=jsonable(comparison), evaluation=jsonable(evaluated),
                                baseline_input_digest=baseline_context.input_digest,
                                candidate_input_digest=candidate_context.input_digest,
                                deployed_pack_digest=candidate_result.prepared.skill_digest)
                    if case["role"] == "source":
                        target_evals.append(evaluated)
                        source_comparisons.append(comparison)
                    else:
                        regression_comparisons.append(comparison)
                        regression_evals.append(evaluated)
            decision = decide_validation(target_evals, source_comparisons, regression_comparisons,
                                         executions_ok=executions_ok, regression_evaluations=regression_evals)
            expected_regressions = len(config.regression_configs) * config.repetitions
            coverage = {"configured_tasks": len(config.regression_configs),
                        "checked_pairs": len(regression_evals),
                        "status": "not_configured" if expected_regressions == 0 else
                                  "complete" if len(regression_evals) == expected_regressions else "incomplete"}
            state["regression_coverage"] = coverage
            attempt["regression_coverage"] = coverage
            attempt.update(decision)
            write_json(attempt_dir / "validation.json", redact(attempt, secrets))
            state.update(status="completed", **decision)
            if decision["ready_for_stage3"]:
                write_json(output / "stage3_queue.json", {"patch": patch,
                    "presentation_mode": attempt["presentation_mode"],
                    "patch_origin": "existing" if replay_patch is not None else "generated",
                    "validation_path": str(attempt_dir / "validation.json"),
                    "validation_sha256": sha256_file(attempt_dir / "validation.json"),
                    "candidate_pack": str(cases[0]["candidate_pack"]),
                    "candidate_pack_digest": hash_manifest(tree_hashes(cases[0]["candidate_pack"])),
                    "coverage_notes": state["coverage_notes"], "regression_coverage": coverage,
                    "active_version_changed": False})
                break
            actions = {e.revision_action for e in [*target_evals, *regression_evals]}
            if replay_patch is not None or decision["decision"] != "rejected" or "stop" in actions or "revisit_diagnosis" in actions or "revise_patch" not in actions:
                if "revisit_diagnosis" in actions:
                    state["next_action"] = "return_to_stage1"
                break
            state["status"] = "running" if revision < config.max_revisions else "completed"
        state["phase"] = "finished"
    except KeyboardInterrupt:
        state.update(status="cancelled", error="Stage 2 interrupted", ready_for_stage3=False)
    except Exception as exc:
        state.update(status="failed", error=redact(f"{type(exc).__name__}: {exc}", secrets), ready_for_stage3=False)
    finally:
        state["finished_at"] = now()
        if budget is not None and budget.held and isinstance(budget.entries, list):
            state["source_generation_attempts_used"] = len(budget.entries)
            state["source_generation_attempt_limit"] = budget.limit
        try:
            save()
        finally:
            if budget is not None:
                budget.release()
    return state
