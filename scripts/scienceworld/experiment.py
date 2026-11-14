"""SkillOpt from blank, then one SkillPatch round; restartable on local macOS."""

import argparse
from dataclasses import replace
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from .splits import ROOT, digest, prepare, read, save
from excutor.executor_adapter.storage import sha256_file, tree_hashes
from set_up.environment import load_environment, redact, secret_values
from .parallel import run_jobs, worker_count


class TransientFailure(RuntimeError):
    pass


def transient(error):
    """Infrastructure errors only; malformed patches/configuration must stop."""
    import urllib.error
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}
    if isinstance(error, (TimeoutError, ConnectionError, urllib.error.URLError,
                          http.client.IncompleteRead, TransientFailure)):
        return True
    status = getattr(error, 'status_code', None)
    if status is not None:
        return status in {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 530}
    message = str(error).lower()
    return any(marker in message for marker in (
        'connection error', 'connectionerror', 'connection reset', 'connection refused',
        'connection aborted', 'remote disconnected', 'remotedisconnected',
        'remote end closed', 'broken pipe', 'disconnected before completion',
        'incompleteread:',
        'ssl: unexpected_eof_while_reading',
        'sslerror: [ssl] record layer failure',
        'modelactionbudgetexceeded:',
        'incompletemodelresponse:',
        'timed out', 'apitimeouterror', 'exceeded timeout_sec', 'rate limit',
        '"status": "timeout"',
        'http request failed: 408', 'http request failed: 429',
        'http request failed: 50', 'http request failed: 52',
        'service unavailable', 'bad gateway', 'server disconnected', 'reconnecting...',
    ))


def backoff():
    print('连接暂时失败；5 秒后从检查点重试。Ctrl-C 可停止，稍后用 --resume 继续。', flush=True)
    time.sleep(5)


