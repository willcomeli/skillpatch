"""Freeze training patches and test initial Skill + supplements on AppWorld test_normal."""

import argparse
from dataclasses import replace
import fcntl
import json
from pathlib import Path
import shutil
from uuid import uuid4

from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split
from excutor.eviskill_runners.launcher import FileSkillBank, run_native
from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from excutor.excutor_runners.patch_sidecars import load_sidecars, write_sidecars
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from set_up.appworld_recover import read
from set_up.appworld_smoke import check_execution
from set_up.appworld_smoke_inputs import _execution_toml
from set_up.appworld_trajectories import load_manifest
from set_up.environment import load_environment, redact, secret_values
from set_up.native_config import load_native_config


def training_entries(training, manifest):
    """Include every generated training patch, regardless of its replay score."""
    if training.get('status') != 'completed':
        raise ValueError('SkillPatch training must finish before final test evaluation')
    failures = {row['task_id'] for row in manifest['rows'] if row['score'] < 1}
    tasks = training['tasks']
    ids = [task['task_id'] for task in tasks]
    if len(ids) != len(set(ids)) or set(ids) != failures:
        raise ValueError('Completed SkillPatch recovery must cover every failed training trajectory')
    original = Path(manifest['initial_skill']['path'])
    entries, provenance = [], []
    for task in tasks:
        if task.get('status') not in {'completed', 'no_supported_issue'}:
            raise ValueError('Unfinished training task cannot be included in final evaluation')
        for issue in task['issues']:
            if issue.get('generation_status') != 'generated':
                continue
            if issue.get('replay_status') != 'completed':
                raise ValueError('Generated patch has no completed source-task replay')
            replay = Path(issue['replay'])
            saved = read(replay / 'result.json')
            if saved['status'] != 'completed':
                raise ValueError('Source-task replay receipt is not completed')
            bundle = replay / 'candidate_skill'
            if (bundle / 'SKILL.md').read_bytes() != original.read_bytes():
                raise ValueError('Patch targets a different initial Skill')
            sidecar = load_sidecars(bundle)
            if sidecar is None or len(sidecar['entries']) != 1:
                raise ValueError('Expected one independently generated patch per replay')
            entries.extend(sidecar['entries'])
            provenance.append({'task_id': task['task_id'], 'issue_id': issue['issue_id'],
                               'bundle': str(bundle), 'hashes': tree_hashes(bundle)})
    if len(entries) != training.get('candidate_count'):
        raise ValueError('Generated patch count differs from the completed training receipt')
    return entries, provenance


