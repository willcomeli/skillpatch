"""Recover a pre-Agent preparation failure at the single-writer batch boundary."""

from pathlib import Path

from .retry_policy import transient_network_failure
from .step_retries import retryable_build_store_failure


def validate_failure(run, name, target, read, sha256_file):
    run = Path(run).resolve(strict=True)
    archive = Path(target['archive']).resolve(strict=True)
    original = Path(target.get('original_method_folder', run / 'skillopt' / name)).resolve(strict=True)
    if (not original.is_relative_to(run) or original.name != name or not archive.is_relative_to(original)
            or sha256_file(archive) != target['archive_sha256']):
        raise ValueError('Preparation recovery archive identity changed')
    raw = read(archive)
    execution = raw['execution']
    summary = execution['summary']
    error = summary.get('error', '')
    if (not raw.get('finished_at') or raw.get('status') != 'agent_error'
            or execution.get('trajectory_events') != 0 or summary.get('n_tool_calls') != 0
            or summary.get('agent_result', {}).get('total_tokens') != 0
            or not error.startswith('Docker compose command failed for environment ')
            or raw['feedback']['status'] != 'unavailable'
            or not (transient_network_failure(error) or retryable_build_store_failure(error))):
        raise ValueError('Recovery is restricted to transient preparation failures before Agent')


def recover(state, requests, run, environment, optimizer, config, policy, secrets, save, *, concurrency=1):
    if not policy:
        return
    run = Path(run).resolve(strict=True)
    from . import experiment as exp
    from .resume_methods import recover_job
    pending = {}
    jobs = []

    def collect(name, result):
        previous, destination = pending[name]
        state['methods']['skillopt/' + name] = {**result, 'previous_preparation_failure': previous,
                                               'transport_recovery': str(destination)}
        save()
        print(f'[preparation-method-recovery] skillopt/{name}: {result["status"]}', flush=True)

    for name, target in policy['tasks'].items():
        key = 'skillopt/' + name
        if key not in state['methods']:
            raise ValueError('Recovery requires a collected failed method result')
        current = state['methods'][key]
        destination = Path(target['destination']).resolve()
        if not destination.is_relative_to(run / 'method_transport_recovery'):
            raise ValueError('Preparation recovery must use its own output folder')
        if current.get('transport_recovery') == str(destination):
            continue
        if current != target['previous_record'] or current['status'] != 'failed':
            raise ValueError('Preparation failure record changed before recovery')
        validate_failure(run, name, target, exp.read, exp.sha256_file)
        # Called only after the SkillOpt dispatcher has drained and execed
        # the original orchestrator. No live writer or Task is taken over.
        pending[name] = (current, destination)
        result_path = destination / 'result.json'
        if result_path.is_file():
            collect(name, exp.read(result_path))
        else:
            original = Path(target.get('original_method_folder', run / key))
            jobs.append((name, (name, requests[name], state['baselines'][name],
                                original, destination, environment, optimizer, config)))
    for name, result in exp._batch_results(recover_job, jobs, concurrency, secrets):
        collect(name, result)
