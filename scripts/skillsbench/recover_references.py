"""Drain live reference work, collect approved reruns, then resume methods."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from . import experiment as exp


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def finished_record(folder):
    """Read a finished archive without executing or repairing its contents."""
    folder = Path(folder)
    path = folder / 'result.json'
    if not path.exists():
        return None
    raw = exp.read(path)
    if raw.get('status') in {'running', 'prepared'} or not raw.get('finished_at'):
        return None
    try:
        archive = exp.load_archive(folder)
        if archive.task_id != folder.name:
            raise ValueError('Reference archive task identity differs')
        return {'status': 'completed', 'source': str(folder),
                'hashes': exp.tree_hashes(folder), 'execution_status': raw['status']}
    except (ValueError, OSError, KeyError, TypeError) as error:
        return {'status': 'failed', 'source': str(folder),
                'error': f'{type(error).__name__}: {error}',
                'execution_status': raw['status']}


def integrate(state, additional, replacements):
    """Replace only approved references before any method has consumed them."""
    if state['methods']:
        raise ValueError('Cannot replace references after method work has started')
    for name, record in replacements.items():
        if name not in state['baselines']:
            raise ValueError('Unknown reference replacement task')
        if record['status'] == 'completed':
            if exp.tree_hashes(Path(record['source'])) != record['hashes']:
                raise ValueError('Finished replacement reference changed')
        previous = state['baselines'][name]
        state['baselines'][name] = {**record, 'previous_reference_record': previous,
                                   'recovery_scope': 'User explicitly requested rerunning these nine references'}
        if name in additional['finished']:
            # The resumed original stage must not restore an older failure.
            additional['finished'][name] = state['baselines'][name]
    return state, additional


def check_original(pid, run):
    command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True)
    if ('scripts.skillsbench.experiment' not in command or str(run) not in command):
        raise ValueError('Original coordinator process identity does not match this run')


def ensure_auxiliaries(run, launch):
    for key, filename in (('mirror_controller_pid', 'mirror_live.py'),
                          ('uv_controller_pid', 'live_uv_bootstrap.py')):
        path = run / filename
        pid = launch.get(key)
        command = subprocess.run(['ps', '-p', str(pid or 0), '-o', 'command='],
                                 capture_output=True, text=True).stdout
        if str(path) in command:
            continue
        with (run / (path.stem + '.log')).open('ab') as log:
            child = subprocess.Popen([str(exp.ROOT / '.venvs/methods/bin/python'), '-u', str(path)],
                cwd=exp.ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        launch[key] = child.pid


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    args = parser.parse_args(argv)
    run = args.run.resolve()
    request = exp.read(run / 'NINE_REFERENCE_RECOVERY_REQUESTED.json')
    original_pid = request['original_coordinator_pid']
    launch_path = run.with_suffix('.launcher.json')
    launch = exp.read(launch_path)
    state = exp.read(run / 'result.json')
    if state['methods'] or state['phase'] != 'additional_preparation_recovery':
        raise ValueError('Recovery must be arranged before any method stage')
    check_original(original_pid, run)
    additional_path = run / 'additional_preparation_recovery_progress.json'
    initial = exp.read(additional_path)
    live_names = list(initial['active'])
    concurrency = request['recovery_concurrency']
    if concurrency < 1 or concurrency + len(live_names) > state['concurrency']:
        raise ValueError('Combined live and recovery work exceeds authorized concurrency')
    command = launch['command']
    config = Path(command[command.index('--config') + 1])
    environment_file = Path(command[command.index('--env-file') + 1])
    cfg, tasks, native, _, environment = exp.load_settings(config, exp.load_environment(environment_file))
    requests = {task.name: exp.request_for(task, cfg, native) for task in tasks}
    secrets = exp.secret_values(environment)
    targets = request['tasks']
    if len(set(targets)) != 9 or len(targets) != 9 or set(targets) - requests.keys():
        raise ValueError('Expected exactly the nine user-approved task identities')
    if set(targets) & set(live_names):
        raise ValueError('Cannot rerun an already active task')
    for name in targets:
        source = Path(request['previous_failures'][name]['source']).resolve()
        if (not source.is_relative_to(run) or source.name != name
                or exp.sha256_file(source / 'result.json') != request['previous_failures'][name]['result_sha256']):
            raise ValueError('Approved previous failure identity changed')
        if state['baselines'][name]['status'] != 'failed':
            raise ValueError('Approved rerun is no longer an unscored failed reference')
    output = run / 'approved_nine_recovery'
    if output.exists():
        raise ValueError('Recovery output already exists; do not repeat Agent execution')
    output.mkdir()
    progress_path = run / 'approved_nine_recovery_progress.json'
    progress = {'started_at': exp.now(), 'active': targets[:], 'finished': {},
                'concurrency': concurrency, 'controller_pid': os.getpid(),
                'original_coordinator_pid': original_pid, 'original_live_tasks': live_names}
    exp.write_json(progress_path, progress)
    exp.write_json(run / 'additional_progress.before-nine-reference-recovery.json', initial)
    exp.write_json(run / 'result.before-nine-reference-recovery.json', state)
    exp.write_json(run / 'nine-reference-recovery-plan.json', {
        'request': request, 'controller_sha256': exp.sha256_file(Path(__file__)),
        'original_plan_sha256': exp.sha256_file(run / 'plan.json'),
        'config_sha256': exp.sha256_file(config),
        'scope': 'References only; all original and rerun references finish before SkillOpt then Trace2Skill',
        'native_budgets': 'unchanged', 'execution_model': native.model,
        'task_hashes': {name: exp.tree_hashes(requests[name].task_dir) for name in targets}})
    launch['reference_recovery_controller_pid'] = os.getpid()
    exp.write_json(launch_path, launch)
    # Signal only the coordinator. ProcessPoolExecutor drains its existing
    # running futures before KeyboardInterrupt returns; no sandbox/Agent is killed.
    os.kill(original_pid, signal.SIGINT)
    original_drained = threading.Event()
    observer_errors = []

    def observe_original():
        try:
            while alive(original_pid):
                time.sleep(2)
            additional = exp.read(additional_path)
            for name in live_names:
                record = finished_record(run / 'additional_preparation_recovery' / name)
                if record is None:
                    raise ValueError('Drained reference lacks a finalized archive: ' + name)
                additional['finished'][name] = record
                if name in additional['active']:
                    additional['active'].remove(name)
            additional['finished_at'] = exp.now()
            exp.write_json(additional_path, additional)
            current = exp.read(run / 'result.json')
            if current['methods']:
                raise ValueError('Original coordinator advanced into methods while draining')
            for name in live_names:
                current['baselines'][name] = additional['finished'][name]
            current.update(status='running', phase='approved_nine_recovery')
            current.pop('finished_at', None)
            exp.write_json(run / 'result.json', current)
            fresh_launch = exp.read(launch_path)
            fresh_launch.update(pid=os.getpid(), phase_owner='approved_nine_recovery')
            ensure_auxiliaries(run, fresh_launch)
            exp.write_json(launch_path, fresh_launch)
            original_drained.set()
        except Exception as error:
            observer_errors.append(error)
            original_drained.set()

    jobs = [(name, (name, requests[name], output / name, environment, False)) for name in targets]
    with ThreadPoolExecutor(max_workers=1) as observers:
        observer = observers.submit(observe_original)
        for name, record in exp._batch_results(exp._baseline_job, jobs, concurrency, secrets):
            if record['status'] != 'completed':
                # Preserve the failed archive identity as well as the error.
                record['source'] = str(output / name)
            progress['finished'][name] = record
            progress['active'].remove(name)
            exp.write_json(progress_path, exp.redact(progress, secrets))
            print('[approved recovery]', name, record['status'], flush=True)
        observer.result()
    if observer_errors:
        raise observer_errors[0]
    assert original_drained.is_set()
    current = exp.read(run / 'result.json')
    additional = exp.read(additional_path)
    current, additional = integrate(current, additional, progress['finished'])
    current.update(status='running', phase='additional_preparation_recovery')
    current.pop('finished_at', None)
    exp.write_json(additional_path, exp.redact(additional, secrets))
    exp.write_json(run / 'result.json', exp.redact(current, secrets))
    progress['finished_at'] = exp.now()
    exp.write_json(progress_path, exp.redact(progress, secrets))
    launch = exp.read(launch_path)
    command = launch['command']
    if '--resume' not in command:
        command = [*command, '--resume']
    with run.with_suffix('.launcher.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=exp.ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    launch.update(pid=child.pid, command=command, phase_owner='experiment')
    launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': child.pid,
        'reason': 'User-approved nine reference reruns and existing live references all collected'})
    ensure_auxiliaries(run, launch)
    exp.write_json(launch_path, launch)
    print('Reference recovery collected; experiment resumed', child.pid, flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Keep an actionable error outside all immutable task archives.
        if '--run' in sys.argv:
            run = Path(sys.argv[sys.argv.index('--run') + 1]).resolve()
            exp.write_json(run / 'nine-reference-recovery-error.json', {
                'at': exp.now(), 'error_type': type(error).__name__, 'error': str(error)})
        raise
