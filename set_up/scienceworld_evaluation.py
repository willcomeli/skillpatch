"""Resumable ScienceWorld replay and paired test execution."""

import argparse
from dataclasses import replace
import fcntl
from pathlib import Path
import shutil
import sys
import tempfile

from excutor.executor_adapter.storage import json_digest, jsonable, sha256_file, tree_hashes, write_json
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from excutor.runner_common.launcher import FileSkillBank, run_native
from method.skillpatch.patch_generation.source import load_patch
from method_input_adapters.archive import load_archive, read_json
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/scienceworld"
TEST_TASKS = ROOT / "data/scienceworld/test/items.json"


def index_tasks(rows, split):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"Expected a nonempty {split} task list")
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or row.get("split") != split
                or not isinstance(row.get("task_id"), str) or not row["task_id"]
                or not isinstance(row.get("task"), str) or not row["task"]
                or type(row.get("variation_idx")) is not int
                or not isinstance(row.get("simplification"), str)):
            raise ValueError(f"Expected complete ScienceWorld {split} task references")
        task_id = row["task_id"]
        if task_id in result:
            raise ValueError(f"Duplicate {split} task: {task_id}")
        result[task_id] = row
    return result


def variant(ref):
    return ref["task"], ref["variation_idx"], ref["simplification"]


def execution_settings(config):
    values = jsonable(config)
    settings = {key: value for key, value in values.items()
                if key not in {"tasks_path", "skill_path", "output_root"}}
    settings.setdefault("max_retries", 0)
    settings["runner_options"] = {key: value for key, value in settings["runner_options"].items()
                                  if key != "server_port_start"}
    return settings


def freeze(path, value):
    if path.exists():
        if read_json(path) != value:
            raise ValueError(f"Frozen evaluation input changed: {path}")
    else:
        write_json(path, value)


def score_row(path, archive):
    return {"task_id": archive.task_id, "success": archive.success,
            "score": archive.score, "path": str(path)}


def replay_inputs(stage3, config, test, base_version):
    receipt = read_json(stage3 / "bundle_receipt.json")
    plan_path = stage3 / "plan.json"
    plan = read_json(plan_path)
    bundle = stage3 / "candidate_skill"
    if (sha256_file(plan_path) != receipt["stage3_plan_sha256"]
            or sha256_file(stage3 / "result.json") != receipt["stage3_result_sha256"]
            or tree_hashes(bundle) != receipt["file_hashes"]
            or base_version != receipt["base_skill_version"]):
        raise ValueError("The Stage 3 inputs or candidate bundle differ from the bundle receipt")
    manifest = Path(plan["stage2_manifest"])
    if sha256_file(manifest) != plan["stage2_manifest_sha256"]:
        raise ValueError("The Stage 2 candidate index changed")
    candidates = read_json(manifest)
    source_paths = {Path(path).resolve() for item in receipt["selections"] for path in item["source_patch_paths"]}
    if source_paths != {Path(row["patch_path"]).resolve() for row in candidates}:
        raise ValueError("The candidate index differs from the bundle's recorded sources")

    archived_sources = {}
    fixed_replay = "replay_sources" in plan
    if fixed_replay:
        for row in plan["replay_sources"]:
            task_id = row["task_id"]
            archive_path = Path(row["archive_path"]).resolve(strict=True)
            if task_id in archived_sources or sha256_file(archive_path) != row["sha256"]:
                raise ValueError("The frozen replay sources are duplicate or have changed")
            archived_sources[task_id] = archive_path

    for row in candidates:
        patch, source, _ = load_patch(Path(row["patch_path"]))
        task_id = source.context.task_id
        archive_path = source.run_dir.resolve(strict=True)
        if (task_id, patch.patch_id, patch.conflict_id) != (row["task_id"], row["patch_id"], row["issue_id"]):
            raise ValueError("The candidate index differs from its patch archive")
        if fixed_replay and archived_sources.get(task_id) != archive_path:
            raise ValueError(f"Candidate source is outside the frozen replay cohort: {task_id}")
        if archived_sources.setdefault(task_id, archive_path) != archive_path:
            raise ValueError(f"Conflicting historical results for {task_id}")

    replay, baseline = verified_training_sources(archived_sources, config, base_version)
    if not replay or {variant(row) for row in replay.values()} & {variant(row) for row in test.values()}:
        raise ValueError("Expected replay tasks disjoint from the test variants")
    return bundle, replay, baseline