def attempt_stage(folder, operation, allowed):
    """Never overwrite failed attempts; commit only a completed stage receipt."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / 'completed.json'
    if receipt.exists():
        saved = read(receipt)
        if tree_hashes(Path(saved['path'])) != saved['hashes']:
            raise ValueError(f'Completed stage was modified: {folder}')
        return saved['result'], Path(saved['path'])
    # A worker can die after the method saved result.json but before this receipt.
    for path in sorted(folder.glob('attempt-*')):
        result_path = path / 'result.json'
        if result_path.exists():
            result = read(result_path)
            if result.get('status') in allowed:
                save(receipt, {'path': str(path), 'result': result, 'hashes': tree_hashes(path)})
                return result, path
    while True:
        path = folder / f'attempt-{len(list(folder.glob("attempt-*"))) + 1:06d}'
        try:
            result = operation(path)
            if result.get('status') == 'cancelled':
                raise KeyboardInterrupt
            if result.get('status') not in allowed:
                # The outer stages often replace a model error with a generic status.
                details = [json.dumps(result, ensure_ascii=False)]
                if path.exists():
                    for file in path.rglob('result.json'):
                        details.append(file.read_text(encoding='utf-8'))
                raise RuntimeError('\n'.join(details))
            save(receipt, {'path': str(path), 'result': result, 'hashes': tree_hashes(path)})
            return result, path
        except Exception as error:
            if not transient(error):
                raise
            backoff()


def execution_error(path):
    details = []
    if path.exists():
        for file in path.rglob('result.json'):
            details.append(file.read_text(encoding='utf-8'))
    return '\n'.join(details)


def retry_limit(environment, name, default):
    raw = str(environment.get(name, str(default))).strip().lower()
    if raw == 'unlimited':
        return None
    if not raw.isdigit():
        raise ValueError(f'{name} must be a nonnegative integer or unlimited')
    return int(raw)


def task_retry_limit(environment):
    return retry_limit(environment, 'SCIENCEWORLD_TASK_MAX_RETRIES', 5)


def request_retry_limit(environment):
    return retry_limit(environment, 'SCIENCEWORLD_REQUEST_MAX_RETRIES', 0)


def execute(entry, skill, folder, environment, patch_dir=None):
    """One task per checkpoint, so a broken connection cannot discard a batch."""
    from excutor.runner_common.launcher import run_native
    from set_up.native_config import load_native_config
    from method_input_adapters.archive import load_archive
    retry_limit = task_retry_limit(environment)
    folder = Path(folder)
    binding = {'entry': entry, 'skill_sha256': hashlib.sha256(skill.encode()).hexdigest(),
               'patch_hashes': tree_hashes(patch_dir) if patch_dir is not None else None}
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / 'completed.json'
    if receipt.exists():
        saved = read(receipt)
        if saved['binding'] != binding or tree_hashes(Path(saved['run'])) != saved['hashes']:
            raise ValueError(f'Task checkpoint binding changed: {folder}')
        load_archive(saved['source'])  # Scored failure is valid; infrastructure failure is not.
        return Path(saved['source'])
    skipped = folder / 'skipped.json'
    if skipped.exists():
        previous_skip = read(skipped)
        if previous_skip['binding'] != binding:
            raise ValueError(f'Task checkpoint binding changed: {folder}')
        if (retry_limit is None and previous_skip.get('reason') == 'infrastructure_retries_exhausted'
                and not previous_skip.get('scoring_override')):
            # A newly unlimited policy can revisit an exhausted infrastructure
            # checkpoint without discarding its attempts or original receipt.
            index = 1
            while (folder / f'skipped.previous-{index:06d}.json').exists():
                index += 1
            skipped.rename(folder / f'skipped.previous-{index:06d}.json')
        else:
            return None
    save(folder / 'binding.json', binding)
    for run in sorted(folder.glob('attempt-*/execution')):
        file = run / 'result.json'
        if file.exists():
            state = read(file)
            if state.get('status') == 'completed' and len(state.get('task_results', [])) == 1:
                source = Path(state['task_results'][0])
                load_archive(source)
                save(receipt, {'binding': binding, 'run': str(run), 'source': str(source),
                               'hashes': tree_hashes(run)})
                return source
    while True:
        failures = []
        for previous in sorted(folder.glob('attempt-*')):
            failure = previous / 'retry_failure.json'
            result = previous / 'execution/result.json'
            if failure.exists():
                failures.append(str(previous))
            elif result.exists():
                state = read(result)
                if state.get('status') not in {'completed', 'cancelled'} and transient(
                        RuntimeError(json.dumps(state))):
                    failures.append(str(previous))
        # User interruptions do not consume retries. Prior failed attempts count
        # across restarts; an explicitly unlimited policy never exhausts them.
        if retry_limit is not None and len(failures) >= retry_limit + 1:
            save(skipped, {'status': 'skipped', 'reason': 'infrastructure_retries_exhausted',
                           'binding': binding, 'failed_attempts': failures,
                           'max_task_retries': retry_limit, 'excluded_from_scoring': True})
            print(f'Task skipped after exhausted retries: {entry["id"]}', flush=True)
            return None
        attempt = folder / f'attempt-{len(list(folder.glob("attempt-*"))) + 1:06d}'
        attempt.mkdir()
        save(attempt / 'tasks.json', [entry['task_ref']])
        skill_path = attempt / 'SKILL.md'
        skill_path.write_text(skill, encoding='utf-8')
        config = replace(load_native_config(Path(entry['config']), environment),
                         tasks_path=attempt / 'tasks.json', skill_path=skill_path,
                         max_retries=request_retry_limit(environment))
        if environment.get('SCIENCEWORLD_JOB_PORT'):
            port = int(environment['SCIENCEWORLD_JOB_PORT'])
            config = replace(config, runner_options={**config.runner_options,
                'auto_server': True, 'parallel_workers': 1, 'server_host': '127.0.0.1',
                'server_port_start': port, 'remote_environment_url': f'http://127.0.0.1:{port}'})
        try:
            state = run_native(config, attempt / 'execution', environment, patch_skill_dir=patch_dir)
            if state['status'] == 'cancelled':
                raise KeyboardInterrupt
            if state['status'] != 'completed' or len(state['task_results']) != 1:
                raise RuntimeError(execution_error(attempt))
            source = Path(state['task_results'][0])
            load_archive(source)
            save(receipt, {'binding': binding, 'run': str(attempt / 'execution'),
                           'source': str(source), 'hashes': tree_hashes(attempt / 'execution')})
            return source
        except Exception as error:
            if not transient(error):
                raise
            save(attempt / 'retry_failure.json', {'type': type(error).__name__,
                                                 'retryable': True})
            backoff()


def execute_many(entries_and_folders, skill, environment, patch_dir=None):
    items = list(entries_and_folders)
    workers = worker_count(environment, 'SCIENCEWORLD_EXECUTION_WORKERS')
    if workers == 1:
        return [execute(entry, skill, folder, environment, patch_dir) for entry, folder in items]
    jobs = [{'kind': 'execute', 'entry': entry, 'skill': skill, 'folder': str(folder),
             'patch_dir': str(patch_dir) if patch_dir else None} for entry, folder in items]
    return [Path(value) if value is not None else None
            for value in run_jobs(jobs, environment, workers)]


def skillpatch_task(entry, skill, task_dir, inputs, environment):
    """One independent training trajectory and its existing diagnosis/generation stages."""
    from method.skillpatch.patch_diagnosis.config import load_discovery_config
    from method.skillpatch.patch_diagnosis.discovery import run_discovery
    from method.skillpatch.patch_generation.config import Stage2Config
    from method.skillpatch.patch_generation.pipeline import run_stage2
    from method_input_adapters.skillpatch import build_diagnosis_context
    from set_up.appworld_experiments import issue_ids
    print(f'SkillPatch training trajectory: {entry["id"]}', flush=True)
    source = execute(entry, skill, task_dir / 'baseline', environment)
    if source is None:
        return []
    diagnosis_config = load_discovery_config(inputs / 'diagnosis.toml')
    generation_config = Stage2Config(inputs / 'generation.toml')
    # Changed evidence or prompts must not reuse stale method-stage receipts.
    # Preserve the original directories and source execution archives in place.
    context = build_diagnosis_context(source)
    method_dir = task_dir / 'method-inputs' / digest({
        'context': context.input_digest,
        'diagnosis_prompt': sha256_file(inputs / 'diagnosis.prompt.md'),
        'generation_prompt': sha256_file(inputs / 'generation.prompt.md')})
    result, diagnosis = attempt_stage(method_dir / 'diagnosis', lambda path: run_discovery(
        source, diagnosis_config, path, environment, progress=print), {'completed', 'skipped'})
    if result['status'] == 'skipped':
        if result.get('reason') != 'succeeded':
            raise ValueError(f'Invalid training trajectory: {result}')
        return []
    patches = []
    for identifier in issue_ids(diagnosis):
        generated, _ = attempt_stage(method_dir / 'issues' / identifier,
            lambda path: run_stage2(diagnosis, identifier, generation_config, path,
                                   environment, progress=print),
            {'generated', 'unpatchable', 'insufficient_evidence'})
        if generated['status'] == 'generated':
            patches.append(Path(generated['patch_path']))
    return patches


def summarize(sources):
    from method_input_adapters.archive import load_archive
    sources = list(sources)
    scored = [source for source in sources if source is not None]
    rows = [load_archive(source) for source in scored]
    return {'count': len(rows), 'requested_count': len(sources),
            'skipped_count': len(sources) - len(rows),
            'success_rate': sum(row.success for row in rows) / len(rows) if rows else None,
            'mean_score': sum(row.score for row in rows) / len(rows) if rows else None,
            'sources': [str(source) for source in scored]}


def write_native(path, tasks, skill, environment, scienceworld):
    jar = environment.get('SCIENCEWORLD_JAR_PATH', str(scienceworld / 'scienceworld/scienceworld.jar'))
    values = {
        'task': {'benchmark': 'scienceworld', 'path': str(tasks), 'skill_path': str(skill)},
        'agent': {'model': environment.get('EXECUTION_MODEL', 'deepseek-flash'),
                  'max_tokens': False,
                  'max_retries': ('unlimited' if request_retry_limit(environment) is None
                                  else request_retry_limit(environment)),
                  'api_key_env': 'EXECUTION_API_KEY', 'base_url_env': 'EXECUTION_BASE_URL'},
        'runtime': {'backend': 'eviskill'},
        'runtime.runner_options': {'remote_environment_url': 'http://127.0.0.1:8811',
                                   'auto_server': True, 'scienceworld_path': str(scienceworld),
                                   'scienceworld_jar_path': jar,
                                   'scienceworld_server_python': sys.executable},
        'output': {'root': str(path.parent / 'executions')},
    }
    # Omit the output token limit from model requests. Dataset step limits and
    # existing temperature/history/HTTP request timeout defaults still apply.
    lines = []
    for table, fields in values.items():
        lines.append(f'[{table}]')
        lines += [f'{key} = {str(value).lower() if isinstance(value, bool) else json.dumps(value)}'
                  for key, value in fields.items()]
        lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


def prepare_experiment(output, environment):
    inputs = output / 'inputs'
    inputs.mkdir(parents=True, exist_ok=True)
    simplification = environment.get('SCIENCEWORLD_SIMPLIFICATION', '')
    if simplification not in {'', 'easy'}:
        raise ValueError('Confirm SCIENCEWORLD_SIMPLIFICATION in the env file: easy or empty')
    cohorts = prepare(inputs / 'splits', simplification)
    scienceworld = Path(environment.get('SCIENCEWORLD_PATH', str(ROOT.parent / 'datasets/ScienceWorld'))).absolute()
    initial = inputs / 'blank_skill.md'
    initial.write_text('', encoding='utf-8')
    save(inputs / 'tasks.json', [row for rows in cohorts.values() for row in rows])
    config = inputs / 'execution.toml'
    write_native(config, inputs / 'tasks.json', initial, environment, scienceworld)
    manifest = []
    for split, refs in cohorts.items():
        for ref in refs:
            manifest.append({'id': f'{split}-{ref["skillnet_index"]:06d}', 'split': split,
                             'task_type': ref['task_family'], 'task_ref': ref, 'config': str(config)})
    save(inputs / 'manifest.json', manifest)
    for stage in ('diagnosis', 'generation'):
        # Resolve prompt names from the project's existing stage settings.
        from method.skillpatch.patch_diagnosis.config import load_discovery_config
        original = ROOT / ('configs/skillpatch-stage1.toml' if stage == 'diagnosis'
                           else 'configs/skillpatch-generation.toml')
        source = load_discovery_config(original)
        destination = inputs / f'{stage}.prompt.md'
        shutil.copyfile(source.prompt_path, destination)
        settings = {'model': environment.get('METHOD_MODEL', 'gpt-6.1-sol'),
                    'base_url': environment.get('METHOD_BASE_URL') or 'https://method-not-configured.invalid/v1',
                    'api_key_env': 'METHOD_API_KEY',
                    'harness': environment.get('SKILLPATCH_HARNESS', 'codex'),
                    'session_timeout_sec': 1800, 'prompt_path': str(destination)}
        if settings['harness'] == 'openhands' and '/' not in settings['model']:
            settings['model'] = 'openai/' + settings['model']
        if environment.get('SKILLPATCH_AGENT_COMMAND'):
            settings['command'] = shlex.split(environment['SKILLPATCH_AGENT_COMMAND'])
        (inputs / f'{stage}.toml').write_text('[diagnosis]\n' + '\n'.join(
            f'{k} = {json.dumps(v)}' for k, v in settings.items()) + '\n', encoding='utf-8')
    return {'counts': {k: len(v) for k, v in cohorts.items()},
            'models': {k: environment.get(k, default) for k, default in (
                ('EXECUTION_MODEL', 'deepseek-flash'), ('METHOD_MODEL', 'gpt-6.1-sol'))},
            'endpoints': {k: environment.get(k, '') for k in ('EXECUTION_BASE_URL', 'METHOD_BASE_URL')},
            'skillpatch': {'rounds': 1, 'all_generated_patches_accepted': True, 'validation': False,
                           'source_split': 'train', 'agent_session_timeout_sec': 1800,
                           'harness': environment.get('SKILLPATCH_HARNESS', 'codex')},
            'skillopt': 'upstream SplitDataLoader full-epoch batches, default recipe, blank initial skill',
            'skillopt_output_tokens': 'API default; token limit fields omitted',
            'parallelism': {name: worker_count(environment, name) for name in
                            ('SCIENCEWORLD_EXECUTION_WORKERS', 'SCIENCEWORLD_SKILLPATCH_WORKERS')},
            'max_task_retries': task_retry_limit(environment),
            'max_request_retries': request_retry_limit(environment),
            'inputs_hashes': tree_hashes(inputs),
            'implementation_hashes': {'set_up/native_config.py': sha256_file(ROOT / 'set_up/native_config.py'),
                                     **{str(path.relative_to(ROOT)): sha256_file(path)
                                      for folder in ('scripts/scienceworld', 'method_input_adapters',
                                                     'excutor', 'harness', 'method/skillpatch')
                                      for path in sorted((ROOT / folder).rglob('*.py'))}},
            'scienceworld_jar_sha256': sha256_file(Path(environment.get('SCIENCEWORLD_JAR_PATH',
                                                      str(scienceworld / 'scienceworld/scienceworld.jar'))))}


def run_pipeline(output, environment):
    from method.skillpatch.patch_consolidation.source import load_patch
    from excutor.excutor_runners.patch_sidecars import write_sidecars
    from .training import train
    inputs = output / 'inputs'
    manifest = read(inputs / 'manifest.json')
    trained, training_path = train(inputs, output / 'skillopt', environment)
    skill_path = training_path / 'trainer/best_skill.md'
    skill = skill_path.read_text(encoding='utf-8')
    # A failed/no-edit training run must not silently hand an empty Skill to Stage1.
    if not skill.strip():
        raise ValueError('SkillOpt best_skill.md is empty; inspect training artifacts before continuing')
    train_entries = [row for row in manifest if row['split'] == 'train']
    workers = worker_count(environment, 'SCIENCEWORLD_SKILLPATCH_WORKERS')
    if workers == 1:
        groups = [skillpatch_task(entry, skill, output / 'skillpatch' / entry['id'], inputs, environment)
                  for entry in train_entries]
    else:
        groups = run_jobs([{'kind': 'skillpatch', 'entry': entry, 'skill': skill,
                            'folder': str(output / 'skillpatch' / entry['id']), 'inputs': str(inputs)}
                           for entry in train_entries], environment, workers)
    patches = [Path(path) for group in groups for path in group]
    candidate = output / 'final_skill'
    candidate.mkdir(exist_ok=True)
    (candidate / 'SKILL.md').write_text(skill, encoding='utf-8')
    entries = []
    for path in patches:
        patch, source, _ = load_patch(path)
        item = next(item for item in source.context.items if item.context_id == patch.target.skill_ref)
        if item.text != skill or skill.count(patch.target.quote) != 1:
            raise ValueError(f'Patch does not bind uniquely to the SkillOpt base: {path}')
        entries.append({'entry_id': patch.patch_id,
                        'target': {'relative_path': 'SKILL.md', 'scope': patch.target.scope,
                                   'quote': patch.target.quote, 'version': sha256_file(candidate / 'SKILL.md')},
                        'when_to_use': patch.when_to_use, 'how_to_interpret': patch.how_to_interpret})
    if entries:
        write_sidecars(candidate, entries)
    save(output / 'patches.json', {'policy': 'all generated structurally valid patches; no validation gate or semantic merge',
                                  'patch_paths': [str(path) for path in patches], 'count': len(entries)})
    test_entries = [row for row in manifest if row['split'] == 'test']
    # The default trainer already evaluates best_skill on the complete test split.
    # Reuse those exact scored archives rather than charging for a duplicate run.
    evaluation = read(training_path / 'trainer/test_eval/orchestration_receipt.json')
    if evaluation['binding']['skill'] != digest(skill):
        raise ValueError('SkillOpt test does not correspond to best_skill.md')
    by_id = {row['id']: row for row in evaluation['rows']}
    skipped_ids = set(evaluation.get('skipped_ids', []))
    if (set(by_id) & skipped_ids or set(by_id) | skipped_ids != {row['id'] for row in test_entries}
            or len(by_id) != len(evaluation['rows'])):
        raise ValueError('SkillOpt did not evaluate the exact full experiment test split')
    scores = {'skillopt': summarize([by_id[row['id']]['source_archive'] if row['id'] in by_id
                                   else None for row in test_entries])}
    scores['skillopt']['skipped_ids'] = sorted(skipped_ids)
    scores['skillopt']['origin'] = 'unmodified SkillOpt trainer final best-Skill evaluation'
    save(output / 'test/skillopt/summary.json', scores['skillopt'])
    for name, pack in [('skillpatch', candidate if entries else None)]:
        sources = execute_many([(entry, output / 'test' / name / entry['id']) for entry in test_entries],
                               skill, environment, pack)
        scores[name] = summarize(sources)
        scores[name]['skipped_ids'] = [row['id'] for row, source in zip(test_entries, sources)
                                      if source is None]
        save(output / 'test' / name / 'summary.json', scores[name])
    return {'status': 'completed', 'scores': scores, 'skillopt_skill': str(skill_path),
            'final_skill': str(candidate), 'patch_count': len(entries),
            'skillpatch_validation_used': False}


def preflight(environment):
    for name in ('EXECUTION_API_KEY', 'EXECUTION_BASE_URL', 'METHOD_API_KEY', 'METHOD_BASE_URL'):
        if not environment.get(name):
            raise ValueError(f'Fill {name} in the env file before running')
    if not (ROOT / 'method/skillopt/skillopt/engine/trainer.py').is_file():
        raise ValueError('Initialize method/skillopt submodule and install it (see scripts/scienceworld/README.md)')
    java = subprocess.run(['java', '-version'], capture_output=True, env=environment)
    if java.returncode:
        raise ValueError('Java runtime is required; install Java before running')
    harness = environment.get('SKILLPATCH_HARNESS', 'codex')
    command = shlex.split(environment.get('SKILLPATCH_AGENT_COMMAND', ''))
    binary = command[0] if command else (str(ROOT / 'harness/openhands/.venv/bin/openhands')
                                        if harness == 'openhands' else shutil.which('codex'))
    if not binary or not (Path(binary).is_file() or shutil.which(binary)):
        raise ValueError(f'{harness} executable not found; install it or fill SKILLPATCH_AGENT_COMMAND')
    import skillopt  # noqa: F401
    if Path(skillopt.__file__).resolve().parent != ROOT / 'method/skillopt/skillopt':
        raise ValueError('Install the pinned method/skillopt checkout in editable mode')
    import requests  # noqa: F401
    import py4j  # noqa: F401


def supervise(output, env_file, environment):
    secrets = secret_values(environment)
    while True:
        with (output / 'experiment.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, '-u', '-m', 'scripts.scienceworld.experiment',
                                        '--output', str(output), '--env-file', str(env_file), '--worker'],
                                       cwd=ROOT, env=environment, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, start_new_session=True)
            try:
                for line in process.stdout:
                    clean = redact(line, secrets)
                    print(clean, end='', flush=True)
                    log.write(clean)
                    log.flush()
                code = process.wait()
            except KeyboardInterrupt:
                import signal
                os.killpg(process.pid, signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        state = read(output / 'result.json') if (output / 'result.json').exists() else {}
        if code == 0 and state.get('status') == 'completed':
            return 0
        if code == 130:
            return 130
        if state.get('status') == 'failed' and not state.get('retryable') and code >= 0:
            return 2
        backoff()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env.scienceworld')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    environment = load_environment(args.env_file)
    if environment.get('JAVA_HOME'):
        environment['PATH'] = (str(Path(environment['JAVA_HOME']) / 'bin') + os.pathsep
                               + environment.get('PATH', os.defpath))
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        save(output / 'result.json', {'status': 'running'})
        try:
            state = run_pipeline(output, environment)
            save(output / 'result.json', state)
            return 0
        except KeyboardInterrupt:
            save(output / 'result.json', {'status': 'cancelled'})
            return 130
        except Exception as error:
            save(output / 'result.json', {'status': 'failed', 'retryable': transient(error),
                                         'error': redact(f'{type(error).__name__}: {error}', secret_values(environment))})
            print(read(output / 'result.json'), flush=True)
            return 2
    with (output / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_path = output / 'plan.json'
        if plan_path.exists() and not args.resume:
            raise ValueError('Output exists; use --resume or choose a new output directory')
        if plan_path.exists():
            previous = read(plan_path)
            if task_retry_limit(environment) != previous.get('max_task_retries', 5):
                raise ValueError('Task retry policy changed; record an authorized recovery amendment '
                                 'or use a new output directory')
            if request_retry_limit(environment) != previous.get('max_request_retries', 0):
                raise ValueError('Request retry policy changed; record an authorized recovery amendment '
                                 'or use a new output directory')
            for name, count in previous.get('parallelism', {}).items():
                if worker_count(environment, name) != count:
                    raise ValueError(f'Parallelism changed: {name}; use a new output directory')
            if tree_hashes(output / 'inputs') != previous['inputs_hashes']:
                raise ValueError('Prepared inputs changed; refusing resume')
            for name, value in previous['models'].items():
                if environment.get(name, value) != value:
                    raise ValueError(f'Model changed: {name}')
            for name, value in previous['endpoints'].items():
                if environment.get(name, '') != value:
                    raise ValueError(f'Endpoint changed: {name}; use a new output directory')
            for name, value in previous['implementation_hashes'].items():
                if sha256_file(ROOT / name) != value:
                    raise ValueError(f'Implementation changed: {name}')
        else:
            save(plan_path, prepare_experiment(output, environment))
        print(f'Experiment: {output}; splits: {read(plan_path)["counts"]}', flush=True)
        if args.prepare_only:
            return 0
        preflight(environment)
        current_jar = Path(environment.get('SCIENCEWORLD_JAR_PATH',
            str(Path(environment.get('SCIENCEWORLD_PATH', str(ROOT.parent / 'datasets/ScienceWorld')))
                / 'scienceworld/scienceworld.jar')))
        if sha256_file(current_jar) != read(plan_path)['scienceworld_jar_sha256']:
            raise ValueError('ScienceWorld JAR changed since preparation')
        return supervise(output, args.env_file.resolve(), environment)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
