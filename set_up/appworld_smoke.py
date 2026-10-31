"""Real AppWorld connectivity checks for all three methods on frozen evidence."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
import json
from uuid import uuid4

from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from method_input_adapters.archive import load_archive
from method_input_adapters.execution import SharedExecutorBridge
from set_up.appworld_experiments import snapshot_agent, skillpatch
from set_up.appworld_trajectories import load_manifest
from set_up.environment import load_environment, redact, redact_artifacts, secret_values
from set_up.native_config import load_native_config
from set_up.optimizer_config import OptimizerConfig, resolve_optimizer, method_optimizer

ROOT = Path(__file__).resolve().parents[1]
METHODS = ('trace2skill', 'skillopt', 'skillpatch')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _check_profile(profile):
    if profile not in {'smoke', 'experiment'}:
        raise ValueError('Method profile must be smoke or experiment')


def prepare_method_configs(inputs, *, profile='smoke', optimizer=None):
    """Freeze the shared optimizer while leaving executor settings untouched."""
    _check_profile(profile)
    from method.skillpatch.patch_diagnosis.config import load_discovery_config
    from method.skillpatch.patch_generation.config import load_stage2_config

    folder = Path(inputs['execution_config']).parent
    native = load_native_config(inputs['execution_config'])
    optimizer = optimizer or OptimizerConfig(native.model, native.api_key_env,
                                             native.base_url_env, native.base_url)
    optimizer_path = folder / 'optimizer.json'
    write_json(optimizer_path, optimizer.metadata())
    diagnosis = load_discovery_config(ROOT / 'configs/skillpatch-stage1.toml')
    stage2 = load_stage2_config(ROOT / 'configs/skillpatch-stage2.toml')
    generation = load_discovery_config(stage2.generation_config)

    def pin(config, name):
        model = optimizer.model if optimizer.harness == 'codex' else 'openai/' + optimizer.model
        agent = replace(config.agent, model=model, harness=optimizer.harness,
                        base_url=optimizer.base_url, api_key_env=optimizer.api_key_env,
                        command=config.agent.command if config.agent.harness == optimizer.harness else ())
        return snapshot_agent(replace(config, agent=agent), folder / name)

    diagnosis_path = pin(diagnosis, 'skillpatch-diagnosis')
    generation_path = pin(generation, 'skillpatch-generation')
    stage2_path = folder / 'skillpatch-stage2.toml'
    stage2_path.write_text('[stage2]\ngeneration_config = ' + json.dumps(str(generation_path)) + '\n')
    # Exactly one training step over the complete shared batch. Keep the gate.
    count = len(inputs['train_entries'])
    minibatch_size = 1 if profile == 'experiment' else count
    merge_setting = '  merge_batch_size: 8\n' if profile == 'experiment' else ''
    config = folder / 'skillopt.yaml'
    config.write_text(
        f'train:\n  num_epochs: 1\n  batch_size: {count}\n  accumulation: 1\n  seed: 100\n'
        f'gradient:\n  minibatch_size: {minibatch_size}\n{merge_setting}'
        '  analyst_workers: 1\n  failure_only: false\n'
        'optimizer:\n  learning_rate: 2\n  min_learning_rate: 1\n'
        '  use_slow_update: false\n  use_meta_skill: false\n'
        'evaluation:\n  use_gate: true\n  sel_env_num: 0\n  eval_test: false\n', encoding='utf-8')
    return {'diagnosis': diagnosis_path, 'stage2': stage2_path, 'skillopt': config,
            'optimizer': optimizer_path}


def skill_text(skill_dir):
    skill_dir = Path(skill_dir)
    text = (skill_dir / 'SKILL.md').read_text(encoding='utf-8')
    for path in sorted(skill_dir.rglob('*')):
        if path.is_symlink():
            raise ValueError('Candidate Skill cannot contain links')
        if path.is_file() and path != skill_dir / 'SKILL.md':
            if path.suffix.lower() not in {'.md', '.txt'}:
                raise ValueError('Native Skill delivery requires text reference documents')
            text += f'\n\n--- Skill reference: {path.relative_to(skill_dir).as_posix()} ---\n' + path.read_text(encoding='utf-8')
    return text


def check_execution(path, entry, expected_text):
    archive = load_archive(path)
    raw = read(path)
    if (archive.benchmark != 'appworld' or archive.task_id != entry['task_ref']['task_id']
            or raw.get('split') != entry['task_ref']['split']
            or raw.get('retrieved_memory_text') != expected_text):
        raise ValueError('Executed task or delivered Skill differs from the planned candidate')
    return {'archive': str(path), 'task_id': archive.task_id, 'success': archive.success,
            'score': archive.score, 'steps': archive.n_turns}


def replay_candidate(inputs, output, environment, *, text=None, skill_dir=None):
    expected = skill_text(skill_dir) if skill_dir else text
    bridge = SharedExecutorBridge(environment)
    comparisons = []
    for entry, baseline in zip(inputs['train_entries'], inputs['manifest']['rows']):
        source = bridge.run(entry, expected, output / entry['id'], skill_dir=skill_dir)
        candidate = check_execution(source, entry, expected)
        comparisons.append({**candidate, 'baseline_archive': baseline['source'],
                            'baseline_success': baseline['success'], 'baseline_score': baseline['score'],
                            'score_delta': candidate['score'] - baseline['score']})
    write_json(output / 'comparisons.json', comparisons)
    return comparisons


def run_trace2skill(inputs, configs, output, environment, *, max_output_tokens, profile='smoke'):
    from method_input_adapters.trace2skill.evolution import evolve

    _check_profile(profile)
    native = load_native_config(inputs['execution_config'])
    optimizer, environment = method_optimizer(configs, native, environment)
    result = evolve([Path(row['source']) for row in inputs['manifest']['rows']],
                    inputs['initial_skill_dir'], output / 'optimization', environment,
                    validation_manifest=inputs['validation_manifest'] if profile == 'smoke' else None,
                    model=optimizer.model,
                    api_key_env=optimizer.api_key_env, base_url_env=optimizer.base_url_env,
                    max_output_tokens=max_output_tokens)
    if result['status'] != 'completed':
        raise ValueError('Trace2Skill optimization did not complete')
    if not result.get('skill_changed') and profile == 'smoke':
        return {'status': 'incomplete', 'reason': 'No changed candidate Skill was produced', 'optimization': result}
    if profile == 'smoke' and result.get('validation_status') != 'executed':
        raise ValueError('Trace2Skill validation was not executed')
    candidate_dir = Path(result['skill_dir'])
    expected = skill_text(candidate_dir)
    if profile == 'experiment' and bool(result.get('skill_changed')) != (
            tree_hashes(candidate_dir) != tree_hashes(inputs['initial_skill_dir'])):
        raise ValueError('Trace2Skill change status differs from the saved Skill files')
    validation = read(output / 'optimization/validation.json')
    if profile == 'smoke' and len(validation) != len(inputs['validation_entries']):
        raise ValueError('Trace2Skill did not evaluate every selected dev task')
    for row, entry in zip(validation, inputs['validation_entries']):
        check_execution(row['baseline_archive'], entry, inputs['initial_skill'].read_text())
        check_execution(row['candidate_archive'], entry, expected)
    if not result.get('skill_changed'):
        return {'status': 'completed', 'skill_changed': False,
                'reason': 'Optimization completed without changing the initial Skill',
                'optimization': result, 'validation_split_used': bool(validation), 'validation': validation,
                'source_replays': [], 'candidate_replays': [],
                'acceptance': 'not_automatically_activated'}
    replay = replay_candidate(inputs, output / 'source_replay', environment, skill_dir=candidate_dir)
    if profile == 'experiment':
        replay = [{**row, 'role': 'candidate_diagnostic'} for row in replay]
    return {'status': 'completed', 'candidate_skill': str(candidate_dir), 'skill_changed': True,
            'validation_split_used': bool(validation), 'validation': validation, 'source_replays': replay,
            'acceptance': 'not_automatically_activated',
            **({'candidate_replays': replay} if profile == 'experiment' else {})}


def _skillopt_validation(folder, inputs, expected):
    rows = [json.loads(line) for line in (folder / 'results.jsonl').read_text().splitlines() if line.strip()]
    entries = {entry['id']: entry for entry in inputs['validation_entries']}
    if len(rows) != len(entries) or {row['id'] for row in rows} != set(entries):
        raise ValueError('SkillOpt did not evaluate every selected dev task')
    return [check_execution(row['source_archive'], entries[row['id']], expected) for row in rows]


def _selected_source_evaluation(inputs, selected_skill, *, candidate_text=None, candidate_replays=None):
    """Report the selected version separately from a rejected candidate's diagnostics."""
    text = selected_skill.read_text(encoding='utf-8')
    initial = inputs['initial_skill'].read_text(encoding='utf-8')
    if text == initial:
        manifest = load_manifest(Path(inputs['manifest_path']))
        if manifest != inputs['manifest']:
            raise ValueError('Frozen inputs changed before selected Skill evaluation')
        rows = [check_execution(baseline['source'], entry, initial)
                for entry, baseline in zip(inputs['train_entries'], manifest['rows'])]
        strategy = 'reuse_frozen_baseline'
    elif candidate_text is not None and text == candidate_text:
        rows = candidate_replays
        strategy = 'reuse_candidate_replay'
    else:
        raise ValueError('Selected Skill differs from the initial and single-step candidate versions')
    return {'strategy': strategy, 'selected_skill': str(selected_skill),
            'results': [{**row, 'role': 'selected_skill'} for row in rows]}


