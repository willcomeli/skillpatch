"""Fresh high-budget executions of original step/token-limit failures, without optimizing Skills."""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import shutil
import time
from uuid import uuid4

from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from excutor.eviskill_runners.launcher import FileSkillBank, run_native
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from method_input_adapters.archive import load_archive
from set_up.appworld_recover import read
from set_up.appworld_smoke import ROOT, check_execution, skill_text
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config

METHODS = ('initial', 'trace2skill', 'skillopt', 'skillpatch')


def select_tasks(experiment, name):
    breakdown = read(experiment / 'failure_budget_breakdown.json')['methods'][name]
    ids = set(breakdown['step_limit_task_ids'] + breakdown['token_limit_task_ids'])
    if name == 'initial':
        rows = [{'task_id': row['task_id'], 'archive': row['initial_archive']}
                for row in read(experiment / 'task_pair_comparison.json')['rows']]
    else:
        rows = read(experiment / 'recovery/test' / name / 'tasks.json')
    selected = []
    refs = {ref['task_id']: ref for ref in read(experiment / 'recovery/inputs/test_tasks.json')}
    for row in rows:
        if row['task_id'] not in ids:
            continue
        original = read(row['archive'])
        archive = load_archive(Path(row['archive']))
        if archive.success or archive.task_id != row['task_id']:
            raise ValueError('Selected source is not an original officially scored failure')
        step_limit = original['steps'] >= original['max_steps'] and not original['task_completed']
        token_limit = original.get('termination_reason') == 'model_output_limit'
        if not step_limit and not token_limit:
            raise ValueError('Selected source did not exhaust its step/token budget')
        selected.append({'task_ref': refs[row['task_id']], 'original_archive': str(archive.source),
                         'original_sha256': sha256_file(archive.source),
                         'original_steps': original['steps'], 'original_max_steps': original['max_steps'],
                         'limit': 'steps' if step_limit else 'output_tokens'})
    if len(selected) != len(ids):
        raise ValueError('Selected task count differs from the budget-failure report')
    return selected


def transient(errors):
    text = str(errors).lower()
    return any(token in text for token in ('connection', 'remotedisconnected', 'timeout', 'timed out', 'eof',
        'http request failed: 408', 'http request failed: 429', 'http request failed: 500',
        'http request failed: 502', 'http request failed: 503', 'http request failed: 504',
        'http request failed: 520', 'http request failed: 521', 'http request failed: 522',
        'http request failed: 523', 'http request failed: 524', 'http request failed: 525',
        'http request failed: 526', 'http request failed: 527', 'http request failed: 530'))


