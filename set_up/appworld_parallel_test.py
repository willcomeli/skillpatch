"""Test frozen initial/Trace2Skill Skills alongside SkillOpt on isolated AppWorld ports."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from method_input_adapters.execution import SharedExecutorBridge
from set_up.appworld_recover import read
from set_up.appworld_smoke import ROOT, check_execution, skill_text
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config


def evaluate_variant(recovery, name, port, environment):
    recovery = Path(recovery).resolve(strict=True)
    inputs = recovery / 'inputs'
    if tree_hashes(inputs) != read(recovery / 'input_hashes.json'):
        raise ValueError('Frozen recovery inputs changed')
    refs = read(inputs / 'test_tasks.json')
    if not refs or any(ref['split'] != 'test_normal' for ref in refs):
        raise ValueError('This experiment only evaluates test_normal')
    plan = read(recovery / 'plan.json')
    if refs != plan['test_tasks']:
        raise ValueError('Parallel tasks differ from the shared final-test plan')
    original_config = inputs / 'execution.toml'
    native = load_native_config(original_config)
    skill = inputs / ('initial_skill' if name == 'initial' else 'trace2skill')
    expected = skill_text(skill)
    frozen = tree_hashes(skill)
    output = recovery / 'test' / name
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.parallel.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        isolated = replace(native, runner_options={**native.runner_options, 'server_port_start': port})
        config_path = output / 'parallel_execution.toml'
        text = _execution_toml(isolated)
        if config_path.exists() and config_path.read_text() != text:
            raise ValueError('Parallel executor configuration changed')
        config_path.write_text(text)
        policy = {'skill_hashes': frozen, 'task_refs': refs, 'source_config_sha256': sha256_file(original_config),
                  'runtime_override': {'server_port_start': port}, 'test_feedback_used_for_optimization': False}
        pin = output / 'parallel_plan.json'
        if pin.exists() and read(pin) != policy:
            raise ValueError('Frozen parallel test policy changed')
        write_json(pin, policy)
        state = {'status': 'running', 'started_at': now(), 'completed': 0, 'total': len(refs),
                 'execution_model': native.model, 'server_port_start': port, 'success': 0}
        write_json(output / 'parallel_status.json', state)
        bridge = SharedExecutorBridge(environment)
        rows = []
        try:
            for ref in refs:
                if tree_hashes(inputs) != read(recovery / 'input_hashes.json'):
                    raise ValueError('Frozen recovery inputs changed during test')
                if tree_hashes(skill) != frozen:
                    raise ValueError('Frozen Skill changed during test')
                entry = {'id': ref['task_id'], 'split': 'test', 'task_ref': ref,
                         'config': str(original_config)}
                identity = {'task': ref, 'text': expected, 'config': sha256_file(original_config)}
                key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                cache = recovery / 'executions' / key
                cache.mkdir(parents=True, exist_ok=True)
                with (cache / '.parallel-task.lock').open('a+b') as task_lock:
                    fcntl.flock(task_lock, fcntl.LOCK_EX)
                    receipt = cache / 'verified.json'
                    if receipt.exists():
                        saved = read(receipt)
                        archive = Path(saved['archive'])
                        if sha256_file(archive) != saved['sha256']:
                            raise ValueError('Verified test archive changed')
                    else:
                        for attempt in range(8):
                            destination = cache / ('parallel-attempt-' + uuid4().hex)
                            try:
                                archive = bridge.run({**entry, 'config': str(config_path)}, '', destination, skill_dir=skill)
                                break
                            except ValueError as error:
                                state_file = destination / 'execution/result.json'
                                details = str(error) + (str(read(state_file).get('errors', [])) if state_file.exists() else '')
                                transient = any(token in details.lower() for token in (
                                    '503', '502', '504', '520', '521', '522', '523', '524', '525', '526', '527', '530', '429', 'timed out', 'timeout', 'connection', 'eof', 'remotedisconnected'))
                                if not transient or attempt == 7:
                                    raise
                                state.update(status='waiting_retry', current_task=ref['task_id'], attempt=attempt + 1)
                                write_json(output / 'parallel_status.json', state)
                                time.sleep(60)
                        row = check_execution(archive, entry, expected)
                        write_json(receipt, {'archive': str(archive), 'sha256': sha256_file(archive),
                                   'runtime_override': {'server_port_start': port},
                                   'actual_config_sha256': sha256_file(archive.parent.parent / 'config.json')})
                    row = check_execution(archive, entry, expected)
                rows.append(row)
                write_json(output / 'tasks.json', rows)
                state.update(status='running', completed=len(rows), success=sum(row['success'] for row in rows))
                write_json(output / 'parallel_status.json', state)
                print(json.dumps({'variant': name, **state}, ensure_ascii=False), flush=True)
            state.update(status='completed', finished_at=now(), scores={'test_normal': {
                'total': len(rows), 'success': state['success'], 'success_rate': state['success'] / len(rows)}})
            write_json(output / 'result.json', state)
        except Exception as error:
            state.update(status='failed', error=redact(str(error), secret_values(environment)), finished_at=now())
        write_json(output / 'parallel_status.json', state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recovery', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--variants', nargs='+', choices=('initial', 'trace2skill'), default=['initial', 'trace2skill'])
    args = parser.parse_args()
    environment = load_environment(args.env_file)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {name: pool.submit(evaluate_variant, args.recovery, name, port, environment)
                for name, port in [('initial', 24600), ('trace2skill', 24700)] if name in args.variants}
        results = {name: future.result() for name, future in jobs.items()}
    write_json(args.recovery / ('test/parallel_result-' + '-'.join(args.variants) + '.json'), results)
    return 0 if all(result['status'] == 'completed' for result in results.values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
