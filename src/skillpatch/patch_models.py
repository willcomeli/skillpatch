"""Stage 2 values shared by induction, rendering and validation."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from skillpatch.models import Conflict, DiscoveryInput, PreparedTask, RunRequest, SkillTarget


@dataclass(frozen=True)
class SkillPatch:
    patch_id: str
    source_run_id: str
    source_input_digest: str
    conflict_id: str
    conflict_type: str
    revision: int
    target: SkillTarget
    when_to_use: str
    how_to_interpret: str


@dataclass
class PatchSource:
    run_dir: Path
    diagnosis_dir: Path
    request: RunRequest
    prepared: PreparedTask
    context: DiscoveryInput
    conflict: Conflict


@dataclass
class FeedbackComparison:
    status: str  # improved, unchanged, regressed, unavailable
    changes: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


@dataclass
class TargetEvaluation:
    baseline_status: str  # present, absent, uncertain
    target_status: str  # repaired, not_repaired, uncertain, invalid_diagnosis, unpatchable
    explanation: str
    baseline_evidence: list[dict[str, str]]
    candidate_evidence: list[dict[str, str]]
    reverse_conflicts: list[dict[str, Any]]
    revision_action: str  # revise_patch, revisit_diagnosis, stop, none


@dataclass(frozen=True)
class Stage2Config:
    generation_config: Path
    evaluation_config: Path
    regression_configs: tuple[Path, ...] = ()
    repetitions: int = 1
    max_revisions: int = 2
    model_session_timeout_sec: int = 900
    generation_session_timeout_sec: int | None = None