def run_skillopt(inputs, configs, output, environment, *, profile='smoke', **kwargs):
    from method_input_adapters.skillopt.training import train

    _check_profile(profile)
    native = load_native_config(inputs['execution_config'])
    optimizer, environment = method_optimizer(configs, native, environment)
    optimization = output / 'optimization'
    summary = train(inputs['method_manifest'], inputs['initial_skill'], optimization, environment,
                    config=configs['skillopt'], model=optimizer.model, api_key_env=optimizer.api_key_env,
                    base_url_env=optimizer.base_url_env, frozen_trajectory_manifest=inputs['manifest_path'])
    history = read(optimization / 'history.json')
    candidates = sorted(optimization.glob('steps/step_*/candidate_skill.md'))
    initial = inputs['initial_skill'].read_text(encoding='utf-8')
    changed = [path for path in candidates if path.read_text(encoding='utf-8') != initial]
    if not changed:
        if profile == 'smoke':
            return {'status': 'incomplete', 'reason': 'No changed candidate Skill was produced',
                    'training_summary': summary, 'history': history}
        validation = [_skillopt_validation(optimization / 'selection_eval_baseline', inputs, initial)]
        phases = ['baseline']
        for candidate in candidates:
            folder = candidate.parent / 'selection_eval'
            if folder.exists():
                validation.append(_skillopt_validation(folder, inputs, initial))
                phases.append(candidate.parent.name)
        selected = optimization / 'best_skill.md'
        selection = _selected_source_evaluation(inputs, selected)
        gate_action = history[-1].get('action') if history else None
        return {'status': 'completed', 'skill_changed': False,
                'reason': 'Optimization completed without changing the initial Skill',
                'training_summary': summary, 'history': history,
                'validation_split_used': True, 'validation': validation, 'validation_phases': phases,
                'candidate_validation_status': ('executed' if len(validation) > 1 else
                    'reused_baseline_for_identical_skill' if candidates else 'not_run_no_candidate'),
                'gate_action': gate_action if gate_action in {'accept', 'accept_new_best', 'reject'}
                    else 'not_run_no_candidate',
                'candidate_replays': [], 'source_replays': [], 'selected_skill': str(selected),
                'selected_source_evaluation': selection}
    candidate = changed[-1]
    text = candidate.read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError('SkillOpt produced an empty candidate')
    step = read(candidate.parent / 'step_record.json')
    if step.get('action') not in {'accept', 'accept_new_best', 'reject'}:
        raise ValueError('SkillOpt did not complete its normal validation gate')
    validation = []
    for folder, expected in ((optimization / 'selection_eval_baseline', initial),
                             (candidate.parent / 'selection_eval', text)):
        validation.append(_skillopt_validation(folder, inputs, expected))
    replay = replay_candidate(inputs, output / 'source_replay', environment, text=text)
    experiment = {}
    if profile == 'experiment':
        replay = [{**row, 'role': 'candidate_diagnostic'} for row in replay]
        experiment = {'candidate_replays': replay, 'selected_source_evaluation':
            _selected_source_evaluation(inputs, optimization / 'best_skill.md',
                                        candidate_text=text, candidate_replays=replay)}
    return {'status': 'completed', 'candidate_skill': str(candidate), 'skill_changed': True,
            'validation_split_used': True, 'gate_action': step['action'], 'validation': validation,
            'source_replays': replay, 'training_summary': summary,
            'selected_skill': str(optimization / 'best_skill.md'), **experiment}


