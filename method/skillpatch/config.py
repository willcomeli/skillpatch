"""Load agent settings for SkillPatch diagnosis, generation and consolidation."""

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Mapping

from harness.agent_client import AgentConfig


BOUNDARY_PROMPTS = Path(__file__).resolve().parents[2] / 'prompts/skillpatch/boundary_aware_patch_induction'
_BOUNDARY_OPTIONS = {
    'stage2': {'inductor_prompt_path', 'proposer_prompt_path'},
    'stage3': {'consolidation_prompt_path'},
}


@dataclass(frozen=True)
class DiscoveryConfig:
    agent: AgentConfig
    prompt_path: Path


@dataclass(frozen=True)
class StageAgentConfig(DiscoveryConfig):
    boundary_inductor_prompt: Path | None = None
    boundary_proposer_prompt: Path | None = None
    boundary_consolidation_prompt: Path | None = None

    def induction_prompts(self) -> dict[str, Path]:
        return {
            'initial': self.boundary_inductor_prompt or BOUNDARY_PROMPTS / 'patch_inductor.md',
            'cases': self.boundary_proposer_prompt or BOUNDARY_PROMPTS / 'boundary_test_proposer.md',
        }

    def for_consolidation(self, *, boundary_aware: bool) -> DiscoveryConfig:
        if not boundary_aware:
            return DiscoveryConfig(self.agent, self.prompt_path)
        prompt = self.boundary_consolidation_prompt or BOUNDARY_PROMPTS / 'patch_consolidation.md'
        return DiscoveryConfig(self.agent, prompt)


def load_discovery_config(
    path: Path, *, environment: Mapping[str, str] | None = None, stage: str = 'stage1',
) -> DiscoveryConfig:
    config = load_stage_agent_config(path, stage, environment=environment)[1]
    return DiscoveryConfig(config.agent, config.prompt_path)


def load_stage_agent_config(
    path: Path, stage: str, *, config_key: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, StageAgentConfig]:
    path = path.expanduser().resolve()
    with path.open('rb') as stream:
        document = tomllib.load(stream)
    if config_key is not None and 'agent' not in document:
        wrapper = document.get(stage)
        if set(document) != {stage} or not isinstance(wrapper, dict) or set(wrapper) != {config_key}:
            raise ValueError(f'Expected [{stage}] with only {config_key}')
        value = wrapper[config_key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{config_key} must be a nonempty path')
        path = (path.parent / value).resolve(strict=True)
        with path.open('rb') as stream:
            document = tomllib.load(stream)

    boundary = {}
    if 'agent' in document:
        stages = ('stage1', 'stage2', 'stage3')
        shared = document['agent']
        if (set(document) - {'agent', *stages, 'boundary_aware'}
                or not {'agent', *stages} <= set(document) or not isinstance(shared, dict)
                or 'prompt_path' in shared):
            raise ValueError('Shared evolution config requires [agent] and [stage1] through [stage3]')
        for name in stages:
            table = document[name]
            if not isinstance(table, dict) or set(table) != {'prompt_path'}:
                raise ValueError(f'[{name}] requires only prompt_path')
        boundary = document.get('boundary_aware', {})
        if (not isinstance(boundary, dict) or set(boundary) - set(_BOUNDARY_OPTIONS)
                or any(not isinstance(value, dict) or set(value) != _BOUNDARY_OPTIONS[name]
                       for name, value in boundary.items())):
            raise ValueError('Invalid boundary_aware prompt configuration')
        values = {**shared, **document[stage]}
    else:
        if set(document) != {'diagnosis'} or not isinstance(document['diagnosis'], dict):
            raise ValueError('Agent configuration requires exactly one [diagnosis] table')
        values = dict(document['diagnosis'])
    if 'include_successful_runs' in values:
        raise ValueError('include_successful_runs was replaced by adapter evolution eligibility')
    text_fields = ('model', 'model_env', 'base_url', 'base_url_env', 'prompt_path', 'api_key_env', 'harness')
    if set(values) - {*text_fields, 'command', 'session_timeout_sec'} or 'prompt_path' not in values:
        raise ValueError('Missing or unknown diagnosis fields; max_input_chars is no longer used')
    for key in text_fields:
        if key in values and (not isinstance(values[key], str) or not values[key].strip()):
            raise ValueError(f'diagnosis.{key} must be nonempty text')
    environment = environment or {}
    for key in ('model', 'base_url'):
        env_field = f'{key}_env'
        if (key in values) == (env_field in values):
            raise ValueError(f'Set exactly one of diagnosis.{key} and diagnosis.{env_field}')
        if env_field in values:
            name = values.pop(env_field)
            value = environment.get(name, '').strip()
            if not value:
                raise ValueError(f'Missing diagnosis environment variable: {name}')
            values[key] = value
    def resolve_prompt(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Expected a nonempty prompt path')
        prompt_path = (path.parent / value).resolve()
        if not prompt_path.is_file():
            raise ValueError(f'Prompt file does not exist: {prompt_path}')
        return prompt_path

    prompt = resolve_prompt(values.pop('prompt_path'))
    overrides = boundary.get(stage, {})
    boundary_prompts = {}
    for field, key in (
        ('boundary_inductor_prompt', 'inductor_prompt_path'),
        ('boundary_proposer_prompt', 'proposer_prompt_path'),
        ('boundary_consolidation_prompt', 'consolidation_prompt_path'),
    ):
        if key in overrides:
            boundary_prompts[field] = resolve_prompt(overrides[key])
    command = values.pop('command', [])
    if not isinstance(command, list) or any(not isinstance(s, str) or not s for s in command):
        raise ValueError('diagnosis.command must be a string array')
    if command and '/' in command[0]:
        # Preserve virtual-environment executable symlinks.
        command[0] = str((path.parent / command[0]).absolute())
    timeout = values.pop('session_timeout_sec', 600)
    agent = AgentConfig(**values, command=tuple(command), timeout_sec=timeout)
    return path, StageAgentConfig(agent, prompt, **boundary_prompts)
