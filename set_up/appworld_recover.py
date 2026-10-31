"""Reuse the deepened Trace2Skill Skill and restart SkillOpt, then evaluate frozen Skills on test."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import time
from unittest.mock import patch
from uuid import uuid4

from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split
from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from method_input_adapters.execution import SharedExecutorBridge
from set_up.appworld_smoke import ROOT, check_execution, skill_text
from set_up.appworld_smoke_inputs import prepare_inputs
from set_up.appworld_trajectories import load_manifest
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config
from set_up.optimizer_config import method_optimizer


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class CachedBridge(SharedExecutorBridge):
    """Resume officially scored executions; a failed request is never a task score."""

    def __init__(self, environment, cache):
        super().__init__(environment)
        self.cache = Path(cache)
        self.reused = 0

    def run(self, entry, skill_content, output, *, skill_dir=None, previous=None):
        expected = skill_text(skill_dir) if skill_dir else skill_content
        identity = {'task': entry['task_ref'], 'text': expected,
                    'config': sha256_file(Path(entry['config']))}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        receipt = self.cache / key / 'verified.json'
        if receipt.exists():
            saved = read(receipt)
            archive = Path(saved['archive'])
            if sha256_file(archive) != saved['sha256']:
                raise ValueError('Previously verified execution archive changed')
            check_execution(archive, entry, expected)
            self.reused += 1
            return archive
        archive = None
        if previous is not None:
            state_file = Path(previous) / 'result.json'
            if state_file.exists():
                state = read(state_file)
                if state.get('status') == 'completed' and not state.get('errors'):
                    if len(state.get('task_results', [])) != 1:
                        raise ValueError('Expected exactly one task in reusable execution')
                    old = read(Path(previous) / 'config.json')
                    current = load_native_config(Path(entry['config']))
                    for field in ('model', 'temperature', 'max_tokens', 'max_retries', 'timeout_sec',
                                  'api_key_env', 'base_url_env', 'base_url', 'runner_options'):
                        if old.get(field) != getattr(current, field):
                            raise ValueError('Historical execution settings differ: ' + field)
                    archive = Path(state['task_results'][0])
                    check_execution(archive, entry, expected)
                    self.reused += 1
        if archive is None:
            attempt = self.cache / key / ('attempt-' + uuid4().hex)
            archive = super().run(entry, expected, attempt, skill_dir=skill_dir)
            check_execution(archive, entry, expected)
        write_json(receipt, {'archive': str(archive), 'sha256': sha256_file(archive)})
        return archive


def run(source, output, environment, *, prepare_only=False, test_splits=('test_normal',)):
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Recovery output must be separate from the original experiment')
    original = read(source / 'result.json')
    if original['status'] == 'running' and not prepare_only:
        raise ValueError('Original pipeline is still running; use --wait-for to avoid AppWorld port conflicts')
    manifest_path = Path(original['trajectory_manifest'])
    manifest = load_manifest(manifest_path)
    source_config = source / 'inputs/execution.toml'
    native = load_native_config(source_config)
    frozen_optimizer = read(source / 'inputs/optimizer.json')
    if environment.get('METHOD_MODEL') and environment['METHOD_MODEL'] != frozen_optimizer['model']:
        raise ValueError('METHOD_MODEL differs from the original optimization model; do not mix models during recovery')
    trace = source / 'trace2skill/optimization'
    if not (trace / 'evolution_summary.json').is_file():
        raise ValueError('Trace2Skill has no completed evolution to resume')
    skill_text(trace / 'skill')
    if not test_splits or len(set(test_splits)) != len(test_splits) or set(test_splits) - {'test_normal', 'test_challenge'}:
        raise ValueError('Choose distinct official test splits')
    dev = load_official_appworld_split(native.runner_options['data_dir'], 'dev')
    tests = [row for split in test_splits
             for row in load_official_appworld_split(native.runner_options['data_dir'], split)]
    forbidden = {r['task_id'] for r in dev} | {r['task_id'] for r in manifest['rows']}
    if not tests or len({r['task_id'] for r in tests}) != len(tests) or any(r['task_id'] in forbidden for r in tests):
        raise ValueError('Test tasks must be unique and disjoint from train/dev')
    pinned = {'source': str(source), 'manifest_sha256': sha256_file(manifest_path),
              'trace_skill_hashes': tree_hashes(trace / 'skill'),
              'execution_config_sha256': sha256_file(source_config),
              'optimizer_config_sha256': sha256_file(source / 'inputs/optimizer.json'),
              'skillopt_config_sha256': sha256_file(source / 'inputs/skillopt.yaml'),
              'test_tasks': tests, 'validation_tasks': dev}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / '.recovery.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / 'plan.json').exists():
            previous_plan = read(output / 'plan.json')
            if previous_plan != pinned:
                unchanged = {k: v for k, v in previous_plan.items() if k != 'test_tasks'} == {
                    k: v for k, v in pinned.items() if k != 'test_tasks'}
                narrowed = all(row in previous_plan.get('test_tasks', []) for row in tests)
                if not unchanged or not narrowed or (output / 'test/selected_hashes.json').exists():
                    raise ValueError('Recovery inputs changed; use a new output directory')
                if tree_hashes(output / 'inputs') != read(output / 'input_hashes.json'):
                    raise ValueError('Frozen recovery inputs changed')
                backup = output / ('before-test-split-change-' + uuid4().hex)
                backup.mkdir()
                for name in ('plan.json', 'input_hashes.json', 'result.json'):
                    if (output / name).exists():
                        shutil.copyfile(output / name, backup / name)
                shutil.copyfile(output / 'inputs/test_tasks.json', backup / 'test_tasks.json')
                write_json(output / 'inputs/test_tasks.json', tests)
                write_json(output / 'input_hashes.json', tree_hashes(output / 'inputs'))
                write_json(output / 'plan.json', pinned)
            if tree_hashes(output / 'inputs') != read(output / 'input_hashes.json'):
                raise ValueError('Frozen recovery inputs changed')
        else:
            inputs = prepare_inputs(manifest_path, output / 'inputs', dev_limit=len(dev))
            shutil.copytree(trace / 'skill', output / 'inputs/trace2skill')
            for filename in ('optimizer.json', 'skillopt.yaml'):
                shutil.copyfile(source / 'inputs' / filename, output / 'inputs' / filename)
            write_json(output / 'inputs/test_tasks.json', tests)
            write_json(output / 'input_hashes.json', tree_hashes(output / 'inputs'))
            write_json(output / 'plan.json', pinned)
        folder = output / 'inputs'
        inputs = {'manifest_path': str(manifest_path), 'manifest': manifest,
                  'execution_config': folder / 'execution.toml', 'initial_skill': folder / 'initial_skill/SKILL.md',
                  'method_manifest': folder / 'method_tasks.json'}
        state = {'status': 'prepared' if prepare_only else 'running', 'phase': 'prepare',
                 'started_at': now(), 'execution_model': native.model,
                 'method_model': frozen_optimizer['model'], 'test_feedback_used_for_optimization': False,
                 'test_splits': list(test_splits), 'test_tasks_per_skill': len(tests)}
        def save(**updates):
            state.update(updates, updated_at=now())
            write_json(output / 'result.json', state)
            progress = {k: state[k] for k in ('phase', 'status', 'trace_validation_completed', 'variants') if k in state}
            print(json.dumps(progress, ensure_ascii=False), flush=True)
        save()
        if prepare_only:
            return state
        bridge = CachedBridge(environment, output / 'executions')
        def verify():
            load_manifest(manifest_path)
            if tree_hashes(folder) != read(output / 'input_hashes.json'):
                raise ValueError('Frozen recovery inputs changed during execution')
        try:
            verify()
            write_json(output / 'trace2skill/result.json', {'status': 'completed',
                       'selected_skill': str(folder / 'trace2skill'),
                       'validation_split_used': False, 'validation_tasks': 0,
                       'selection_policy': 'fixed deepened Skill; no dev selection'})
            opt_receipt = output / 'skillopt/result.json'
            if opt_receipt.exists():
                opt_result = read(opt_receipt)
                best = Path(opt_result['selected_skill'])
                if opt_result['status'] != 'completed' or sha256_file(best) != opt_result['sha256']:
                    raise ValueError('Selected SkillOpt artifact changed')
            else:
                from method_input_adapters.skillopt.training import train
                optimizer, optimizer_environment = method_optimizer(
                    {'optimizer': folder / 'optimizer.json'}, native, environment)
                attempt = output / 'skillopt' / ('optimization-' + uuid4().hex)
                log_path = attempt.parent / ('training-' + attempt.name + '.log')
                log_path.parent.mkdir(parents=True, exist_ok=True)
                save(phase='skillopt_optimization', skillopt_attempt=str(attempt), skillopt_log=str(log_path))
                with log_path.open('w', buffering=1) as log, redirect_stdout(log), redirect_stderr(log), \
                     patch('method_input_adapters.skillopt.training.SharedExecutorBridge', return_value=bridge):
                    summary = train(inputs['method_manifest'], inputs['initial_skill'], attempt, optimizer_environment,
                        config=folder / 'skillopt.yaml', model=optimizer.model, api_key_env=optimizer.api_key_env,
                        base_url_env=optimizer.base_url_env, frozen_trajectory_manifest=manifest_path)
                verify()
                best = attempt / 'best_skill.md'
                if not best.is_file() or not best.read_text().strip():
                    raise ValueError('SkillOpt produced no selected best_skill.md')
                write_json(opt_receipt, {'status': 'completed', 'selected_skill': str(best),
                           'sha256': sha256_file(best), 'training_summary': summary})
            selected = {'initial': folder / 'initial_skill', 'trace2skill': folder / 'trace2skill'}
            selected_best = output / 'selected_skillopt/SKILL.md'
            if selected_best.exists():
                if selected_best.read_bytes() != best.read_bytes():
                    raise ValueError('Frozen selected SkillOpt differs from best_skill.md')
            else:
                selected_best.parent.mkdir()
                shutil.copyfile(best, selected_best)
            selected['skillopt'] = selected_best.parent
            hashes = {name: tree_hashes(path) for name, path in selected.items()}
            freeze_file = output / 'test/selected_hashes.json'
            if freeze_file.exists() and read(freeze_file) != hashes:
                raise ValueError('Final Skills changed after testing started')
            write_json(freeze_file, hashes)
            save(phase='test', variants={})
            for name, skill in selected.items():
                rows = []
                for ref in tests:
                    verify()
                    if any(tree_hashes(path) != hashes[key] for key, path in selected.items()):
                        raise ValueError('Final Skill changed during test evaluation')
                    entry = {'id': ref['task_id'], 'split': 'test', 'task_ref': ref,
                             'config': str(folder / 'execution.toml')}
                    archive = bridge.run(entry, '', output, skill_dir=skill)
                    rows.append({**check_execution(archive, entry, skill_text(skill)), 'split': ref['split']})
                    write_json(output / 'test' / name / 'tasks.json', rows)
                    state['variants'][name] = {'completed': len(rows), 'total': len(tests)}
                    save()
                metrics = {}
                for split in test_splits:
                    batch = [r for r in rows if r['split'] == split]
                    metrics[split] = {'total': len(batch), 'success': sum(r['success'] for r in batch),
                                      'success_rate': sum(r['success'] for r in batch) / len(batch)}
                state['variants'][name].update(status='completed', scores=metrics)
                write_json(output / 'test' / name / 'result.json', state['variants'][name])
                save()
            save(status='completed', phase='finished', finished_at=now())
        except KeyboardInterrupt:
            save(status='cancelled', finished_at=now())
        except Exception as error:
            save(status='failed', error=redact(f'{type(error).__name__}: {error}', secret_values(environment)), finished_at=now())
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--wait-for', type=Path, help='Wait until the active pipeline status is no longer running/waiting')
    parser.add_argument('--test-splits', nargs='+', choices=('test_normal', 'test_challenge'),
                        default=['test_normal'])
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    environment = {}
    try:
        if args.wait_for:
            print('Waiting for the active AppWorld pipeline to release its server port.', flush=True)
            while read(args.wait_for).get('status') in {'running', 'waiting'}:
                time.sleep(15)
        environment = load_environment(args.env_file)
        result = run(args.source, args.output, environment, prepare_only=args.prepare_only,
                     test_splits=tuple(args.test_splits))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result['status'] in {'prepared', 'completed'} else 2
    except (OSError, ValueError, KeyError) as error:
        print(redact(str(error), secret_values(environment)), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
