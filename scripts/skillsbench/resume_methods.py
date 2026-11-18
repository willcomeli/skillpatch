"""Resume interrupted SkillOpt packs after their containers have been removed.

Completed task results and individual SkillOpt summaries are reused. Incomplete
Skills and pack replays receive new directories; original artifacts stay intact.
After recovering the interrupted batch, exec the unchanged experiment launcher.
"""

import argparse
from contextlib import redirect_stdout, redirect_stderr, nullcontext
from dataclasses import replace
import fcntl
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch
from uuid import uuid4

from . import experiment as exp
from .step_retries import method_request_retries


def completed_skill(folder, initial):
    """Require the trainer's final summary, checkpoint and selected Skill."""
    required = ('summary.json', 'runtime_state.json', 'best_skill.md')
    if not all((folder / name).is_file() for name in required):
        return None
    summary = exp.read(folder / 'summary.json')
    runtime = exp.read(folder / 'runtime_state.json')
    if (summary.get('config', {}).get('skill_init') != str(initial)
            or summary.get('config', {}).get('out_root') != str(folder)
            or int(summary.get('total_steps', 0)) < 1
            or int(runtime.get('last_completed_step', 0)) < int(summary['total_steps'])):
        raise ValueError(f'Inconsistent completed SkillOpt checkpoint: {folder}')
    return summary


def recover_pack(baseline, request, original, recovery, environment, optimizer, config, *, stage_sources=None):
    frozen = exp.tree_hashes(baseline)
    recovery.mkdir(parents=True, exist_ok=True)
    progress_file = recovery / 'progress.json'
    progress = exp.read(progress_file) if progress_file.exists() else {'skills': {}}
    previous_progress = exp.read(original / 'progress.json') if (original / 'progress.json').exists() else {'skills': {}}
    selected = []
    for initial in sorted(request.skills_dir.glob('*/SKILL.md')):
        name = initial.parent.name
        saved = progress['skills'].get(name) or previous_progress['skills'].get(name)
        source = (Path(saved['folder']) if saved else
                  Path(stage_sources[name]) if stage_sources and name in stage_sources else original / 'skills' / name)
        summary = completed_skill(source, initial)
        if saved and (summary is None or exp.sha256_file(source / 'best_skill.md') != saved['best_sha256']):
            raise ValueError(f'Recovered Skill checkpoint changed: {name}')
        reused = summary is not None
        if summary is None:
            # The previous container no longer exists. Start only this
            # unfinished Skill in a fresh output, retaining all older stages.
            source = recovery / 'skills' / name / ('attempt_' + uuid4().hex[:12])
            from .skillopt_stage_cache import cached_stages
            cached = Path(stage_sources[name]) if stage_sources and name in stage_sources else original / 'skills' / name
            with cached_stages(cached, source, baseline, initial, optimizer, config):
                summary = exp.optimize_skillopt_one(baseline, request, name, source,
                                                    environment, optimizer, config)
            if completed_skill(source, initial) is None:
                raise ValueError(f'SkillOpt did not finish: {name}')
        progress['skills'][name] = {'folder': str(source), 'reused_completed_skill': reused,
            'best_sha256': exp.sha256_file(source / 'best_skill.md')}
        exp.write_json(progress_file, progress)
        selected.append({'skill': name, 'optimization': summary,
                         'selected_skill_source': str(source / 'best_skill.md')})
        print(f'[resume-skill] {request.task_dir.name}/{name}: '
              f'{"reused" if reused else "completed"}', flush=True)

    pack = recovery / ('candidate_pack_' + uuid4().hex[:12])
    shutil.copytree(request.skills_dir, pack)
    for row in selected:
        shutil.copyfile(row['selected_skill_source'], pack / row['skill'] / 'SKILL.md')
    # Each new replay has its own archive; never overwrite interrupted runs.
    candidate = exp.execute(replace(request, skills_dir=pack),
                            recovery / ('source_replay_' + uuid4().hex[:12]), environment)
    if exp.tree_hashes(baseline) != frozen:
        raise ValueError('Reference archive changed during method recovery')
    return {'status': 'completed', 'skills': selected, 'candidate_pack': str(pack),
            'skill_changed': exp.tree_hashes(pack) != exp.tree_hashes(request.skills_dir),
            'comparison': exp.comparison(baseline, candidate),
            'recovery': {'original_method_folder': str(original),
                         'reason': 'User resumed after Docker cleanup; old containers unavailable'}}


