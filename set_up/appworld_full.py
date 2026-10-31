"""Full official AppWorld training batch, shared across three one-round methods."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from excutor.eviskill_runners.appworld_manifests import load_official_appworld_split
from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from set_up.appworld_smoke import METHODS, ROOT, RUNNERS, prepare_method_configs
from set_up.appworld_smoke_inputs import _execution_toml, prepare_inputs
from set_up.appworld_trajectories import collect, load_manifest
from set_up.environment import load_environment, redact, redact_artifacts, secret_values
from set_up.native_config import NativeRunConfig, load_native_config
from set_up.optimizer_config import resolve_optimizer


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def full_splits(config, refs):
    """Reject a smoke subset rather than silently label it a full experiment."""
    if config.benchmark != 'appworld' or config.skill_path is None:
        raise ValueError('Full experiments require AppWorld and an initial Skill')
    data_dir = config.runner_options['data_dir']
    train = load_official_appworld_split(data_dir, 'train')
    dev = load_official_appworld_split(data_dir, 'dev')
    if (not isinstance(refs, list) or len(refs) != len(train)
            or any(not isinstance(row, dict) or row.get('split') != 'train' for row in refs)
            or {row.get('task_id') for row in refs} != {row['task_id'] for row in train}):
        raise ValueError('Full experiment requires every official train task exactly once; subset supplied')
    if not train or not dev or {row['task_id'] for row in train} & {row['task_id'] for row in dev}:
        raise ValueError('Official train/dev splits must be nonempty and disjoint')
    return train, dev


def run_full(output, environment, *, config_path=None, manifest_path=None,
             methods=METHODS, prepare_only=False, max_output_tokens=32768, optimizer_config_path=None):
    """Collect once, then run selected methods sequentially on the same evidence.

    This is one offline improvement round, with all official dev tasks for the
    baselines. It does not perform final test-set evaluation or merge patches.
    """
    methods = tuple(methods)
    if not methods or len(set(methods)) != len(methods) or set(methods) - set(METHODS):
        raise ValueError('Choose distinct supported methods')
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError('max_output_tokens must be a positive integer')
    if manifest_path is not None and config_path is not None:
        raise ValueError('Use either a collection config or a frozen manifest')
    manifest = None
    if manifest_path is not None:
        manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
        manifest = load_manifest(manifest_path)
        raw = read(manifest['execution_config']['path'])
        native = NativeRunConfig(**{
            **raw, **{name: Path(raw[name]) for name in ('tasks_path', 'skill_path', 'output_root')}})
        native = replace(native, tasks_path=Path(manifest['tasks_snapshot']['path']),
                         skill_path=Path(manifest['initial_skill']['path']))
        refs = read(manifest['tasks_snapshot']['path'])
    else:
        native = load_native_config(config_path or ROOT / 'configs/appworld/execution.full.toml', environment)
        refs = read(native.tasks_path)
    _, dev = full_splits(native, refs)
    endpoint = native.base_url or environment.get(native.base_url_env)
    if not endpoint:
        raise ValueError('A model base URL is required')
    if not prepare_only and not environment.get(native.api_key_env):
        raise ValueError(f'Missing credential environment variable: {native.api_key_env}')
    native = replace(native, base_url=endpoint)
    environment = {**environment, native.base_url_env: endpoint}
    optimizer = resolve_optimizer(optimizer_config_path, native, environment,
                                  require_credentials=not prepare_only)
    secrets = secret_values(environment)
    output = Path(output).expanduser().resolve()
    if manifest is not None:
        protected = [manifest_path.parent, Path(manifest['source_run']['path']).parent]
        if any(output.is_relative_to(path) or path.is_relative_to(output) for path in protected):
            raise ValueError('Experiment output must be outside frozen archive directories and their parents')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    state = {'status': 'running', 'profile': 'experiment', 'started_at': now(),
             'phase': 'prepare', 'methods': {}, 'methods_selected': list(methods),
             'train_task_ids': [row['task_id'] for row in refs],
             'validation_task_ids': [row['task_id'] for row in dev],
             'full_train_coverage': True, 'full_dev_coverage_planned': True,
             'test_split_used': False, 'complete_three_method_coverage': False,
             'optimizer': optimizer.metadata(),
             'scope': 'full train, one shared-evidence improvement round; no final test-set evaluation',
             'completion_condition': 'method execution completed; no change or no improvement is a valid result',
             'validation_policy': {'trace2skill': 'no dev; fixed deepened Skill',
                                   'skillopt': 'all official dev and original acceptance gate',
                                   'skillpatch': 'no dev; every generated candidate replays its source task'}}
    configs = None
    collection_hashes = None

    def save():
        write_json(output / 'result.json', redact(state, secrets))

    try:
        save()
        write_json(output / 'plan.json', {**state, 'prepare_only': prepare_only,
                   'execution_model': native.model, 'base_url': endpoint,
                   'api_key_env': native.api_key_env,
                   'initial_skill_sha256': sha256_file(native.skill_path),
                   'baseline_collection': 'reuse frozen full batch' if manifest else 'collect full train once',
                   'skillopt': {'num_epochs': 1, 'batch_size': len(refs), 'minibatch_size': 1,
                                'accumulation': 1, 'use_gate': True, 'eval_test': False},
                   'skillpatch': {'task_limit': 0, 'max_issues_per_task': 0,
                                  'acceptance': 'not_decided'}})
        if manifest is None:
            snapshot = output / 'collection_inputs'
            snapshot.mkdir()
            write_json(snapshot / 'train_tasks.json', refs)
            (snapshot / 'SKILL.md').write_text(native.skill_path.read_text(encoding='utf-8'), encoding='utf-8')
            native = replace(native, tasks_path=snapshot / 'train_tasks.json',
                             skill_path=snapshot / 'SKILL.md', output_root=output / 'shared')
            config_snapshot = snapshot / 'execution.toml'
            config_snapshot.write_text(_execution_toml(native), encoding='utf-8')
            if load_native_config(config_snapshot) != native:
                raise ValueError('Collection config snapshot differs from planned execution')
            # Validate and freeze optimizer settings before spending on full collection.
            configs = prepare_method_configs({'execution_config': config_snapshot,
                                              'train_entries': refs}, profile='experiment', optimizer=optimizer)
            collection_hashes = tree_hashes(snapshot)
            state.update(method_configs_prepared=True, method_configs=configs)
            state['phase'] = 'collect'
            save()
            print(f'Collect all {len(refs)} train tasks once; log: {output / "collection.log"}', flush=True)
            with (output / 'collection.log').open('w', encoding='utf-8', buffering=1) as log, \
                    redirect_stdout(log), redirect_stderr(log):
                collected = collect(config_snapshot, output / 'shared', environment,
                                    limit=0, prepare_only=prepare_only)
            state['collection_result'] = str(output / 'shared/result.json')
            if collected['status'] == 'cancelled':
                raise KeyboardInterrupt
            if collected['status'] != ('prepared' if prepare_only else 'completed'):
                raise ValueError('Shared collection failed; inspect shared/execution/result.json and collection.log')
            if tree_hashes(snapshot) != collection_hashes:
                raise ValueError('Collection or method settings changed during collection')
            if prepare_only:
                state.update(status='prepared', phase='finished')
                return state
            manifest_path = Path(collected['manifest_path'])
            manifest = load_manifest(manifest_path)
        state.update(trajectory_manifest=str(manifest_path), manifest_sha256=sha256_file(manifest_path))
        inputs = prepare_inputs(manifest_path, output / 'inputs', dev_limit=len(dev))
        if [entry['id'] for entry in inputs['validation_entries']] != state['validation_task_ids']:
            raise ValueError('Prepared dev tasks differ from the full official dev split')
        if configs is None:
            configs = prepare_method_configs(inputs, profile='experiment', optimizer=optimizer)
        state.update(method_configs_prepared=True, method_configs=configs)
        input_hashes = tree_hashes(output / 'inputs')

        def verify():
            if sha256_file(manifest_path) != state['manifest_sha256'] or load_manifest(manifest_path) != manifest:
                raise ValueError('Frozen full training evidence changed')
            if tree_hashes(output / 'inputs') != input_hashes:
                raise ValueError('Shared method inputs or configuration changed')
            if collection_hashes is not None and tree_hashes(output / 'collection_inputs') != collection_hashes:
                raise ValueError('Collection or method settings changed')

        if prepare_only:
            state.update(status='prepared', phase='finished')
            return state
        for name in methods:
            verify()
            folder = output / name
            folder.mkdir()
            state['phase'] = name
            state['methods'][name] = {'status': 'running', 'started_at': now()}
            save()
            print(f'[{name}] Full single-round experiment; log: {folder / "run.log"}', flush=True)
            try:
                with (folder / 'run.log').open('w', encoding='utf-8', buffering=1) as log, \
                        redirect_stdout(log), redirect_stderr(log):
                    result = RUNNERS[name](inputs, configs, folder, environment,
                                          max_output_tokens=max_output_tokens, profile='experiment')
            except Exception as error:
                result = {'status': 'failed', 'error': redact(f'{type(error).__name__}: {error}', secrets)}
            try:
                verify()
            except Exception as error:
                state['methods'][name].update(status='failed', error=redact(str(error), secrets), finished_at=now())
                write_json(folder / 'result.json', state['methods'][name])
                raise
            result = {**state['methods'][name], **result, 'finished_at': now()}
            state['methods'][name] = result
            write_json(folder / 'result.json', redact(result, secrets))
            save()
            print(f'[{name}] {result["status"]}', flush=True)
            if result['status'] == 'cancelled':
                raise KeyboardInterrupt
        statuses = [row['status'] for row in state['methods'].values()]
        state.update(status='completed' if all(value == 'completed' for value in statuses)
                     else 'failed' if 'failed' in statuses else 'incomplete', phase='finished')
        state['complete_three_method_coverage'] = state['status'] == 'completed' and set(methods) == set(METHODS)
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
        current = state['methods'].get(state['phase'])
        if current is not None and current['status'] == 'running':
            current.update(status='cancelled', finished_at=now())
            write_json(output / state['phase'] / 'result.json', current)
    except Exception as error:
        state.update(status='failed', error=redact(f'{type(error).__name__}: {error}', secrets))
    finally:
        state['finished_at'] = now()
        try:
            redact_artifacts(output, secrets)
        except OSError as error:
            state.update(status='failed', error=redact(str(error), secrets))
        save()
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--config', type=Path, help='Native config containing the complete official train split')
    source.add_argument('--trajectory-manifest', type=Path, help='Reuse an already collected full train batch')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--optimizer-config', type=Path, default=ROOT / 'configs/appworld/optimizer.toml')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--methods', choices=METHODS, nargs='+', default=METHODS)
    parser.add_argument('--max-output-tokens', type=int, default=32768)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    environment = {}
    try:
        environment = load_environment(args.env_file)
        stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%dT%H%M%S')
        output = args.output or ROOT / 'runs/appworld' / f'full-{stamp}-{uuid4().hex[:8]}'
        print(f'Output: {output.resolve()}', flush=True)
        result = run_full(output, environment, config_path=args.config, manifest_path=args.trajectory_manifest,
                          methods=args.methods, prepare_only=args.prepare_only,
                          max_output_tokens=args.max_output_tokens, optimizer_config_path=args.optimizer_config)
        print(f'Status: {result["status"]}', flush=True)
        if result.get('error'):
            print(result['error'], flush=True)
        return (0 if result['status'] in {'completed', 'prepared'} else 130 if result['status'] == 'cancelled'
                else 3 if result['status'] == 'incomplete' else 2)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(redact(f'error: {error}', secret_values(environment)), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
