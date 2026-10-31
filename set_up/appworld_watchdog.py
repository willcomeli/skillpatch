"""Persistently check this AppWorld experiment every five minutes and resume exited workers."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from excutor.executor_adapter.storage import now, write_json
from set_up.appworld_smoke import ROOT


def read_optional(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}


def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def recovery_process_running():
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path / 'cmdline').read_bytes().split(b'\0')
        except (OSError, ProcessLookupError):
            continue
        if not args or b'python' not in Path(os.fsdecode(args[0])).name.encode():
            continue
        if any(arg.endswith(b'/resume_skillopt_after_test.py') or arg.endswith(b'/restart_pipeline.py')
               or arg == b'set_up.appworld_recover' for arg in args[1:]):
            return True
    return False


def error_details(state):
    message = str(state.get('error', ''))
    if '; see ' in message:
        path = Path(message.split('; see ', 1)[1])
        if path.is_file():
            message += ' ' + str(read_optional(path).get('errors', []))
    return message


def restart_decision(state, attempts, config_changed=False):
    """Bound repeated attempts at the same progress; do not hide configuration or code errors."""
    if state.get('status') == 'completed':
        return 'completed'
    detail = error_details(state).lower()
    if any(token in detail for token in ('insufficient_balance', 'http request failed: 403', 'error code: 403', 'http request failed: 401', 'error code: 401')):
        return 'restart' if config_changed else 'needs_configuration'
    known = not detail or any(token in detail for token in (
        'incomplete or empty action', 'connection', 'timeout', 'timed out', 'eof', 'remotedisconnected',
        '503', '502', '504', '520', '521', '522', '523', '524', '525', '526', '527', '530', '429', 'stream disconnected', 'temporarily unavailable'))
    if not known:
        return 'needs_review'
    if attempts >= 3 and not config_changed:
        return 'needs_review'
    return 'restart'


def access_restored(recovery, name):
    """A small real request can detect restored credit even when .env did not change."""
    from dataclasses import replace
    from excutor.eviskill_runners.launcher import ChatClient
    from set_up.environment import load_environment
    from set_up.native_config import load_native_config
    environment = load_environment(ROOT / '.env')
    try:
        native = replace(load_native_config(recovery / 'inputs/execution.toml'), timeout_sec=15, max_retries=0)
        reply = ChatClient(native, environment).chat([{'role': 'user', 'content': 'Reply exactly OK.'}])
        if not reply.strip():
            return False
        if name == 'skillopt':
            from openai import OpenAI
            from set_up.optimizer_config import method_optimizer
            optimizer, environment = method_optimizer({'optimizer': recovery / 'inputs/optimizer.json'}, native, environment)
            with OpenAI(api_key=environment[optimizer.api_key_env], base_url=optimizer.base_url,
                        timeout=15, max_retries=0) as client:
                response = client.chat.completions.create(model=optimizer.model,
                    messages=[{'role': 'user', 'content': 'Reply exactly OK.'}], max_completion_tokens=4096)
                if not response.choices[0].message.content or response.choices[0].finish_reason != 'stop':
                    return False
        return True
    except Exception:
        return False


def check(base, state, *, spawn=True):
    recovery = base / 'recovery'
    configuration = hashlib.sha256((ROOT / '.env').read_bytes()).hexdigest()
    old_configuration = state.get('configuration')
    changed = old_configuration is not None and old_configuration != configuration
    state.update(configuration=configuration, checked_at=now(), interval_seconds=300, experiments={})
    attempts = state.setdefault('restart_attempts', {})
    fingerprints = state.setdefault('failure_fingerprints', {})
    environment = dict(os.environ)
    for name in ('EXECUTION_MODEL', 'EXECUTION_API_KEY', 'EXECUTION_BASE_URL', 'EXECUTION_STREAM',
                 'METHOD_MODEL', 'METHOD_API_KEY', 'METHOD_BASE_URL', 'SKILLPATCH_HARNESS',
                 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL', 'GPT_API_KEY', 'GPT_BASE_URL'):
        environment.pop(name, None)
    environment['PYTHONUNBUFFERED'] = '1'
    for name in ('initial', 'trace2skill', 'skillopt', 'skillpatch'):
        folder = recovery / 'test' / name
        result = read_optional(folder / 'result.json')
        status_file = folder / 'parallel_status.json' if name in {'initial', 'trace2skill'} else recovery / 'result.json'
        current = result if result.get('status') == 'completed' else read_optional(status_file)
        if name == 'skillpatch':
            current = result
        active = (locked(folder / '.parallel.lock') if name in {'initial', 'trace2skill'}
                  else recovery_process_running() or locked(recovery / '.recovery.lock') if name == 'skillopt'
                  else False)
        snapshot = {key: current[key] for key in ('status', 'phase', 'completed', 'total', 'success', 'scores', 'error') if key in current}
        if name == 'skillopt' and current.get('skillopt_log'):
            log = Path(current['skillopt_log'])
            if log.exists():
                import re
                progress = re.findall(r'\[analyst\] (\d+/\d+)', log.read_text())
                if progress:
                    snapshot['analysis_progress'] = progress[-1]
                snapshot['log_age_seconds'] = round(time.time() - log.stat().st_mtime)
        snapshot['process_active'] = active
        if result.get('status') == 'completed':
            snapshot['monitor_action'] = 'completed'
        elif active:
            snapshot['monitor_action'] = 'observe'
        else:
            progress = current.get('completed', 0)
            # Verified executions survive trainer restarts, so progress resets only when real work advanced.
            if name == 'skillopt':
                progress = len(list((recovery / 'executions').glob('*/verified.json')))
            fingerprint = hashlib.sha256(str(progress).encode()).hexdigest()
            if changed or fingerprints.get(name) != fingerprint:
                attempts[name] = 0
                fingerprints[name] = fingerprint
            action = restart_decision(current, attempts.get(name, 0), changed)
            if action == 'needs_configuration' and spawn and access_restored(recovery, name):
                action = 'restart'
                attempts[name] = 0
            if name == 'skillpatch' and action != 'completed':
                action = 'needs_review'
            snapshot['monitor_action'] = action
            if action == 'restart' and spawn:
                if name in {'initial', 'trace2skill'}:
                    command = [sys.executable, '-m', 'set_up.appworld_parallel_test', '--recovery', str(recovery),
                               '--env-file', str(ROOT / '.env'), '--variants', name]
                else:
                    command = [sys.executable, str(base / 'resume_skillopt_after_test.py')]
                with (base / f'watchdog-{name}.console.log').open('a') as log:
                    process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                attempts[name] = attempts.get(name, 0) + 1
                snapshot.update(restarted_pid=process.pid, restart_attempt=attempts[name])
        state['experiments'][name] = snapshot
    state['status'] = ('completed' if all(v['monitor_action'] == 'completed' for v in state['experiments'].values()) else 'monitoring')
    write_json(base / 'monitor_status.json', state)
    with (base / 'monitor_history.jsonl').open('a') as history:
        history.write(json.dumps(state, ensure_ascii=False) + '\n')
    print(json.dumps(state, ensure_ascii=False), flush=True)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    args = parser.parse_args()
    base = args.experiment.resolve(strict=True)
    with (base / '.monitor.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = read_optional(base / 'monitor_status.json')
        while True:
            started = time.monotonic()
            try:
                check(base, state)
            except Exception as error:
                # Preserve the monitor itself if a result is temporarily unreadable.
                with (base / 'monitor_errors.log').open('a') as log:
                    log.write(now() + ' ' + type(error).__name__ + '\n')
            if state.get('status') == 'completed':
                return
            time.sleep(max(1, 300 - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
