"""Read diagnosis and candidate archives for generation, replay and consolidation."""
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path

from context import context_digest
from excutor.executor_adapter.storage import json_digest, jsonable
from harness.agent_client import parse_json_object
from method_input_adapters.skillpatch.models import ContextItem, DiagnosisContext
from method.skillpatch.patch_diagnosis.discovery_validation import validate_report, validate_saved_report
from method.skillpatch.patch_diagnosis.models import Conflict, EvidenceRef, SkillImprovement
from .models import FindingIssue, PatchSource
from .patches import validate_generation


def read_json(path):
    return parse_json_object(Path(path).read_text(encoding='utf-8'))


@dataclass(frozen=True)
class DiagnosisArchive:
    diagnosis_dir: Path
    run_dir: Path
    context: DiagnosisContext
    issues: tuple[Conflict | FindingIssue | SkillImprovement, ...]
    file_hashes: dict[str, str]

    @property
    def issue_ids(self):
        return [issue.conflict_id for issue in self.issues]

    def select(self, issue_id):
        matches = [issue for issue in self.issues if issue.conflict_id == issue_id]
        if len(matches) != 1:
            raise ValueError(f'Unknown or ambiguous Stage1 issue: {issue_id}')
        return PatchSource(self.run_dir, self.diagnosis_dir, self.context, matches[0])


def load_diagnosis(diagnosis_dir: Path) -> DiagnosisArchive:
    root = diagnosis_dir.expanduser().resolve(strict=True)
    hashes = {}

    def read(relative):
        content = (root / relative).read_bytes()
        hashes[relative] = 'sha256:' + hashlib.sha256(content).hexdigest()
        return parse_json_object(content.decode('utf-8'))

    state = read('result.json')
    if state.get('status') != 'completed':
        raise ValueError('Stage2 requires a completed Stage1 report')
    raw = read('input/context.json')
    # Validate the archived bytes before normalizing retired method metadata.
    digest = context_digest(raw)
    if digest != raw.get('input_digest') or digest != state.get('input_digest'):
        raise ValueError('Stage1 context digest changed')
    # Verify legacy archives with their original digest, but do not forward the
    # retired completeness label into new method inputs.
    fields = {key: value for key, value in raw.items() if key != 'trajectory_complete'}
    retired_notes = ('Trajectory completeness', 'Trajectory is explicitly partial;',
                     'Trajectory is partial;', 'trajectory_complete refers to')
    fields['limitations'] = [line.replace(
        'Saved Runner steps are not a complete model-message or tool-protocol transcript. ', '')
        for line in raw['limitations'] if not line.startswith(retired_notes)]
    context = DiagnosisContext(**{**fields, 'items': [ContextItem(**row) for row in raw['items']]})
    payload = read('report.json')
    report = (validate_saved_report(payload, context) if 'schema_version' in payload
              else validate_report(payload, context))
    issues = [*report.conflicts, *getattr(report, 'improvements', [])]
    for index, finding in enumerate(report.other_findings, 1):
        issues.append(FindingIssue(f'O{index:06d}', finding['category'], finding['description'],
                                   [EvidenceRef(**ref) for ref in finding['evidence']]))
    return DiagnosisArchive(root, Path(state['source']), context, tuple(issues), hashes)


def load_source(diagnosis_dir: Path, issue_id: str):
    return load_diagnosis(diagnosis_dir).select(issue_id)


def load_patch(path: Path):
    path = path.expanduser().resolve(strict=True)
    episode = path.parent.parent
    if path != episode / 'generation/patch.json':
        raise ValueError('Expected a patch_generation output at generation/patch.json')
    state = read_json(episode / 'result.json')
    if state.get('status') != 'generated':
        raise ValueError('Patch generation episode did not finish successfully')

    version = state.get('archive_version', 1)
    if version == 1:
        provenance = read_json(episode / 'source.json')
        diagnosis_dir = provenance['diagnosis_dir']
    elif version == 2:
        diagnosis_dir = state['diagnosis_dir']
    else:
        raise ValueError('Unsupported patch generation archive version')
    source = load_source(Path(diagnosis_dir), state['issue_id'])
    issue = jsonable(source.conflict)
    context_hash = source.context.input_digest
    if version == 1:
        matches_source = provenance['input_digest'] == context_hash and provenance['issue'] == issue
    else:
        matches_source = (state['source_input_digest'] == context_hash
                          and state['source_issue_digest'] == json_digest(issue))
    if not matches_source:
        raise ValueError('Patch source no longer matches its diagnosis archive')

    raw = read_json(path)
    revision = raw.get('revision')
    if type(revision) is not int or revision < 0:
        raise ValueError('Invalid patch revision')
    response = read_json(episode / 'generation/response.json')
    patch = validate_generation(response, source, revision, patch_format=raw.get('format'))
    if patch is None or jsonable(patch) != raw:
        raise ValueError('Patch differs from its source-bound generation response')
    if version == 1:
        record = read_json(episode / 'generation/generation.json')
        matches_record = record.get('patch') == raw and record.get('source_digest') == context_hash
    else:
        matches_record = state.get('patch_digest') == json_digest(raw)
    if not matches_record:
        raise ValueError('Patch generation record differs from its patch or context')
    if isinstance(source.conflict, FindingIssue):
        source.conflict = replace(source.conflict, target=patch.target)
    return patch, source, episode
