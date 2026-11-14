"""Propose keep/merge/defer using all separately namespaced support archives."""
from excutor.executor_adapter.storage import write_json
from method.skillpatch.patch_generation.models import NOTE_EXAMPLE_FORMAT
from method.skillpatch.session import run_session


def validate_proposal(payload: dict, *, patch_format=None) -> dict:
    """Validate the proposal for the input candidates' patch format."""
    if patch_format is None:
        sections = ("when_to_use", "how_to_interpret")
    elif patch_format == NOTE_EXAMPLE_FORMAT:
        sections = ("note", "example")
    else:
        raise ValueError("Unsupported consolidation patch format")
    if not isinstance(payload, dict) or set(payload) != {"action", "reason", *sections}:
        raise ValueError("Consolidation fields do not match the schema")
    if not isinstance(payload["action"], str) or payload["action"] not in {"keep", "merge", "defer"}:
        raise ValueError("Invalid consolidation action")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Consolidation reason is required")
    for name in sections:
        value = payload[name]
        if payload["action"] != "merge":
            if value is not None:
                raise ValueError("Only a merge proposal may contain replacement patch text")
        elif (not isinstance(value, str) or not value.strip() or len(value) > 16000
              or "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value or "\x00" in value):
            raise ValueError("Merged patch sections must be nonempty bounded text without reserved markers")
    return payload



def propose_consolidation(entries, contexts, binding, config, output, environment, *,
                          patch_format=None, client=None, progress=None):
    request = {
        'target_binding': binding, 'existing': entries[:-1], 'incoming': entries[-1],
        'support_archives': [{'collection': entry['collection'], 'patch_id': entry['patch']['patch_id'],
                              'issue': entry['issue']} for entry in entries],
        'input_status': 'generated_candidates; execution_effectiveness_not_verified',
        'binding_limitations': 'Exact archived declaration, target text and anchor only; no registry or whole-pack identity.'
    }
    record = {'source_patches': entries, 'target_binding': binding, 'semantic_correctness': 'not_verified',
              'ready_for_activation': False, 'access_tracking': 'harness_read_ranges_not_verified'}
    if patch_format is not None:
        request['patch_format'] = record['patch_format'] = patch_format
    payload = run_session(config, request, contexts, output, environment, client=client, progress=progress)
    validate_proposal(payload, patch_format=patch_format)
    write_json(output / 'proposal.json', {'proposal': payload, **record})
    return payload
