"""Reference rollouts -> independent single-task optimizations -> source validation.

Run from the repository root with .venvs/methods/bin/python -m
scripts.skillsbench.experiment. All custom wiring lives in this script.
"""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing
from dataclasses import replace
import json
import math
from pathlib import Path
import shutil
import tomllib
from types import SimpleNamespace
from uuid import uuid4

import yaml

from excutor.executor_adapter.models import RunRequest
from excutor.executor_adapter.registry import get_execution_adapter
from excutor.executor_adapter.storage import hash_manifest, jsonable, now, sha256_file, tree_hashes, write_json
from excutor.excutor_runners.benchflow import BenchFlowBackend
from excutor.excutors import Executor
from method_input_adapters.archive import load_archive
from set_up.environment import load_environment, redact, secret_values
from set_up.optimizer_config import resolve_optimizer

ROOT = Path(__file__).resolve().parents[2]
METHODS = ('skillpatch', 'skillopt', 'trace2skill')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def metadata(task):
    text = (task / 'task.md').read_text(encoding='utf-8')
    parts = text.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError(f'Task needs native YAML front matter: {task}')
    return yaml.safe_load(parts[1])


def load_settings(path, environment, task_names=(), *, require_credentials=True):
    path = Path(path).resolve(strict=True)
    with path.open('rb') as stream:
        cfg = tomllib.load(stream)
    tasks_root = (path.parent / cfg['tasks_dir']).resolve(strict=True)
    candidates = sorted(p for p in tasks_root.iterdir() if p.is_dir() and (p / 'task.md').is_file())
    if task_names:
        names = set(task_names)
        if len(names) != len(task_names) or names - {p.name for p in candidates}:
            raise ValueError('Task selection contains duplicate or unknown task names')
        candidates = [p for p in candidates if p.name in names]
    if not candidates:
        raise ValueError('No SkillsBench tasks selected')
    for task in candidates:
        if not list((task / 'environment/skills').glob('*/SKILL.md')):
            raise ValueError(f'Task has no reference Skills: {task.name}')
    key_name, url_name = cfg['execution_api_key_env'], cfg['execution_base_url_env']
    for name in (cfg['execution_model_env'], url_name, *([key_name] if require_credentials else [])):
        if not environment.get(name):
            raise ValueError(f'Missing execution environment variable: {name}')
    native = SimpleNamespace(model=environment[cfg['execution_model_env']], api_key_env=key_name,
                             base_url_env=url_name, base_url=environment[url_name])
    optimizer = resolve_optimizer(path.parent / cfg['optimizer_config'], native, environment,
                                  require_credentials=require_credentials)
    # BenchFlow's existing provider resolver consumes these aliases. Keep these
    # identical during baseline, method validation, and final source replays.
    execution_environment = dict(environment)
    for name in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'LLM_API_KEY', 'BENCHFLOW_PROVIDER_API_KEY'):
        if environment.get(key_name):
            execution_environment[name] = environment[key_name]
    for name in ('OPENAI_BASE_URL', 'DEEPSEEK_BASE_URL', 'LLM_BASE_URL', 'BENCHFLOW_PROVIDER_BASE_URL'):
        execution_environment[name] = native.base_url
    execution_environment[optimizer.base_url_env] = optimizer.base_url
    return cfg, candidates, native, optimizer, execution_environment


def request_for(task, cfg, native):
    # Agent/verifier timeouts, CPU, memory, storage and build budgets remain in
    # the unchanged task.md consumed by BenchFlow. No extra idle/run/step cap.
    sandbox = metadata(task).get('sandbox', {})
    setup_timeout = sandbox.get('build_timeout_sec', 1800)
    if type(setup_timeout) not in (int, float) or not math.isfinite(setup_timeout) or setup_timeout <= 0:
        raise ValueError(f'Invalid task sandbox build timeout: {task}')
    return RunRequest(benchmark='skillsbench', task_dir=task, agent=cfg['agent'],
                      model='openai/' + native.model.removeprefix('openai/'),
                      bench_command=tuple(cfg['bench_command']),
                      expected_backend_version=cfg['expected_backend_version'],
                      skill_condition='reference-explicit', explicit_skill_read=True,
                      sandbox_setup_timeout=math.ceil(setup_timeout), agent_idle_timeout=0)


