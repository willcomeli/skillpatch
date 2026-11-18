"""Backfill free SkillOpt slots while existing recovery workers finish unchanged."""

import argparse
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait
import fcntl
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import time

from . import experiment as exp
from .resume_methods import preflight


def drain_queue(jobs, legacy, worker, concurrency, on_result, heartbeat, retire_legacy,
                *, executor_factory=None):
    """Account for live inherited jobs before submitting fresh work."""
    pending = dict(legacy)
    remaining = iter(jobs)
    queued = len(jobs)
    futures = {}
    retired = False
    pool = (ProcessPoolExecutor(max_workers=concurrency,
            mp_context=multiprocessing.get_context('spawn')) if executor_factory is None
            else executor_factory(max_workers=concurrency))
    try:
        while queued or futures or pending:
            for key, path in list(pending.items()):
                if not path.exists():
                    continue
                result = exp.read(path)
                if result.get('status') not in {'completed', 'failed', 'blocked'}:
                    raise ValueError(f'Unexpected inherited task checkpoint: {path}')
                on_result(key, result)
                del pending[key]
            if not pending and not retired:
                retire_legacy()
                retired = True
            for future in [f for f in futures if f.done()]:
                key = futures.pop(future)
                try:
                    result = future.result()
                except Exception as error:
                    result = {'status': 'failed', 'error': type(error).__name__}
                on_result(key, result)
            while queued and len(futures) + len(pending) < concurrency:
                key, arguments = next(remaining)
                futures[pool.submit(worker, *arguments)] = key
                queued -= 1
            heartbeat(list(pending), list(futures.values()), queued)
            if futures:
                wait(futures, timeout=1, return_when=FIRST_COMPLETED)
            elif pending:
                time.sleep(1)
        pool.shutdown(wait=True)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise


def owned_dispatcher(pid, output):
    process = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,command='], capture_output=True, text=True)
    if process.returncode or str(output) not in process.stdout or 'scripts.skillsbench.resume_methods' not in process.stdout:
        raise ValueError('Expected the owned live interrupted-batch dispatcher')
    if os.getpgid(pid) != pid or pid == os.getpgrp():
        raise ValueError('Cannot isolate the old dispatcher process group')
    return process.stdout.split()[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--old-dispatcher-pid', type=int, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=True)
    old_pid = args.old_dispatcher_pid
    lock = (output / 'queue-handoff.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    launcher_path = output.with_suffix('.launcher.json')
    launch = exp.read(launcher_path)
    if launch['pid'] != old_pid or launch.get('phase_owner') != 'method_resume':
        raise ValueError('Dispatcher already handed off; refuse duplicate scheduling')
    command = launch['command']
    preflight(command, output)
    config = Path(command[command.index('--config') + 1])
    environment = exp.load_environment(Path(command[command.index('--env-file') + 1]))
    cfg, tasks, native, optimizer, environment = exp.load_settings(config, environment)
    requests = {task.name: exp.request_for(task, cfg, native) for task in tasks}
    if 'T' in owned_dispatcher(old_pid, output):
        raise ValueError('Another controller has already held this dispatcher')
    os.kill(old_pid, signal.SIGSTOP)
    committed = False
    try:
        state = exp.read(output / 'result.json')
        if state['status'] != 'running' or state['phase'] != 'skillopt':
            raise ValueError('Experiment is no longer in the expected running phase')
        audit = output / 'queue_handoff'
        audit.mkdir(exist_ok=False)
        exp.write_json(audit / 'previous-result.json', state)
        exp.write_json(audit / 'previous-launcher.json', launch)
        legacy = {}
        for key in exp.read(output / 'resume_after_docker_cleanup/plan.json')['tasks']:
            if key not in state['methods']:
                legacy[key] = output / 'resume_after_docker_cleanup' / key.split('/', 1)[1] / 'result.json'
        if not legacy:
            raise ValueError('No inherited tasks remain; use the original resume path')
        exp.adopt_completed_references(state, requests, output)
        jobs = []
        for name, request in requests.items():
            key = 'skillopt/' + name
            if key in state['methods'] or key in legacy:
                continue
            folder = output / 'skillopt' / name
            if folder.exists():
                raise ValueError(f'Unexpected existing pending task folder: {name}')
            if state['baselines'][name]['status'] != 'completed':
                raise ValueError(f'Pending task lacks an approved reference: {name}')
            jobs.append((key, ('skillopt', name, request, state['baselines'][name], folder,
                environment, optimizer, config.parent / cfg['skillopt_config'])))
        exp.write_json(audit / 'plan.json', {'at': exp.now(),
            'runner_sha256': exp.sha256_file(Path(__file__)), 'concurrency': 10,
            'inherited_jobs': list(legacy), 'new_jobs': [key for key, _ in jobs],
            'policy': 'No inherited Agent or method restart; share ten slots, '
                      'complete all SkillOpt before Trace2Skill; preserve previous failures'})
        launch = exp.read(launcher_path)
        launch.update(pid=os.getpid(), phase_owner='skillopt_queue', suspended_dispatcher_pid=old_pid,
                      additional_live_roots=[old_pid])
        launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': os.getpid(),
            'reason': 'Remove unintended interrupted-batch barrier; reuse all live workers and keep total task concurrency10'})
        exp.write_json(launcher_path, launch)
        exp.write_json(output / 'result.json', state)
        exp.write_json(output / 'trajectory_manifest.json', state['baselines'])
        committed = True
        print(f'Adopting {len(legacy)} live tasks; backfilling {len(jobs)} pending SkillOpt tasks, concurrency10', flush=True)

        def collect(key, result):
            state['methods'][key] = result
            exp.write_json(output / 'result.json', exp.redact(state, exp.secret_values(environment)))
            print(f'[queue] {key}: {result["status"]}', flush=True)

        def heartbeat(inherited, active, queued):
            exp.write_json(audit / 'progress.json', {'at': exp.now(), 'pid': os.getpid(),
                'legacy_active': inherited, 'new_active': active, 'queued': queued, 'concurrency': 10,
                'agent_or_method_restarted_for_handoff': False})

        def retire():
            # Each inherited worker writes result.json only after its method
            # and native executor have returned. All old workers are now idle;
            # never resume the stale parent to write its older state snapshot.
            owned_dispatcher(old_pid, output)
            os.killpg(old_pid, signal.SIGTERM)
            os.kill(old_pid, signal.SIGCONT)
            current = exp.read(launcher_path)
            current.update(additional_live_roots=[], old_dispatcher_retired_at=exp.now())
            exp.write_json(launcher_path, current)
            exp.write_json(audit / 'old-dispatcher-retired.json', {'at': exp.now(), 'pid': old_pid,
                'all_inherited_task_checkpoints_collected': True})

        drain_queue(jobs, legacy, exp._method_job, 10, collect, heartbeat, retire)
        current = exp.read(launcher_path)
        current.update(pid=os.getpid(), phase_owner='experiment', additional_live_roots=[])
        exp.write_json(launcher_path, current)
        exp.write_json(audit / 'finished.json', {'at': exp.now(), 'next_phase': 'trace2skill'})
        print('SkillOpt queue collected; continuing the original experiment', flush=True)
        os.execv(command[0], command)
    except BaseException as error:
        if not committed:
            try:
                os.kill(old_pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        exp.write_json(output / 'queue-handoff-error.json', {'at': exp.now(),
            'error_type': type(error).__name__, 'ownership_transferred': committed})
        raise


if __name__ == '__main__':
    main()
