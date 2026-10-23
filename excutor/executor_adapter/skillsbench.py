"""Snapshot native SkillsBench tasks and read their official verifier feedback."""

from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
import stat
from typing import Any

from .models import BackendRun, EvaluationFeedback, PreparedTask, RunRequest
from .storage import hash_manifest, sha256_file, write_json


def _tree_state(root: Path) -> tuple[dict[str, str], set[str]]:
    """Reject links and special files before hashing any regular files."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Snapshot source must be a real directory, not a symlink: {root}")
    entries = sorted(root.rglob("*"))
    files: list[Path] = []
    directories: set[str] = set()
    for entry in entries:
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlinks are not supported in task or skill snapshots: {entry}")
        if stat.S_ISDIR(mode):
            directories.add(entry.relative_to(root).as_posix())
        elif stat.S_ISREG(mode):
            files.append(entry)
        else:
            raise ValueError(f"Special files are not supported in snapshots: {entry}")
    return ({file.relative_to(root).as_posix(): sha256_file(file) for file in files}, directories)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("reward must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("reward must be a finite number")
    return number


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not supported: {value}")


def _serializable_metrics(value: Any, issues: list[str], location: str = "rewards") -> Any:
    """Keep malformed numeric evidence readable without emitting invalid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        description = f"<non-finite float: {value}>"
        issues.append(f"Non-finite metric at {location}: {description}")
        return description
    if isinstance(value, dict):
        return {key: _serializable_metrics(item, issues, f"{location}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_serializable_metrics(item, issues, f"{location}[{index}]") for index, item in enumerate(value)]
    return deepcopy(value)


class SkillsBenchExecutionAdapter:
    def prepare(self, request: RunRequest, destination: Path) -> PreparedTask:
        if request.benchmark != "skillsbench":
            raise ValueError(f"Unsupported benchmark: {request.benchmark}")
        if request.skill_mode not in {"with-skill", "no-skill"}:
            raise ValueError(f"Unsupported skill mode: {request.skill_mode}")

        source = request.task_dir.expanduser().absolute()
        source_before = _tree_state(source)
        source = source.resolve()
        for required in ("task.md", "environment/Dockerfile", "verifier/test.sh"):
            if required not in source_before[0]:
                raise ValueError(f"SkillsBench task is missing required file: {source / required}")

        target_root = destination.expanduser().absolute()
        resolved_target = target_root.resolve()
        if resolved_target.is_relative_to(source):
            raise ValueError("Snapshot destination must not be inside the task source")
        if target_root.exists() or target_root.is_symlink():
            raise ValueError(f"Snapshot destination must not already exist: {target_root}")

        skill_source = source / "environment" / "skills"
        external_before = None
        if request.skills_dir is not None:
            skill_source = request.skills_dir.expanduser().absolute()
            external_before = _tree_state(skill_source)
            skill_source = skill_source.resolve()
            if resolved_target.is_relative_to(skill_source):
                raise ValueError("Snapshot destination must not be inside the external skills source")
        skill_before = external_before
        if skill_before is None and skill_source.is_dir():
            skill_before = _tree_state(skill_source)
        if request.skill_mode == "with-skill":
            if skill_before is None or not any(
                len(Path(name).parts) == 2 and Path(name).name == "SKILL.md"
                for name in skill_before[0]
            ):
                raise ValueError(
                    "with-skill requires a discoverable skill pack with "
                    f"<name>/SKILL.md directly inside the skills directory: {skill_source}"
                )

        target_root.mkdir(parents=True)
        task_dir = target_root / "tasks" / source.name
        # copy2/copytree make independent files. Copying links as links ensures a
        # link introduced during copying is rejected by the final tree check.
        shutil.copytree(source, task_dir, symlinks=True)
        if _tree_state(source) != source_before or _tree_state(task_dir) != source_before:
            raise ValueError("Task source changed while preparing its snapshot; discard this run")

        prepared_skills = task_dir / "environment" / "skills"
        if external_before is not None:
            if prepared_skills.exists():
                if prepared_skills.is_dir():
                    shutil.rmtree(prepared_skills)
                else:
                    prepared_skills.unlink()
            shutil.copytree(skill_source, prepared_skills, symlinks=True)
            if _tree_state(skill_source) != external_before or _tree_state(prepared_skills) != external_before:
                raise ValueError("External skills source changed while preparing its snapshot; discard this run")

        file_hashes, _ = _tree_state(task_dir)
        if _tree_state(source) != source_before:
            raise ValueError("Task source changed while preparing its snapshot; discard this run")
        if external_before is not None and _tree_state(skill_source) != external_before:
            raise ValueError("External skills source changed while preparing its snapshot; discard this run")
        task_digest = hash_manifest(file_hashes)
        skill_hashes = _tree_state(prepared_skills)[0] if prepared_skills.is_dir() else {}
        requested_deployment = request.skill_mode == "with-skill"
        skill_digest = hash_manifest(skill_hashes) if requested_deployment else None
        manifest_path = target_root / "manifest.json"
        write_json(manifest_path, {
            "schema_version": 1,
            "benchmark": "skillsbench",
            "task_id": source.name,
            "source": {"task_dir": source, "file_hashes": source_before[0],
                       "task_digest": hash_manifest(source_before[0])},
            "prepared": {"task_dir": task_dir, "file_hashes": file_hashes,
                         "task_digest": task_digest},
            "skills": {
                "source": "external" if external_before is not None else "task_bundled",
                "source_dir": skill_source if skill_before is not None else None,
                "source_file_hashes": skill_before[0] if skill_before is not None else {},
                "source_digest": hash_manifest(skill_before[0]) if skill_before is not None else None,
                "skill_mode": request.skill_mode,
                "skill_condition": request.skill_condition,
                "requested_deployment": requested_deployment,
                "prepared_dir": prepared_skills if prepared_skills.is_dir() else None,
                "skill_digest": skill_digest,
            },
        })
        return PreparedTask(
            benchmark="skillsbench", task_id=source.name, task_dir=task_dir,
            task_document=task_dir / "task.md",
            skills_dir=prepared_skills if requested_deployment else None,
            manifest_path=manifest_path, task_digest=task_digest,
            skill_digest=skill_digest, file_hashes=file_hashes,
        )

    def evaluate(self, execution: BackendRun) -> EvaluationFeedback:
        summary = execution.summary
        rewards = summary.get("rewards")
        issues: list[str] = []
        if rewards is not None and not isinstance(rewards, dict):
            issues.append("Backend rewards is not an object")
        metrics = _serializable_metrics(rewards, issues) if isinstance(rewards, dict) else {}
        raw_paths: dict[str, str] = {}
        if execution.result_path is not None:
            raw_paths["backend_result"] = str(execution.result_path)

        summary_reward = None
        if "reward" in metrics:
            try:
                summary_reward = _number(metrics["reward"])
            except (ValueError, OverflowError) as exc:
                issues.append(f"Invalid backend reward: {exc}")

        file_reward = None
        tests: list[dict[str, Any]] = []
        verifier_dir = execution.verifier_dir
        if verifier_dir is not None and verifier_dir.is_dir():
            raw_paths["verifier_dir"] = str(verifier_dir)
            for name in ("test-stdout.txt", "test-stderr.txt", "reward.txt", "ctrf.json"):
                file = verifier_dir / name
                if file.is_file():
                    raw_paths[name] = str(file)
            if "reward.txt" in raw_paths:
                try:
                    file_reward = _number((verifier_dir / "reward.txt").read_text(encoding="utf-8").strip())
                except (OSError, UnicodeError, ValueError, OverflowError) as exc:
                    issues.append(f"Cannot read verifier reward.txt: {exc}")
            if "ctrf.json" in raw_paths:
                try:
                    ctrf = json.loads(
                        (verifier_dir / "ctrf.json").read_text(encoding="utf-8"),
                        parse_constant=_reject_json_constant,
                        parse_float=_number,
                    )
                    if not isinstance(ctrf, dict) or not isinstance(ctrf.get("results"), dict):
                        raise ValueError("CTRF results must be an object")
                    results = ctrf["results"]
                    ctrf_tests = results.get("tests", [])
                    if not isinstance(ctrf_tests, list) or any(not isinstance(test, dict) for test in ctrf_tests):
                        raise ValueError("CTRF tests must be a list of objects")
                    tests = ctrf_tests
                    if "summary" in results:
                        if "ctrf_summary" in metrics:
                            raise ValueError("CTRF summary conflicts with a backend metric named ctrf_summary")
                        metrics["ctrf_summary"] = results["summary"]
                except (OSError, UnicodeError, ValueError) as exc:
                    issues.append(f"Cannot read verifier ctrf.json: {exc}")

        if summary_reward is not None and file_reward is not None and summary_reward != file_reward:
            issues.append(f"Conflicting verifier rewards: backend={summary_reward}, reward.txt={file_reward}")
        reward = summary_reward if summary_reward is not None else file_reward
        if "reward" not in metrics and file_reward is not None:
            metrics["reward"] = file_reward
        if summary.get("verifier_error") or summary.get("verifier_error_category"):
            issues.append(f"Verifier error: {summary.get('verifier_error') or summary['verifier_error_category']}")

        status = "error" if issues else ("scored" if reward is not None or tests or metrics else "unavailable")
        passed = (reward == 1) if status == "scored" and reward in (0, 1) else None
        return EvaluationFeedback(
            status=status, metrics=metrics, passed=passed, tests=tests,
            raw_paths=raw_paths, issues=issues,
        )