def recover_job(name, original_request, record, original, recovery, environment, optimizer, config, *, stage_sources=None):
    recovery.mkdir(parents=True, exist_ok=True)
    secrets = exp.secret_values(environment)
    try:
        baseline = Path(record['source'])
        if exp.tree_hashes(baseline) != record['hashes']:
            raise ValueError(f'Reference archive changed: {name}')
        raw = exp.read(baseline / 'result.json')
        request = replace(original_request, task_dir=Path(raw['prepared']['task_dir']),
                          skills_dir=Path(raw['prepared']['skills_dir']))
        retries = method_request_retries() if '--step-retries' in request.bench_command else nullcontext()
        with (recovery / 'run.log').open('a') as log, redirect_stdout(log), redirect_stderr(log), retries:
            result = recover_pack(baseline, request, original, recovery, environment, optimizer, config,
                                  stage_sources=stage_sources)
    except Exception as error:
        result = exp._failure(error, secrets)
    exp.write_json(recovery / 'result.json', exp.redact(result, secrets))
    return result


def preflight(command, output):
    """Use the original resume validation, stopping before its first write."""
    class Validated(Exception):
        pass

    def stop(path, value):
        if Path(path) != output / 'result.json':
            raise ValueError(f'Unexpected preflight write: {path}')
        raise Validated

    with patch.object(exp, 'write_json', side_effect=stop):
        try:
            exp.main(command[4:])
        except Validated:
            return
    raise ValueError('Resume preflight did not reach the validated save point')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight-only', action='store_true')
    args = parser.parse_args(argv)
    output = args.output.resolve(strict=True)
    launcher_path = output.with_suffix('.launcher.json')
    launcher = exp.read(launcher_path)
    command = launcher['command']
    if command[1:4] != ['-u', '-m', 'scripts.skillsbench.experiment'] or '--resume' not in command:
        raise ValueError('Expected the saved original experiment resume command')
    lock = (output / 'method-resume.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    preflight(command, output)
    if args.preflight_only:
        print('Original experiment plan, configurations and implementation hashes match', flush=True)
        return
    config = Path(command[command.index('--config') + 1])
    env_file = Path(command[command.index('--env-file') + 1])
    cfg, tasks, native, optimizer, environment = exp.load_settings(config, exp.load_environment(env_file))
    state = exp.read(output / 'result.json')
    if state['status'] not in {'paused', 'running'}:
        raise ValueError('Expected a paused experiment or an interrupted resume')
    recovery_root = output / 'resume_after_docker_cleanup'
    recovery_root.mkdir(exist_ok=True)
    if not (recovery_root / 'state_before_resume.json').exists():
        exp.write_json(recovery_root / 'state_before_resume.json', state)
    jobs = []
    for task in tasks:
        key = 'skillopt/' + task.name
        original = output / 'skillopt' / task.name
        if key in state['methods'] or not original.exists():
            continue
        recovery = recovery_root / task.name
        if (recovery / 'result.json').exists():
            state['methods'][key] = exp.read(recovery / 'result.json')
            continue
        jobs.append((key, (task.name, exp.request_for(task, cfg, native), state['baselines'][task.name],
            original, recovery, environment, optimizer, config.parent / cfg['skillopt_config'])))
    exp.write_json(recovery_root / 'plan.json', {'at': exp.now(),
        'runner_sha256': exp.sha256_file(Path(__file__)), 'concurrency': 10,
        'tasks': [key for key, _ in jobs], 'policy': 'Reuse completed individual Skills; '
        'new directories for unfinished Skills and pack replay; preserve existing completed/failed/blocked task results; '
        'original model, method configurations, native budgets and in-place network retries unchanged'})
    state.update(status='running', phase='skillopt', resumed_at=exp.now(), concurrency=10)
    state.pop('finished_at', None)
    launcher.update(pid=os.getpid(), status='running', phase_owner='method_resume',
                    method_resume_runner=str(Path(__file__)), resumed_at=exp.now())
    exp.write_json(launcher_path, launcher)
    exp.write_json(output / 'result.json', state)
    marker = output / 'USER_PAUSE_REQUESTED.json'
    if marker.exists():
        data = exp.read(marker)
        data.update(status='resumed', resumed_at=exp.now())
        exp.write_json(marker, data)
    print(f'Recovering {len(jobs)} interrupted method tasks, concurrency=10', flush=True)
    for key, result in exp._batch_results(recover_job, jobs, 10, exp.secret_values(environment)):
        state['methods'][key] = result
        exp.write_json(output / 'result.json', state)
        print(f'[method-resume] {key}: {result["status"]}', flush=True)
    launcher.update(phase_owner='experiment', pid=os.getpid())
    exp.write_json(launcher_path, launcher)
    print('Interrupted batch collected; continuing the original full experiment', flush=True)
    os.execv(command[0], command)


if __name__ == '__main__':
    main()
