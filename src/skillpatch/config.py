"""Explicit TOML configuration, with paths relative to the config file."""

import os
import tomllib
from pathlib import Path
from typing import Any

from skillpatch.models import RunRequest


def _keys(section: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(f"Unknown {label} options: {', '.join(sorted(unknown))}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def load_config(path: Path) -> tuple[RunRequest, Path]:
    path = path.expanduser().resolve()
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    _keys(document, {"task", "agent", "runtime", "output"}, "top-level")
    for name in ("task", "agent", "runtime", "output"):
        if not isinstance(document.get(name, {}), dict):
            raise ValueError(f"[{name}] must be a table")
    task = document.get("task", {})
    agent = document.get("agent", {})
    runtime = document.get("runtime", {})
    output = document.get("output", {})
    _keys(task, {"benchmark", "path", "skills_dir", "skill_mode", "skill_condition", "explicit_skill_read"}, "task")
    _keys(agent, {"name", "model", "reasoning_effort"}, "agent")
    _keys(runtime, {"backend", "bench_command", "expected_backend_version", "sandbox",
                    "sandbox_user", "sandbox_setup_timeout", "agent_idle_timeout",
                    "timeout_sec", "usage_tracking", "bench_python", "diagnosis_config",
                    "diagnosis_timeout_sec"}, "runtime")
    _keys(output, {"root"}, "output")

    def resolve(value: Any, label: str, *, executable: bool = False) -> Path:
        candidate = Path(_string(value, label)).expanduser()
        candidate = candidate if candidate.is_absolute() else path.parent / candidate
        # A venv Python is commonly a symlink. Its invoked path selects the venv;
        # resolving to the base interpreter silently loses installed packages.
        return Path(os.path.abspath(candidate)) if executable else candidate.resolve()

    command = runtime.get("bench_command", ["bench"])
    if not isinstance(command, list) or not command:
        raise ValueError("runtime.bench_command must be a non-empty array of strings")
    command = [_string(part, "bench_command argument") for part in command]
    # Resolve a path-valued executable while preserving a PATH lookup such as 'bench'.
    if "/" in command[0] or command[0].startswith("~"):
        command[0] = str(resolve(command[0], "bench_command executable", executable=True))
    mode = task.get("skill_mode", "with-skill")
    if mode not in {"with-skill", "no-skill"}:
        raise ValueError("skill_mode must be with-skill or no-skill; evolution belongs to SkillPatch")
    if mode == "no-skill" and task.get("skills_dir"):
        raise ValueError("skills_dir cannot be combined with no-skill")
    usage = runtime.get("usage_tracking", "auto")
    if usage not in {"auto", "required", "off"}:
        raise ValueError("usage_tracking must be auto, required or off")
    explicit_skill_read = task.get("explicit_skill_read", True)
    if type(explicit_skill_read) is not bool:
        raise ValueError("task.explicit_skill_read must be a boolean")
    if (runtime.get("diagnosis_config") is not None
            and runtime.get("backend", "benchflow") == "benchflow"
            and not runtime.get("bench_python")):
        raise ValueError("BenchFlow live diagnosis requires runtime.bench_python")
    request = RunRequest(
        benchmark=_string(task.get("benchmark"), "task.benchmark"),
        task_dir=resolve(task.get("path"), "task.path"),
        agent=_string(agent.get("name"), "agent.name"),
        model=_string(agent.get("model"), "agent.model"),
        bench_command=tuple(command),
        backend=_string(runtime.get("backend", "benchflow"), "runtime.backend"),
        skills_dir=resolve(task["skills_dir"], "task.skills_dir") if task.get("skills_dir") else None,
        skill_mode=mode,
        skill_condition=_string(task.get("skill_condition", "task-provided"), "task.skill_condition"),
        sandbox=_string(runtime.get("sandbox", "docker"), "runtime.sandbox"),
        sandbox_user=_string(runtime.get("sandbox_user", "agent"), "runtime.sandbox_user"),
        sandbox_setup_timeout=_integer(runtime.get("sandbox_setup_timeout", 1800), "sandbox_setup_timeout"),
        agent_idle_timeout=_integer(runtime.get("agent_idle_timeout", 600), "agent_idle_timeout", 0),
        timeout_sec=_integer(runtime.get("timeout_sec", 3600), "timeout_sec"),
        reasoning_effort=_string(agent["reasoning_effort"], "agent.reasoning_effort") if "reasoning_effort" in agent else None,
        usage_tracking=usage,
        expected_backend_version=_string(runtime["expected_backend_version"], "expected_backend_version") if "expected_backend_version" in runtime else None,
        bench_python=resolve(runtime["bench_python"], "runtime.bench_python", executable=True) if "bench_python" in runtime else None,
        diagnosis_config=resolve(runtime["diagnosis_config"], "runtime.diagnosis_config") if "diagnosis_config" in runtime else None,
        diagnosis_timeout_sec=_integer(runtime.get("diagnosis_timeout_sec", 900), "diagnosis_timeout_sec"),
        explicit_skill_read=explicit_skill_read,
    )
    return request, resolve(output.get("root", "../runs"), "output.root")
