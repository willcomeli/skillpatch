"""Stage2 generation inputs and candidate patch output."""

from dataclasses import dataclass
from pathlib import Path

from method.skillpatch.patch_diagnosis.models import SkillImprovement, Conflict, EvidenceRef, SkillTarget
from method_input_adapters.skillpatch.models import DiagnosisContext as DiscoveryInput


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
class FindingIssue:
    """An archived non-conflict finding with a separately selected Skill target."""

    finding_id: str
    category: str
    description: str
    evidence: list[EvidenceRef]
    target: SkillTarget | None = None

    @property
    def conflict_id(self) -> str:
        return self.finding_id

    @property
    def type(self) -> str:
        return self.category

    @property
    def diagnosis(self) -> str:
        return self.description

    @property
    def feedback_evidence(self) -> list[EvidenceRef]:
        return self.evidence


@dataclass
class PatchSource:
    run_dir: Path
    diagnosis_dir: Path
    context: DiscoveryInput
    conflict: Conflict | FindingIssue | SkillImprovement
