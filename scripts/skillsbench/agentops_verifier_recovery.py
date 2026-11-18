"""Re-grade saved AgentOps patches with an explicitly approved verifier budget."""

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import uuid

from excutor.executor_adapter.models import BackendRun, ExecutionResult, RunRequest
from excutor.executor_adapter.skillsbench import SkillsBenchExecutionAdapter
from excutor.executor_adapter.storage import now, write_json
from excutor.excutor_runners.benchflow import BenchFlowBackend

TASK = 'fix-build-agentops'
FILES = ('patch_1.diff', 'patch_2.diff', 'failed_reasons.txt')
REPOSITORY = '/home/github/build/failed/AgentOps-AI/agentops'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source(source, artifacts):
    proof = json.loads((artifacts / 'provenance.json').read_text())
    trials = list(source.glob('jobs/*/*/result.json'))
    if len(trials) != 1 or proof['source_archive'] != str(source):
        raise ValueError('Artifact provenance does not identify one source rollout')
    native = trials[0]
    summary = json.loads(native.read_text())
    trajectory = native.parent / 'trajectory/acp_trajectory.jsonl'
    rows = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
    if (summary['task_name'] != TASK or summary.get('error') or summary.get('agent_timeout_info')
            or not rows or rows[-1].get('type') != 'agent_message'):
        raise ValueError('Original Agent did not finish normally')
    if sha(trajectory) != proof['trajectory_sha256']:
        raise ValueError('Original trajectory checksum changed')
    if not proof.get('patches_match_two_independent_recorded_git_diffs'):
        raise ValueError('Patches lack recorded diff evidence')
    if not proof.get('original_image_patch_application_check', {}).get('success'):
        raise ValueError('Restored patches lack original-image application check')
    for name in FILES:
        path = artifacts / name
        meta = proof['files'][name]
        if path.stat().st_size != meta['bytes'] or sha(path) != meta['sha256']:
            raise ValueError(f'Restored artifact checksum changed: {name}')
    return native, summary, trajectory, proof


def restoration_command(proof):
    repo = shlex.quote(REPOSITORY)
    head = shlex.quote(proof['original_image_patch_application_check']['base_head'])
    digest = proof['original_image_patch_application_check']['combined_diff_sha256']
    patches = ' '.join(shlex.quote(REPOSITORY + '/' + name) for name in FILES[:2])
    checks = []
    for name in FILES:
        destination = REPOSITORY + '/' + name if name.endswith('.diff') else '/home/github/build/failed/' + name
        checks.append('echo ' + shlex.quote(proof['files'][name]['sha256'] + '  ' + destination) + ' | sha256sum -c -')
    return ('set -eu; ' + '; '.join(checks) + '; '
            f'test "$(git -C {repo} rev-parse HEAD)" = {head}; '
            f'git -C {repo} apply --check {patches}; git -C {repo} apply {patches}; '
            f'git -C {repo} diff --binary > /tmp/agentops-restored.diff; '
            'echo ' + shlex.quote(digest + '  /tmp/agentops-restored.diff') + ' | sha256sum -c -')


async def reserve_verifier_slot(run):
    """Keep total task concurrency at ten; existing workers keep running."""
    launch = json.loads(run.with_suffix('.launcher.json').read_text())
    pid = launch['pid']
    process = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,command='], capture_output=True, text=True)
    if process.returncode:
        return None
    if str(run) not in process.stdout or 'scripts.skillsbench.' not in process.stdout or 'T' in process.stdout.split()[0]:
        raise ValueError('Cannot reserve a slot from this dispatcher')
    state = json.loads((run / 'result.json').read_text())
    if state['status'] != 'running':
        raise ValueError('Experiment is not running; honor its pause before grading')
    os.kill(pid, signal.SIGSTOP)
    write_json(run / 'VERIFIER_SLOT_HOLD.json', {'status': 'held', 'pid': pid, 'at': now(),
        'reason': 'Reserve one of ten task slots for approved verifier-only recovery; existing workers continue'})
    try:
        await asyncio.sleep(1)
        while True:
            state = json.loads((run / 'result.json').read_text())
            if state['status'] != 'running':
                raise ValueError('User paused the experiment during verifier recovery')
            active = 0
            phase = state['phase']
            for name in state['baselines']:
                if phase + '/' + name in state['methods']:
                    continue
                folder = run / phase / name
                recovered = run / 'resume_after_docker_cleanup' / name
                if phase == 'skillopt' and recovered.exists():
                    folder = recovered
                if folder.exists() and not (folder / 'result.json').exists():
                    active += 1
            if active < 10:
                return pid
            await asyncio.sleep(1)
    except BaseException:
        release_verifier_slot(run, pid)
        raise


