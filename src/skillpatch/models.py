"""Execution records and benchmark-independent Stage 1 evidence contracts."""

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
    timeout_sec: int = 3600
    reasoning_effort: str | None = None
    usage_tracking: str = "auto"
    expected_backend_version: str | None = None
    bench_python: Path | None = None
    diagnosis_config: Path | None = None
    diagnosis_timeout_sec: int = 900
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
    diagnosis: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceItem:
    evidence_id: str
    kind: str  # task, skill, trajectory, feedback, artifact
    text: str
    source_path: str
    source_line: int | None = None
    skill_id: str | None = None
    relative_path: str | None = None
    version: str | None = None
    availability: str | None = None  # initial, post_execution, unknown
    source_role: str | None = None
    observation: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryInput:
    run_id: str
    benchmark: str
    task_id: str
    task_digest: str
    skill_digest: str | None
    trajectory_complete: bool | None
    evidence: list[EvidenceItem]
    limitations: list[str]
    input_digest: str


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    quote: str


@dataclass(frozen=True)
class SkillTarget:
    scope: str  # whole_skill or substep
    skill_id: str
    skill_ref: str
    quote: str
    # Bound by the validator, not supplied by the diagnosis model.
    relative_path: str
    version: str
    source_path: str


@dataclass
class Conflict:
    # Identity and citation span are program-derived for compact model responses.
    conflict_id: str
    type: str  # under_use or misuse
    target: SkillTarget
    diagnosis: str  # Applicability, observed/expected behavior, and feedback limits.
    decision_evidence: list[EvidenceRef]
    behavior_evidence: list[EvidenceRef]
    feedback_evidence: list[EvidenceRef]
    trajectory_range: dict[str, str]  # Citation span, not a claim of full inspection.
    uncertainty: list[str]


@dataclass
class DiscoveryReport:
    run_id: str
    input_digest: str
    status: str  # conflicts_found, no_supported_conflict, insufficient_evidence
    summary: str
    conflicts: list[Conflict]
    other_findings: list[dict[str, Any]]
    coverage_notes: list[str]
    # Successful local validation only establishes structure and reference validity.
    semantic_status: str = "candidate_diagnosis"
    schema_version: int = 2