def verified_training_sources(archives, config, base_version):
    """Bind source scores to the original Skill, execution settings and train variants."""
    current_settings = execution_settings(config)
    refs, baseline, source_tasks = {}, {}, {}
    for task_id, archive_path in archives.items():
        source_run = archive_path.parent.parent
        if archive_path.parent.name != "tasks":
            raise ValueError(f"Expected a native training task archive: {archive_path}")
        if source_run not in source_tasks:
            if (execution_settings(read_json(source_run / "config.json")) != current_settings
                    or sha256_file(source_run / "input/SKILL.md") != base_version):
                raise ValueError(f"Historical execution settings or Skill differ: {source_run}")
            source_tasks[source_run] = index_tasks(read_json(source_run / "input/tasks.json"), "train")
        ref = source_tasks[source_run].get(task_id)
        archive = load_archive(archive_path)
        if (ref is None or archive.benchmark != "scienceworld" or archive.task_id != task_id
                or variant(read_json(archive_path)) != variant(ref)):
            raise ValueError(f"The historical result does not match the archived train variant: {task_id}")
        refs[task_id] = ref
        baseline[task_id] = {**score_row(archive_path, archive), "sha256": sha256_file(archive_path)}
    return refs, baseline


def metrics(rows):
    count = len(rows)
    success = sum(row["success"] for row in rows)
    return {"count": count, "success": success, "success_rate": success / count if count else None,
            "mean_score": sum(row["score"] for row in rows) / count if count else None}


def compare(tasks, initial, patched):
    paired = [task_id for task_id in tasks if task_id in initial and task_id in patched]
    before = metrics([initial[task_id] for task_id in paired])
    after = metrics([patched[task_id] for task_id in paired])
    return {
        "status": "completed" if len(paired) == len(tasks) else "incomplete",
        "expected": len(tasks), "paired": len(paired),
        "initial_missing": [task_id for task_id in tasks if task_id not in initial],
        "patched_missing": [task_id for task_id in tasks if task_id not in patched],
        "initial": before, "patched": after,
        "mean_score_delta": after["mean_score"] - before["mean_score"] if paired else None,
        "success_rate_delta": after["success_rate"] - before["success_rate"] if paired else None,
        "tasks": [{"task_id": task_id, "initial": initial[task_id], "patched": patched[task_id],
                   "score_delta": patched[task_id]["score"] - initial[task_id]["score"]}
                  for task_id in paired],
    }


def attempt_scores(run, tasks, expected_skill, patch_digest):
    paths = sorted(run.glob("tasks/task_*.json"))
    if not paths:
        return {}
    planned = read_json(run / "input/tasks.json")
    scores = {}
    for path in paths:
        index = int(path.stem.split("_")[-1]) - 1
        if not 0 <= index < len(planned):
            raise ValueError(f"Saved result index is outside the attempt manifest: {path}")
        try:
            archive = load_archive(path)
            raw = read_json(path)
        except (OSError, ValueError):
            # Unscorable executions stay pending.
            continue
        ref = tasks.get(archive.task_id)
        deployment = raw.get("patch_deployment") or {}
        if (archive.benchmark != "scienceworld" or ref is None or planned[index] != ref
                or variant(raw) != variant(ref) or raw.get("retrieved_memory_text") != expected_skill
                or deployment.get("skill_digest") != patch_digest):
            raise ValueError(f"Saved task does not match the planned variant or Skill: {path}")
        scores[archive.task_id] = score_row(path, archive)
    return scores


