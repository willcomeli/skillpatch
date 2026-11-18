"""Add an explicitly requested failure-recovery round without restarting live jobs."""

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time

from . import experiment as exp
from .resume_methods import preflight, recover_job
from .resume_queue import drain_queue


def retain_transport_provenance(output, name, result):
    """Keep prior recovery provenance across later retries and handoffs."""
    output = Path(output).resolve()
    policy_path = output / 'METHOD_TRANSPORT_RECOVERY_REQUESTED.json'
    if not policy_path.exists():
        return result
    target = exp.read(policy_path).get('tasks', {}).get(name)
    if target is None:
        return result
    destination = Path(target['destination']).resolve()
    if destination != output / 'method_transport_recovery' / name:
        raise ValueError('Preparation recovery provenance destination changed')
    if not (destination / 'result.json').is_file():
        return result
    if result.get('transport_recovery') not in (None, str(destination)):
        raise ValueError('Conflicting preparation recovery provenance')
    return {**result, 'transport_recovery': str(destination),
            'previous_preparation_failure': target['previous_record']}


def worker(kind, arguments):
    if kind == 'agentops_verifier_resume':
        from .agentops_scoring_resume import resume_job
        return resume_job(*arguments)
    return recover_job(*arguments) if kind in {'retry', 'transport_retry'} else exp._method_job(*arguments)


def process(pid, output, module):
    p = subprocess.run(['ps', '-p', str(pid), '-o', 'stat=,command='], capture_output=True, text=True)
    if p.returncode:
        return None
    if str(output) not in p.stdout or module not in p.stdout or os.getpgid(pid) != pid:
        raise ValueError('Dispatcher ownership changed')
    return p.stdout.split()[0]


def active_layout(output, launch):
    """Bind inherited checkpoints to each suspended writer's actual workers."""
    pid = launch['pid']
    parents = {}
    if launch['phase_owner'] == 'skillopt_queue':
        progress = exp.read(output / 'queue_handoff/progress.json')
        parents[pid] = {'module': 'scripts.skillsbench.resume_queue', 'keys': progress['new_active']}
        roots = launch.get('additional_live_roots', [])
        if len(roots) > 1 or bool(roots) != bool(progress['legacy_active']):
            raise ValueError('Unexpected initial inherited dispatcher layout')
        if roots:
            parents[roots[0]] = {'module': 'scripts.skillsbench.resume_methods', 'keys': progress['legacy_active']}
        folders = {key: (output / 'resume_after_docker_cleanup' / key.split('/', 1)[1]
                        if key in progress['legacy_active'] else output / key)
                   for key in progress['legacy_active'] + progress['new_active']}
    elif launch['phase_owner'] == 'skillopt_retry_queue':
        round_dir = Path(launch.get('active_retry_round') or
                         exp.read(output / 'METHOD_FAILURE_RETRY_REQUESTED.json')['round_dir'])
        progress = exp.read(round_dir / 'progress.json')
        saved_parents = round_dir / 'parents.json'
        if saved_parents.exists():
            previous = {int(p): binding for p, binding in exp.read(saved_parents).items()}
        else:
            # First nine-task round predates persisted parent bindings.
            initial = exp.read(round_dir / 'previous-launcher.json')
            old_progress = exp.read(output / 'queue_handoff/progress.json')
            previous = {initial['pid']: {'module': 'scripts.skillsbench.resume_queue',
                                        'keys': old_progress['new_active']}}
            for root in initial.get('additional_live_roots', []):
                previous[root] = {'module': 'scripts.skillsbench.resume_methods',
                                  'keys': old_progress['legacy_active']}
        roots = launch.get('additional_live_roots', [])
        for root in roots:
            if root not in previous:
                raise ValueError('Unknown inherited dispatcher owner')
            binding = previous[root]
            parents[root] = {**binding, 'keys': [key for key in binding['keys']
                                                if key in progress['legacy_active']]}
        bound = [key for binding in parents.values() for key in binding['keys']]
        if len(bound) != len(set(bound)) or set(bound) != set(progress['legacy_active']):
            raise ValueError('Inherited active tasks do not have unique owners')
        parents[pid] = {'module': 'scripts.skillsbench.retry_queue', 'keys': progress['new_active']}
        outputs = exp.read(output / 'METHOD_RETRY_OUTPUTS.json')['tasks']
        folders = {}
        for key in progress['legacy_active'] + progress['new_active']:
            owner = next(binding for binding in parents.values() if key in binding['keys'])
            name = key.split('/', 1)[1]
            if owner['module'] == 'scripts.skillsbench.resume_methods':
                folders[key] = output / 'resume_after_docker_cleanup' / name
            elif owner['module'] == 'scripts.skillsbench.resume_queue':
                folders[key] = output / key
            else:
                folders[key] = Path(outputs[name]) if name in outputs else output / key
    else:
        raise ValueError('Expected a live SkillOpt queue dispatcher')
    return progress, folders, parents


