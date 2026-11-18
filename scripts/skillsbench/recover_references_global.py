"""Share all reference recovery slots while draining existing coordinators."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import time

from . import experiment as exp
from .recover_references import alive, ensure_auxiliaries, finished_record, integrate


def take_available(pending, external_count, submitted_count, limit):
    available = limit - external_count - submitted_count
    if available < 0:
        raise ValueError('Running reference work exceeds the global concurrency')
    return pending[:available], pending[available:]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    args = parser.parse_args(argv)
    run = args.run.resolve()
    if (run / 'global_reference_recovery_progress.json').exists():
        raise ValueError('Global recovery already has a checkpoint; do not start duplicate executions')
    request_path = run / 'GLOBAL_REFERENCE_RECOVERY_REQUESTED.json'
    control = exp.read(request_path)
    previous = exp.read(run / 'NINE_REFERENCE_RECOVERY_REQUESTED.json')
    nine_path = run / 'approved_nine_recovery_progress.json'
    nine = exp.read(nine_path)
    legacy_nine_pid = nine['controller_pid']
    original_pid = nine['original_coordinator_pid']
    legacy_command = subprocess.check_output(['ps', '-p', str(legacy_nine_pid), '-o', 'command='], text=True)
    if 'scripts.skillsbench.recover_references ' not in legacy_command or str(run) not in legacy_command:
        raise ValueError('Prior recovery coordinator process identity differs')
    state = exp.read(run / 'result.json')
    if state['methods']:
        raise ValueError('Global reference collection must finish before methods')
    launch_path = run.with_suffix('.launcher.json')
    launch = exp.read(launch_path)
    command = launch['command']
    config = Path(command[command.index('--config') + 1])
    env_file = Path(command[command.index('--env-file') + 1])
    cfg, tasks, native, _, environment = exp.load_settings(config, exp.load_environment(env_file))
    requests = {task.name: exp.request_for(task, cfg, native) for task in tasks}
    if len(previous['tasks']) != 9 or len(set(previous['tasks'])) != 9 or set(previous['tasks']) - requests.keys():
        raise ValueError('Approved reference task identities differ')
    secrets = exp.secret_values(environment)
    limit = state['concurrency']
    if limit != control['concurrency']:
        raise ValueError('Global recovery concurrency differs from authorized settings')
    # Stop submission from the fixed four-worker queue. Its running workers
    # drain unchanged, as do the already-draining original reference workers.
    os.kill(legacy_nine_pid, signal.SIGINT)
    time.sleep(2)  # Coordinator signal handling, not an execution deadline.
    nine = exp.read(nine_path)
    original_names = nine['original_live_tasks']
    source_map = {name: run / 'additional_preparation_recovery' / name for name in original_names}
    source_map.update({name: run / 'approved_nine_recovery' / name for name in previous['tasks']})
    external = {name: folder for name, folder in source_map.items() if folder.exists()}
    pending = [name for name in previous['tasks'] if not source_map[name].exists()]
    # Existing directories always belong to their original execution; never
    # submit them again, even if their result file is not finalized yet.
    records = {}
    progress_path = run / 'global_reference_recovery_progress.json'
    progress = {'started_at': exp.now(), 'controller_pid': os.getpid(), 'concurrency': limit,
                'legacy_coordinators': [original_pid, legacy_nine_pid],
                'task_sources': {name: str(path) for name, path in source_map.items()},
                'active': list(external), 'queued': pending, 'finished': records}
    launch.update(pid=os.getpid(), phase_owner='global_reference_recovery',
                  global_reference_controller_pid=os.getpid(),
                  additional_live_roots=[original_pid, legacy_nine_pid, os.getpid()])
    exp.write_json(launch_path, launch)
    exp.write_json(progress_path, progress)
    last_saved = None
    last_auxiliary_check = 0
    with ProcessPoolExecutor(max_workers=limit,
                             mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = {}
        while external or futures or pending:
            if exp.read(request_path).get('status') == 'cancelled':
                raise KeyboardInterrupt
            for name, folder in list(external.items()):
                record = finished_record(folder)
                if record is not None:
                    records[name] = record
                    del external[name]
                    print('[collected existing]', name, record['status'], flush=True)
                elif not any(alive(pid) for pid in progress['legacy_coordinators']):
                    raise ValueError('Existing execution has no final archive or live coordinator: ' + name)
            for future, name in list(futures.items()):
                if not future.done():
                    continue
                try:
                    record = future.result()
                except Exception as error:
                    record = exp._failure(error, secrets)
                    record['source'] = str(source_map[name])
                records[name] = record
                del futures[future]
                print('[global recovery]', name, record['status'], flush=True)
            selected, pending = take_available(pending, len(external), len(futures), limit)
            for name in selected:
                if source_map[name].exists():
                    # Fail closed on a late legacy submission, instead of
                    # replaying the task or overwriting its working directory.
                    external[name] = source_map[name]
                    continue
                futures[pool.submit(exp._baseline_job, name, requests[name],
                                    source_map[name], environment, False)] = name
            progress.update(active=[*external, *futures.values()], queued=pending,
                            finished=records, checked_at=exp.now())
            signature = (tuple(progress['active']), tuple(pending), tuple(records))
            if signature != last_saved:
                exp.write_json(progress_path, exp.redact(progress, secrets))
                last_saved = signature
            # The legacy observer may finish its original collection and write
            # its PID once. Keep the live owner accurate without touching its state.
            fresh_launch = exp.read(launch_path)
            changed = False
            if fresh_launch['pid'] != os.getpid():
                fresh_launch.update(pid=os.getpid(), phase_owner='global_reference_recovery',
                                    additional_live_roots=progress['legacy_coordinators'] + [os.getpid()])
                changed = True
            if time.monotonic() - last_auxiliary_check >= 15:
                before_aux = (fresh_launch.get('mirror_controller_pid'), fresh_launch.get('uv_controller_pid'))
                ensure_auxiliaries(run, fresh_launch)
                changed |= before_aux != (fresh_launch.get('mirror_controller_pid'), fresh_launch.get('uv_controller_pid'))
                last_auxiliary_check = time.monotonic()
            if changed:
                exp.write_json(launch_path, fresh_launch)
            time.sleep(2)
    while any(alive(pid) for pid in progress['legacy_coordinators']):
        if exp.read(request_path).get('status') == 'cancelled':
            raise KeyboardInterrupt
        time.sleep(2)
    # Both legacy state writers have now exited. Commit validated references
    # once and restore their checkpoints before the normal method scheduler resumes.
    current = exp.read(run / 'result.json')
    if current['methods']:
        raise ValueError('A method started before the global reference barrier')
    additional_path = run / 'additional_preparation_recovery_progress.json'
    additional = exp.read(additional_path)
    for name in original_names:
        additional['finished'][name] = records[name]
        if name in additional['active']:
            additional['active'].remove(name)
        current['baselines'][name] = records[name]
    replacements = {name: records[name] for name in previous['tasks']}
    current, additional = integrate(current, additional, replacements)
    current.update(status='running', phase='additional_preparation_recovery')
    current.pop('finished_at', None)
    additional['finished_at'] = exp.now()
    exp.write_json(additional_path, exp.redact(additional, secrets))
    exp.write_json(run / 'result.json', exp.redact(current, secrets))
    nine.update(active=[], finished=replacements, finished_at=exp.now())
    exp.write_json(nine_path, exp.redact(nine, secrets))
    progress.update(active=[], queued=[], finished_at=exp.now())
    exp.write_json(progress_path, exp.redact(progress, secrets))
    launch = exp.read(launch_path)
    ensure_auxiliaries(run, launch)
    with run.with_suffix('.launcher.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=exp.ROOT, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    launch.update(pid=child.pid, phase_owner='experiment', additional_live_roots=[])
    launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': child.pid,
        'reason': 'All original and user-approved references collected with one global concurrency pool'})
    exp.write_json(launch_path, launch)
    print('Global reference collection complete; experiment resumed', child.pid, flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import sys
        if '--run' in sys.argv:
            run = Path(sys.argv[sys.argv.index('--run') + 1]).resolve()
            exp.write_json(run / 'global-reference-recovery-error.json', {
                'at': exp.now(), 'error_type': type(error).__name__, 'error': str(error)})
        raise
