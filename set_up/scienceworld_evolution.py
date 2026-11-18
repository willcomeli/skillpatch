"""Shared ScienceWorld batch generation, consolidation and Skill delivery."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
from pathlib import Path
import shutil
import tempfile

from excutor.executor_adapter.storage import json_digest, jsonable, sha256_file, tree_hashes
from excutor.excutor_runners.patch_sidecars import NOTE_EXAMPLE_SIDECAR_VERSION, write_sidecars
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from excutor.runner_common.launcher import FileSkillBank
from method.skillpatch.config import load_discovery_config
from method.skillpatch.patch_consolidation.config import load_consolidation_config
from method.skillpatch.patch_consolidation.patch_consolidation import validate_proposal
from method.skillpatch.patch_consolidation.pipeline import run_consolidation
from method.skillpatch.patch_consolidation.source import bind_target
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import load_stage2_config
from method.skillpatch.patch_generation.models import INDUCTIONS, NOTE_EXAMPLE_FORMAT, NoteExamplePatch, SkillPatch
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_generation.source import load_diagnosis, load_patch
from method_input_adapters.archive import load_archive, read_json
from set_up.native_config import load_native_config
from set_up.scienceworld_evaluation import freeze, index_tasks, variant, verified_training_sources


SKILL_KEY = "scienceworld_initial"
TERMINAL_GENERATION = {"generated", "insufficient_evidence", "unpatchable"}


def load_sources(source_plan):
    """Load a Stage 1 issue index, including the historical Stage 2 plan format."""
    plan = read_json(source_plan)
    issues = plan.get("issues") if isinstance(plan, dict) else None
    if not isinstance(issues, list) or not issues:
        raise ValueError("The source plan must list Stage 1 issues")
    archives, jobs, replay_sources = {}, {}, {}
    for row in issues:
        diagnosis = Path(row["diagnosis_dir"]).expanduser().resolve(strict=True)
        if diagnosis not in archives:
            archives[diagnosis] = load_diagnosis(diagnosis)
        archive = archives[diagnosis]
        task_id, issue_id = row["task_id"], row["issue_id"]
        key = task_id, issue_id
        source = archive.select(issue_id)
        if (archive.context.benchmark != "scienceworld" or archive.context.task_id != task_id
                or key in jobs or source.conflict.target is None
                or ("diagnosis_files" in row and row["diagnosis_files"] != archive.file_hashes)):
            raise ValueError(f"Invalid or duplicate anchored ScienceWorld issue: {key}")
        jobs[key] = archive
        if task_id not in replay_sources:
            path = archive.run_dir.resolve(strict=True)
            replay_sources[task_id] = {"task_id": task_id, "archive_path": str(path), "sha256": sha256_file(path)}
        elif Path(replay_sources[task_id]["archive_path"]) != archive.run_dir.resolve(strict=True):
            raise ValueError(f"Conflicting source trajectories for {task_id}")
    sources = list(replay_sources.values())
    if "replay_sources" in plan and plan["replay_sources"] != sources:
        raise ValueError("The frozen issue sources differ from the original trajectories")
    return jobs, sources


def next_attempt(folder, completed_statuses, *, latest_only=False):
    attempts = sorted(folder.glob("attempt-*"))
    for attempt in reversed(attempts[-1:] if latest_only else attempts):
        path = attempt / "result.json"
        if path.is_file():
            state = read_json(path)
            if state["status"] in completed_statuses:
                return attempt, state
    number = int(attempts[-1].name.split("-")[-1]) + 1 if attempts else 1
    return folder / f"attempt-{number:04d}", None


def diagnose_training(output, train_runs, config_path, evolution_config, test_manifest, evolution_env, execution_env, *,
                      workers, client=None):
    """Publish one validated Stage 1 issue index for both induction strategies."""
    config = load_native_config(config_path, execution_env)
    if config.benchmark != "scienceworld" or config.skill_path is None:
        raise ValueError("Expected a ScienceWorld execution config with the original Skill")
    base_version = sha256_file(config.skill_path)
    output = output.expanduser().resolve()
    archives = {}
    for root in train_runs:
        root = root.expanduser().resolve(strict=True)
        if output.is_relative_to(root):
            raise ValueError("Diagnosis output must be outside the training run")
        report = read_json(root / "result.json")
        paths = ([row["path"] for row in report["rows"]] if "rows" in report else report["task_results"])
        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError(f"Saved trajectory is outside its training run: {path}")
            archive = load_archive(path)
            if archive.task_id in archives and path != archives[archive.task_id]:
                raise ValueError(f"Select one frozen trajectory for {archive.task_id}")
            archives[archive.task_id] = path
    if not archives:
        raise ValueError("Select a training run containing scored trajectories")
    refs, baseline = verified_training_sources(archives, config, base_version)
    test = index_tasks(read_json(test_manifest), "test")
    if {variant(row) for row in refs.values()} & {variant(row) for row in test.values()}:
        raise ValueError("Training trajectories and test variants overlap")
    failed = [row for row in baseline.values() if not row["success"]]
    if not failed:
        raise ValueError("The selected training runs contain no failed tasks")

    discovery = load_discovery_config(evolution_config, environment=evolution_env)
    prompt_digest = sha256_file(discovery.prompt_path)
    agent = jsonable(discovery.agent)

    def diagnose_one(job):
        index, row = job
        path = Path(row["path"])
        folder = output / "tasks" / f"task-{index:04d}"
        attempt, state = next_attempt(folder, {"completed"}, latest_only=True)
        archive = None
        if state is not None:
            try:
                archive = load_diagnosis(attempt)
            except ValueError:
                # Preserve the malformed response and use a fresh attempt on resume.
                attempt, state = next_attempt(folder, set())
        if state is None:
            state = run_discovery(path, discovery, attempt, evolution_env, client=client)
            if state["status"] == "cancelled":
                raise KeyboardInterrupt
            if state["status"] != "completed":
                raise ValueError(f"Diagnosis failed for {row['task_id']}. See {attempt / 'result.json'}")
            try:
                archive = load_diagnosis(attempt)
            except ValueError as error:
                raise ValueError(f"Invalid diagnosis for {row['task_id']}: {attempt}: {error}") from error
        if (archive.run_dir.resolve() != path or archive.context.task_id != row["task_id"]
                or sha256_file(path) != row["sha256"]
                or sha256_file(attempt / "prompt.md") != prompt_digest
                or read_json(attempt / "config.json")["agent"] != agent):
            raise ValueError(f"Diagnosis differs from its frozen trajectory or settings: {attempt}")
        print(f"{row['task_id']}: diagnosed", flush=True)
        issues = [
            {"task_id": row["task_id"], "issue_id": issue_id, "diagnosis_dir": str(attempt),
             "diagnosis_files": archive.file_hashes}
            for issue_id in archive.issue_ids if archive.select(issue_id).conflict.target is not None
        ]
        source = {"task_id": row["task_id"], "archive_path": str(path), "sha256": row["sha256"]}
        return issues, source

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        freeze(output / "plan.json", {"base_skill_version": base_version, "sources": failed,
                                      "agent_digest": json_digest(agent), "prompt_sha256": prompt_digest})
        with ThreadPoolExecutor(max_workers=workers) as pool:
            diagnosed = list(pool.map(diagnose_one, enumerate(failed, 1)))
        issues = [issue for task_issues, _ in diagnosed for issue in task_issues]
        manifest = output / "issues.json"
        freeze(manifest, {"issues": issues, "replay_sources": [source for task_issues, source in diagnosed if task_issues]})
    return manifest


def generation_prompts(config, environment, induction):
    if induction == "standard":
        return {"generation": config.agent_config(environment).prompt_path}
    return config.induction_prompts(environment)


def batch_generation(root, jobs, config, environment, workers, *, induction, client=None):
    agent = jsonable(config.agent_config(environment).agent)
    prompts = {name: sha256_file(path) for name, path in generation_prompts(config, environment, induction).items()}
    patch_type = NoteExamplePatch if induction == "boundary-aware" else SkillPatch

    def generate(job):
        index, ((task_id, issue_id), archive) = job
        folder = root / f"issue-{index:04d}"
        attempt, state = next_attempt(folder, TERMINAL_GENERATION)
        if state is None:
            state = run_stage2(archive, issue_id, config, attempt, environment, induction=induction, client=client)
        status = state["status"]
        print(f"{task_id} {issue_id}: {status}", flush=True)
        if status == "cancelled":
            raise KeyboardInterrupt
        if status not in TERMINAL_GENERATION:
            raise ValueError(f"Generation failed for {task_id} {issue_id}. See {attempt / 'result.json'}")
        if (state.get("induction", "standard") != induction or state.get("issue_id") != issue_id
                or state.get("diagnosis_dir") != str(archive.diagnosis_dir)
                or state.get("source_input_digest") != archive.context.input_digest):
            raise ValueError(f"Generation archive differs from its planned issue: {attempt}")

        if induction == "standard":
            sessions = [(attempt / "generation", prompts["generation"])]
        else:
            sessions = [(attempt / "boundary_induction/initial", prompts["initial"])]
            if status == "generated" or state["revision"] == 1:
                sessions.append((attempt / "boundary_induction/cases", prompts["cases"]))
            if state["revision"] == 1:
                sessions.append((attempt / "generation", prompts["initial"]))
        for session, digest in sessions:
            if (sha256_file(session / "prompt.md") != digest
                    or read_json(session / "config.json")["agent"] != agent):
                raise ValueError(f"Generation session differs from the planned prompt or model: {session}")
        if status != "generated":
            return None
        path = (attempt / "generation/patch.json").resolve(strict=True)
        patch, source, _ = load_patch(path)
        if (not isinstance(patch, patch_type) or source.context.task_id != task_id
                or patch.conflict_id != issue_id):
            raise ValueError(f"Expected a {induction} patch for {task_id}: {path}")
        return {"task_id": task_id, "issue_id": issue_id, "diagnosis_dir": str(archive.diagnosis_dir),
                "patch_id": patch.patch_id, "patch_path": str(path)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(generate, enumerate(jobs.items(), 1)))
    candidates = [row for row in rows if row is not None]
    if not candidates:
        raise ValueError(f"Stage 2 produced no supported {induction} candidates")
    if len({row["patch_id"] for row in candidates}) != len(candidates):
        raise ValueError("Duplicate Stage 2 patch IDs")
    manifest = root / "candidates.json"
    freeze(manifest, candidates)
    return manifest, candidates


def batch_consolidation(root, manifest, candidates, replay_sources, base_skill, config, environment, workers, *,
                        induction, client=None):
    patch_format = NOTE_EXAMPLE_FORMAT if induction == "boundary-aware" else None
    fields = ("note", "example") if patch_format else ("when_to_use", "how_to_interpret")
    patch_type = NoteExamplePatch if patch_format else SkillPatch
    schema = NOTE_EXAMPLE_SIDECAR_VERSION if patch_format else 1
    version = sha256_file(base_skill)
    base_text = base_skill.read_text(encoding="utf-8")
    groups, patches = {}, {}
    proposal_config = config.proposal_config(environment, patch_format=patch_format)
    agent = jsonable(proposal_config.agent)
    prompt_hash = sha256_file(proposal_config.prompt_path)
    for row in candidates:
        patch, source, _ = load_patch(Path(row["patch_path"]))
        binding = bind_target(patch, source, SKILL_KEY)
        quote = binding["quote"]
        if (not isinstance(patch, patch_type) or patch.patch_id != row["patch_id"]
                or patch.target.version != version or binding["relative_path"] != "SKILL.md"
                or base_text.count(quote) != 1):
            raise ValueError(f"Patch target differs from the original ScienceWorld Skill: {patch.patch_id}")
        patches[patch.patch_id] = patch
        group = groups.setdefault(binding["target_id"], {
            "line": base_text.count("\n", 0, base_text.index(quote)) + 1,
            "binding": binding, "candidates": [],
        })
        group["candidates"].append(row)
    ordered = sorted(groups.values(), key=lambda group: (group["line"], group["binding"]["target_id"]))
    plan_path = root / "plan.json"
    freeze(plan_path, {"stage2_manifest": str(manifest), "stage2_manifest_sha256": sha256_file(manifest),
                       "base_skill": str(base_skill), "base_skill_version": version, "skill_key": SKILL_KEY,
                       "replay_sources": replay_sources, "groups": ordered})

    def select(job):
        index, group = job
        rows, binding = group["candidates"], group["binding"]
        ids = [row["patch_id"] for row in rows]
        paths = [row["patch_path"] for row in rows]
        selection = {"line": group["line"], "target_id": binding["target_id"],
                     "source_patch_ids": ids, "source_patch_paths": paths}
        target = {name: binding[name] for name in ("relative_path", "scope", "quote")}
        target["version"] = version
        action = "singleton"
        proposal = None
        if len(rows) > 1:
            folder = root / "groups" / f"group-{index:04d}"
            attempt, state = next_attempt(folder, {"proposed"})
            if state is None:
                state = run_consolidation([Path(path) for path in paths[:-1]], Path(paths[-1]),
                                          SKILL_KEY, config, attempt, environment, client=client)
            if state["status"] == "cancelled":
                raise KeyboardInterrupt
            if state["status"] != "proposed":
                raise ValueError(f"Consolidation failed. See {attempt / 'result.json'}")
            proposal_path = attempt / "proposal/proposal.json"
            record = read_json(proposal_path)
            proposal = validate_proposal(record["proposal"], patch_format=patch_format)
            action = proposal["action"]
            if (record.get("patch_format") != patch_format or record["target_binding"] != binding
                    or [item["patch"]["patch_id"] for item in record["source_patches"]] != ids
                    or proposal != read_json(attempt / "proposal/response.json") or action != state["action"]
                    or sha256_file(attempt / "proposal/prompt.md") != prompt_hash
                    or read_json(attempt / "proposal/config.json")["agent"] != agent):
                raise ValueError(f"Consolidation archive differs from its planned source group: {attempt}")
            selection["proposal_path"] = str(proposal_path)
        if action == "merge":
            target_key = "target_id" if patch_format else "target"
            identity = json_digest({target_key: binding["target_id"], "sources": ids, "proposal": proposal})
            entries = [{"entry_id": "M" + identity.split(":")[1][:16], "target": target,
                        **{name: proposal[name] for name in fields}}]
        else:
            entries = [{"entry_id": patch_id, "target": target,
                        **{name: getattr(patches[patch_id], name) for name in fields}} for patch_id in ids]
        selection.update(action=action, entry_ids=[entry["entry_id"] for entry in entries])
        print(f"Stage 3 line {group['line']}: {action} ({len(ids)} candidates)", flush=True)
        return selection, entries

    with ThreadPoolExecutor(max_workers=workers) as pool:
        selected = list(pool.map(select, enumerate(ordered, 1)))
    selections = [selection for selection, _ in selected]
    entries = [entry for _, group_entries in selected for entry in group_entries]
    result_path = root / "result.json"
    freeze(result_path, {"status": "completed", "groups": selections, "entry_count": len(entries)})

    destination = root / "candidate_skill"
    with tempfile.TemporaryDirectory(prefix=".build-", dir=root) as temporary:
        bundle = Path(temporary) / "candidate_skill"
        bundle.mkdir()
        shutil.copyfile(base_skill, bundle / "SKILL.md")
        write_sidecars(bundle, entries, schema_version=schema)
        PatchedSkillBank(FileSkillBank(base_skill), bundle).format_for_prompt({})
        hashes = tree_hashes(bundle)
        if destination.exists():
            if tree_hashes(destination) != hashes:
                raise ValueError(f"The saved Skill bundle differs: {destination}")
        else:
            bundle.rename(destination)

    freeze(root / "bundle_receipt.json", {
        "status": "candidate", "candidate_skill": str(destination), "base_skill_version": version,
        "stage3_plan_sha256": sha256_file(plan_path), "stage3_result_sha256": sha256_file(result_path),
        "entry_count": len(entries), "source_candidate_count": len(candidates),
        "file_hashes": hashes, "selections": selections,
    })


def workflow_plan(source_plan, jobs, replay_sources, base_skill, stage2, stage3, environment, induction):
    patch_format = NOTE_EXAMPLE_FORMAT if induction == "boundary-aware" else None
    prompts = (*generation_prompts(stage2, environment, induction).values(),
               stage3.proposal_config(environment, patch_format=patch_format).prompt_path)
    return {
        "induction": induction, "source_plan": str(source_plan), "base_skill_version": sha256_file(base_skill),
        "issues": [{"task_id": task_id, "issue_id": issue_id, "diagnosis_dir": str(archive.diagnosis_dir),
                    "diagnosis_files": archive.file_hashes} for (task_id, issue_id), archive in jobs.items()],
        "replay_sources": replay_sources,
        "stage2_agent_digest": json_digest(jsonable(stage2.agent_config(environment).agent)),
        "stage3_agent_digest": json_digest(jsonable(stage3.agent_config(environment).agent)),
        "prompt_hashes": {path.name: sha256_file(path) for path in prompts},
    }


def evolve(output, source_plan, config_path, evolution_config, test_manifest, evolution_env, execution_env, *,
           induction, workers, client=None):
    """Generate and package candidate patches from one shared diagnosis index."""
    if induction not in INDUCTIONS:
        raise ValueError(f"Unknown induction strategy: {induction}")
    output = output.expanduser().resolve()
    source_plan = source_plan.expanduser().resolve(strict=True)
    jobs, replay_sources = load_sources(source_plan)
    config = load_native_config(config_path, execution_env)
    if config.benchmark != "scienceworld" or config.skill_path is None:
        raise ValueError("Expected a ScienceWorld execution config with the original Skill")
    base_version = sha256_file(config.skill_path)
    archives = {row["task_id"]: Path(row["archive_path"]) for row in replay_sources}
    refs, baseline = verified_training_sources(archives, config, base_version)
    test = index_tasks(read_json(test_manifest), "test")
    if any(row["success"] for row in baseline.values()):
        raise ValueError("The issue index must refer to failed training tasks")
    if {variant(row) for row in refs.values()} & {variant(row) for row in test.values()}:
        raise ValueError("Training trajectories and test variants overlap")
    if any(output.is_relative_to(archive.diagnosis_dir) or output.is_relative_to(archive.run_dir.parent.parent)
           for archive in jobs.values()):
        raise ValueError("Evolution output must be outside source diagnosis and execution archives")

    stage2 = load_stage2_config(evolution_config, environment=evolution_env)
    stage3 = load_consolidation_config(evolution_config, environment=evolution_env)
    plan_path = output / "plan.json"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = workflow_plan(source_plan, jobs, replay_sources, config.skill_path,
                             stage2, stage3, evolution_env, induction)
        # The first Boundary-Aware release misspelled the proposer filename.
        # Reuse its frozen plan only when the file content digest still matches.
        if plan_path.is_file():
            saved = read_json(plan_path)
            old_name, new_name = "boudary_test_proposer.md", "boundary_test_proposer.md"
            expected = plan["prompt_hashes"]
            if (new_name in expected and saved.get("prompt_hashes", {}).get(old_name) == expected[new_name]
                    and new_name not in saved["prompt_hashes"]):
                expected[old_name] = expected.pop(new_name)
        freeze(plan_path, plan)
        manifest, candidates = batch_generation(output / "stage2", jobs, stage2, evolution_env,
                                                workers, induction=induction, client=client)
        consolidation = output / "stage3"
        batch_consolidation(consolidation, manifest, candidates, replay_sources, config.skill_path, stage3,
                            evolution_env, workers, induction=induction, client=client)
    return consolidation