def execute_variant(experiment, output, name, environment, max_steps, max_tokens):
    source = experiment / 'recovery'
    old_config = load_native_config(source / 'inputs/execution.toml')
    skills = {'initial': source / 'inputs/initial_skill', 'trace2skill': source / 'inputs/trace2skill',
              'skillopt': source / 'selected_skillopt', 'skillpatch': source / 'test/skillpatch/input/skill'}
    if name == 'skillopt':
        selected = read(source / 'skillopt/result.json')
        if sha256_file(skills[name] / 'SKILL.md') != selected['sha256']:
            raise ValueError('Selected SkillOpt differs from the validation-selected best Skill')
    tasks = select_tasks(experiment, name)
    target = output / name
    target.mkdir(parents=True, exist_ok=True)
    with (target / '.variant.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        port = 25000 + METHODS.index(name) * 100
        plan = {'method': name, 'tasks': tasks, 'source_skill_hashes': tree_hashes(skills[name]),
                'source_execution_config_sha256': sha256_file(source / 'inputs/execution.toml'),
                'max_steps': max_steps, 'max_tokens': max_tokens, 'server_port_start': port,
                'post_hoc_budget_sensitivity_rerun': True, 'optimization_performed': False,
                'original_official_scores_preserved': True}
        if (target / 'plan.json').exists():
            if read(target / 'plan.json') != plan or tree_hashes(target / 'input') != read(target / 'input_hashes.json'):
                raise ValueError('Frozen high-budget rerun inputs changed')
        else:
            shutil.copytree(skills[name], target / 'input/skill')
            write_json(target / 'input/tasks.json', [t['task_ref'] for t in tasks])
            delivered = target / 'input/skill/SKILL.md'
            if name != 'skillpatch':
                delivered = target / 'input/delivered_skill.md'
                delivered.write_text(skill_text(target / 'input/skill'), encoding='utf-8')
            config = replace(old_config, max_tokens=max_tokens, tasks_path=target / 'input/tasks.json',
                             skill_path=delivered, output_root=target,
                             runner_options={**old_config.runner_options, 'max_steps': max_steps, 'server_port_start': port})
            (target / 'input/execution.toml').write_text(_execution_toml(config))
            assert load_native_config(target / 'input/execution.toml') == config
            assert tree_hashes(target / 'input/skill') == plan['source_skill_hashes']
            write_json(target / 'input_hashes.json', tree_hashes(target / 'input'))
            write_json(target / 'plan.json', plan)
        config = load_native_config(target / 'input/execution.toml')
        bundle = target / 'input/skill'
        patched = PatchedSkillBank(FileSkillBank(bundle / 'SKILL.md'), bundle) if name == 'skillpatch' else None
        expected = patched.format_for_prompt({}) if patched else skill_text(bundle)
        state = {'status': 'running', 'method': name, 'started_at': now(), 'total': len(tasks), 'completed': 0,
                 'success': 0, 'max_steps': max_steps, 'max_tokens': max_tokens, 'tasks': [],
                 'post_hoc_budget_sensitivity_rerun': True}
        def save():
            state['updated_at'] = now()
            write_json(target / 'result.json', state)
            print(json.dumps({k: state[k] for k in ('method', 'status', 'total', 'completed', 'success')}, ensure_ascii=False), flush=True)
        save()
        for task in tasks:
            ref = task['task_ref']
            row = {'task_id': ref['task_id'], 'original_limit': task['limit'], 'status': 'running'}
            state['tasks'].append(row)
            save()
            receipt = target / 'tasks' / ref['task_id'] / 'verified.json'
            try:
                if receipt.exists():
                    saved = read(receipt)
                    archive = Path(saved['archive'])
                    if sha256_file(archive) != saved['sha256']:
                        raise ValueError('Verified high-budget rerun archive changed')
                else:
                    for attempt in range(1, 9):
                        if tree_hashes(target / 'input') != read(target / 'input_hashes.json'):
                            raise ValueError('Frozen high-budget rerun inputs changed')
                        attempt_dir = receipt.parent / ('attempt-' + uuid4().hex)
                        write_json(attempt_dir / 'tasks.json', [ref])
                        single = replace(config, tasks_path=attempt_dir / 'tasks.json')
                        result = run_native(single, attempt_dir / 'execution', environment,
                                            **({'patch_skill_dir': bundle} if patched else {}))
                        if result['status'] == 'cancelled':
                            raise KeyboardInterrupt
                        if result['status'] == 'completed' and len(result['task_results']) == 1:
                            archive = Path(result['task_results'][0])
                            break
                        row.update(infrastructure_attempt=attempt,
                                   last_error=redact(str(result.get('errors', [])), secret_values(environment)))
                        save()
                        if not transient(result.get('errors', [])) or attempt == 8:
                            raise ValueError(row['last_error'])
                        time.sleep(60)
                scored = check_execution(archive, {'task_ref': ref}, expected)
                if patched and read(archive).get('patch_deployment', {}).get('skill_digest') != patched.digest:
                    raise ValueError('High-budget execution did not receive the fixed patches')
                raw = read(archive)
                if raw['max_steps'] != max_steps:
                    raise ValueError('Executed step budget differs from the plan')
                write_json(receipt, {'archive': str(archive), 'sha256': sha256_file(archive)})
                row.update(status='completed', success=scored['success'], steps=scored['steps'],
                           archive=str(archive), termination_reason=raw.get('termination_reason'))
                state['completed'] += 1
                state['success'] += bool(scored['success'])
            except KeyboardInterrupt:
                state.update(status='cancelled', finished_at=now())
                save()
                return state
            except Exception as error:
                row.update(status='infrastructure_error', error=redact(str(error), secret_values(environment)))
            save()
        state.update(status='completed' if state['completed'] == len(tasks) else 'incomplete', finished_at=now())
        save()
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--max-steps', type=int, default=300)
    parser.add_argument('--max-tokens', type=int, default=32768)
    args = parser.parse_args()
    if args.max_steps <= 0 or args.max_tokens <= 0:
        parser.error('Budgets must be positive')
    experiment, output = args.experiment.resolve(strict=True), args.output.resolve()
    selected = {name: select_tasks(experiment, name) for name in METHODS}
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.run.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = {'status': 'running', 'started_at': now(), 'max_steps': args.max_steps, 'max_tokens': args.max_tokens,
                 'monitor_interval_seconds': 300, 'methods': {},
                 'selected_counts': {name: len(rows) for name, rows in selected.items()},
                 'task_method_pairs': sum(len(rows) for rows in selected.values()),
                 'distinct_tasks': len({row['task_ref']['task_id'] for rows in selected.values() for row in rows})}
        write_json(output / 'result.json', state)
        restarts = {name: 0 for name in METHODS}
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {pool.submit(execute_variant, experiment, output, name, load_environment(args.env_file),
                                args.max_steps, args.max_tokens): name for name in METHODS}
            while jobs:
                done, _ = wait(jobs, timeout=300)
                for job in done:
                    name = jobs.pop(job)
                    try:
                        result = job.result()
                    except Exception as error:
                        result = {'status': 'failed', 'error': redact(str(error), secret_values(load_environment(args.env_file)))}
                    state['methods'][name] = result
                    errors = [row.get('error', '') for row in result.get('tasks', []) if row.get('status') == 'infrastructure_error']
                    if result['status'] == 'incomplete' and transient(errors) and restarts[name] < 3:
                        restarts[name] += 1
                        jobs[pool.submit(execute_variant, experiment, output, name, load_environment(args.env_file),
                                         args.max_steps, args.max_tokens)] = name
                        state.setdefault('infrastructure_restarts', {})[name] = restarts[name]
                snapshot = {name: read(output / name / 'result.json') for name in METHODS
                            if (output / name / 'result.json').exists()}
                state.update(checked_at=now(), progress={name: {k: r.get(k) for k in ('status', 'completed', 'total', 'success')}
                                                       for name, r in snapshot.items()})
                write_json(output / 'result.json', state)
                with (output / 'monitor_history.jsonl').open('a') as log:
                    log.write(json.dumps({'checked_at': state['checked_at'], 'progress': state['progress']}) + '\n')
        state.update(status='completed' if all(r['status'] == 'completed' for r in state['methods'].values()) else 'incomplete', finished_at=now())
        write_json(output / 'result.json', state)
        return 0 if state['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
