"""Continue interrupted SkillPatch diagnosis/generation/replay without overwriting prior attempts."""

import argparse
import fcntl
import json
from pathlib import Path
from uuid import uuid4

from excutor.executor_adapter.storage import now, sha256_file, tree_hashes, write_json
from set_up.appworld_experiments import issue_ids
from set_up.appworld_trajectories import load_manifest
from set_up.environment import load_environment, redact, secret_values
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from method.skillpatch.patch_diagnosis.discovery import run_discovery
from method.skillpatch.patch_generation.config import Stage2Config
from method.skillpatch.patch_generation.pipeline import run_stage2
from method.skillpatch.patch_validation.appworld_patch_validation.pipeline import run_validation


def read(path):
    return json.loads(Path(path).read_text())


def resume(source, output, environment):
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Resume output must be separate from historical SkillPatch output')
    old = read(source / 'result.json')
    if old['status'] == 'running':
        raise ValueError('Historical SkillPatch process is still running')
    manifest_path = Path(old['trajectory_manifest'])
    manifest = load_manifest(manifest_path)
    rows = [row for row in manifest['rows'] if row['score'] < 1]
    pinned = {'source_result_sha256': sha256_file(source / 'result.json'),
              'manifest_sha256': sha256_file(manifest_path), 'settings': tree_hashes(source / 'inputs')}
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (output / '.resume.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (output / 'plan.json').exists() and read(output / 'plan.json') != pinned:
            raise ValueError('Historical inputs changed since recovery started')
        write_json(output / 'plan.json', pinned)
        if (output / 'result.json').exists():
            state = read(output / 'result.json')
            if state.get('status') == 'completed':
                return state
        else:
            previous = {task['task_id']: task for task in old['tasks']}
            state = {'started_at': now(), 'trajectory_manifest': str(manifest_path),
                     'tasks': [previous.get(row['task_id'], {'task_id': row['task_id'],
                               'source': row['source'], 'issues': []}) for row in rows],
                     'validation_split_used': False, 'acceptance': 'not_decided'}
        state.update(status='running')
        state.pop('error', None)
        state.pop('finished_at', None)
        def save():
            issues = [issue for task in state['tasks'] for issue in task['issues']]
            state.update(updated_at=now(), completed_tasks=sum(t.get('status') in {'completed', 'no_supported_issue'} for t in state['tasks']),
                total_tasks=len(rows), candidate_count=sum(i.get('generation_status') == 'generated' for i in issues),
                replayed_count=sum(i.get('replay_status') == 'completed' for i in issues),
                repaired_count=sum(i.get('comparison', {}).get('outcome') == 'repaired' for i in issues))
            write_json(output / 'result.json', redact(state, secret_values(environment)))
            print(json.dumps({k: state.get(k) for k in ('status', 'phase', 'completed_tasks', 'total_tasks', 'candidate_count', 'replayed_count') }), flush=True)
        def verify():
            load_manifest(manifest_path)
            if tree_hashes(source / 'inputs') != pinned['settings']:
                raise ValueError('Frozen agent configuration changed')
        def require(result, allowed):
            if result['status'] == 'cancelled':
                raise KeyboardInterrupt
            if result['status'] not in allowed:
                raise ValueError('Stage failed: ' + str(result.get('error', result['status'])))
        diagnosis_config = load_discovery_config(source / 'inputs/diagnosis/agent.toml')
        stage2 = Stage2Config(source / 'inputs/generation/agent.toml')
        save()
        try:
            for task in state['tasks']:
                verify()
                if task.get('status') in {'completed', 'no_supported_issue'}:
                    continue
                task_dir = output / 'tasks' / task['task_id']
                state.update(phase='diagnosis', current_task=task['task_id'])
                save()
                diagnosis = task.get('diagnosis', {})
                if diagnosis.get('status') != 'completed':
                    destination = task_dir / ('diagnosis-' + uuid4().hex)
                    result = run_discovery(Path(task['source']), diagnosis_config, destination, environment, progress=print)
                    task['diagnosis'] = {'status': result['status'], 'path': str(destination)}
                    save()
                    require(result, {'completed'})
                diagnosis_dir = Path(task['diagnosis']['path'])
                ids = issue_ids(diagnosis_dir)
                task['reported_issue_count'] = len(ids)
                for identifier in ids:
                    issue = next((i for i in task['issues'] if i['issue_id'] == identifier), None)
                    if issue is None:
                        issue = {'issue_id': identifier}
                        task['issues'].append(issue)
                    if issue.get('generation_status') in {'unpatchable', 'insufficient_evidence'}:
                        continue
                    if issue.get('generation_status') != 'generated':
                        state['phase'] = 'generation'
                        destination = task_dir / identifier / ('generation-' + uuid4().hex)
                        result = run_stage2(diagnosis_dir, identifier, stage2, destination, environment, progress=print)
                        issue.update(generation=str(destination), generation_status=result['status'])
                        if result['status'] == 'generated':
                            issue['patch_path'] = result['patch_path']
                        save()
                        require(result, {'generated', 'unpatchable', 'insufficient_evidence'})
                    if issue['generation_status'] != 'generated' or issue.get('replay_status') == 'completed':
                        continue
                    verify()
                    state['phase'] = 'replay'
                    destination = task_dir / identifier / ('replay-' + uuid4().hex)
                    result = run_validation(Path(issue['patch_path']), destination, environment)
                    issue.update(replay=str(destination), replay_status=result['status'])
                    if result['status'] == 'completed':
                        issue['comparison'] = result['comparison']
                    save()
                    require(result, {'completed'})
                task['status'] = 'completed' if ids else 'no_supported_issue'
                save()
            save()
            state.update(status='completed', phase='finished', finished_at=now())
        except KeyboardInterrupt:
            state.update(status='cancelled', finished_at=now())
        except Exception as error:
            state.update(status='failed', error=redact(str(error), secret_values(environment)), finished_at=now())
        save()
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    args = parser.parse_args()
    result = resume(args.source, args.output, load_environment(args.env_file))
    return 0 if result['status'] == 'completed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
