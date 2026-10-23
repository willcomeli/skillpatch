"""Run patch generation as a file-based harness-agent session."""
from pathlib import Path
from harness.agent_client import AgentClient, parse_json_object
from excutor.executor_adapter.storage import write_json, tree_hashes, jsonable
from set_up.environment import redact, secret_values


def run_session(config, data, contexts, output: Path, environment, *, client=None, progress=None):
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'input'
    inputs.mkdir()
    materials = []
    secrets = set(secret_values(environment))
    if environment.get(config.agent.api_key_env):
        secrets.add(environment[config.agent.api_key_env])
    values = {'request': data, **{name: jsonable(context) for name, context in contexts.items()}}
    for name, value in values.items():
        if redact(value, secrets) != value:
            raise ValueError('Known credential appears in agent materials')
        path = inputs / f'{name}.json'
        write_json(path, value)
        materials.append(path)
    hashes = tree_hashes(inputs)
    prompt = config.prompt_path.read_text(encoding='utf-8')
    if not prompt.strip() or redact(prompt, secrets) != prompt:
        raise ValueError('Agent prompt is empty or contains a known credential')
    (output / 'prompt.md').write_text(prompt, encoding='utf-8')
    write_json(output / 'config.json', config)
    result = (client or AgentClient()).run(config.agent, prompt, materials, output / 'agent',
                                          environment, progress=progress)
    if tree_hashes(inputs) != hashes:
        raise ValueError('Agent changed Stage2 input materials')
    if result.status != 'completed':
        raise ValueError(f'Stage2 agent session {result.status}: {result.errors}')
    if result.report_path is None:
        raise ValueError('Agent produced no JSON report')
    path = result.report_path.resolve(strict=True)
    if not path.is_relative_to((output / 'agent/workspace').resolve()):
        raise ValueError('Agent report is outside its workspace')
    payload = parse_json_object(path.read_text(encoding='utf-8'))
    if redact(payload, secrets) != payload:
        raise ValueError('Known credential appears in Stage2 output')
    write_json(output / 'response.json', payload)
    return payload
