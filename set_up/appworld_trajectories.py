"""Collect and verify the frozen, unpatched AppWorld inputs shared by methods.

The manifest indexes original native task results, including successes. It does
not create a method-specific trajectory representation or execute a dev split.
"""

from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import urlsplit

from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split
from excutor.eviskill_runners.launcher import run_native
from excutor.executor_adapter.storage import now, sha256_file, write_json
from method_input_adapters.archive import load_archive
from set_up.environment import redact, secret_values
from set_up.native_config import load_native_config


def _read(path: Path):
    value = json.loads(path.read_text(encoding='utf-8'))
    json.dumps(value, allow_nan=False)
    return value


def _task_refs(tasks, data_dir):
    if not isinstance(tasks, list) or not tasks:
        raise ValueError('Shared trajectories require a nonempty list of AppWorld train task references')
    official = {row['task_id']: row for row in load_official_appworld_split(data_dir, 'train')}
    ids = []
    for task in tasks:
        if (not isinstance(task, dict) or task.get('split') != 'train'
                or task.get('dataset', 'appworld') != 'appworld'):
            raise ValueError('Every shared task must explicitly use split=train and dataset=appworld')
        task_id = task.get('task_id')
        if not isinstance(task_id, str) or task_id not in official:
            raise ValueError(f'Task is not in the official AppWorld train split: {task_id!r}')
        ref = official[task_id]
        if (task.get('task_family', ref['task_family']) != ref['task_family']
                or task.get('variant', ref['variant']) != ref['variant']):
            raise ValueError(f'Inconsistent AppWorld task identity: {task_id}')
        ids.append(task_id)
    if len(ids) != len(set(ids)):
        raise ValueError('Shared task references contain duplicate task IDs')
    return ids


def _record(path: Path) -> dict:
    return {'path': str(path.resolve()), 'sha256': sha256_file(path)}


def _verified_path(record, label) -> Path:
    if not isinstance(record, dict) or not isinstance(record.get('path'), str):
        raise ValueError(f'Manifest {label} must contain a path and sha256')
    path = Path(record['path'])
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f'Manifest {label} must reference an existing absolute file')
    if sha256_file(path) != record.get('sha256'):
        raise ValueError(f'Manifest {label} integrity check failed')
    return path.resolve()


def _verify(manifest: dict) -> dict:
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or manifest.get('benchmark') != 'appworld'
            or manifest.get('status') != 'frozen' or manifest.get('split') != 'train'):
        raise ValueError('Expected a frozen schema_version=1 AppWorld train trajectory manifest')
    paths = {key: _verified_path(manifest.get(key), key)
             for key in ('initial_skill', 'execution_config', 'source_run', 'tasks_snapshot')}
    root = paths['source_run'].parent
    expected = {'initial_skill': root / 'input/SKILL.md', 'execution_config': root / 'config.json',
                'source_run': root / 'result.json', 'tasks_snapshot': root / 'input/tasks.json'}
    if paths != {key: value.resolve() for key, value in expected.items()}:
        raise ValueError('Manifest inputs must belong to the same managed native execution')
    source = _read(paths['source_run'])
    config = _read(paths['execution_config'])
    plan = _read(root / 'plan.json')
    if (not isinstance(source, dict) or source.get('benchmark') != 'appworld'
            or source.get('status') != 'completed' or source.get('errors') != []):
        raise ValueError('Source batch must complete without execution errors')
    if (not isinstance(config, dict) or config.get('benchmark') != 'appworld'
            or not config.get('skill_path') or not isinstance(config.get('runner_options'), dict)):
        raise ValueError('Source execution must be AppWorld with an initial Skill')
    if (not isinstance(plan, dict) or plan.get('benchmark') != 'appworld'
            or plan.get('patch_skill_dir') is not None or (root / 'input/patched_skill').exists()):
        raise ValueError('Shared trajectories must be collected without patches')
    tasks = _read(paths['tasks_snapshot'])
    ids = _task_refs(tasks, config['runner_options'].get('data_dir', ''))
    rows = manifest.get('rows')
    if not isinstance(rows, list) or len(rows) != len(ids) or plan.get('task_count') != len(ids):
        raise ValueError('Manifest rows must cover the complete source task batch')
    expected_sources = [root / 'tasks' / f'task_{index:06d}.json' for index in range(1, len(ids) + 1)]
    if source.get('task_results') != [str(path) for path in expected_sources]:
        raise ValueError('Source task_results do not match the complete ordered native batch')
    skill = paths['initial_skill'].read_text(encoding='utf-8')
    if not skill.strip():
        raise ValueError('The shared initial Skill cannot be empty')
    for row, task_id, expected_source in zip(rows, ids, expected_sources):
        if not isinstance(row, dict) or row.get('task_id') != task_id or row.get('split') != 'train':
            raise ValueError('Manifest task identity/order differs from the task snapshot')
        source_path = _verified_path({'path': row.get('source'), 'sha256': row.get('sha256')}, task_id)
        if source_path != expected_source.resolve():
            raise ValueError('Manifest task source differs from the registered source batch')
        raw = _read(source_path)
        summary = raw.get('evaluation_summary') if isinstance(raw, dict) else None
        if not isinstance(summary, dict) or 'num_tests' not in summary:
            raise ValueError('Shared AppWorld trajectories require saved official evaluation counts')
        archive = load_archive(source_path)
        if archive.benchmark != 'appworld' or archive.task_id != task_id or raw.get('split') != 'train':
            raise ValueError('Saved trajectory identity differs from the shared train task')
        if raw.get('patch_deployment') or raw.get('retrieved_memory_text') != skill:
            raise ValueError('Saved trajectory did not receive the exact unpatched initial Skill')
        if archive.score != int(archive.success):
            raise ValueError('Shared AppWorld trajectories require official binary success scores')
        if (type(row.get('success')) is not bool or row['success'] != archive.success
                or type(row.get('score')) not in (int, float) or row['score'] != archive.score):
            raise ValueError('Manifest scores differ from official saved execution feedback')
    return manifest


