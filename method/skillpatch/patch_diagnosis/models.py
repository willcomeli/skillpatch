"""Stage report types migrated from SkillPatch; citation schema retained."""

from dataclasses import dataclass
from typing import Any

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


@dataclass
class SkillImprovement:
    improvement_id: str
    target: SkillTarget
    diagnosis: str
    suggestion: str
    evidence: list[EvidenceRef]

    @property
    def conflict_id(self) -> str:
        """Compatibility identity for Stage2 patch provenance."""
        return self.improvement_id

    @property
    def type(self) -> str:
        return "skill_improvement"

    @property
    def feedback_evidence(self) -> list[EvidenceRef]:
        return self.evidence


@dataclass
class ImprovementReport:
    run_id: str
    input_digest: str
    summary: str
    improvements: list[SkillImprovement]
    semantic_status: str = "candidate_diagnosis"
    schema_version: int = 3

    @property
    def status(self) -> str:
        return "improvements_found" if self.improvements else "no_improvements"

    @property
    def conflicts(self) -> list:
        return []

    @property
    def other_findings(self) -> list:
        return []

    @property
    def coverage_notes(self) -> list:
        return []