def run_phase(phase, task_file, tasks, config, environment, *, attempts, prepare_only,
              bundle=None, baseline=None):
    phase.mkdir(parents=True, exist_ok=True)
    bank = FileSkillBank(config.skill_path)
    if bundle is not None:
        bank = PatchedSkillBank(bank, bundle)
    expected_skill = bank.format_for_prompt({})
    patch_digest = bank.digest if bundle is not None else None
    if prepare_only:
        output = phase / "prepare"
        if output.exists():
            state = read_json(output / "result.json")
        else:
            state = run_native(replace(config, tasks_path=task_file), output, environment,
                               patch_skill_dir=bundle, prepare_only=True)
        if (state["status"] != "prepared" or read_json(output / "input/tasks.json") != list(tasks.values())
                or sha256_file(output / "input/SKILL.md") != sha256_file(config.skill_path)
                or bundle is not None and tree_hashes(output / "input/patched_skill") != tree_hashes(bundle)):
            raise ValueError(f"Preparation failed: {output / 'result.json'}")
        print(f"{phase.parent.name}/{phase.name}: prepared {len(tasks)} tasks", flush=True)
        return "prepared"

    previous = sorted(phase.glob("attempt-*"))
    scores = {}
    for attempt in previous:
        for task_id, row in attempt_scores(attempt, tasks, expected_skill, patch_digest).items():
            scores.setdefault(task_id, row)
    next_attempt = max((int(path.name.split("-")[-1]) for path in previous), default=0) + 1
    last_run = previous[-1] / "result.json" if previous else None

    def save(status):
        report = {"status": status, "expected": len(tasks), "summary": metrics(list(scores.values())),
                  "missing": [task_id for task_id in tasks if task_id not in scores],
                  "rows": [scores[task_id] for task_id in tasks if task_id in scores],
                  "last_run": str(last_run) if last_run else None}
        if baseline is not None:
            report["comparison"] = compare(tasks, baseline, scores)
        write_json(phase / "result.json", report)
        print(f"{phase.parent.name}/{phase.name}: {status} {len(scores)}/{len(tasks)} scored", flush=True)
        return status

    postponed = None
    for number in range(next_attempt, next_attempt + attempts):
        missing = [row for task_id, row in tasks.items() if task_id not in scores]
        if not missing:
            break
        if postponed:
            missing.sort(key=lambda row: row["task_id"] == postponed)
        manifest = task_file
        if missing != list(tasks.values()):
            manifest = phase / "pending_tasks.json"
            write_json(manifest, missing)
        attempt = phase / f"attempt-{number:04d}"
        print(f"{phase.parent.name}/{phase.name}: attempt {number}, {len(missing)} tasks", flush=True)
        state = run_native(replace(config, tasks_path=manifest), attempt, environment,
                           patch_skill_dir=bundle, skip_model_budget_errors=True)
        last_run = attempt / "result.json"
        for task_id, row in attempt_scores(attempt, tasks, expected_skill, patch_digest).items():
            scores.setdefault(task_id, row)
        status = state["status"]
        if status == "cancelled":
            return save("cancelled")
        if status == "backend_error" and "model_requests" not in state:
            break
        if status == "artifact_error":
            break
        postponed = None
        if status == "backend_error":
            processed = len(state["task_results"]) + len(state.get("skipped_tasks", []))
            if processed < len(missing):
                postponed = missing[processed]["task_id"]
        if len(scores) < len(tasks):
            save("running")
    return save("completed" if len(scores) == len(tasks) else "incomplete")


