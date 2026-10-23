from dataclasses import dataclass, field
from typing import Any

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
