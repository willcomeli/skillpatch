"""Use an available reference slot for an approved pre-Agent preparation repair."""
import argparse
import os
from pathlib import Path
import subprocess
import time

from . import experiment as exp
from .recover_references import alive, integrate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--request-name', default='PREPARATION_FOLLOWUP_REQUESTED.json')
    args = parser.parse_args(argv)
    run = args.run.resolve()
    request_path = run / args.request_name
    request = exp.read(request_path)
    progress_path = run / request.get('progress_name', 'preparation_followup_progress.json')
    if progress_path.exists():
        raise ValueError('Preparation follow-up already has a checkpoint; refuse duplicate execution')
    name = request['task']
    source = Path(request['failure_source']).resolve()
    raw = exp.read(source / 'result.json')
    if (not source.is_relative_to(run) or source.name != name
            or exp.sha256_file(source / 'result.json') != request['failure_result_sha256']
            or (not request.get('allow_agent_reexecution') and
                (raw['execution']['trajectory_events'] != 0 or raw['feedback']['status'] != 'unavailable'))):
        raise ValueError('Follow-up would replace changed evidence or replay Agent execution')
    launch_path = run.with_suffix('.launcher.json')
    launch = exp.read(launch_path)
    command = launch['command']
    cfg, tasks, native, _, environment = exp.load_settings(
        Path(command[command.index('--config') + 1]),
        exp.load_environment(Path(command[command.index('--env-file') + 1])))
    tasks = {task.name: task for task in tasks}
    secrets = exp.secret_values(environment)
    output = run / request.get('output_subdirectory', 'preparation_followup') / name
    progress = {'task': name, 'source': str(output), 'controller_pid': os.getpid(),
                'execution_mode': request.get('execution_mode', 'baseline'),
                'started_at': exp.now(), 'status': 'waiting_slot'}
    exp.write_json(progress_path, progress)
    launch[request.get('launcher_pid_field', 'preparation_followup_controller_pid')] = os.getpid()
    exp.write_json(launch_path, launch)
    # The global queue must be empty: after it has submitted every approved
    # task, its active count can only fall. One released slot is safe to reuse.
    while True:
        if exp.read(request_path).get('status') == 'cancelled':
            return
        global_progress = exp.read(run / 'global_reference_recovery_progress.json')
        if not global_progress['queued'] and len(global_progress['active']) < global_progress['concurrency']:
            break
        time.sleep(2)
    progress['status'] = 'running'
    exp.write_json(progress_path, progress)
    try:
        if request.get('execution_mode') == 'verifier_only':
            recovery = request['verifier_recovery']
            with (run / 'verifier-only-recovery.log').open('ab') as log:
                subprocess.run([request['native_python'], '-m', 'scripts.skillsbench.verifier_recovery',
                    '--source', str(source), '--output', str(output), '--report', recovery['report'],
                    '--report-sha256', recovery['report_sha256']], cwd=exp.ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, check=False)
            from .recover_references import finished_record
            record = finished_record(output)
            if record is None:
                raise ValueError('Verifier recovery did not produce a final archive')
        else:
            record = exp._baseline_job(name, exp.request_for(tasks[name], cfg, native), output, environment, False)
    except Exception as error:
        record = exp._failure(error, secrets)
        record['source'] = str(output)
    record.update(previous_preparation_failure=str(source), recovery_scope=request['reason'])
    progress.update(status='awaiting_collection_barrier', record=record, execution_finished_at=exp.now())
    exp.write_json(progress_path, exp.redact(progress, secrets))
    # The normal experiment sees BASELINE_ONLY_REQUESTED and exits before any
    # method. Join that handoff before updating its reference registry.
    while True:
        if exp.read(request_path).get('status') == 'cancelled':
            return
        global_progress = exp.read(run / 'global_reference_recovery_progress.json')
        state = exp.read(run / 'result.json')
        launch = exp.read(launch_path)
        dependency = request.get('wait_for_progress')
        dependency_done = not dependency or exp.read(run / dependency).get('finished_at')
        if (global_progress.get('finished_at') and state['phase'] == 'baseline_finished'
                and not alive(launch['pid']) and dependency_done):
            break
        time.sleep(2)
    if state['methods']:
        raise ValueError('Method work started before follow-up collection')
    additional_path = run / 'additional_preparation_recovery_progress.json'
    additional = exp.read(additional_path)
    state, additional = integrate(state, additional, {name: record})
    state.update(status='running', phase='additional_preparation_recovery')
    state.pop('finished_at', None)
    exp.write_json(additional_path, exp.redact(additional, secrets))
    exp.write_json(run / 'result.json', exp.redact(state, secrets))
    # Both earlier recovery schedulers have exited, so their checkpoints can
    # now adopt the replacement without concurrent writers.
    global_progress['finished'][name] = record
    exp.write_json(run / 'global_reference_recovery_progress.json', exp.redact(global_progress, secrets))
    if request.get('update_nine_checkpoint', True):
        nine_path = run / 'approved_nine_recovery_progress.json'
        nine = exp.read(nine_path)
        nine['finished'][name] = record
        exp.write_json(nine_path, exp.redact(nine, secrets))
    (run / request.get('barrier_name', 'BASELINE_ONLY_REQUESTED.json')).rename(
        run / request.get('completed_barrier_name', 'rpm-followup-collection-barrier.json'))
    progress.update(status=record['status'], finished_at=exp.now())
    exp.write_json(progress_path, exp.redact(progress, secrets))
    with run.with_suffix('.launcher.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=exp.ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    launch.update(pid=child.pid, phase_owner='experiment')
    launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': child.pid,
        'reason': 'Collect the approved preparation follow-up before starting methods'})
    exp.write_json(launch_path, launch)
    print('Preparation follow-up collected; experiment resumed', child.pid, flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import sys
        if '--run' in sys.argv:
            run = Path(sys.argv[sys.argv.index('--run') + 1]).resolve()
            exp.write_json(run / 'preparation-followup-error.json', {
                'at': exp.now(), 'error_type': type(error).__name__, 'error': str(error)})
        raise
