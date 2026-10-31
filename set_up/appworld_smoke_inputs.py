"""Prepare the same frozen AppWorld inputs for all three method smoke runs."""

from dataclasses import replace
import json
import math
from pathlib import Path
import shutil

from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split
from excutor.executor_adapter.storage import sha256_file, write_json
from set_up.appworld_trajectories import load_manifest
from set_up.native_config import NativeRunConfig, load_native_config


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _toml_value(value):
    """Serialize JSON-compatible configuration values without dropping options."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if type(value) is float and math.isfinite(value):
        return repr(value)
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(item) for item in value) + ']'
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return '{ ' + ', '.join(
            f'{json.dumps(key, ensure_ascii=False)} = {_toml_value(item)}'
            for key, item in value.items()) + ' }'
    raise ValueError('Source execution configuration contains an unsupported TOML value')


def _execution_toml(config):
    sections = {
        'task': {'benchmark': config.benchmark, 'path': str(config.tasks_path),
                 'skill_path': str(config.skill_path)},
        'agent': {'name': 'native', 'model': config.model, 'api_key_env': config.api_key_env,
                  'base_url_env': config.base_url_env, 'base_url': config.base_url,
                  'temperature': config.temperature, 'max_tokens': config.max_tokens,
                  'max_retries': config.max_retries},
        'runtime': {'backend': 'eviskill', 'timeout_sec': config.timeout_sec},
        'runtime.runner_options': config.runner_options,
        'output': {'root': str(config.output_root)},
    }
    return '\n'.join(
        f'[{section}]\n' + ''.join(
            f'{json.dumps(key, ensure_ascii=False)} = {_toml_value(value)}\n'
            for key, value in values.items())
        for section, values in sections.items())


def prepare_inputs(manifest_path: Path, folder: Path, *, dev_limit=1) -> dict:
    """Snapshot frozen train evidence and independent official dev task references.

    This creates configuration files only. It does not load credentials, invoke
    a model, start AppWorld, or modify the original shared execution archive.
    """
    if type(dev_limit) is not int or dev_limit <= 0:
        raise ValueError('dev_limit must be a positive integer')
    manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest_digest = sha256_file(manifest_path)
    manifest = load_manifest(manifest_path)
    raw = _read(manifest['execution_config']['path'])
    if not isinstance(raw.get('base_url'), str) or not raw['base_url'].strip():
        raise ValueError('The frozen source execution requires an explicit base_url')
    source_config = NativeRunConfig(**{
        **raw, **{name: Path(raw[name]) for name in ('tasks_path', 'skill_path', 'output_root')}
    })
    train_refs = _read(manifest['tasks_snapshot']['path'])
    dev_refs = load_official_appworld_split(source_config.runner_options['data_dir'], 'dev')[:dev_limit]
    if not dev_refs:
        raise ValueError('The official AppWorld dev split contains no tasks')
    train_ids = {ref['task_id'] for ref in train_refs}
    if any(ref['task_id'] in train_ids for ref in dev_refs):
        raise ValueError('Official dev tasks overlap the frozen training tasks')

    folder = Path(folder).expanduser().resolve()
    protected = [manifest_path.parent, Path(manifest['source_run']['path']).parent]
    if any(folder.is_relative_to(path.resolve()) for path in protected):
        raise ValueError('Smoke inputs must be outside the shared trajectory archives')
    initial_skill_dir = folder / 'initial_skill'
    initial_skill = initial_skill_dir / 'SKILL.md'
    tasks_path = folder / 'tasks.json'
    config_path = folder / 'execution.toml'
    config = replace(source_config, tasks_path=tasks_path, skill_path=initial_skill,
                     output_root=folder / 'executions')
    toml = _execution_toml(config)
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    initial_skill_dir.mkdir()
    shutil.copyfile(manifest['initial_skill']['path'], initial_skill)
    shutil.copyfile(manifest['tasks_snapshot']['path'], tasks_path)
    shutil.copyfile(manifest_path, folder / 'trajectory_manifest.snapshot.json')
    config_path.write_text(toml, encoding='utf-8')
    if load_native_config(config_path) != config:
        raise ValueError('Prepared execution configuration differs from the frozen source settings')

    def entries(refs, split):
        return [{'id': ref['task_id'], 'split': split, 'config': str(config_path), 'task_ref': ref}
                for ref in refs]

    train_entries = entries(train_refs, 'train')
    validation_entries = entries(dev_refs, 'val')
    method_manifest = folder / 'method_tasks.json'
    validation_manifest = folder / 'validation_tasks.json'
    write_json(method_manifest, train_entries + validation_entries)
    write_json(validation_manifest, validation_entries)
    if (sha256_file(manifest_path) != manifest_digest
            or sha256_file(folder / 'trajectory_manifest.snapshot.json') != manifest_digest
            or sha256_file(initial_skill) != manifest['initial_skill']['sha256']
            or sha256_file(tasks_path) != manifest['tasks_snapshot']['sha256']
            or load_manifest(manifest_path) != manifest):
        raise ValueError('Shared trajectory inputs changed while preparing smoke inputs')
    return {'manifest': manifest, 'manifest_path': str(manifest_path),
            'manifest_sha256': manifest_digest, 'initial_skill': initial_skill,
            'initial_skill_dir': initial_skill_dir, 'execution_config': config_path,
            'method_manifest': method_manifest, 'validation_manifest': validation_manifest,
            'train_entries': train_entries, 'validation_entries': validation_entries}
