"""Re-grade an exact saved Energy report with native BenchFlow; no Agent replay."""
import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import shutil
import uuid

from excutor.executor_adapter.models import BackendRun, ExecutionResult, RunRequest
from excutor.executor_adapter.skillsbench import SkillsBenchExecutionAdapter
from excutor.executor_adapter.storage import now, write_json
from excutor.excutor_runners.benchflow import BenchFlowBackend


def validate_report_source(source, report, digest):
    if hashlib.sha256(report.read_bytes()).hexdigest() != digest:
        raise ValueError('Saved report checksum changed')
    matches = list(source.glob('jobs/*/*/result.json'))
    if len(matches) != 1:
        raise ValueError('Expected one original native Agent result')
    native = matches[0]
    summary = json.loads(native.read_text())
    events = native.parent / 'trajectory/acp_trajectory.jsonl'
    rows = [json.loads(line) for line in events.read_text().split('\n') if line.strip()]
    if (summary['task_name'] != 'energy-ac-optimal-power-flow' or summary.get('error')
            or summary.get('agent_timeout_info') or not rows or rows[-1].get('type') != 'agent_message'):
        raise ValueError('Original Agent execution did not finish normally')
    provenance = report.parent / 'provenance.jsonl'
    proof = [json.loads(line) for line in provenance.read_text().splitlines() if line.strip()]
    if not any(row.get('source_archive') == str(source) and row.get('sha256') == digest
               and row.get('phase') == 'after_agent' for row in proof):
        raise ValueError('No after-Agent provenance binds this report to this trajectory')
    return native, summary, events


async def recover(source, output, report, digest):
    from benchflow.task import Task, RolloutPaths
    from benchflow.rollout_planes import DefaultRolloutPlanes
    from benchflow.rollout import _setup
    from .step_retries import enable_step_retries
    from .python_mirrors import enable_verifier_routing, policy_for_directory
    assert version('benchflow') == '0.6.3'
    original_native, original_summary, trajectory = validate_report_source(source, report, digest)
    old = json.loads((source / 'result.json').read_text())
    spec = json.loads((source / 'request.json').read_text())
    spec['task_dir'] = Path(spec['task_dir'])
    if spec.get('skills_dir'):
        spec['skills_dir'] = Path(spec['skills_dir'])
    spec['bench_command'] = tuple(spec['bench_command'])
    request = RunRequest(**spec)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = ExecutionResult(run_id=output.name, benchmark='skillsbench', task_id=request.task_dir.name,
                             status='running', started_at=old['started_at'])
    write_json(output / 'request.json', request)
    write_json(output / 'result.json', result)
    adapter = SkillsBenchExecutionAdapter()
    env = None
    try:
        result.prepared = adapter.prepare(request, output / 'input')
        assert result.prepared.file_hashes == old['prepared']['file_hashes']
        if (source / 'prompt').is_dir():
            shutil.copytree(source / 'prompt', output / 'prompt')
        sdk = output / 'sdk' / request.task_dir.name
        shutil.copytree(result.prepared.task_dir, sdk)
        task = Task(result.prepared.task_dir)
        assert task.config.verifier.timeout_sec == 240
        planes = DefaultRolloutPlanes()
        if result.prepared.skills_dir:
            planes.inject_skills_into_dockerfile(sdk, result.prepared.skills_dir)
        trial_name = task.name + '__verify_' + uuid.uuid4().hex[:8]
        paths = RolloutPaths(output / 'jobs' / 'verifier-recovery' / trial_name)
        paths.mkdir()
        shutil.copytree(trajectory.parent, paths.rollout_dir / 'trajectory')
        write_json(paths.rollout_dir / 'original-native-summary.json', original_summary)
        policy = policy_for_directory(output)
        enable_verifier_routing(index=policy['index_url'])
        enable_step_retries(apt_mirror=True)
        proxy = 'http://127.0.0.1:7897'
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ.setdefault(key, proxy)
        env = planes.create_environment('docker', task, sdk, trial_name, paths,
                    preserve_agent_network=False, environment_manifest=None)
        await env.start(force_build=False)
        await env.upload_file(report, '/root/report.json')
        timing = {}
        rewards, error, diagnostic = await _setup._verify_rollout(
            env, task, paths, timing, planes, sandbox_user=None, workspace='/root')
        recovery = {'source_archive': str(source), 'source_native_summary': str(original_native),
                    'source_trajectory_sha256': hashlib.sha256(trajectory.read_bytes()).hexdigest(),
                    'report_sha256': digest, 'additional_agent_runs': 0, 'additional_model_calls': 0,
                    'native_verifier_timeout_seconds': 240, 'finished_at': now(),
                    'scope': 'Native verifier only on exact backed-up Agent deliverable; original Agent trace reused unchanged'}
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
        raise
    finally:
        if env is not None:
            try:
                await env.stop(delete=True)
            except Exception as error:
                write_json(output / 'cleanup-warning.json', {'error_type': type(error).__name__})
        result.finished_at = now()
        write_json(output / 'result.json', result)
    print(json.dumps({'status': result.status, 'feedback': result.feedback.status,
                      'metrics': result.feedback.metrics, 'additional_agent_runs': 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'output', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--report-sha256', required=True)
    args = parser.parse_args()
    asyncio.run(recover(args.source.resolve(), args.output.resolve(), args.report.resolve(), args.report_sha256))


if __name__ == '__main__':
    main()