def execute(request, output, environment, *, prepare_only=False):
    digest = hash_manifest(tree_hashes(request.skills_dir)) if request.skills_dir else None
    result = Executor(get_execution_adapter('skillsbench'), BenchFlowBackend()).run(
        request, output, environment, prepare_only=prepare_only, expected_skill_digest=digest)
    if result.status == 'cancelled':
        raise KeyboardInterrupt
    if result.status != ('prepared' if prepare_only else 'completed'):
        raise ValueError(f'Execution {result.status}: {output / "result.json"}')
    if not prepare_only:
        archive = load_archive(output)
        if archive.task_id != request.task_dir.name:
            raise ValueError('Executed task identity differs from the source task')
    return output


def comparison(baseline, candidate):
    before, after = load_archive(baseline), load_archive(candidate)
    if before.task_id != after.task_id:
        raise ValueError('Validation must use the original source task')
    return {'baseline_archive': str(baseline), 'candidate_archive': str(candidate),
            'task_id': before.task_id, 'baseline_score': before.score,
            'candidate_score': after.score, 'score_delta': after.score - before.score,
            'baseline_success': before.success, 'candidate_success': after.success}


def method_configs(folder, optimizer):
    from method.skillpatch.patch_diagnosis.config import load_discovery_config
    from set_up.appworld_experiments import snapshot_agent
    from method.skillpatch.patch_generation.config import Stage2Config
    paths = {}
    for name, config in (('diagnosis', 'skillpatch-stage1.toml'),
                         ('generation', 'skillpatch-generation.toml'),
                         ('evaluation', 'skillpatch-evaluation.toml')):
        original = load_discovery_config(ROOT / 'configs' / config)
        model = optimizer.model if optimizer.harness == 'codex' else 'openai/' + optimizer.model
        agent = replace(original.agent, model=model, base_url=optimizer.base_url,
                        api_key_env=optimizer.api_key_env, harness=optimizer.harness,
                        command=original.agent.command if original.agent.harness == optimizer.harness else ())
        paths[name] = snapshot_agent(replace(original, agent=agent), folder / name)
    return paths, Stage2Config(paths['generation'])


def optimize_skillpatch(baseline, request, output, environment, optimizer):
    from method.skillpatch.patch_diagnosis.config import load_discovery_config
    from method.skillpatch.patch_diagnosis.discovery import run_discovery
    from method.skillpatch.patch_generation.pipeline import run_stage2
    from method.skillpatch.patch_validation.skillsbench_patch_validation.config import ValidationConfig
    from method.skillpatch.patch_validation.skillsbench_patch_validation.pipeline import run_validation
    from set_up.appworld_experiments import issue_ids

    configs, generation = method_configs(output / 'configs', optimizer)
    diagnosis = output / 'diagnosis'
    result = run_discovery(baseline, load_discovery_config(configs['diagnosis']), diagnosis, environment)
    if result['status'] == 'cancelled':
        raise KeyboardInterrupt
    if result['status'] == 'skipped':
        # Preserve SkillPatch's original success eligibility policy.
        candidate = execute(request, output / 'source_replay', environment)
        return {'status': 'completed', 'optimization_status': 'skipped', 'reason': result['reason'],
                'skill_changed': False, 'comparison': comparison(baseline, candidate)}
    if result['status'] != 'completed':
        raise ValueError(f'SkillPatch diagnosis {result["status"]}')
    records = []
    # All original issues; independent candidates, without an issue-count cap.
    for index, identifier in enumerate(issue_ids(diagnosis), 1):
        folder = output / 'issues' / f'{index:06d}'
        generated = run_stage2(diagnosis, identifier, generation, folder / 'generation', environment)
        if generated['status'] == 'cancelled':
            raise KeyboardInterrupt
        row = {'issue_id': identifier, 'generation_status': generated['status']}
        records.append(row)
        if generated['status'] in {'unpatchable', 'insufficient_evidence'}:
            continue
        if generated['status'] != 'generated':
            raise ValueError(f'SkillPatch generation {generated["status"]}')
        validated = run_validation(Path(generated['patch_path']), ValidationConfig(configs['evaluation']),
                                   folder / 'validation', environment)
        if validated['status'] == 'cancelled':
            raise KeyboardInterrupt
        row['validation_status'] = validated['status']
        row['validation_path'] = str(folder / 'validation/result.json')
        if validated['status'] != 'completed':
            raise ValueError(f'SkillPatch validation {validated["status"]}')
        case = validated['cases'][0]
        if case.get('status') != 'completed':
            raise ValueError('SkillPatch source validation did not produce a scored replay')
        row['comparison'] = comparison(baseline, Path(case['candidate_dir']))
    if not any('comparison' in row for row in records):
        candidate = execute(request, output / 'source_replay', environment)
        return {'status': 'completed', 'skill_changed': False, 'issues': records,
                'comparison': comparison(baseline, candidate)}
    return {'status': 'completed', 'skill_changed': True, 'issues': records,
            'candidate_policy': 'one independent source-task validation per generated patch; no automatic merging'}