def freeze_at_checkpoint(output):
    """Freeze only at a complete published active-set checkpoint."""
    while True:
        launch = exp.read(output.with_suffix('.launcher.json'))
        pid = launch['pid']
        module = ('scripts.skillsbench.retry_queue' if launch['phase_owner'] == 'skillopt_retry_queue'
                  else 'scripts.skillsbench.resume_queue')
        status = process(pid, output, module)
        if status is None or 'T' in status:
            raise ValueError('Dispatcher is already stopped or unavailable')
        os.kill(pid, signal.SIGSTOP)
        state = exp.read(output / 'result.json')
        try:
            progress, folders, parents = active_layout(output, launch)
        except BaseException:
            if state['status'] == 'running':
                os.kill(pid, signal.SIGCONT)
            raise
        keys = progress['legacy_active'] + progress['new_active']
        stable = (state['status'] == 'running' and state['phase'] == 'skillopt'
            and len(keys) == len(set(keys)) and len(keys) <= 10
            and all(key not in state['methods'] and folder.exists() for key, folder in folders.items()))
        if stable:
            launch = exp.read(output.with_suffix('.launcher.json'))
            return pid, launch, state, progress, folders, parents
        if state['status'] != 'running':
            raise ValueError('Honor the current experiment pause before retrying')
        os.kill(pid, signal.SIGCONT)
        time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=True)
    request_path = args.request.resolve(strict=True)
    request = exp.read(request_path)
    if request.get('status') != 'requested' or not request['tasks']:
        raise ValueError('Expected an authorized nonempty retry request')
    lock = request_path.with_suffix('.controller.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    launch = exp.read(output.with_suffix('.launcher.json'))
    command = launch['command']
    preflight(command, output)
    config = Path(command[command.index('--config') + 1])
    cfg, tasks, native, optimizer, environment = exp.load_settings(config,
        exp.load_environment(Path(command[command.index('--env-file') + 1])))
    requests = {task.name: exp.request_for(task, cfg, native) for task in tasks}
    old_pid, old_launch, state, progress, folders, parents = freeze_at_checkpoint(output)
    committed = False
    try:
        round_dir = Path(request['round_dir']).resolve()
        if not round_dir.is_relative_to(output):
            raise ValueError('Retry outputs must stay inside the experiment')
        round_dir.mkdir(parents=True, exist_ok=False)
        exp.write_json(round_dir / 'previous-result.json', state)
        exp.write_json(round_dir / 'previous-launcher.json', old_launch)
        legacy = {key: folder / 'result.json' for key, folder in folders.items()}
        exp.write_json(round_dir / 'parents.json', parents)
        jobs = []
        outputs = {}
        for name, original_record in request['tasks'].items():
            key = 'skillopt/' + name
            if state['methods'].get(key) != original_record or original_record.get('status') != 'failed':
                raise ValueError('Requested failure record changed before recovery')
            mode = request.get('mode', 'retry')
            destination = round_dir / name
            original = Path(request['resume'][name]['original_method_folder']) if mode in {'agentops_verifier_resume', 'transport_retry'} else output / key
            if mode == 'transport_retry':
                from .method_transport_recovery import validate_failure
                target = request['resume'][name]
                if target['previous_record'] != original_record:
                    raise ValueError('Preparation recovery failure record changed')
                validate_failure(output, name, target, exp.read, exp.sha256_file)
                destination = Path(target['destination']).resolve()
                if destination != output / 'method_transport_recovery' / name:
                    raise ValueError('Preparation recovery destination changed')
            outputs[name] = str(destination)
            if not original.resolve().is_relative_to(output):
                raise ValueError('Recovery source must stay inside the experiment')
            arguments = (name, requests[name], state['baselines'][name], original,
                destination, environment, optimizer, config.parent / cfg['skillopt_config'])
            if mode == 'agentops_verifier_resume':
                arguments += (request['resume'][name],)
            elif mode not in {'retry', 'transport_retry'}:
                raise ValueError('Unknown recovery mode')
            jobs.append((key, (mode, arguments)))
            del state['methods'][key]
        for name, original in requests.items():
            key = 'skillopt/' + name
            if name in request['tasks'] or key in state['methods'] or key in legacy:
                continue
            folder = output / key
            if folder.exists():
                raise ValueError('Unexpected pending method folder outside inherited active set')
            arguments = ('skillopt', name, original, state['baselines'][name], folder,
                         environment, optimizer, config.parent / cfg['skillopt_config'])
            jobs.append((key, ('normal', arguments)))
        exp.write_json(round_dir / 'plan.json', {'at': exp.now(), 'request': request,
            'runner_sha256': exp.sha256_file(Path(__file__)),
            'stage_cache_sha256': exp.sha256_file(Path(__file__).with_name('skillopt_stage_cache.py')),
            'resume_helpers_sha256': exp.sha256_file(Path(__file__).with_name('resume_methods.py')),
            'inherited_tasks': list(legacy), 'concurrency': 10,
            'policy': 'Prioritize requested retries; retain live jobs; reuse completed Skills and '
                      'exact cached candidates; original trainer gate and budgets; SkillOpt then Trace2Skill'})
        mapping_path = output / 'METHOD_RETRY_OUTPUTS.json'
        previous_outputs = exp.read(mapping_path).get('tasks', {}) if mapping_path.exists() else {}
        exp.write_json(mapping_path, {'round_dir': str(round_dir), 'tasks': {**previous_outputs, **outputs}})
        launch = exp.read(output.with_suffix('.launcher.json'))
        launch.update(pid=os.getpid(), phase_owner='skillopt_retry_queue',
                      additional_live_roots=list(parents), suspended_dispatcher_pids=list(parents),
                      active_retry_round=str(round_dir), active_retry_request=str(request_path))
        launch.setdefault('resume_history', []).append({'at': exp.now(), 'pid': os.getpid(),
            'reason': request['authorization']})
        exp.write_json(output.with_suffix('.launcher.json'), launch)
        state['method_retry_round'] = str(round_dir)
        exp.write_json(output / 'result.json', state)
        request.update(status='active', controller_pid=os.getpid(), started_at=exp.now())
        exp.write_json(request_path, request)
        committed = True
        retired = set()
        print(f'Inheriting {len(legacy)} live tasks; prioritizing {len(request["tasks"])} requested retries; '
              f'{len(jobs)-len(request["tasks"])} other SkillOpt tasks queued; total concurrency10', flush=True)

        def collect(key, result):
            name = key.split('/', 1)[1]
            if name in request['tasks']:
                result = {**result, 'retry_of': request['tasks'][name], 'retry_round': str(round_dir)}
                if request.get('mode') == 'transport_retry':
                    result.update(transport_recovery=outputs[name],
                                  previous_preparation_failure=request['tasks'][name])
            result = retain_transport_provenance(output, name, result)
            state['methods'][key] = result
            exp.write_json(output / 'result.json', exp.redact(state, exp.secret_values(environment)))
            print(f'[retry-queue] {key}: {result["status"]}', flush=True)

        def heartbeat(inherited, active, queued):
            for pid, binding in parents.items():
                if pid in retired or set(binding['keys']).intersection(inherited):
                    continue
                if process(pid, output, binding['module']) is not None:
                    os.killpg(pid, signal.SIGTERM)
                    os.kill(pid, signal.SIGCONT)
                retired.add(pid)
                current = exp.read(output.with_suffix('.launcher.json'))
                current['additional_live_roots'] = [p for p in parents if p not in retired]
                exp.write_json(output.with_suffix('.launcher.json'), current)
            exp.write_json(round_dir / 'progress.json', {'at': exp.now(), 'pid': os.getpid(),
                'legacy_active': inherited, 'new_active': active, 'queued': queued, 'concurrency': 10,
                'retired_dispatchers': sorted(retired), 'agent_or_method_restarted_for_handoff': False})

        drain_queue(jobs, legacy, worker, 10, collect, heartbeat, lambda: None)
        request.update(status='completed', finished_at=exp.now())
        exp.write_json(request_path, request)
        current = exp.read(output.with_suffix('.launcher.json'))
        current.update(pid=os.getpid(), phase_owner='experiment', additional_live_roots=[])
        exp.write_json(output.with_suffix('.launcher.json'), current)
        os.execv(command[0], command)
    except BaseException as error:
        if not committed and exp.read(output / 'result.json')['status'] == 'running':
            os.kill(old_pid, signal.SIGCONT)
        exp.write_json(output / 'method-retry-error.json', {'at': exp.now(),
            'error_type': type(error).__name__, 'ownership_transferred': committed})
        raise


if __name__ == '__main__':
    main()
