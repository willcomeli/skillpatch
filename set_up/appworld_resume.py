"""Resume an interrupted shared AppWorld collection without replacing completed tasks."""

import argparse
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
from uuid import uuid4

from excutor.eviskill_runners.launcher import create_runner, FileSkillBank, runner_arguments
from excutor.executor_adapter.storage import jsonable, now, sha256_file
from method_input_adapters.archive import load_archive
from set_up.appworld_trajectories import _task_refs, _verify, load_manifest
from set_up.environment import load_environment, redact, redact_artifacts, secret_values
from set_up.native_config import NativeRunConfig, load_native_config

ROOT = Path(__file__).resolve().parents[1]
LOCK_NAME = '.resume.lock'
CHECKPOINT_NAME = 'resume_checkpoint.json'


def _read(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    json.dumps(value, allow_nan=False)
    return value


def _regular(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'Expected a regular managed file: {path}')
    return path


def _atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'Refusing to replace a linked file: {path}')
    temporary = path.parent / ('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(jsonable(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record(path):
    return {'path': str(path), 'sha256': sha256_file(path)}


def _hashes(paths):
    return {str(path): sha256_file(_regular(path)) for path in paths}


def _check_hashes(hashes):
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError('Missing checkpoint file hashes')
    for name, digest in hashes.items():
        if sha256_file(_regular(Path(name))) != digest:
            raise ValueError(f'Frozen collection input or saved task changed: {name}')


def _owner():
    return {'pid': os.getpid(), 'host': socket.gethostname(),
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def _check_running(state):
    if state.get('status') not in {'running', 'preparing'}:
        return
    owner = state.get('resume', {}).get('owner', {})
    if (owner.get('host') != socket.gethostname() or type(owner.get('pid')) is not int
            or owner['pid'] <= 0 or not isinstance(owner.get('boot_id'), str)):
        raise ValueError('Collection is running/preparing without verifiable resume ownership; stop it first')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if owner['boot_id'] == boot and Path(f'/proc/{owner["pid"]}').exists():
        raise ValueError('The recorded resume process is still running; refusing concurrent collection')


def _check_lock(shared):
    lock = shared / LOCK_NAME
    if lock.exists() or lock.is_symlink():
        _regular(lock)
        with lock.open('rb') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another resume process holds the collection lock') from None
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _validate_task(path, ref, skill):
    raw = _read(_regular(path))
    summary = raw.get('evaluation_summary') if isinstance(raw, dict) else None
    if not isinstance(summary, dict) or 'num_tests' not in summary:
        raise ValueError('A completed task requires saved official evaluation counts')
    archive = load_archive(path)
    if (archive.benchmark != 'appworld' or archive.task_id != ref['task_id']
            or raw.get('split') != 'train' or raw.get('retrieved_memory_text') != skill
            or raw.get('patch_deployment') or archive.score != int(archive.success)):
        raise ValueError('Saved task identity, initial Skill or official score differs from the collection')
    return {'task_id': archive.task_id, 'split': 'train', 'source': str(path),
            'sha256': sha256_file(path), 'success': archive.success, 'score': archive.score}


def _inspect(shared_dir, *, check_lock=True):
    shared = Path(shared_dir).expanduser().resolve(strict=True)
    execution = shared / 'execution'
    for folder in (shared / 'input', execution, execution / 'input'):
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'Expected a managed collection directory: {folder}')
    if check_lock:
        _check_lock(shared)
    protected = [shared / 'plan.json', shared / 'input/tasks.json', shared / 'input/SKILL.md',
                 execution / 'config.json', execution / 'plan.json',
                 execution / 'input/tasks.json', execution / 'input/SKILL.md']
    anchors = _hashes(protected)
    plan, state = _read(shared / 'plan.json'), _read(_regular(shared / 'result.json'))
    config_raw, execution_plan = _read(execution / 'config.json'), _read(execution / 'plan.json')
    native_state = _read(_regular(execution / 'result.json'))
    if not all(isinstance(row, dict) for row in (plan, state, config_raw, execution_plan, native_state)):
        raise ValueError('Collection plans, configuration and states must be objects')
    if (state.get('status') not in {'failed', 'cancelled', 'prepared', 'running', 'completed'}
            or native_state.get('status') not in {'backend_error', 'artifact_error', 'cancelled',
                                                 'prepared', 'preparing', 'running', 'completed'}
            or not isinstance(native_state.get('errors'), list)
            or any(not isinstance(error, str) for error in native_state['errors'])):
        raise ValueError('Collection has an unsupported or malformed execution state')
    for current in (state, native_state):
        if current.get('benchmark') != 'appworld':
            raise ValueError('Only managed AppWorld collection states can be resumed')
        _check_running(current)
    if (plan.get('benchmark') != 'appworld' or plan.get('split') != 'train'
            or plan.get('validation_split_used') is not False
            or plan.get('execution_directory') != str(execution)):
        raise ValueError('Collection plan is not the expected unpatched train collection')
    for name, path in (('initial_skill', shared / 'input/SKILL.md'),
                       ('tasks_snapshot', shared / 'input/tasks.json')):
        if plan.get(name) != _record(path):
            raise ValueError(f'Collection plan input hash/path mismatch: {name}')
    tasks = _read(execution / 'input/tasks.json')
    skill = (execution / 'input/SKILL.md').read_text(encoding='utf-8')
    if (tasks != _read(shared / 'input/tasks.json') or not skill.strip()
            or skill != (shared / 'input/SKILL.md').read_text(encoding='utf-8')):
        raise ValueError('Execution snapshots differ from the hashed collection inputs')
    if (config_raw.get('tasks_path') != str(shared / 'input/tasks.json')
            or config_raw.get('skill_path') != str(shared / 'input/SKILL.md')
            or config_raw.get('output_root') != str(shared)
            or config_raw.get('benchmark') != 'appworld'
            or not isinstance(config_raw.get('base_url'), str) or not config_raw['base_url'].strip()):
        raise ValueError('Frozen execution configuration is not bound to this collection')
    config = NativeRunConfig(**{**config_raw, **{key: Path(config_raw[key])
                           for key in ('tasks_path', 'skill_path', 'output_root')}})
    ids = _task_refs(tasks, config.runner_options.get('data_dir', ''))
    if (plan.get('task_ids') != ids or plan.get('task_count') != len(ids)
            or state.get('task_count') != len(ids) or state.get('split') != 'train'
            or state.get('validation_split_used') is not False):
        raise ValueError('Collection task order/count/split differs from the snapshots')
    if (execution_plan.get('benchmark') != 'appworld' or execution_plan.get('task_count') != len(ids)
            or execution_plan.get('patch_skill_dir') is not None
            or (execution / 'input/patched_skill').exists()
            or execution_plan.get('skill_path') != str(execution / 'input/SKILL.md')
            or execution_plan.get('model_request_timeout_sec') != config.timeout_sec
            or execution_plan.get('runner_options') != runner_arguments(config, execution)):
        raise ValueError('Frozen native configuration differs from its execution plan')
    # Full experiments retain an independent TOML snapshot; ordinary older
    # collections only have the native snapshot and cannot prove prior edits to it.
    full_config_path = shared.parent / 'collection_inputs/execution.toml'
    if full_config_path.exists():
        reference = load_native_config(_regular(full_config_path))
        keys = ('benchmark', 'model', 'api_key_env', 'base_url_env', 'base_url', 'temperature',
                'max_tokens', 'timeout_sec', 'max_retries', 'runner_options')
        if any(getattr(reference, key) != getattr(config, key) for key in keys):
            raise ValueError('Frozen model/budget differs from the full experiment configuration snapshot')
        anchors.update(_hashes([full_config_path]))
    paths = native_state.get('task_results')
    if not isinstance(paths, list) or len(paths) > len(tasks):
        raise ValueError('Execution task_results must be a complete ordered prefix')
    expected = [execution / 'tasks' / f'task_{index:06d}.json' for index in range(1, len(paths) + 1)]
    if paths != [str(path) for path in expected]:
        raise ValueError('Saved task results are not a contiguous canonical prefix')
    task_dir = execution / 'tasks'
    if task_dir.exists():
        if task_dir.is_symlink() or not task_dir.is_dir() or set(task_dir.iterdir()) != set(expected):
            raise ValueError('Orphan, unexpected or noncontiguous task files; refusing to overwrite evidence')
    elif expected:
        raise ValueError('Saved task directory is missing')
    rows = [_validate_task(path, ref, skill) for path, ref in zip(expected, tasks)]
    if len(rows) == len(tasks) and (native_state.get('status') != 'completed' or native_state.get('errors')):
        raise ValueError('All task files exist, but successful runner cleanup is unconfirmed; inspect cleanup before finalization')
    checkpoint_path = shared / CHECKPOINT_NAME
    if checkpoint_path.exists() or checkpoint_path.is_symlink():
        checkpoint = _read(_regular(checkpoint_path))
        if (checkpoint.get('schema_version') != 1 or checkpoint.get('shared_dir') != str(shared)
                or checkpoint.get('rows') != rows or checkpoint.get('input_hashes') != anchors):
            raise ValueError('Resume checkpoint differs from the frozen inputs or canonical task prefix')
        _check_hashes(checkpoint['input_hashes'])
    _check_hashes(anchors)
    manifest_path = shared / 'trajectory_manifest.json'
    status = 'prepared'
    if manifest_path.exists() or manifest_path.is_symlink():
        manifest = load_manifest(_regular(manifest_path))
        if len(rows) != len(tasks) or manifest['rows'] != rows:
            raise ValueError('Existing manifest does not cover the completed collection')
        status = 'completed'
    elif native_state.get('status') == 'completed' and (len(rows) != len(tasks) or native_state.get('errors')):
        raise ValueError('Completed native status has missing tasks or cleanup errors')
    public = {'status': status, 'shared_dir': str(shared), 'execution_dir': str(execution),
              'total_count': len(tasks), 'completed_count': len(rows), 'remaining_count': len(tasks) - len(rows),
              'next_task_index': len(rows) + 1 if len(rows) < len(tasks) else None,
              'next_task_id': ids[len(rows)] if len(rows) < len(tasks) else None,
              'completed_task_ids': ids[:len(rows)], 'remaining_task_ids': ids[len(rows):],
              'execution_model': config.model, 'validation_split_used': False,
              'manifest_path': str(manifest_path) if status == 'completed' else None}
    return public, {'shared': shared, 'execution': execution, 'config': config, 'tasks': tasks, 'skill': skill,
                    'rows': rows, 'anchors': anchors, 'state': state, 'native_state': native_state}


def inspect_resume(shared_dir: Path) -> dict:
    """Read-only preflight; never create a lock, alter state, or start a runner."""
    return _inspect(shared_dir)[0]


def resume_collection(shared_dir, environment, *, prepare_only=False) -> dict:
    preview = inspect_resume(shared_dir)
    if prepare_only or preview['status'] == 'completed':
        return preview
    shared = Path(preview['shared_dir'])
    lock_path = shared / LOCK_NAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another resume process holds the collection lock') from None
        preview, context = _inspect(shared, check_lock=False)
        if preview['status'] == 'completed':
            return preview
        config = context['config']
        if preview['remaining_count'] and not environment.get(config.api_key_env):
            raise ValueError(f'Missing credential environment variable: {config.api_key_env}')
        secrets = secret_values(environment)
        key = environment.get(config.api_key_env)
        if key:
            secrets = tuple(set(secrets) | {key})
        if redact(context['skill'], secrets) != context['skill']:
            raise ValueError('Initial Skill contains a configured secret')
        execution = context['execution']
        attempts = execution / 'resume_attempts'
        if attempts.is_symlink():
            raise ValueError('Resume attempts directory must not be a link')
        attempts.mkdir(exist_ok=True)
        attempt = attempts / uuid4().hex
        attempt.mkdir(mode=0o700)
        before = attempt / 'before'
        before.mkdir()
        for source, name in ((shared / 'result.json', 'collection_result.json'),
                             (execution / 'result.json', 'execution_result.json'),
                             (execution / 'config.json', 'execution_config.json'),
                             (execution / 'plan.json', 'execution_plan.json'),
                             (shared / 'plan.json', 'collection_plan.json')):
            shutil.copyfile(source, before / name)
        if (shared / CHECKPOINT_NAME).exists():
            shutil.copyfile(shared / CHECKPOINT_NAME, before / CHECKPOINT_NAME)
        rows = list(context['rows'])
        protected = {**context['anchors'], **{row['source']: row['sha256'] for row in rows}}
        _atomic_json(before / 'checkpoint.json', {'input_hashes': context['anchors'], 'file_hashes': protected,
                                                 'rows': rows, 'captured_at': now()})
        owner = _owner()
        resume = {'attempt_dir': str(attempt), 'owner': owner}
        _atomic_json(attempt / 'attempt.json', {'schema_version': 1, 'started_at': now(), 'owner': owner,
                    'shared_dir': str(shared), 'starting_completed_count': len(rows),
                    'runtime_directory': str(attempt / 'runtime'), 'input_hashes': context['anchors']})
        state = {**context['state'], 'status': 'running', 'resume': resume, 'resumed_at': now()}
        for field in ('error', 'finished_at', 'manifest_path'):
            state.pop(field, None)
        native = {**context['native_state'], 'status': 'running', 'errors': [], 'resume': resume,
                  'resumed_at': now(), 'task_results': [row['source'] for row in rows]}
        native.pop('finished_at', None)
        base_usage = dict(native.get('model_requests') or {})
        runner = None

        def save():
            checkpoint = {'schema_version': 1, 'shared_dir': str(shared), 'attempt_dir': str(attempt),
                          'input_hashes': context['anchors'], 'rows': rows, 'updated_at': now()}
            _atomic_json(attempt / 'checkpoint.json', checkpoint)
            _atomic_json(shared / CHECKPOINT_NAME, checkpoint)
            _atomic_json(execution / 'result.json', redact(native, secrets))
            state['completed_count'] = len(rows)
            state['remaining_count'] = len(context['tasks']) - len(rows)
            _atomic_json(shared / 'result.json', redact(state, secrets))

        try:
            save()
            _check_hashes(protected)
            if len(rows) < len(context['tasks']):
                runtime = attempt / 'runtime'
                runtime.mkdir()
                actual = replace(config, tasks_path=execution / 'input/tasks.json',
                                 skill_path=execution / 'input/SKILL.md')
                runner = create_runner(actual, runtime, environment)
                bank = FileSkillBank(actual.skill_path)
                staged = attempt / 'tasks'
                staged.mkdir()
                (execution / 'tasks').mkdir(exist_ok=True)
                for index in range(len(rows), len(context['tasks'])):
                    _check_hashes(protected)
                    task = context['tasks'][index]
                    print(f'[{index + 1}/{len(context["tasks"])}] Resume {task["task_id"]} from initial state', flush=True)
                    raw = runner.run_task(task, bank)
                    staged_path = staged / f'task_{index + 1:06d}.json'
                    _atomic_json(staged_path, redact(raw, secrets))
                    _validate_task(staged_path, task, context['skill'])
                    _check_hashes(protected)
                    canonical = execution / 'tasks' / staged_path.name
                    # A hard-link publishes a complete file atomically and refuses
                    # any existing destination instead of replacing saved evidence.
                    os.link(staged_path, canonical)
                    row = _validate_task(canonical, task, context['skill'])
                    rows.append(row)
                    protected[row['source']] = row['sha256']
                    native['task_results'].append(row['source'])
                    save()
                    _check_hashes(protected)
            native['status'] = 'completed'
        except KeyboardInterrupt:
            native['status'] = 'cancelled'
            state['status'] = 'cancelled'
        except Exception as error:
            native['status'] = 'backend_error'
            native['errors'].append(redact(f'{type(error).__name__}: {error}', secrets))
            state['status'] = 'failed'
        finally:
            if runner is not None:
                client = getattr(getattr(runner, 'action_agent', None), 'llm', None)
                if client is not None and hasattr(client, 'request_attempts'):
                    usage = {'attempts': client.request_attempts, 'retries': client.retry_count}
                    native['model_requests'] = {key: base_usage.get(key, 0) + value for key, value in usage.items()}
                    native['resume_attempt_model_requests'] = usage
                try:
                    runner.close()
                except Exception as error:
                    native['errors'].append(redact(f'Cleanup failed: {type(error).__name__}: {error}', secrets))
                    if native['status'] == 'completed':
                        native['status'] = 'backend_error'
                        state['status'] = 'failed'
            try:
                if (attempt / 'runtime').exists():
                    redact_artifacts(attempt / 'runtime', secrets)
                _check_hashes(protected)
            except Exception as error:
                native['status'] = 'artifact_error'
                native['errors'].append(redact(f'{type(error).__name__}: {error}', secrets))
                state['status'] = 'failed'
            native['finished_at'] = now()
            if native['status'] == 'completed' and not native['errors']:
                # Save the final native state before hashing it into the manifest.
                state['status'] = 'running'
                save()
                try:
                    manifest = {'schema_version': 1, 'benchmark': 'appworld', 'status': 'frozen', 'split': 'train',
                                'initial_skill': _record(execution / 'input/SKILL.md'),
                                'execution_config': _record(execution / 'config.json'),
                                'source_run': _record(execution / 'result.json'),
                                'tasks_snapshot': _record(execution / 'input/tasks.json'), 'rows': rows}
                    _verify(manifest)
                    manifest_path = shared / 'trajectory_manifest.json'
                    if manifest_path.exists():
                        raise ValueError('A trajectory manifest appeared during resume')
                    _atomic_json(manifest_path, manifest)
                    load_manifest(manifest_path)
                    state.update(status='completed', manifest_path=str(manifest_path),
                                 successes=sum(row['success'] for row in rows),
                                 failures=sum(not row['success'] for row in rows))
                except Exception as error:
                    state.update(status='failed', error=redact(f'{type(error).__name__}: {error}', secrets))
            else:
                if state['status'] != 'cancelled':
                    state['status'] = 'failed'
                state['error'] = '; '.join(native['errors']) or 'Collection interrupted'
                save()
            state['finished_at'] = now()
            _atomic_json(attempt / 'result.json', {'status': state['status'], 'finished_at': now(),
                         'completed_count': len(rows), 'execution_result': redact(native, secrets),
                         'collection_result': redact(state, secrets)})
            # Do not rewrite execution/result.json after the manifest hashes it.
            _atomic_json(shared / 'result.json', redact(state, secrets))
        return redact(state, secrets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shared-dir', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    environment = {}
    try:
        # Preparation does not even need to read credentials.
        environment = {} if args.prepare_only else load_environment(args.env_file)
        result = resume_collection(args.shared_dir, environment, prepare_only=args.prepare_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] in {'prepared', 'completed'} else 130 if result['status'] == 'cancelled' else 2
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(redact(f'error: {error}', secret_values(environment)), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