def optimize_skillopt_one(baseline, request, skill, output, environment, optimizer, config_path):
    """Bind the unmodified trainer to one task as both feedback and validation."""
    from method_input_adapters.skillopt.adapter import SharedExecutionAdapter
    from method_input_adapters.skillopt.results import export_results
    from method_input_adapters.chat_compat import skillopt_chat_parameters
    from skillopt.config import flatten_config, load_config
    from skillopt.engine.trainer import ReflACTTrainer

    initial = (request.skills_dir / skill / 'SKILL.md').read_text(encoding='utf-8')
    frozen = tree_hashes(baseline)

    class SingleTaskAdapter(SharedExecutionAdapter):
        def build_train_env(self, batch_size, seed, **kwargs):
            return [{'id': request.task_dir.name}]

        def build_eval_env(self, env_num, split, seed, **kwargs):
            if split not in {'val', 'valid_seen', 'selection'}:
                raise ValueError('This protocol has only source-task validation')
            return [{'id': request.task_dir.name}]

        def rollout(self, env_manager, skill_content, out_dir, **kwargs):
            if tree_hashes(baseline) != frozen:
                raise ValueError('Shared reference trajectory changed')
            out_dir = Path(out_dir)
            if skill_content == initial:
                source = baseline
            else:
                pack = out_dir.with_name(out_dir.name + '-pack')
                shutil.copytree(request.skills_dir, pack)
                (pack / skill / 'SKILL.md').write_text(skill_content, encoding='utf-8')
                source = execute(replace(request, skills_dir=pack),
                                 out_dir.with_name(out_dir.name + '-execution'), environment)
            rows = export_results([source], out_dir, ids=[request.task_dir.name])
            write_json(out_dir / 'source_binding.json', {'source': source, 'reference_reused': source == baseline,
                       'validation_policy': 'same task as optimization', 'skill_target': skill + '/SKILL.md'})
            return rows

    cfg = flatten_config(load_config(str(ROOT / 'method/skillopt/configs/_base_/default.yaml')))
    cfg.update(flatten_config(load_config(str(config_path))))
    cfg.update(out_root=str(output), skill_init=str(request.skills_dir / skill / 'SKILL.md'),
               model_backend='openai_compatible', optimizer_backend='openai_compatible',
               target_backend='openai_compatible', optimizer_model=optimizer.model,
               target_model='shared-executor', reasoning_effort=None,
               openai_compatible_base_url=optimizer.base_url,
               openai_compatible_api_key=environment[optimizer.api_key_env])
    # Model transport configuration matches the existing method adapter. These
    # are method HTTP settings; they do not limit benchmark task execution.
    cfg.setdefault('optimizer_openai_compatible_timeout_seconds', 1800)
    with skillopt_chat_parameters(optimizer.model):
        summary = ReflACTTrainer(cfg, SingleTaskAdapter([], None)).train()
    if tree_hashes(baseline) != frozen:
        raise ValueError('Shared reference trajectory changed during SkillOpt')
    return summary


