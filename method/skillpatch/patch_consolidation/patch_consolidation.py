"""Propose keep/merge/defer using all separately namespaced support archives."""
from excutor.executor_adapter.storage import write_json, jsonable
from method.skillpatch.patch_generation.session import run_session


def validate_proposal(payload: dict) -> dict:
    """Validate the small model contract, not its semantic correctness."""
    if not isinstance(payload, dict) or set(payload) != {
            "action", "reason", "when_to_use", "how_to_interpret"}:
        raise ValueError("Consolidation fields do not match the schema")
    if not isinstance(payload["action"], str) or payload["action"] not in {"keep", "merge", "defer"}:
        raise ValueError("Invalid consolidation action")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Consolidation reason is required")
    for name in ("when_to_use", "how_to_interpret"):
        value = payload[name]
        if payload["action"] != "merge":
            if value is not None:
                raise ValueError("Only a merge proposal may contain replacement patch text")
        elif (not isinstance(value, str) or not value.strip() or len(value) > 16000
              or "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value or "\x00" in value):
            raise ValueError("Merged patch sections must be nonempty bounded text without reserved markers")
    return payload



def propose_consolidation(entries, contexts, binding, config, output, environment, *, client=None, progress=None):
    payload = run_session(config, {
        'target_binding': binding, 'existing': entries[:-1], 'incoming': entries[-1],
        'support_archives': [{'collection': entry['collection'], 'patch_id': entry['patch']['patch_id'],
                              'issue': entry['issue']} for entry in entries],
        'input_status': 'generated_candidates; execution_effectiveness_not_verified',
        'binding_limitations': 'Exact archived declaration, target text and anchor only; no registry or whole-pack identity.'
    }, contexts, output, environment, client=client, progress=progress)
    validate_proposal(payload)
    write_json(output / 'proposal.json', {'proposal': payload, 'source_patches': entries,
        'target_binding': binding, 'semantic_correctness': 'not_verified',
        'ready_for_activation': False, 'access_tracking': 'harness_read_ranges_not_verified'})
    return payload
