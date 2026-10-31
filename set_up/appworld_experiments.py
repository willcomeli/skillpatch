"""Shared AppWorld evidence collection and one-round SkillPatch experiments."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json, jsonable
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_diagnosis.discovery_validation import validate_report, validate_saved_report
from method.skillpatch.patch_generation.config import Stage2Config, load_stage2_config
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_validation.appworld_patch_validation.pipeline import run_validation
from method_input_adapters.skillpatch.models import ContextItem, DiagnosisContext
from set_up.environment import load_environment, redact, secret_values

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def issue_ids(diagnosis):
    raw = read_json(diagnosis / 'input/context.json')
    context = DiagnosisContext(**{**raw, 'items': [ContextItem(**item) for item in raw['items']]})
    payload = read_json(diagnosis / 'report.json')
    report = (validate_saved_report(payload, context) if 'schema_version' in payload
              else validate_report(payload, context))
    return [item.conflict_id for item in [*report.conflicts, *getattr(report, 'improvements', [])]] + [
        f'O{index:06d}' for index in range(1, len(report.other_findings) + 1)]


def snapshot_agent(config, folder):
    """Make prompt and resolved agent settings independent of later repo edits."""
    folder.mkdir(parents=True)
    prompt = folder / 'prompt.md'
    shutil.copyfile(config.prompt_path, prompt)
    values = {**asdict(config.agent), 'session_timeout_sec': config.agent.timeout_sec,
              'prompt_path': str(prompt)}
    values.pop('timeout_sec')
    path = folder / 'agent.toml'
    path.write_text('[diagnosis]\n' + ''.join(
        f'{name} = {json.dumps(value, ensure_ascii=False)}\n' for name, value in values.items()), encoding='utf-8')
    return path


def skillpatch(manifest_path, output, environment, *, profile='experiment', task_limit=0,
               max_issues_per_task=0, diagnosis_config=None, stage2_config=None, prepare_only=False):
    from set_up.appworld_trajectories import load_manifest

    if profile not in {'smoke', 'experiment'}:
        raise ValueError('Unknown SkillPatch profile')
    if task_limit < 0 or max_issues_per_task < 0:
        raise ValueError('Limits must be nonnegative; zero means all')
    manifest_path = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = load_manifest(manifest_path)
    manifest_digest = sha256_file(manifest_path)
    rows = manifest['rows'][:task_limit] if task_limit else manifest['rows']
    discovery = load_discovery_config(diagnosis_config or ROOT / 'configs/skillpatch-stage1.toml')
    generation = load_stage2_config(stage2_config or ROOT / 'configs/skillpatch-stage2.toml')
    generation_agent = load_discovery_config(generation.generation_config)
    if not prepare_only:
        # Check all three model consumers before launching the first diagnosis.
        native = read_json(manifest['execution_config']['path'])
        for name in {discovery.agent.api_key_env, generation_agent.agent.api_key_env, native['api_key_env']}:
            if not environment.get(name):
                raise ValueError(f'Missing credential environment variable: {name}')
        if not (native.get('base_url') or environment.get(native['base_url_env'])):
            raise ValueError('Missing source execution model base URL')
    output = Path(output).expanduser().resolve()
    protected = [manifest_path.parent, *[Path(row['source']).parent.parent for row in manifest['rows']]]
    if any(output.is_relative_to(path.resolve()) for path in protected):
        raise ValueError('Method output must be outside the shared trajectory archives')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = secret_values(environment)
    state = {'status': 'running', 'profile': profile, 'started_at': now(),
             'phase': 'preflight', 'trajectory_manifest': str(manifest_path),
             'manifest_sha256': manifest_digest, 'tasks': [], 'candidate_count': 0,
             'replayed_count': 0, 'repaired_count': 0, 'validation_split_used': False,
             'acceptance': 'not_decided', 'active_version_changed': False,
             'scope': 'one round; independent candidates from frozen initial evidence'}
    settings_hashes = None

    def save():
        write_json(output / 'result.json', redact(state, secrets))

    def verify_inputs():
        if sha256_file(manifest_path) != manifest_digest:
            raise ValueError('Shared trajectory manifest changed during optimization')
        load_manifest(manifest_path)
        if settings_hashes is not None and tree_hashes(output / 'inputs') != settings_hashes:
            raise ValueError('Snapshotted method configuration or prompts changed during optimization')

    def check_stage(result, allowed, label):
        if result['status'] == 'cancelled':
            raise KeyboardInterrupt
        if result['status'] not in allowed:
            raise ValueError(f"{label} failed ({result['status']}); inspect its result.json")

    try:
        save()
        shutil.copyfile(manifest_path, output / 'trajectory_manifest.snapshot.json')
        diag_path = snapshot_agent(discovery, output / 'inputs/diagnosis')
        gen_path = snapshot_agent(generation_agent, output / 'inputs/generation')
        settings_hashes = tree_hashes(output / 'inputs')
        discovery = load_discovery_config(diag_path)
        generation = Stage2Config(gen_path)
        plan = {'profile': profile, 'prepare_only': prepare_only, 'task_limit': task_limit,
                'max_issues_per_task': max_issues_per_task, 'selected_task_ids': [row['task_id'] for row in rows],
                'diagnosis_config': diag_path, 'generation_config': gen_path,
                'initial_skill': manifest['initial_skill'], 'manifest_sha256': manifest_digest,
                'replay_policy': 'one fresh source-task replay for each generated candidate',
                'candidate_policy': 'independent candidates; no automatic acceptance, merging or activation',
                'validation_split_used': False, 'test_split_used': False}
        write_json(output / 'plan.json', redact(jsonable(plan), secrets))
        for index, row in enumerate(rows, 1):
            verify_inputs()
            task_dir = output / 'tasks' / f'{index:06d}'
            record = {'task_id': row['task_id'], 'source': row['source'], 'issues': []}
            state['tasks'].append(record)
            state['phase'] = 'diagnosis'
            save()
            print(f"[{index}/{len(rows)}] Diagnose {row['task_id']}", flush=True)
            diagnosis = task_dir / 'diagnosis'
            result = run_discovery(Path(row['source']), discovery, diagnosis, environment,
                                   prepare_only=prepare_only, progress=lambda message: print(message, flush=True))
            record['diagnosis'] = {'status': result['status'], 'path': str(diagnosis)}
            check_stage(result, {'prepared', 'completed', 'skipped'}, 'Diagnosis')
            verify_inputs()
            if result['status'] == 'skipped':
                record.update(status='skipped', reason=result['reason'])
                if result['reason'] != 'succeeded':
                    raise ValueError('A frozen trajectory unexpectedly became ineligible')
            elif prepare_only:
                record['status'] = 'prepared'
            else:
                ids = issue_ids(diagnosis)
                record['reported_issue_count'] = len(ids)
                ids = ids[:max_issues_per_task] if max_issues_per_task else ids
                for issue_index, identifier in enumerate(ids, 1):
                    issue_dir = task_dir / 'issues' / f'{issue_index:06d}'
                    issue = {'issue_id': identifier, 'generation': str(issue_dir / 'generation')}
                    record['issues'].append(issue)
                    state['phase'] = 'generation'
                    save()
                    result = run_stage2(diagnosis, identifier, generation, issue_dir / 'generation', environment,
                                        progress=lambda message: print(message, flush=True))
                    issue['generation_status'] = result['status']
                    check_stage(result, {'generated', 'unpatchable', 'insufficient_evidence'}, 'Generation')
                    verify_inputs()
                    if result['status'] != 'generated':
                        continue
                    state['candidate_count'] += 1
                    issue['patch_path'] = result['patch_path']
                    issue['replay'] = str(issue_dir / 'replay')
                    state['phase'] = 'replay'
                    save()
                    print(f"Replay {row['task_id']} for {identifier}", flush=True)
                    replay = run_validation(Path(result['patch_path']), issue_dir / 'replay', environment)
                    issue['replay_status'] = replay['status']
                    check_stage(replay, {'completed'}, 'Source-task replay')
                    issue['comparison'] = replay['comparison']
                    state['replayed_count'] += 1
                    state['repaired_count'] += int(replay['comparison']['outcome'] == 'repaired')
                    verify_inputs()
                record['status'] = 'completed' if ids else 'no_supported_issue'
            save()
        state.update(status='prepared' if prepare_only else 'completed', phase='finished')
        if profile == 'smoke' and not prepare_only and state['replayed_count'] == 0:
            state.update(status='incomplete', reason='No candidate replay occurred; end-to-end coverage is incomplete')
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except Exception as error:
        state.update(status='failed', error=redact(f'{type(error).__name__}: {error}', secrets))
    finally:
        state['finished_at'] = now()
        save()
    return state


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be nonnegative; 0 means all')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    collect_parser = commands.add_parser('collect', help='Collect one shared set of initial-Skill training trajectories')
    collect_parser.add_argument('--config', type=Path, default=ROOT / 'configs/appworld/execution.toml')
    collect_parser.add_argument('--limit', type=nonnegative, default=0, help='First N tasks in configured order; 0 = all')
    method = commands.add_parser('skillpatch', help='One round of diagnosis, generation and original-task replay')
    method.add_argument('--trajectory-manifest', type=Path, required=True)
    method.add_argument('--profile', choices=('smoke', 'experiment'), default='experiment')
    method.add_argument('--task-limit', type=nonnegative, help='Default: smoke 3, experiment all')
    method.add_argument('--max-issues-per-task', type=nonnegative, help='Default: smoke 1, experiment all')
    method.add_argument('--diagnosis-config', type=Path)
    method.add_argument('--stage2-config', type=Path)
    for sub in (collect_parser, method):
        sub.add_argument('--output', type=Path, help='New directory; defaults to a unique directory under runs/appworld')
        sub.add_argument('--env-file', type=Path, default=ROOT / '.env')
        sub.add_argument('--prepare-only', action='store_true', help='Prepare inputs without model calls or AppWorld servers')
    args = parser.parse_args(argv)
    environment = {}
    try:
        environment = load_environment(args.env_file) if args.env_file.is_file() else load_environment()
        if not args.env_file.is_file() and args.env_file != ROOT / '.env':
            raise ValueError(f'Environment file does not exist: {args.env_file}')
        stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
        label = 'collect' if args.command == 'collect' else f'skillpatch-{args.profile}'
        output = args.output or ROOT / 'runs/appworld' / f'{label}-{stamp}-{uuid4().hex[:8]}'
        print(f'Output: {output.resolve()}', flush=True)
        if args.command == 'collect':
            from set_up.appworld_trajectories import collect
            result = collect(args.config, output, environment, limit=args.limit, prepare_only=args.prepare_only)
        else:
            result = skillpatch(args.trajectory_manifest, output, environment, profile=args.profile,
                task_limit=args.task_limit if args.task_limit is not None else (3 if args.profile == 'smoke' else 0),
                max_issues_per_task=args.max_issues_per_task if args.max_issues_per_task is not None else (1 if args.profile == 'smoke' else 0),
                diagnosis_config=args.diagnosis_config, stage2_config=args.stage2_config, prepare_only=args.prepare_only)
        print(f"Status: {result['status']}", flush=True)
        if result.get('error') or result.get('reason'):
            print(redact(result.get('error') or result['reason'], secret_values(environment)), file=sys.stderr)
        if result.get('manifest_path'):
            print(f"Trajectory manifest: {result['manifest_path']}")
        return 0 if result['status'] in {'completed', 'prepared'} else 130 if result['status'] == 'cancelled' else 3 if result['status'] == 'incomplete' else 2
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(redact(f'error: {error}', secret_values(environment)), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