def optimize_pack(method, baseline, request, output, environment, optimizer, skillopt_config):
    from method_input_adapters.trace2skill.evolution import evolve

    pack = output / 'candidate_pack'
    shutil.copytree(request.skills_dir, pack)
    records = []
    # Every Skill receives the same source task feedback and starts from its
    # original reference version. No earlier Skill's changes enter reflection.
    for skill_file in sorted(request.skills_dir.glob('*/SKILL.md')):
        skill = skill_file.parent.name
        folder = output / 'skills' / skill
        if method == 'trace2skill':
            result = evolve([baseline], skill_file.parent, folder, environment,
                            model=optimizer.model, api_key_env=optimizer.api_key_env,
                            base_url_env=optimizer.base_url_env)
            if result['status'] != 'completed':
                raise ValueError('Trace2Skill evolution failed')
            shutil.rmtree(pack / skill)
            shutil.copytree(Path(result['skill_dir']), pack / skill)
        else:
            result = optimize_skillopt_one(baseline, request, skill, folder, environment, optimizer, skillopt_config)
            # Keep the original trainer's selection gate and selected Skill.
            shutil.copyfile(folder / 'best_skill.md', pack / skill / 'SKILL.md')
        records.append({'skill': skill, 'optimization': result})
    candidate = execute(replace(request, skills_dir=pack), output / 'source_replay', environment)
    return {'status': 'completed', 'skills': records, 'candidate_pack': str(pack),
            'skill_changed': tree_hashes(pack) != tree_hashes(request.skills_dir),
            'comparison': comparison(baseline, candidate)}


def _failure(error, secrets):
    return {'status': 'failed', 'error': redact(f'{type(error).__name__}: {error}', secrets)}


