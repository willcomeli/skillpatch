"""Read a selected issue and context solely from the unified Stage1 archive."""
from pathlib import Path
import hashlib
import json
from method_input_adapters.skillpatch.models import ContextItem, DiagnosisContext
from harness.agent_client import parse_json_object
from method.skillpatch.patch_diagnosis.discovery_validation import validate_report, validate_saved_report
from .models import FindingIssue, PatchSource


def read_json(path):
    return parse_json_object(Path(path).read_text(encoding='utf-8'))


def load_source(diagnosis_dir: Path, issue_id: str):
    root = diagnosis_dir.expanduser().resolve(strict=True)
    state = read_json(root / 'result.json')
    if state.get('status') != 'completed':
        raise ValueError('Stage2 requires a completed Stage1 report')
    raw = read_json(root / 'input/context.json')
    context = DiagnosisContext(**{**raw, 'items': [ContextItem(**row) for row in raw['items']]})
    digest = context.input_digest
    # Hash the archived fields, before dataclass defaults add newer optional fields.
    encoded = json.dumps({key: value for key, value in raw.items() if key != 'input_digest'},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         allow_nan=False).encode('utf-8')
    actual_digest = 'sha256:' + hashlib.sha256(encoded).hexdigest()
    if actual_digest != digest or state.get('input_digest') != digest:
        raise ValueError('Stage1 context digest changed')
    payload = read_json(root / 'report.json')
    report = (validate_saved_report(payload, context) if 'schema_version' in payload
              else validate_report(payload, context))
    issues = [*report.conflicts, *getattr(report, 'improvements', [])]
    for index, finding in enumerate(report.other_findings, 1):
        from method.skillpatch.patch_diagnosis.models import EvidenceRef
        issues.append(FindingIssue(f'O{index:06d}', finding['category'], finding['description'],
                                   [EvidenceRef(**ref) for ref in finding['evidence']]))
    matches = [issue for issue in issues if issue.conflict_id == issue_id]
    if len(matches) != 1:
        raise ValueError(f'Unknown or ambiguous Stage1 issue: {issue_id}')
    return PatchSource(Path(state['source']), root, context, matches[0])
