"""Collect explicitly approved pre-Agent build restarts, then resume methods."""
import argparse
import os
from pathlib import Path
import subprocess
import time

from . import experiment as exp
from .recover_references import alive, integrate


def validate_source(run, name, target):
    source = Path(target['failure_source']).resolve()
    if (not source.is_relative_to(run) or source.name != name
            or exp.sha256_file(source / 'result.json') != target['failure_result_sha256']):
        raise ValueError('Approved preparation archive identity changed')
    raw = exp.read(source / 'result.json')
    if (raw.get('status') in {'running', 'prepared'} or not raw.get('finished_at')
            or raw['execution']['trajectory_events'] != 0 or raw['feedback']['status'] != 'unavailable'):
        raise ValueError('Preparation restart would replay Agent execution or grading')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    args = parser.parse_args(argv)
    run = args.run.resolve()
    request_path = run / 'BUILD_MIRROR_FOLLOWUP_REQUESTED.json'
    request = exp.read(request_path)
    progress_path = run / 'build_mirror_followup_progress.json'
    if progress_path.exists():
        raise ValueError('Preparation batch already has a checkpoint; refuse duplicate execution')
    for name, target in request['tasks'].items():
        validate_source(run, name, target)
    launch_path = run.with_suffix('.launcher.json')
    launch = exp.read(launch_path)
    command = launch['command']
    cfg, tasks, native, _, environment = exp.load_settings(
        Path(command[command.index('--config') + 1]),
        exp.load_environment(Path(command[command.index('--env-file') + 1])))
    tasks = {task.name: task for task in tasks}
    secrets = exp.secret_values(environment)
    names = list(request['tasks'])
    progress = {'controller_pid': os.getpid(), 'started_at': exp.now(), 'status': 'waiting_slot',
                'active': names, 'records': {}, 'sources': {name: str(run / 'build_mirror_recovery' / name) for name in names}}
    exp.write_json(progress_path, progress)
    launch['build_mirror_followup_controller_pid'] = os.getpid()
    exp.write_json(launch_path, launch)
    while True:
        if exp.read(request_path).get('status') == 'cancelled':
            return
        global_progress = exp.read(run / 'global_reference_recovery_progress.json')
        external = len(global_progress['active'])
        for filename in ('preparation_followup_progress.json', 'energy_precache_followup_progress.json'):
            external += int(exp.read(run / filename)['status'] == 'running')
        if (not global_progress['queued'] and not set(names).intersection(global_progress['active'])
                and external + len(names) <= global_progress['concurrency']):
            break
        time.sleep(2)
    progress['status'] = 'running'
    exp.write_json(progress_path, progress)
    jobs = [(name, (name, exp.request_for(tasks[name], cfg, native),
                    Path(progress['sources'][name]), environment, False)) for name in names]
    for name, record in exp._batch_results(exp._baseline_job, jobs, len(names), secrets):
        record.setdefault('source', progress['sources'][name])
        record.update(previous_preparation_failure=request['tasks'][name]['failure_source'],
                      recovery_scope=request['reason'])
        progress['records'][name] = record
        progress['active'].remove(name)
        exp.write_json(progress_path, exp.redact(progress, secrets))
    progress.update(status='awaiting_collection_barrier', execution_finished_at=exp.now())
    exp.write_json(progress_path, exp.redact(progress, secrets))
    while True:
        if exp.read(request_path).get('status') == 'cancelled':
            return
        global_progress = exp.read(run / 'global_reference_recovery_progress.json')
        state = exp.read(run / 'result.json')
        launch = exp.read(launch_path)
        energy = exp.read(run / 'energy_precache_followup_progress.json')
        if (global_progress.get('finished_at') and energy.get('finished_at')
                and state['phase'] == 'baseline_finished' and not alive(launch['pid'])):
            break
        time.sleep(2)
    additional_path = run / 'additional_preparation_recovery_progress.json'
    state, additional = integrate(state, exp.read(additional_path), progress['records'])
    state.update(status='running', phase='additional_preparation_recovery')
    state.pop('finished_at', None)
    exp.write_json(additional_path, exp.redact(additional, secrets))
    exp.write_json(run / 'result.json', exp.redact(state, secrets))
    global_progress['finished'].update(progress['records'])
    exp.write_json(run / 'global_reference_recovery_progress.json', exp.redact(global_progress, secrets))
    nine_path = run / 'approved_nine_recovery_progress.json'
    nine = exp.read(nine_path)
    nine['finished'].update({name: record for name, record in progress['records'].items()
                            if name in request['update_nine_tasks']})
    exp.write_json(nine_path, exp.redact(nine, secrets))
    (run / 'SEISMIC_COMPAT_COLLECTION_REQUESTED.json').rename(run / 'build-mirror-collection-barrier.json')
    progress.update(status='completed', finished_at=exp.now())
    exp.write_json(progress_path, exp.redact(progress, secrets))
    with run.with_suffix('.launcher.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=exp.ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    launch.update(pid=child.pid, phase_owner='experiment')
    launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': child.pid,
        'reason': 'Collect approved three-task build mirror recovery, then resume methods'})
    exp.write_json(launch_path, launch)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import sys
        if '--run' in sys.argv:
            run = Path(sys.argv[sys.argv.index('--run') + 1]).resolve()
            exp.write_json(run / 'build-mirror-followup-error.json', {
                'at': exp.now(), 'error_type': type(error).__name__, 'error': str(error)})
        raise