def _batch_results(worker, jobs, concurrency, secrets):
    # Separate processes isolate method globals and output redirection.
    if concurrency == 1:
        for key, arguments in jobs:
            try:
                result = worker(*arguments)
            except Exception as error:
                result = _failure(error, secrets)
            yield key, result
        return
    with ProcessPoolExecutor(max_workers=concurrency,
                             mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = {pool.submit(worker, *arguments): key for key, arguments in jobs}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                result = _failure(error, secrets)
            yield futures[future], result


def _baseline_job(name, request, folder, environment, prepare_only):
    print(f'[baseline] {name}', flush=True)
    execute(request, folder, environment, prepare_only=prepare_only)
    return {'status': 'prepared' if prepare_only else 'completed',
            'source': str(folder), 'hashes': tree_hashes(folder)}


def _method_job(method, name, original, baseline_record, folder, environment, optimizer, skillopt_config):
    secrets = secret_values(environment)
    created = False
    try:
        baseline = Path(baseline_record['source'])
        if tree_hashes(baseline) != baseline_record['hashes']:
            raise ValueError(f'Shared reference archive changed: {name}')
        raw = read(baseline / 'result.json')
        request = replace(original, task_dir=Path(raw['prepared']['task_dir']),
                          skills_dir=Path(raw['prepared']['skills_dir']))
        folder.mkdir(parents=True, exist_ok=False)
        created = True
        print(f'[{method}] {name}; log: {folder / "run.log"}', flush=True)
        with (folder / 'run.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
            if method == 'skillpatch':
                result = optimize_skillpatch(baseline, request, folder, environment, optimizer)
            else:
                result = optimize_pack(method, baseline, request, folder, environment, optimizer, skillopt_config)
        if tree_hashes(baseline) != baseline_record['hashes']:
            raise ValueError('Method changed the shared reference archive')
    except Exception as error:
        result = _failure(error, secrets)
    if created:
        write_json(folder / 'result.json', redact(result, secrets))
    return result


def _reused_baselines(source, plan, names):
    source = Path(source).resolve(strict=True)
    previous_plan = read(source / 'plan.json')
    previous_state = read(source / 'result.json')
    if previous_state['status'] == 'running':
        raise ValueError('Cannot reuse baselines from an active experiment')
    for key in ('executor', 'execution_model', 'execution_base_url', 'config_sha256', 'implementation_hashes'):
        if previous_plan.get(key) != plan.get(key):
            raise ValueError(f'Reference reuse settings differ: {key}')
    reused = {}
    for name in names:
        record = previous_state.get('baselines', {}).get(name, {})
        if record.get('status') != 'completed':
            continue
        if previous_plan['task_hashes'].get(name) != plan['task_hashes'].get(name):
            raise ValueError(f'Reference source task changed: {name}')
        if tree_hashes(Path(record['source'])) != record['hashes']:
            raise ValueError(f'Reference archive changed: {name}')
        reused[name] = {**record, 'reused_from': str(source)}
    return reused


def run(config_path, output, environment, *, task_names=(), methods=METHODS, prepare_only=False,
        resume=False, concurrency=1, reuse_baselines_from=None, baseline_only=False):
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError('Concurrency must be a positive integer')
    if prepare_only and reuse_baselines_from:
        raise ValueError('Live reference reuse cannot be used with prepare-only')
    cfg, tasks, native, optimizer, environment = load_settings(
        config_path, environment, task_names, require_credentials=not prepare_only)
    output = Path(output).resolve()
    secrets = secret_values(environment)
    requests = {task.name: request_for(task, cfg, native) for task in tasks}
    # Pin all implementation and adapter sources; the orchestrator never edits
    # them. Snapshots also make later resume reject changed methods/executors.
    protected = {name: tree_hashes(ROOT / name) for name in ('method', 'method_input_adapters', 'excutor', 'prompts')}
    protected = {name: {p: h for p, h in files.items() if '__pycache__' not in Path(p).parts
                        and not p.endswith(('.pyc', '.pyo'))} for name, files in protected.items()}
    plan = {'protocol': 'skillsbench-single-task-v1', 'concurrency': concurrency, 'baseline_only': baseline_only,
            'reuse_baselines_from': str(Path(reuse_baselines_from).resolve()) if reuse_baselines_from else None,
            'exception_policy': 'record task failures and continue; no reference means blocked optimization',
            'orchestrator_sha256': sha256_file(Path(__file__)),
            'bench_wrapper_sha256': sha256_file(ROOT / 'scripts/skillsbench/bench.py') if (ROOT / 'scripts/skillsbench/bench.py').is_file() else None,
            'tasks': [task.name for task in tasks],
            'methods': list(methods), 'executor': 'Executor + SkillsBenchExecutionAdapter + BenchFlowBackend',
            'execution_model': native.model, 'execution_base_url': native.base_url,
            'optimizer': optimizer.metadata(), 'budget_policy': 'native task.md budgets; no outer/idle/step/token cap',
            'validation_policy': 'original SkillsBench task; not a held-out generalization evaluation',
            'baseline_policy': 'collect all reference rollouts before optimization; one shared source per task',
            'skill_policy': 'independent reference initialization for every task/method/Skill',
            'skillpatch_policy': 'original eligibility, generation and independent patch validation; no issue limit',
            'config_sha256': sha256_file(config_path),
            'optimizer_config_sha256': sha256_file(Path(config_path).resolve().parent / cfg['optimizer_config']),
            'skillopt_config_sha256': sha256_file(Path(config_path).resolve().parent / cfg['skillopt_config']),
            'task_hashes': {task.name: tree_hashes(task) for task in tasks}, 'implementation_hashes': protected}
    plan = jsonable(plan)
    if resume:
        if read(output / 'plan.json') != plan:
            raise ValueError('Resume settings or protected source files differ from the saved plan')
        state = read(output / 'result.json')
        if state['status'] == 'prepared' and not prepare_only:
            raise ValueError('A preparation preview cannot be resumed as a live experiment; choose a new output')
    else:
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        write_json(output / 'plan.json', plan)
        state = {'status': 'running', 'phase': 'baseline', 'started_at': now(), 'baselines': {}, 'methods': {}}
        if reuse_baselines_from:
            state['baselines'] = _reused_baselines(reuse_baselines_from, plan, requests)
            print(f"Reused {len(state['baselines'])} verified reference archives", flush=True)
    state.update(status='running', concurrency=concurrency)
    state.pop('finished_at', None)

    def save():
        write_json(output / 'result.json', redact(state, secrets))

    save()
    try:
        # Global collection barrier is preserved even with concurrent execution.
        jobs = [(name, (name, request, output / 'baseline' / name, environment, prepare_only))
                for name, request in requests.items() if name not in state['baselines']]
        for name, result in _batch_results(_baseline_job, jobs, concurrency, secrets):
            state['baselines'][name] = result
            print(f'[baseline] {name}: {result["status"]}', flush=True)
            save()
        write_json(output / 'trajectory_manifest.json', state['baselines'])
        if prepare_only:
            state.update(status='prepared' if all(r['status'] == 'prepared' for r in state['baselines'].values())
                         else 'failed', phase='finished')
            return state
        if baseline_only or (output / 'BASELINE_ONLY_REQUESTED.json').exists():
            state.update(status='completed' if all(r['status'] == 'completed' for r in state['baselines'].values())
                         else 'completed_with_errors', phase='baseline_finished', method_stage='awaiting_user')
            return state
        for method in methods:
            state['phase'] = method
            jobs = []
            for name, original in requests.items():
                key = method + '/' + name
                if key in state['methods']:
                    continue
                baseline_record = state['baselines'][name]
                if baseline_record['status'] != 'completed':
                    state['methods'][key] = {'status': 'blocked', 'reason': 'No valid reference trajectory'}
                    continue
                jobs.append((key, (method, name, original, baseline_record, output / method / name,
                    environment, optimizer, Path(config_path).resolve().parent / cfg['skillopt_config'])))
            save()
            for key, result in _batch_results(_method_job, jobs, concurrency, secrets):
                state['methods'][key] = result
                save()
                print(f'[{key}] {result["status"]}', flush=True)
        expected = len(tasks) * len(methods)
        state.update(status='completed' if len(state['methods']) == expected and
                     all(r['status'] == 'completed' for r in state['methods'].values()) else 'completed_with_errors', phase='finished')
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except Exception as error:
        state.update(status='failed', error=redact(f'{type(error).__name__}: {error}', secrets))
    finally:
        state['finished_at'] = now()
        save()
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/skillsbench/experiment.toml')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--tasks', nargs='+', default=())
    parser.add_argument('--methods', nargs='+', choices=METHODS, default=METHODS)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--baseline-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--reuse-baselines-from', type=Path)
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error('--concurrency must be positive')
    if len(args.methods) != len(set(args.methods)):
        parser.error('Methods must be distinct')
    if args.resume and not args.output:
        parser.error('--resume requires --output')
    output = args.output or ROOT / 'runs/skillsbench' / ('experiment-' + uuid4().hex[:12])
    environment = load_environment(args.env_file)
    print(f'Output: {output.resolve()}', flush=True)
    result = run(args.config, output, environment, task_names=args.tasks, methods=args.methods,
                 prepare_only=args.prepare_only, resume=args.resume,
                 concurrency=args.concurrency, reuse_baselines_from=args.reuse_baselines_from,
                 baseline_only=args.baseline_only)
    print(f'Status: {result["status"]}', flush=True)
    return 0 if result['status'] in {'prepared', 'completed'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