def evaluate(training_dir, experiment, output, environment, *, prepare_only=False):
    training_dir, experiment = Path(training_dir).resolve(strict=True), Path(experiment).resolve(strict=True)
    output = Path(output).resolve()
    if any(output.is_relative_to(p) or p.is_relative_to(output) for p in (training_dir, experiment)):
        raise ValueError('Final test output must be separate from training and historical experiments')
    training = read(training_dir / 'result.json')
    manifest_path = Path(training['trajectory_manifest'])
    manifest = load_manifest(manifest_path)
    entries, provenance = training_entries(training, manifest)
    config_path = experiment / 'inputs/execution.toml'
    native = load_native_config(config_path)
    tests = load_official_appworld_split(native.runner_options['data_dir'], 'test_normal')
    forbidden = {row['task_id'] for split in ('train', 'dev')
                 for row in load_official_appworld_split(native.runner_options['data_dir'], split)}
    if not tests or len({row['task_id'] for row in tests}) != len(tests) or any(row['task_id'] in forbidden for row in tests):
        raise ValueError('Test tasks must be unique and disjoint from train/dev')
    plan = {'training_result_sha256': sha256_file(training_dir / 'result.json'),
            'manifest_sha256': sha256_file(manifest_path), 'provenance': provenance,
            'execution_config_sha256': sha256_file(config_path), 'execution_model': native.model,
            'tasks': tests, 'split': 'test_normal', 'patch_count': len(entries),
            'selection_policy': 'all generated training patches in fixed training order; no replay-score filtering',
            'test_feedback_used_for_optimization': False}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / '.test.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / 'plan.json').exists():
            if read(output / 'plan.json') != plan:
                raise ValueError('Frozen test policy, training patches or task list changed')
            if tree_hashes(output / 'input') != read(output / 'input_hashes.json'):
                raise ValueError('Frozen SkillPatch test inputs changed')
        else:
            inputs = output / 'input'
            bundle = inputs / 'skill'
            bundle.mkdir(parents=True)
            shutil.copyfile(manifest['initial_skill']['path'], bundle / 'SKILL.md')
            if entries:
                write_sidecars(bundle, entries)
            write_json(inputs / 'tasks.json', tests)
            config = replace(native, tasks_path=inputs / 'tasks.json', skill_path=bundle / 'SKILL.md', output_root=output)
            (inputs / 'execution.toml').write_text(_execution_toml(config), encoding='utf-8')
            if load_native_config(inputs / 'execution.toml') != config:
                raise ValueError('Test snapshot differs from the frozen executor')
            write_json(output / 'input_hashes.json', tree_hashes(inputs))
            write_json(output / 'plan.json', plan)
        bundle = output / 'input/skill'
        config = load_native_config(output / 'input/execution.toml')
        bank = FileSkillBank(bundle / 'SKILL.md')
        patched = PatchedSkillBank(bank, bundle) if entries else None
        expected = (patched or bank).format_for_prompt({})
        state = {'status': 'prepared' if prepare_only else 'running', 'phase': 'test', 'started_at': now(),
                 'split': 'test_normal', 'total': len(tests), 'completed': 0, 'patch_count': len(entries),
                 'execution_model': config.model, 'test_feedback_used_for_optimization': False}
        def save():
            write_json(output / 'result.json', state)
            print(json.dumps(state, ensure_ascii=False), flush=True)
        save()
        if prepare_only:
            return state
        results = []
        try:
            for ref in tests:
                if tree_hashes(output / 'input') != read(output / 'input_hashes.json'):
                    raise ValueError('Frozen test instructions changed')
                entry = {'id': ref['task_id'], 'split': 'test', 'task_ref': ref}
                task_dir = output / 'tasks' / ref['task_id']
                receipt = task_dir / 'verified.json'
                if receipt.exists():
                    saved = read(receipt)
                    archive = Path(saved['archive'])
                    if sha256_file(archive) != saved['sha256']:
                        raise ValueError('Verified test archive changed')
                else:
                    attempt = task_dir / ('attempt-' + uuid4().hex)
                    attempt.mkdir(parents=True)
                    write_json(attempt / 'tasks.json', [ref])
                    single = replace(config, tasks_path=attempt / 'tasks.json')
                    execution = run_native(single, attempt / 'execution', environment,
                                           **({'patch_skill_dir': bundle} if entries else {}))
                    if execution['status'] == 'cancelled':
                        raise KeyboardInterrupt
                    if execution['status'] != 'completed' or len(execution['task_results']) != 1:
                        raise ValueError(f'Shared execution failed: {execution["status"]}; see {attempt / "execution/result.json"}')
                    archive = Path(execution['task_results'][0])
                row = check_execution(archive, entry, expected)
                if patched and read(archive).get('patch_deployment', {}).get('skill_digest') != patched.digest:
                    raise ValueError('Test task did not receive the frozen patch bundle')
                write_json(receipt, {'archive': str(archive), 'sha256': sha256_file(archive)})
                results.append(row)
                write_json(output / 'tasks.json', results)
                state.update(completed=len(results), success=sum(r['success'] for r in results))
                save()
            state.update(status='completed', phase='finished', success_rate=state['success'] / len(tests), finished_at=now())
        except KeyboardInterrupt:
            state.update(status='cancelled', finished_at=now())
        except Exception as error:
            state.update(status='failed', error=redact(str(error), secret_values(environment)), finished_at=now())
        save()
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training', type=Path, required=True)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    result = evaluate(args.training, args.experiment, args.output, load_environment(args.env_file), prepare_only=args.prepare_only)
    return 0 if result['status'] in {'prepared', 'completed'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