def load_manifest(path: Path) -> dict:
    """Read and verify all indexed evidence without modifying the source archive."""
    return _verify(_read(path.expanduser().resolve(strict=True)))


def collect(config_path: Path, output: Path, environment: dict, *, limit: int = 0,
            prepare_only: bool = False) -> dict:
    """Run one shared baseline batch, freezing a manifest only after full validation.

    Configuration errors raise before execution. Runtime/invalid-feedback failures
    save a failed result and leave no frozen manifest. Preparation creates only
    snapshots and plans and never starts a runner or model request.
    """
    if type(limit) is not int or limit < 0:
        raise ValueError('limit must be a nonnegative integer; zero selects all tasks')
    config = load_native_config(config_path, environment)
    if config.benchmark != 'appworld' or config.skill_path is None:
        raise ValueError('Shared trajectory collection requires AppWorld with an initial Skill')
    tasks = _read(config.tasks_path)
    _task_refs(tasks, config.runner_options.get('data_dir', ''))
    tasks = tasks[:limit] if limit else tasks
    skill = config.skill_path.read_text(encoding='utf-8')
    if not skill.strip():
        raise ValueError('The shared initial Skill cannot be empty')
    secrets = secret_values(environment)
    key = environment.get(config.api_key_env)
    if key:
        secrets = tuple(set(secrets) | {key})
    # Pin the actual service used for collection so replay cannot silently use a
    # different environment-resolved endpoint. No environment values are dumped.
    endpoint = config.base_url or environment.get(config.base_url_env)
    if any(redact(value, secrets) != value for value in (skill, json.dumps(tasks), endpoint or '')):
        raise ValueError('Shared input snapshots or model endpoint contain a configured secret')
    if endpoint and (urlsplit(endpoint).username is not None or urlsplit(endpoint).password is not None):
        raise ValueError('Model endpoint must not embed credentials; use the configured API key environment variable')
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {'benchmark': 'appworld', 'status': 'running', 'started_at': now(),
             'split': 'train', 'task_count': len(tasks), 'validation_split_used': False}
    try:
        inputs = output / 'input'
        inputs.mkdir()
        (inputs / 'SKILL.md').write_text(skill, encoding='utf-8')
        write_json(inputs / 'tasks.json', tasks)
        config = replace(config, tasks_path=inputs / 'tasks.json', skill_path=inputs / 'SKILL.md',
                         base_url=endpoint, output_root=output)
        write_json(output / 'plan.json', {'benchmark': 'appworld', 'split': 'train',
                   'task_ids': [task['task_id'] for task in tasks], 'task_count': len(tasks),
                   'initial_skill': _record(inputs / 'SKILL.md'),
                   'tasks_snapshot': _record(inputs / 'tasks.json'),
                   'execution_directory': str(output / 'execution'),
                   'validation_split_used': False})
        result = run_native(config, output / 'execution', environment, prepare_only=prepare_only)
        if result['status'] == 'cancelled':
            state['status'] = 'cancelled'
        elif result['status'] != ('prepared' if prepare_only else 'completed'):
            raise ValueError(f"Shared execution failed: {result['status']}; see execution/result.json")
        elif prepare_only:
            state['status'] = 'prepared'
        else:
            execution = output / 'execution'
            if (_read(execution / 'input/tasks.json') != tasks
                    or (execution / 'input/SKILL.md').read_text(encoding='utf-8') != skill):
                raise ValueError('Execution snapshots differ from the planned shared inputs')
            rows = []
            for source in result['task_results']:
                path = Path(source)
                raw = _read(path)
                rows.append({'task_id': raw.get('task_id'), 'split': raw.get('split'),
                             'source': str(path.resolve()), 'sha256': sha256_file(path),
                             'success': raw.get('success'), 'score': raw.get('score')})
            manifest = {'schema_version': 1, 'benchmark': 'appworld', 'status': 'frozen', 'split': 'train',
                        'initial_skill': _record(execution / 'input/SKILL.md'),
                        'execution_config': _record(execution / 'config.json'),
                        'source_run': _record(execution / 'result.json'),
                        'tasks_snapshot': _record(execution / 'input/tasks.json'), 'rows': rows}
            _verify(manifest)
            manifest_path = output / 'trajectory_manifest.json'
            write_json(manifest_path, manifest)
            state.update(status='completed', manifest_path=str(manifest_path),
                         successes=sum(row['success'] for row in rows),
                         failures=sum(not row['success'] for row in rows))
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except (OSError, ValueError, KeyError, TypeError) as error:
        state.update(status='failed', error=redact(str(error), secrets))
    finally:
        state['finished_at'] = now()
        write_json(output / 'result.json', redact(state, secrets))
    return state
