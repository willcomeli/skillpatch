"""Offline Stage1: archive adaptation, local agent session and model report."""

from dataclasses import replace
from pathlib import Path
from typing import Callable

from context import refresh_input_digest
from method_input_adapters.skillpatch import build_diagnosis_context
from harness.agent_client import AgentClient, parse_json_object
from excutor.executor_adapter.storage import jsonable, now, sha256_file, tree_hashes, write_json
from set_up.environment import redact, redact_artifacts, secret_values
from .config import DiscoveryConfig
from .tool.reporting import render_model_report


def _bind_delivered_memory(context, inputs):
    """Snapshot a native bank's delivered text, without inventing original file provenance."""
    if context.benchmark not in {'alfworld', 'scienceworld', 'appworld'}:
        return
    for index, item in enumerate(context.items):
        if item.kind != 'skill' or item.skill_id is not None:
            continue
        relative = f'delivered_{item.context_id}/SKILL.md'
        path = inputs / 'delivered_skills' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.text.encode('utf-8'))
        context.items[index] = replace(
            item, skill_id=relative, relative_path=relative, source_path=str(path),
            version=sha256_file(path), source_line=1,
            observation={**item.observation, 'original_source_path': item.source_path,
                         'target_binding': 'delivered_memory_snapshot'},
        )
    context.limitations.append(
        'Native Skill targets bind to an exact snapshot of delivered memory text, '
        'not an independently recovered original Skill file; later deployment must '
        'confirm the same base text before using such a patch.'
    )


def run_discovery(run_dir: Path, config: DiscoveryConfig, output: Path, environment,
                  *, prepare_only=False, client=None, progress: Callable[[str], None] | None = None,
                  input_builder=None) -> dict:
    source = run_dir.expanduser().resolve(strict=True)
    output = output.expanduser().resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    emit = progress or (lambda _: None)
    secrets = set(secret_values(environment))
    if environment.get(config.agent.api_key_env):
        secrets.add(environment[config.agent.api_key_env])
    secrets = tuple(sorted(secrets, key=len, reverse=True))
    state = {'status': 'running', 'phase': 'input', 'started_at': now(),
             'source': str(source), 'output': str(output), 'prepare_only': prepare_only,
             'semantic_status': 'not_assessed'}
    try:
        emit('Collecting offline diagnosis context')
        context = (input_builder or build_diagnosis_context)(source)
        if redact(jsonable(context), secrets) != jsonable(context):
            raise ValueError('Known credential value appears in diagnosis materials; not sent')
        state.update(run_id=context.run_id, benchmark=context.benchmark,
                     task_id=context.task_id, task_succeeded=context.task_succeeded)
        write_json(output / 'config.json', config)
        eligibility = context.evolution_eligibility
        if eligibility not in {'eligible', 'succeeded', 'invalid'}:
            eligibility = 'invalid'
        state.update(evolution_eligibility=eligibility, eligibility_reason=context.eligibility_reason)
        if eligibility != 'eligible':
            state.update(status='skipped', phase='eligibility', reason=eligibility)
            emit(f'Skipping Stage1: {eligibility} ({context.eligibility_reason})')
            return state
        inputs = output / 'input'
        inputs.mkdir()
        write_json(inputs / 'source_context.json', context)
        _bind_delivered_memory(context, inputs)
        refresh_input_digest(context)
        context_path = inputs / 'context.json'
        write_json(context_path, context)
        input_hashes = tree_hashes(inputs)
        prompt = config.prompt_path.read_text(encoding='utf-8')
        if not prompt.strip():
            raise ValueError('Diagnosis prompt is empty')
        if redact(prompt, secrets) != prompt:
            raise ValueError('Known credential value appears in diagnosis prompt')
        (output / 'prompt.md').write_text(prompt, encoding='utf-8')
        write_json(output / 'config.json', config)
        state.update(run_id=context.run_id, benchmark=context.benchmark, task_id=context.task_id,
                     input_digest=context.input_digest, context_path=str(context_path),
                     item_count=len(context.items), prompt_sha256=sha256_file(output / 'prompt.md'))
        if prepare_only:
            state['status'] = 'prepared'
        else:
            state['phase'] = 'agent'
            result = (client or AgentClient()).run(config.agent, prompt, [context_path],
                                                  output / 'agent', environment, progress=emit)
            state['agent_status'] = result.status
            if tree_hashes(inputs) != input_hashes:
                raise ValueError('Agent changed diagnosis input materials')
            if result.status != 'completed':
                state.update(status=result.status, errors=result.errors)
            else:
                state['phase'] = 'report'
                if result.report_path is None:
                    raise ValueError('Completed agent session has no report')
                report_path = result.report_path.resolve(strict=True)
                if not report_path.is_relative_to((output / 'agent/workspace').resolve()):
                    raise ValueError('Agent report is outside its working directory')
                payload = parse_json_object(report_path.read_text(encoding='utf-8'))
                if redact(payload, secrets) != payload:
                    raise ValueError('Known credential value appears in diagnosis report')
                write_json(output / 'report.json', payload)
                (output / 'report.md').write_text(render_model_report(payload), encoding='utf-8')
                improvements = payload.get('improvements')
                state.update(status='completed', phase='finished',
                             semantic_status='candidate_diagnosis',
                             report_validation='disabled',
                             improvement_count=len(improvements) if isinstance(improvements, list) else None,
                             report_path=str(output / 'report.json'))
    except KeyboardInterrupt:
        state.update(status='cancelled', error='Diagnosis interrupted')
    except (OSError, ValueError) as error:
        state.update(status='failed', error=redact(str(error), secrets))
    finally:
        state['finished_at'] = now()
        try:
            redact_artifacts(output, secrets)
        except OSError as error:
            state.update(status='failed', error=redact(str(error), secrets))
        write_json(output / 'result.json', state)
    return state
