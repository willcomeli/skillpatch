"""Generate an evidence-bound supplement through the shared harness client."""
from excutor.executor_adapter.storage import write_json, jsonable
from .patches import validate_generation
from .session import run_session


def generate_patch(source, config, output, environment, *, revision=0, history=None, client=None, progress=None):
    payload = run_session(config, {'issue': jsonable(source.conflict), 'previous_attempts': history or [],
        'issue_is_candidate': True, 'revision': revision}, {'source': source.context}, output,
        environment, client=client, progress=progress)
    patch = validate_generation(payload, source, revision)
    write_json(output / 'generation.json', {'status': payload['status'], 'reason': payload['reason'],
        'patch': patch, 'source_digest': source.context.input_digest,
        'semantic_correctness': 'not_verified', 'access_tracking': 'harness_read_ranges_not_verified'})
    if patch is not None:
        write_json(output / 'patch.json', patch)
    return patch, payload
