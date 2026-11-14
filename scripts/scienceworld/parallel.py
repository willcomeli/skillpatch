"""Bounded subprocess scheduling; method stages consume results in input order."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from .splits import ROOT, read, save


def worker_count(environment, name):
    raw = environment.get(name, '1')
    if not str(raw).isdigit() or int(raw) < 1:
        raise ValueError(f'{name} must be a positive integer')
    return int(raw)


@contextmanager
def reserve_port():
    """Keep a cross-process lease until the task and its environment exit."""
    root = Path(tempfile.gettempdir()) / f'skillpatch-scienceworld-ports-{os.getuid()}'
    root.mkdir(mode=0o700, exist_ok=True)
    for port in range(8811, 65536):
        lock = (root / f'{port}.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        except (BlockingIOError, OSError):
            lock.close()
            continue
        try:
            yield port
        finally:
            lock.close()
        return
    raise RuntimeError('No free ScienceWorld task port')


def stop_jobs(processes):
    # Each child owns a process group, including its environment and harness.
    # This grace period is cancellation cleanup, not a task runtime limit.
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while any(p.poll() is None for p in processes) and time.monotonic() < deadline:
        time.sleep(0.05)
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_jobs(jobs, environment, workers, *, command=None):
    """Run at most workers jobs; fail fast, preserve checkpoints, order results.

    Jobs contain public input only. Credentials travel in the subprocess
    environment, never in request files or command-line arguments.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be a positive integer')
    jobs = list(jobs)
    folders = [Path(job['folder']).resolve() for job in jobs]
    if len(set(folders)) != len(folders):
        raise ValueError('Concurrent jobs must have distinct output directories')
    command = command or [sys.executable, '-u', '-m', 'scripts.scienceworld.jobs']
    results = [None] * len(jobs)
    active = {}
    cursor = 0
    try:
        while cursor < len(jobs) or active:
            while cursor < len(jobs) and len(active) < workers:
                index = cursor
                folder = folders[index] / '.dispatch'
                folder.mkdir(parents=True, exist_ok=True)
                request, response = folder / 'request.json', folder / 'response.json'
                response.unlink(missing_ok=True)
                save(request, {**jobs[index], 'folder': str(folders[index])})
                lease = reserve_port()
                port = lease.__enter__()
                try:
                    child_env = {**environment, 'SCIENCEWORLD_JOB_PORT': str(port)}
                    process = subprocess.Popen([*command, str(request), str(response)],
                                               cwd=ROOT, env=child_env, start_new_session=True)
                except BaseException:
                    lease.__exit__(*sys.exc_info())
                    raise
                active[index] = (process, response, lease)
                print(f'Job started: {jobs[index]["kind"]} {jobs[index]["entry"]["id"]} '
                      f'(pid={process.pid}, port={port})', flush=True)
                cursor += 1
            for index, (process, response, lease) in list(active.items()):
                if process.poll() is None:
                    continue
                if process.returncode == 130:
                    raise KeyboardInterrupt
                if not response.exists():
                    from .experiment import TransientFailure
                    raise TransientFailure(f'Job process exited without a receipt (code {process.returncode})')
                record = read(response)
                if process.returncode != 0 or record['status'] != 'completed':
                    from .experiment import TransientFailure
                    error = TransientFailure if record.get('retryable') else RuntimeError
                    raise error(record.get('error', 'Job process failed'))
                results[index] = record['value']
                print(f'Job completed: {jobs[index]["kind"]} {jobs[index]["entry"]["id"]}', flush=True)
                lease.__exit__(None, None, None)
                del active[index]
            if active:
                time.sleep(0.05)
    finally:
        stop_jobs([value[0] for value in active.values()])
        for _, _, lease in active.values():
            lease.__exit__(None, None, None)
    return results
