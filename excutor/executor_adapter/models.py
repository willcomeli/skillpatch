"""Execution request, prepared task, backend run and verifier feedback records."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunRequest:
    benchmark: str
    task_dir: Path
    agent: str
    model: str
    bench_command: tuple[str, ...]
    backend: str = "benchflow"
    skills_dir: Path | None = None
    skill_mode: str = "with-skill"
    skill_condition: str = "task-provided"
    sandbox: str = "docker"
    sandbox_user: str = "agent"
    sandbox_setup_timeout: int = 1800
    agent_idle_timeout: int = 600
    reasoning_effort: str | None = None
    usage_tracking: str = "auto"
    expected_backend_version: str | None = None
    explicit_skill_read: bool = True


@dataclass
class PreparedTask:
    benchmark: str
    task_id: str
    task_dir: Path
    task_document: Path
    skills_dir: Path | None
    manifest_path: Path
    task_digest: str
    skill_digest: str | None
    file_hashes: dict[str, str] = field(default_factory=dict)


@dataclass
class BackendRun:
    status: str
    return_code: int | None = None
    backend_version: str | None = None
    result_path: Path | None = None
    trajectory_path: Path | None = None
    normalized_trajectory_path: Path | None = None
    verifier_dir: Path | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    trajectory_complete: bool | None = None
    trajectory_events: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)


@dataclass
class EvaluationFeedback:
    status: str  # scored, unavailable, error
    metrics: dict[str, Any] = field(default_factory=dict)
    passed: bool | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    raw_paths: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    run_id: str
    benchmark: str
    task_id: str
    status: str
    started_at: str
    finished_at: str | None = None
    prepared: PreparedTask | None = None
    execution: BackendRun | None = None
    feedback: EvaluationFeedback | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    schema_version: int = 1