def publish_test_comparison(output, tasks):
    with (output / ".test.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        paths = [output / variant_name / "test/result.json" for variant_name in ("initial", "patched")]
        if all(path.is_file() for path in paths):
            initial, patched = ({row["task_id"]: row for row in read_json(path)["rows"]} for path in paths)
            write_json(output / "test_comparison.json", compare(tasks, initial, patched))


def execution_plan(config, tasks, environment, skill_version):
    settings = execution_settings(config)
    settings["base_url"] = config.base_url or environment.get(config.base_url_env)
    settings["stream"] = environment.get("EXECUTION_STREAM", "false").lower() in {"true", "1", "yes"}
    return {"model": config.model, "execution_digest": json_digest(settings),
            "base_skill_version": skill_version, "task_digest": json_digest(list(tasks.values()))}


def freeze_skill(source, destination, version):
    if not destination.exists():
        staged = destination.with_suffix(".tmp")
        shutil.copyfile(source, staged)
        if sha256_file(staged) != version:
            raise ValueError("Initial Skill changed during preparation")
        staged.replace(destination)
    if sha256_file(destination) != version:
        raise ValueError("The frozen initial Skill differs from the execution plan")


def collect_train(args, environment):
    """Collect or resume the unpatched training archive with the shared runner."""
    config = load_native_config(args.config, environment)
    if config.benchmark != "scienceworld" or config.skill_path is None:
        raise ValueError("Expected a ScienceWorld execution config with the original Skill")
    manifest = args.train_manifest or config.tasks_path
    tasks = index_tasks(read_json(manifest), "train")
    base_version = sha256_file(config.skill_path)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    skill = output / "SKILL.md"
    task_file = output / "train_tasks.json"
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        freeze(output / "train_plan.json", execution_plan(config, tasks, environment, base_version))
        freeze(task_file, list(tasks.values()))
        freeze_skill(config.skill_path, skill, base_version)
        config = replace(config, skill_path=skill,
                         runner_options={**config.runner_options, "server_port_start": args.server_port_start})
        status = run_phase(output, task_file, tasks, config, environment, attempts=args.attempts,
                           prepare_only=args.prepare_only)
    return 130 if status == "cancelled" else 0 if status in {"completed", "prepared"} else 2


def evaluate(args, variant_name, environment, *, phases=None):
    output = args.output.expanduser().resolve()
    config = load_native_config(args.config, environment)
    if config.benchmark != "scienceworld" or config.skill_path is None:
        raise ValueError("Expected a ScienceWorld execution config with the initial Skill")
    test = index_tasks(read_json(args.test_manifest), "test")
    base_version = sha256_file(config.skill_path)
    test_plan = execution_plan(config, test, environment, base_version)
    phases = tuple(phases) if phases is not None else (
        ("replay", "test") if variant_name == "patched" else ("test",)
    )
    if not phases or set(phases) - {"replay", "test"} or ("replay" in phases and variant_name != "patched"):
        raise ValueError("Replay requires a Stage 3 bundle")
    bundle = replay = baseline = None
    if variant_name == "patched":
        stage3 = args.stage3_run.expanduser().resolve(strict=True)
        bundle, replay, baseline = replay_inputs(stage3, config, test, base_version)
        if output.is_relative_to(bundle):
            raise ValueError("Evaluation output must be outside the candidate Skill directory")

    output.mkdir(parents=True, exist_ok=True)
    test_file = output / "test_tasks.json"
    initial_skill = output / "SKILL.md"
    with (output / ".test.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        freeze(output / "test_plan.json", test_plan)
        freeze(test_file, list(test.values()))
        freeze_skill(config.skill_path, initial_skill, base_version)

    work = output / variant_name
    work.mkdir(exist_ok=True)
    with (work / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = replace(config, skill_path=initial_skill,
                         runner_options={**config.runner_options, "server_port_start": args.server_port_start})
        statuses = []
        if bundle is not None:
            hashes = tree_hashes(bundle)
            if hashes != read_json(stage3 / "bundle_receipt.json")["file_hashes"]:
                raise ValueError("The candidate Skill changed during preparation")
            freeze(work / "replay_plan.json", {"candidate_skill": str(bundle),
                                             "candidate_hashes": hashes, "baseline": baseline})
            frozen_bundle = work / "candidate_skill"
            if not frozen_bundle.exists():
                with tempfile.TemporaryDirectory(prefix=".skill-", dir=work) as tmp:
                    staged = Path(tmp) / "candidate_skill"
                    shutil.copytree(bundle, staged)
                    if tree_hashes(staged) != hashes:
                        raise ValueError("Candidate Skill changed while copying")
                    staged.rename(frozen_bundle)
            if tree_hashes(frozen_bundle) != hashes:
                raise ValueError("The frozen candidate Skill differs from the evaluation plan")
            bundle = frozen_bundle
        for phase in phases:
            if phase == "replay":
                replay_file = work / "replay_tasks.json"
                freeze(replay_file, list(replay.values()))
                status = run_phase(work / phase, replay_file, replay, config, environment,
                                   bundle=bundle, baseline=baseline, attempts=args.attempts,
                                   prepare_only=args.prepare_only)
            else:
                status = run_phase(work / phase, test_file, test, config, environment, bundle=bundle,
                                   attempts=args.attempts, prepare_only=args.prepare_only)
            if status == "cancelled":
                return 130
            statuses.append(status)
        if "test" in phases and not args.prepare_only:
            publish_test_comparison(output, test)
        return 0 if all(status in {"prepared", "completed"} for status in statuses) else 2


def compare_test_runs(runs, output):
    """Compare all supplied methods on their shared, identically configured test tasks."""
    if len(runs) < 2:
        raise ValueError("Select at least two completed or partial test runs")
    test_plan = None
    tasks = None
    results = {}
    for name, (folder, variant_name) in runs.items():
        plan = read_json(folder / "test_plan.json")
        refs = index_tasks(read_json(folder / "test_tasks.json"), "test")
        if test_plan is None:
            test_plan, tasks = plan, refs
        elif plan != test_plan or refs != tasks:
            raise ValueError(f"Test tasks or execution settings differ for {name}")
        result = read_json(folder / variant_name / "test/result.json")
        rows = {row["task_id"]: row for row in result["rows"]}
        if len(rows) != len(result["rows"]) or not rows.keys() <= tasks.keys():
            raise ValueError(f"Invalid or duplicate test results for {name}")
        results[name] = rows

    paired = [task_id for task_id in tasks if all(task_id in rows for rows in results.values())]
    summary = {name: metrics([rows[task_id] for task_id in paired]) for name, rows in results.items()}
    report = {
        "status": "completed" if len(paired) == len(tasks) else "incomplete",
        "expected": len(tasks), "paired": len(paired), "test_plan": test_plan,
        "methods": summary,
        "missing": {name: [task_id for task_id in tasks if task_id not in rows] for name, rows in results.items()},
        "tasks": [{"task_id": task_id, "methods": {name: rows[task_id] for name, rows in results.items()}}
                  for task_id in paired],
    }
    write_json(output, report)
    return report


def main(variant_name, description, argv=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--output", type=Path, required=True, help="Evaluation root shared by both scripts")
    parser.add_argument("--config", type=Path, default=CONFIG / "execution.toml")
    parser.add_argument("--env-file", type=Path, default=CONFIG / "scienceworld.env")
    parser.add_argument("--test-manifest", type=Path, default=TEST_TASKS)
    parser.add_argument("--server-port-start", type=int, default=8911 if variant_name == "patched" else 8811)
    parser.add_argument("--attempts", type=int, default=3, help="Maximum new execution attempts per phase and invocation")
    parser.add_argument("--prepare-only", action="store_true")
    if variant_name == "patched":
        parser.add_argument("--stage3-run", type=Path, required=True, help="Completed Stage 3 batch with candidate_skill")
    args = parser.parse_args(argv)
    if args.attempts < 1 or not 1 <= args.server_port_start <= 65535:
        parser.error("--attempts must be positive and --server-port-start must be a valid port")
    environment = {}
    try:
        environment = load_environment(args.env_file)
        return evaluate(args, variant_name, environment)
    except KeyboardInterrupt:
        return 130
    except BlockingIOError:
        print(f"{variant_name} evaluation is already running in {args.output}", file=sys.stderr)
    except (OSError, ValueError) as error:
        print(redact(str(error), secret_values(environment)), file=sys.stderr)
    return 2