def run_skillpatch(inputs, configs, output, environment, *, profile='smoke', **kwargs):
    _check_profile(profile)
    result = skillpatch(inputs['manifest_path'], output / 'optimization', environment,
                       profile=profile, task_limit=0, max_issues_per_task=0 if profile == 'experiment' else 1,
                       diagnosis_config=configs['diagnosis'], stage2_config=configs['stage2'])
    return {'status': result['status'], 'validation_split_used': False,
            'candidate_count': result['candidate_count'], 'replayed_count': result['replayed_count'],
            'repaired_count': result['repaired_count'], 'acceptance': 'not_decided',
            'result_path': str(output / 'optimization/result.json'),
            **{key: result[key] for key in ('error', 'reason') if key in result}}


RUNNERS = {'trace2skill': run_trace2skill, 'skillopt': run_skillopt, 'skillpatch': run_skillpatch}


def run_smoke(manifest_path, output, environment, *, methods=METHODS, dev_limit=1,
              max_output_tokens=32768, prepare_only=False, optimizer_config_path=None):
    from set_up.appworld_smoke_inputs import prepare_inputs

    methods = tuple(methods)
    if not methods or len(set(methods)) != len(methods) or set(methods) - set(METHODS):
        raise ValueError('Choose distinct supported methods')
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError('max_output_tokens must be a positive integer')
    manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = load_manifest(manifest_path)
    output = Path(output).expanduser().resolve()
    if output.is_relative_to(manifest_path.parent):
        raise ValueError('Smoke output must be outside the frozen source archive')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = secret_values(environment)
    state = {'status': 'running', 'started_at': now(), 'methods': {}, 'phase': 'prepare',
             'pass_condition': 'changed candidate delivered and officially scored; score improvement is not required',
             'baseline_collection': 'reuse verified completed shared batch',
             'trajectory_manifest': str(manifest_path), 'manifest_sha256': sha256_file(manifest_path),
             'train_task_ids': [row['task_id'] for row in manifest['rows']],
             'scope': 'one optimization step and source re-execution; no final test-set evaluation',
             'test_split_used': False, 'complete_three_method_coverage': False}

    def save():
        write_json(output / 'result.json', redact(state, secrets))

    try:
        save()
        inputs = prepare_inputs(manifest_path, output / 'inputs', dev_limit=dev_limit)
        native = load_native_config(inputs['execution_config'])
        optimizer = resolve_optimizer(optimizer_config_path, native, environment,
                                      require_credentials=not prepare_only)
        configs = prepare_method_configs(inputs, optimizer=optimizer)
        state['optimizer'] = optimizer.metadata()
        # Task execution retains its frozen endpoint regardless of optimizer choice.
        environment = {**environment, native.base_url_env: native.base_url}
        if not prepare_only and not environment.get(native.api_key_env):
            raise ValueError(f'Missing credential environment variable: {native.api_key_env}')
        state['validation_task_ids'] = [row['id'] for row in inputs['validation_entries']]
        write_json(output / 'plan.json', {**state, 'methods_selected': methods,
                   'execution_model': native.model, 'base_url': native.base_url,
                   'api_key_env': native.api_key_env, 'method_configs': configs,
                   'validation_policy': {'trace2skill': 'official dev baseline and candidate',
                                         'skillopt': 'official dev baseline, candidate and normal gate',
                                         'skillpatch': 'no dev; source-task replay only'}})
        hashes = tree_hashes(output / 'inputs')

        def verify():
            if sha256_file(manifest_path) != state['manifest_sha256']:
                raise ValueError('Frozen trajectory manifest changed')
            load_manifest(manifest_path)
            if tree_hashes(output / 'inputs') != hashes:
                raise ValueError('Shared smoke inputs or method settings changed')

        if prepare_only:
            state.update(status='prepared', phase='finished')
        else:
            for name in methods:
                verify()
                folder = output / name
                folder.mkdir()
                state['phase'] = name
                state['methods'][name] = {'status': 'running', 'started_at': now()}
                save()
                print(f'[{name}] Optimize, validate and execute candidate; log: {folder / "run.log"}', flush=True)
                try:
                    with (folder / 'run.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
                        result = RUNNERS[name](inputs, configs, folder, environment, max_output_tokens=max_output_tokens)
                except Exception as error:
                    result = {'status': 'failed', 'error': redact(f'{type(error).__name__}: {error}', secrets)}
                try:
                    verify()
                except Exception as error:
                    result = {**state['methods'][name], 'status': 'failed',
                              'error': redact(f'{type(error).__name__}: {error}', secrets),
                              'finished_at': now()}
                    state['methods'][name] = result
                    write_json(folder / 'result.json', result)
                    raise
                result = {**state['methods'][name], **result, 'finished_at': now()}
                write_json(folder / 'result.json', redact(result, secrets))
                state['methods'][name] = result
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
    parser.add_argument('--trajectory-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--optimizer-config', type=Path, default=ROOT / 'configs/appworld/optimizer.toml')
    parser.add_argument('--methods', choices=METHODS, nargs='+', default=METHODS)
    parser.add_argument('--dev-limit', type=int, default=1)
    parser.add_argument('--max-output-tokens', type=int, default=32768)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    environment = {}
    try:
        environment = load_environment(args.env_file)
        output = args.output or ROOT / 'runs/appworld' / ('all-methods-smoke-' + uuid4().hex[:12])
        print(f'Output: {output.resolve()}', flush=True)
        result = run_smoke(args.trajectory_manifest, output, environment, methods=args.methods,
                           dev_limit=args.dev_limit, max_output_tokens=args.max_output_tokens,
                           prepare_only=args.prepare_only, optimizer_config_path=args.optimizer_config)
        print(f'Status: {result["status"]}', flush=True)
        if result.get('error'):
            print(result['error'], flush=True)
        return (0 if result['status'] in {'completed', 'prepared'} else 130 if result['status'] == 'cancelled'
                else 3 if result['status'] == 'incomplete' else 2)
    except (OSError, ValueError) as error:
        print(redact(f'error: {error}', secret_values(environment)), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
