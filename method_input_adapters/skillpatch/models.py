"""Text inputs for offline diagnosis, with source references and limitations."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ContextItem:
    context_id: str
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
class DiagnosisContext:
    run_id: str
    benchmark: str
    task_id: str
    task_digest: str
    skill_digest: str | None
    trajectory_complete: bool | None
    items: list[ContextItem]
    limitations: list[str]
    input_digest: str
    task_succeeded: bool | None = None
    evolution_eligibility: str = "invalid"
    eligibility_reason: str = "eligibility_not_recorded"
