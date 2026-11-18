"""Regrade one saved candidate, then resume its original SkillOpt trainer."""

from contextlib import contextmanager
from pathlib import Path
import subprocess
from unittest.mock import patch

from . import experiment as exp
from .resume_methods import recover_job

TASK = 'fix-build-agentops'


def task_files(folder):
    return {name: digest for name, digest in exp.tree_hashes(folder).items()
            if Path(name).parts[:2] != ('environment', 'skills')}


def validate_score(failed, graded):
    original = exp.read(failed / 'result.json')
    recovered = exp.read(graded / 'result.json')
    if (recovered.get('status') != 'completed' or recovered['feedback']['status'] != 'scored'
            or recovered['prepared']['file_hashes'] != original['prepared']['file_hashes']):
        raise ValueError('Regrading did not produce a formal score on the same candidate snapshot')
    provenance = recovered['execution']['summary'].get('verification_recovery', {})
    if (provenance.get('source_archive') != str(failed)
            or provenance.get('additional_agent_runs') != 0
            or provenance.get('additional_model_calls') != 0
            or provenance.get('verifier_timeout_seconds') != 900):
        raise ValueError('Regrading provenance does not bind the original Agent and approved budget')
    exp.load_archive(graded)
    return original


@contextmanager
def use_recovered_score(failed, graded, recovery, skill):
    original = validate_score(failed, graded)
    prepared = original['prepared']
    expected_skills = exp.tree_hashes(Path(prepared['skills_dir']))
    expected_task = task_files(Path(prepared['task_dir']))
    native = exp.execute
    audit = {'source_archive': str(failed), 'graded_archive': str(graded), 'skill': skill,
             'additional_agent_runs_for_recovered_candidate': 0, 'used': 0}

    def execute(request, output, environment, **kwargs):
        output = Path(output).resolve()
        target = output.is_relative_to((recovery / 'skills' / skill).resolve())
        if not target:
            return native(request, output, environment, **kwargs)
        # Fail closed for this Skill: a changed candidate must never silently
        # start another Agent instead of replaying the saved evaluation.
        if (audit['used'] or output.name != 'selection_eval-execution'
                or request.task_dir.name != TASK
                or task_files(request.task_dir) != expected_task
                or exp.tree_hashes(request.skills_dir) != expected_skills):
            raise ValueError('Requested evaluation differs from the exact regraded Skill candidate')
        output.mkdir(parents=True, exist_ok=False)
        audit['used'] += 1
        exp.write_json(output / 'reused-score.json', audit)
        return graded

    with patch.object(exp, 'execute', side_effect=execute):
        yield audit
    exp.write_json(recovery / 'recovered-score-use.json', audit)


def resume_job(name, request, baseline, original, recovery, environment, optimizer, config, spec):
    if name != TASK:
        raise ValueError('This scoring recovery is restricted to AgentOps')
    recovery.mkdir(parents=True, exist_ok=True)
    failed = Path(spec['failed_archive']).resolve(strict=True)
    artifacts = Path(spec['artifacts']).resolve(strict=True)
    run = Path(spec['run']).resolve(strict=True)
    if (not failed.is_relative_to(original.resolve()) or not artifacts.is_relative_to(run)
            or exp.sha256_file(artifacts / 'provenance.json') != spec['artifacts_provenance_sha256']):
        raise ValueError('Saved candidate/artifact identity changed')
    from .agentops_verifier_recovery import validate_source
    validate_source(failed, artifacts)
    grade = recovery / 'verifier_retry_900'
    exp.write_json(recovery / 'resume-spec.json', spec)
    try:
        if not (grade / 'result.json').exists():
            # The enclosing method worker already owns one of the ten slots.
            # No additional dispatcher hold or Task Agent is launched here.
            command = [request.bench_command[0], '-u', '-m',
                'scripts.skillsbench.agentops_verifier_recovery', '--source', str(failed),
                '--output', str(grade), '--artifacts', str(artifacts),
                '--verifier-timeout', '900', '--run', str(run), '--slot-reserved']
            with (recovery / 'verifier-retry.log').open('ab') as log:
                result = subprocess.run(command, cwd=exp.ROOT, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                raise ValueError('Verifier-only recovery failed; see verifier-retry.log')
        skill = spec['skill']
        stage = failed.parents[2]
        if stage.parent.name != skill:
            raise ValueError('Failed evaluation is not inside the expected Skill attempt')
        with use_recovered_score(failed, grade, recovery, skill) as audit:
            result = recover_job(name, request, baseline, original, recovery, environment, optimizer,
                                 config, stage_sources={skill: stage})
        if result.get('status') == 'completed' and audit['used'] != 1:
            raise ValueError('Original trainer did not consume the recovered candidate score exactly once')
        result['scoring_recovery'] = audit
    except Exception as error:
        result = exp._failure(error, exp.secret_values(environment))
        result['verifier_retry_archive'] = str(grade)
    exp.write_json(recovery / 'result.json', exp.redact(result, exp.secret_values(environment)))
    return result
