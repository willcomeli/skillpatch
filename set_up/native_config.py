"""Configuration for native ALFWorld, ScienceWorld and AppWorld runners."""

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib

from .config import _keys, _string, _integer

NATIVE_BENCHMARKS = {'alfworld', 'scienceworld', 'appworld'}


def read_benchmark(path: Path) -> str:
    with path.expanduser().open('rb') as stream:
        return _string(tomllib.load(stream).get('task', {}).get('benchmark'), 'task.benchmark')


@dataclass
class NativeRunConfig:
    benchmark: str
    tasks_path: Path
    skill_path: Path | None
    model: str
    api_key_env: str
    base_url_env: str
    base_url: str | None
    temperature: float
    max_tokens: int
    timeout_sec: int
    runner_options: dict
    output_root: Path


def load_native_config(path: Path) -> NativeRunConfig:
    path = path.expanduser().resolve()
    with path.open('rb') as stream:
        document = tomllib.load(stream)
    _keys(document, {'task', 'agent', 'runtime', 'output'}, 'top-level')
    for key in ('task', 'agent', 'runtime', 'output'):
        if not isinstance(document.get(key, {}), dict):
            raise ValueError(f'[{key}] must be a table')
    task, agent = document.get('task', {}), document.get('agent', {})
    runtime, output = document.get('runtime', {}), document.get('output', {})
    _keys(task, {'benchmark', 'path', 'skill_path', 'skill_mode'}, 'task')
    _keys(agent, {'name', 'model', 'api_key_env', 'base_url_env', 'base_url', 'temperature', 'max_tokens'}, 'agent')
    _keys(runtime, {'backend', 'timeout_sec', 'runner_options'}, 'runtime')
    _keys(output, {'root'}, 'output')
    benchmark = task.get('benchmark')
    if benchmark not in NATIVE_BENCHMARKS:
        raise ValueError('Unsupported native benchmark')
    if runtime.get('backend', 'eviskill') != 'eviskill':
        raise ValueError('Native datasets require runtime.backend=eviskill')
    if agent.get('name', 'native') != 'native':
        raise ValueError('Native datasets use agent.name=native')
    mode = task.get('skill_mode', 'with-skill')
    if mode not in {'with-skill', 'no-skill'}:
        raise ValueError('skill_mode must be with-skill or no-skill')
    if mode == 'with-skill' and not task.get('skill_path'):
        raise ValueError('with-skill requires task.skill_path')
    if mode == 'no-skill' and task.get('skill_path'):
        raise ValueError('no-skill cannot specify task.skill_path')

    def resolve(value):
        candidate = Path(_string(value, 'path')).expanduser()
        return (path.parent / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()

    options = runtime.get('runner_options', {})
    if not isinstance(options, dict):
        raise ValueError('runtime.runner_options must be a table')
    options = dict(options)
    for key in ('alfworld_path', 'alfworld_config', 'scienceworld_path', 'scienceworld_jar_path', 'data_dir'):
        if options.get(key):
            options[key] = str(resolve(options[key]))
    # Preserve a venv Python's invoked path rather than resolving its symlink.
    for key in ('server_python', 'scienceworld_server_python'):
        if options.get(key):
            candidate = Path(options[key]).expanduser()
            options[key] = os.path.abspath(path.parent / candidate)
    temperature = agent.get('temperature', 0.0)
    if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
        raise ValueError('agent.temperature must be between 0 and 2')
    return NativeRunConfig(
        benchmark, resolve(task.get('path')),
        resolve(task['skill_path']) if mode == 'with-skill' else None,
        _string(agent.get('model'), 'agent.model'),
        _string(agent.get('api_key_env', 'OPENAI_API_KEY'), 'agent.api_key_env'),
        _string(agent.get('base_url_env', 'OPENAI_BASE_URL'), 'agent.base_url_env'),
        _string(agent['base_url'], 'agent.base_url') if agent.get('base_url') else None,
        float(temperature), _integer(agent.get('max_tokens', 4096), 'agent.max_tokens'),
        _integer(runtime.get('timeout_sec', 300), 'runtime.timeout_sec'), options,
        resolve(output.get('root', '../runs')),
    )
