"""Prepare evidence files and read a JSON report from the shared agent harness."""
from pathlib import Path

from excutor.executor_adapter.storage import jsonable, tree_hashes, write_json
from harness.agent_client import AgentClient, parse_json_object
from set_up.environment import redact, secret_values


def session_secrets(config, environment):
    secrets = set(secret_values(environment))
    if key := environment.get(config.agent.api_key_env):
        secrets.add(key)
    return tuple(sorted(secrets, key=len, reverse=True))


def save_prompt(config, output, secrets):
    prompt = config.prompt_path.read_text(encoding='utf-8')
    if not prompt.strip() or redact(prompt, secrets) != prompt:
        raise ValueError('Agent prompt is empty or contains a known credential')
    (output / 'prompt.md').write_text(prompt, encoding='utf-8')
    return prompt


def run_agent_report(config, prompt, materials, output, environment, *,
                     secrets, protected_input, client=None, progress=None):
    hashes = tree_hashes(protected_input)
    result = (client or AgentClient()).run(config.agent, prompt, materials, output,
                                          environment, progress=progress)
    if tree_hashes(protected_input) != hashes:
        raise ValueError('Agent changed input materials')
    if result.status != 'completed':
        return result, None
    if result.report_path is None:
        raise ValueError('Completed agent session has no JSON report')
    report_path = result.report_path.resolve(strict=True)
    if not report_path.is_relative_to((output / 'workspace').resolve()):
        raise ValueError('Agent report is outside its workspace')
    payload = parse_json_object(report_path.read_text(encoding='utf-8'))
    if redact(payload, secrets) != payload:
        raise ValueError('Known credential appears in agent report')
    return result, payload


def run_session(config, data, contexts, output: Path, environment, *,
                context_paths=None, client=None, progress=None):
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / 'input'
    inputs.mkdir()
    materials = []
    secrets = session_secrets(config, environment)
    context_paths = context_paths or {}
    values = {'request': data, **{name: jsonable(context) for name, context in contexts.items()}}
    for name, value in values.items():
        if redact(value, secrets) != value:
            raise ValueError('Known credential appears in agent materials')
        path = context_paths.get(name)
        if path is None:
            path = inputs / f'{name}.json'
            write_json(path, value)
        materials.append(path)
    prompt = save_prompt(config, output, secrets)
    write_json(output / 'config.json', config)
    result, payload = run_agent_report(config, prompt, materials, output / 'agent', environment,
                                       secrets=secrets, protected_input=inputs,
                                       client=client, progress=progress)
    if result.status != 'completed':
        raise ValueError(f'Agent session {result.status}: {result.errors}')
    write_json(output / 'response.json', payload)
    return payload
