"""Finish baselines, retry execution timeouts once, and collect a review bundle."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from scripts.skillsbench import experiment as exp
from set_up.environment import load_environment, redact, secret_values


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    os.replace(temporary, path)


def classify(raw):
    raw = raw or {}
    summary = (raw.get('execution') or {}).get('summary') or {}
    info = summary.get('agent_timeout_info') or {}
    errors = raw.get('errors', [])
    if info.get('reason') == 'wall_clock_timeout' or any(
            'Agent prompt exceeded wall-clock budget' in str(e.get('message', '')) for e in errors):
        return {'category': 'task_timeout', 'score': 0.0}
    score = (raw.get('feedback') or {}).get('metrics', {}).get('reward')
    if raw.get('status') == 'completed' and isinstance(score, (int, float)) and math.isfinite(score):
        return {'category': 'scored', 'score': float(score)}
    return {'category': 'exception', 'score': None}


def select_attempt(first, retry=None):
    if retry and retry['category'] in {'scored', 'task_timeout'}:
        return 'timeout-rerun', retry
    return 'first-pass', first


def score_summary(rows):
    rows = list(rows)
    normal = [r for r in rows if r['category'] in {'scored', 'task_timeout'}]
    return {'total': len(rows), 'normal_tasks': len(normal),
            'full_success': sum(r['score'] == 1.0 for r in normal),
            'partial_score': sum(0 < r['score'] < 1 for r in normal),
            'task_failure_zero': sum(r['score'] == 0 for r in normal),
            'execution_timeouts': sum(r['category'] == 'task_timeout' for r in rows),
            'exceptions': sum(r['category'] == 'exception' for r in rows)}


def source_active(config):
    try:
        text = Path(f"/proc/{config['source_pid']}/cmdline").read_bytes().replace(b'\0', b' ').decode()
        return '-m scripts.skillsbench.experiment' in text and config['source_output'] in text
    except FileNotFoundError:
        return False


def collect_first(config, plan, final=False):
    source = Path(config['source_output'])
    saved = read_json(source / 'result.json', {})
    records = dict(saved.get('baselines', {}))
    for name in plan['tasks']:
        folder = source / 'baseline' / name
        if name in records:
            records[name] = dict(records[name])
            records[name].setdefault('source', str(folder))
        raw = read_json(folder / 'result.json')
        if raw and raw.get('status') not in {'running', 'prepared'}:
            records[name] = {'status': 'completed' if raw.get('status') == 'completed' else 'failed',
                             'source': str(folder)}
        elif name not in records and final:
            records[name] = {'status': 'failed', 'source': str(folder),
                             'error': 'No persisted terminal result after baseline coordinator exited'}
    if final:
        for row in records.values():
            folder = Path(row['source'])
            if folder.is_dir():
                row['hashes'] = exp.tree_hashes(folder)
    return records


def describe(record):
    path = Path(record['source'])
    raw = read_json(path / 'result.json', {})
    return {**classify(raw), 'source_archive': str(path), 'raw_status': raw.get('status'),
            'error_codes': [e.get('code') for e in raw.get('errors', [])]}


def verify_retry_settings(plan, names):
    config = exp.ROOT / 'configs/skillsbench/experiment.toml'
    cfg, tasks, native, optimizer, environment = exp.load_settings(config, load_environment(exp.ROOT / '.env'), names)
    if exp.sha256_file(config) != plan['config_sha256']:
        raise ValueError('Execution configuration changed before timeout retry')
    if native.model != plan['execution_model'] or native.base_url != plan['execution_base_url']:
        raise ValueError('Execution model or endpoint changed before timeout retry')
    if plan.get('bench_wrapper_sha256') and exp.sha256_file(exp.ROOT / 'scripts/skillsbench/bench.py') != plan['bench_wrapper_sha256']:
        raise ValueError('BenchFlow wrapper changed before timeout retry')
    for task in tasks:
        if exp.tree_hashes(task) != plan['task_hashes'][task.name]:
            raise ValueError('Task changed before timeout retry: ' + task.name)
    for name, expected in plan['implementation_hashes'].items():
        actual = {p: h for p, h in exp.tree_hashes(exp.ROOT / name).items()
                  if '__pycache__' not in Path(p).parts and not p.endswith(('.pyc', '.pyo'))}
        if actual != expected:
            raise ValueError('Protected implementation changed: ' + name)


def merge_bundle(root, plan, first, retry):
    merged = root / 'merged'
    merged.mkdir()
    (merged / 'selected').mkdir()
    rows = {}
    for name in plan['tasks']:
        attempts = {}
        for label, records in [('first-pass', first), ('timeout-rerun', retry)]:
            if name not in records:
                continue
            record = records[name]
            source = Path(record['source'])
            target = merged / 'tasks' / name / label
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                expected = record.get('hashes') or exp.tree_hashes(source)
                shutil.copytree(source, target, symlinks=True)
                if exp.tree_hashes(target) != expected:
                    raise ValueError('Copied archive failed hash verification: ' + name + '/' + label)
            else:
                target.mkdir()
                atomic_json(target / 'missing-archive.json', record)
            attempts[label] = {**describe(record), 'copied_archive': str(target)}
        chosen, selected = select_attempt(attempts['first-pass'], attempts.get('timeout-rerun'))
        (merged / 'selected' / name).symlink_to(Path('../tasks') / name / chosen, target_is_directory=True)
        rows[name] = {'task': name, 'attempts': attempts, 'selected_attempt': chosen,
                      'selected_category': selected['category'], 'selected_score': selected['score'],
                      'selected_archive': selected['copied_archive']}
        print('Merged and verified:', name, flush=True)
    summary = score_summary({'category': r['selected_category'], 'score': r['selected_score']} for r in rows.values())
    atomic_json(merged / 'manifest.json', {'selection_policy': 'latest non-exception retry; never maximum score', 'tasks': rows})
    atomic_json(merged / 'summary.json', summary)
    with (merged / 'summary.csv').open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['task', 'first_category', 'first_score', 'retry_category', 'retry_score', 'selected_attempt', 'selected_category', 'selected_score'])
        for name, row in rows.items():
            a = row['attempts']['first-pass']; b = row['attempts'].get('timeout-rerun', {})
            writer.writerow([name, a['category'], a['score'], b.get('category'), b.get('score'), row['selected_attempt'], row['selected_category'], row['selected_score']])
    (merged / 'README.txt').write_text('首轮、执行超时重跑和统一 selected 视图均在本目录。\n执行超时按 0 分统计；基础设施和验证异常单独记录。\n优先采用重跑的非异常结果，不按两次最高分择优。两次原始记录均保留。\n原始 JSON 中绝对路径保持不变，原输出目录也保留；manifest.json 记录新旧路径。\n三种优化方法未执行，等待用户决定。\n')
    return summary


def run_workflow(config):
    root = Path(config['root'])
    source = Path(config['source_output'])
    plan = read_json(source / 'plan.json')
    first_folder = root / 'first-pass'
    first_folder.mkdir(exist_ok=True)
    atomic_json(first_folder / 'plan.json', plan)
    last_count = -1
    while True:
        active = source_active(config)
        records = collect_first(config, plan, final=not active)
        descriptions = {n: describe(row) for n, row in records.items()}
        state = {'status': 'running' if active else 'completed_with_errors',
                 'phase': 'baseline' if active else 'baseline_finished', 'baselines': records,
                 'methods': {}, 'concurrency': config['concurrency'], 'method_stage': 'awaiting_user'}
        atomic_json(first_folder / 'result.json', state)
        progress = {'status': 'running', 'phase': 'first_pass', 'methods_started': False,
                    'total_tasks': len(plan['tasks']), 'first_pass_recorded': len(records),
                    'first_pass_summary': score_summary(descriptions.values()),
                    'updated_at': datetime.now(timezone.utc).isoformat()}
        atomic_json(root / 'result.json', progress)
        if len(records) != last_count:
            print('First pass recorded:', len(records), '/', len(plan['tasks']), flush=True)
            last_count = len(records)
        if not active:
            break
        time.sleep(10)
    assert len(records) == len(plan['tasks'])
    first = records
    atomic_json(first_folder / 'trajectory_manifest.json', first)
    atomic_json(first_folder / 'summary.json', progress['first_pass_summary'])
    names = [n for n in plan['tasks'] if descriptions[n]['category'] == 'task_timeout']
    atomic_json(root / 'timeout-tasks.json', names)
    print('First pass finished. Retrying execution timeouts once:', len(names), flush=True)
    retry_folder = root / 'timeout-rerun'
    retry_records = {}
    if names:
        verify_retry_settings(plan, names)
        command = [sys.executable, '-m', 'scripts.skillsbench.experiment', '--baseline-only',
                   '--concurrency', str(config['concurrency']), '--output', str(retry_folder), '--tasks', *names]
        with (root / 'timeout-rerun-console.log').open('w') as log:
            process = subprocess.Popen(command, cwd=exp.ROOT, stdout=log, stderr=subprocess.STDOUT)
        atomic_json(root / 'retry-launch.json', {'pid': process.pid, 'tasks': names, 'output': str(retry_folder), 'baseline_only': True})
        while True:
            code = process.poll()
            retry_state = read_json(retry_folder / 'result.json', {})
            retry_records = dict(retry_state.get('baselines', {}))
            progress.update(phase='timeout_retry', retry_total=len(names), retry_recorded=len(retry_records),
                            updated_at=datetime.now(timezone.utc).isoformat())
            atomic_json(root / 'result.json', progress)
            if code is not None:
                break
            time.sleep(10)
        if retry_state.get('methods'):
            raise ValueError('Unexpected method records in baseline-only retry')
        for name in names:
            row = retry_records.setdefault(name, {'status': 'failed', 'error': 'Retry has no persisted result'})
            row.setdefault('source', str(retry_folder / 'baseline' / name))
        atomic_json(root / 'retry-records.json', retry_records)
    progress.update(phase='merging', retry_recorded=len(retry_records))
    atomic_json(root / 'result.json', progress)
    summary = merge_bundle(root, plan, first, retry_records)
    progress.update(status='ready_for_review', phase='waiting_for_user', merged_summary=summary,
                    merged_folder=str(root / 'merged'), methods_started=False,
                    finished_at=datetime.now(timezone.utc).isoformat())
    atomic_json(root / 'result.json', progress)
    print('Ready for user review. Methods remain stopped.', flush=True)
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = read_json(args.config)
    try:
        run_workflow(config)
    except Exception as error:
        secrets = secret_values(load_environment(exp.ROOT / '.env'))
        message = redact(f'{type(error).__name__}: {error}', secrets)
        atomic_json(Path(config['root']) / 'result.json', {'status': 'failed', 'phase': 'workflow_error',
                    'methods_started': False, 'error': message, 'updated_at': datetime.now(timezone.utc).isoformat()})
        print(message, flush=True)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