def release_verifier_slot(run, pid):
    if pid is None:
        return
    state = json.loads((run / 'result.json').read_text())
    if state['status'] == 'running':
        process = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
        if process.returncode == 0 and str(run) in process.stdout and 'scripts.skillsbench.' in process.stdout:
            try:
                os.kill(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
    write_json(run / 'VERIFIER_SLOT_HOLD.json', {'status': 'released', 'pid': pid, 'at': now()})


async def observe_build(env, output, finished):
    """Observe existing grader processes/logs without changing its script."""
    script = """import json,pathlib,subprocess
root=pathlib.Path('/home/github/build/passed/AgentOps-AI/agentops/.tox')
paths=sorted(root.glob('*/log/*.log'),key=lambda p:p.stat().st_mtime,reverse=True)[:12]
print(json.dumps({'processes':subprocess.run(['ps','-eo','pid,ppid,etime,stat,pcpu,comm'],capture_output=True,text=True).stdout,'tox_logs':{str(p):p.read_text(errors='replace')[-4000:] for p in paths}}))
"""
    folder = output / 'build-observations'
    folder.mkdir(exist_ok=True)
    index = 0
    while not finished.done():
        try:
            response = await env.exec('python3 -I -S -c ' + shlex.quote(script), user='root', timeout_sec=30)
            write_json(folder / f'{index:06d}.json', {'at': now(), 'return_code': response.return_code,
                'stdout': response.stdout, 'stderr': response.stderr})
            index += 1
        except Exception as error:
            write_json(folder / 'observation-unavailable.json', {'at': now(), 'error_type': type(error).__name__})
        await asyncio.sleep(15)


async def recover(source, output, artifacts, seconds, *, run=None, slot_reserved=False):
    from benchflow.task import Task, RolloutPaths
    from benchflow.rollout_planes import DefaultRolloutPlanes
    from benchflow.rollout import _setup
    from .step_retries import enable_step_retries, retry_operation
    from .python_mirrors import enable_verifier_routing, policy_for_directory
    from .image_cache import main as preload

    if seconds != 900:
        raise ValueError('This recovery is authorized for exactly 900 seconds')
    run = Path(run).resolve() if run is not None else source.parent.parent
    assert version('benchflow') == '0.6.3'
    original_native, original_summary, trajectory, proof = validate_source(source, artifacts)
    old = json.loads((source / 'result.json').read_text())
    spec = json.loads((source / 'request.json').read_text())
    spec['task_dir'] = Path(spec['task_dir'])
    if spec.get('skills_dir'):
        spec['skills_dir'] = Path(spec['skills_dir'])
    spec['bench_command'] = tuple(spec['bench_command'])
    request = RunRequest(**spec)
    from dataclasses import replace
    request = replace(request, task_dir=Path(old['prepared']['task_dir']),
                      skills_dir=Path(old['prepared']['skills_dir']))
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = ExecutionResult(run_id=output.name, benchmark='skillsbench', task_id=TASK,
                             status='running', started_at=now())
    write_json(output / 'request.json', request)
    adapter = SkillsBenchExecutionAdapter()
    env = None
    held_dispatcher = None
    recovery = {'source_archive': str(source), 'source_native_summary': str(original_native),
        'source_trajectory_sha256': sha(trajectory), 'artifacts': proof,
        'additional_agent_runs': 0, 'additional_model_calls': 0,
        'original_verifier_timeout_seconds': 240, 'verifier_timeout_seconds': seconds,
        'authorization': 'User explicitly selected 900 seconds for verifier-only re-evaluation',
        'environment_difference': 'Rebuilt original image after Docker cleanup; code patches and note '
        'restored from recorded Agent tool outputs; original note byte hash was not recorded',
        'dependency_precache_outside_grader': False, 'stage': 'preparing'}

    def save(stage):
        recovery.update(stage=stage, updated_at=now())
        write_json(output / 'verifier-recovery.json', recovery)
        write_json(output / 'result.json', result)

    save('preparing')
    try:
        result.prepared = adapter.prepare(request, output / 'input')
        if result.prepared.file_hashes != old['prepared']['file_hashes']:
            raise ValueError('Original task snapshot changed')
        if (source / 'prompt').is_dir():
            shutil.copytree(source / 'prompt', output / 'prompt')
        sdk = output / 'sdk' / TASK
        shutil.copytree(result.prepared.task_dir, sdk)
        task = Task(result.prepared.task_dir)
        if task.config.verifier.timeout_sec != 240:
            raise ValueError('Original verifier budget differs from recorded 240 seconds')
        task.config.verifier.timeout_sec = seconds
        planes = DefaultRolloutPlanes()
        if result.prepared.skills_dir:
            planes.inject_skills_into_dockerfile(sdk, result.prepared.skills_dir)
        trial_name = TASK + '__verify_' + uuid.uuid4().hex[:8]
        paths = RolloutPaths(output / 'jobs' / 'verifier-recovery' / trial_name)
        paths.mkdir()
        shutil.copytree(trajectory.parent, paths.rollout_dir / 'trajectory')
        write_json(paths.rollout_dir / 'original-native-summary.json', original_summary)
        # Existing exact-image download/cache support, with the same approved
        # transport and digest validation; this never starts an Agent.
        save('preloading_original_image')
        cache = output / 'image-preload'
        cache.mkdir()
        manifests = [item for item in json.loads((run / 'image-resume-manifests.json').read_text())
                     if item['task'] == TASK]
        approved = json.loads((run / 'IMAGE_RESUME_PREPARATION_REQUESTED.json').read_text())
        if len(manifests) != 1 or manifests[0]['manifest_digest'] != approved['verified_original_images'][TASK]['manifest_digest']:
            raise ValueError('Original AgentOps image manifest differs from approved source')
        write_json(cache / 'image-resume-manifests.json', manifests)
        from . import image_cache
        write_json(cache / 'IMAGE_RESUME_PREPARATION_REQUESTED.json', {'status': 'active',
            'tool_sha256': sha(Path(image_cache.__file__)),
            'manifests_sha256': sha(cache / 'image-resume-manifests.json'),
            'scope': 'Original approved AgentOps image only, before verifier-only recovery'})
        # A verified original image can already be present from earlier
        # grading. Seed only its attested filesystem, never an arbitrary tag.
        if proof.get('rootfs_matches_previously_verified_original') and proof.get('original_rootfs_diff_ids'):
            reference = 'bugswarm/cached-images:' + manifests[0]['tag']
            inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference], text=True))[0]
            digest = manifests[0]['manifest_digest']
            if (proof['original_image_manifest_digest'] == digest
                    and any(d.endswith('@' + digest) for d in inspected['RepoDigests'])
                    and inspected['RootFS']['Layers'] == proof['original_rootfs_diff_ids']):
                write_json(cache / 'image-resume-progress.json', {'imported': {TASK: {
                    'reference': reference, 'manifest_digest': digest, 'image_id': inspected['Id'],
                    'platform': inspected['Os'] + '/' + inspected['Architecture'],
                    'rootfs_diff_ids_verified': True, 'rootfs_diff_ids': inspected['RootFS']['Layers']}}})
        await preload(cache)
        policy = policy_for_directory(output)
        if policy:
            enable_verifier_routing(index=policy['index_url'])
        enable_step_retries(apt_mirror=True)
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ.setdefault(key, 'http://127.0.0.1:7897')
        if not slot_reserved:
            held_dispatcher = await reserve_verifier_slot(run)
        env = planes.create_environment('docker', task, sdk, trial_name, paths,
            preserve_agent_network=False, environment_manifest=None)
        save('building_environment')
        await env.start(force_build=False)
        save('restoring_agent_artifacts')
        for name in FILES:
            destination = REPOSITORY + '/' + name if name.endswith('.diff') else '/home/github/build/failed/' + name
            await retry_operation(lambda n=name, d=destination: env.upload_file(artifacts / n, d), 'artifact_restore')
        restored = await env.exec(restoration_command(proof), user='root', timeout_sec=None)
        (output / 'restored-code-check.txt').write_text((restored.stdout or '') + (restored.stderr or ''))
        if restored.return_code:
            raise ValueError('Restored repository differs from recorded Agent patch result')
        save('grading')
        timing = {}
        grading = asyncio.create_task(_setup._verify_rollout(
            env, task, paths, timing, planes, sandbox_user=None, workspace='/home/github/build'))
        observer = asyncio.create_task(observe_build(env, output, grading))
        try:
            rewards, error, diagnostic = await grading
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        recovery.update(finished_at=now(), stage='finished')
        write_json(output / 'verifier-recovery.json', recovery)
        summary = dict(original_summary)
        summary.update(rewards=rewards, verifier_error=error,
            verifier_error_category='verifier_timeout' if diagnostic else ('verifier_error' if error else None),
            verifier_timeout_info=diagnostic.to_dict() if diagnostic else None,
            rollout_name=trial_name, finished_at=now(),
            timing={**(summary.get('timing') or {}), **timing}, verification_recovery=recovery)
        write_json(paths.rollout_dir / 'result.json', summary)
        backend_dir = output / 'backend'
        backend_dir.mkdir()
        write_json(backend_dir / 'command.json', {'mode': 'verifier_only_recovery', 'agent_replayed': False})
        backend = BackendRun(status='verifier_error' if error else 'completed',
                             return_code=1 if error else 0, backend_version='0.6.3')
        BenchFlowBackend()._collect(backend, result.prepared, output / 'jobs', backend_dir)
        result.execution = backend
        result.feedback = adapter.evaluate(backend)
        result.status = backend.status
        result.errors.extend(backend.errors)
        if result.feedback.status != 'scored' and result.status == 'completed':
            result.status = 'verifier_error'
    except Exception as error:
        result.status = 'backend_error'
        result.errors.append({'stage': 'verifier_recovery', 'message': type(error).__name__ + ': ' + str(error)})
        save('failed')
        raise
    finally:
        if env is not None:
            try:
                await env.stop(delete=True)
            except Exception as error:
                write_json(output / 'cleanup-warning.json', {'error_type': type(error).__name__})
        if not slot_reserved:
            release_verifier_slot(run, held_dispatcher)
        result.finished_at = now()
        write_json(output / 'result.json', result)
    print(json.dumps({'status': result.status, 'feedback': result.feedback.status,
                      'metrics': result.feedback.metrics, 'additional_agent_runs': 0}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'artifacts'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--verifier-timeout', type=int, required=True)
    parser.add_argument('--run', type=Path)
    parser.add_argument('--slot-reserved', action='store_true', help='Run inside an already counted method-worker slot')
    args = parser.parse_args()
    asyncio.run(recover(args.source.resolve(), args.output.resolve(), args.artifacts.resolve(), args.verifier_timeout,
                        run=args.run, slot_reserved=args.slot_reserved))


if __name__ == '__main__':
    main()
