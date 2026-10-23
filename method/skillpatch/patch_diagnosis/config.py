"""Stage1 configuration for file-based agent diagnosis."""

from dataclasses import dataclass
from pathlib import Path
import tomllib

from harness.agent_client import AgentConfig


@dataclass(frozen=True)
class DiscoveryConfig:
    agent: AgentConfig
    prompt_path: Path


def load_discovery_config(path: Path) -> DiscoveryConfig:
    path = path.expanduser().resolve()
    with path.open('rb') as stream:
        document = tomllib.load(stream)
    if set(document) != {'diagnosis'} or not isinstance(document['diagnosis'], dict):
        raise ValueError('Stage1 configuration requires exactly one [diagnosis] table')
    values = dict(document['diagnosis'])
    if 'include_successful_runs' in values:
        raise ValueError('include_successful_runs was replaced by adapter evolution eligibility')
    allowed = {'model', 'base_url', 'api_key_env', 'harness', 'command', 'session_timeout_sec', 'prompt_path'}
    if set(values) - allowed or not {'model', 'base_url', 'prompt_path'} <= set(values):
        raise ValueError('Missing or unknown diagnosis fields; max_input_chars is no longer used')
    for key in ('model', 'base_url', 'prompt_path', 'api_key_env', 'harness'):
        if key in values and (not isinstance(values[key], str) or not values[key].strip()):
            raise ValueError(f'diagnosis.{key} must be nonempty text')
    prompt = (path.parent / values.pop('prompt_path')).resolve()
    if not prompt.is_file():
        raise ValueError('Diagnosis prompt file does not exist')
    command = values.pop('command', [])
    if not isinstance(command, list) or any(not isinstance(s, str) or not s for s in command):
        raise ValueError('diagnosis.command must be a string array')
    if command and '/' in command[0]:
        # Preserve virtual-environment executable symlinks.
        command[0] = str((path.parent / command[0]).absolute())
    timeout = values.pop('session_timeout_sec', 600)
    return DiscoveryConfig(AgentConfig(**values, command=tuple(command), timeout_sec=timeout), prompt)
