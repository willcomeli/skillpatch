"""Generate one candidate patch from unified Stage1 outputs."""
from pathlib import Path
from excutor.executor_adapter.storage import write_json, tree_hashes, now
from method.skillpatch.patch_diagnosis.config import load_discovery_config
from set_up.environment import redact, redact_artifacts, secret_values
from .source import load_source
from .patch_generation import generate_patch


def run_stage2(diagnosis_dir: Path, issue_id: str, config, output: Path, environment, *,
               client=None, progress=None):
    output = output.expanduser().resolve()
    diagnosis_dir = diagnosis_dir.expanduser().resolve(strict=True)
    if output.is_relative_to(diagnosis_dir):
        raise ValueError('Stage2 output must be outside the immutable Stage1 archive')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    emit = progress or (lambda _: None)
    secrets = set(secret_values(environment))
    state = {'status': 'running', 'phase': 'source', 'started_at': now(),
             'issue_id': issue_id, 'semantic_status': 'not_assessed'}
    try:
        generation = load_discovery_config(config.generation_config)
        if environment.get(generation.agent.api_key_env):
            secrets.add(environment[generation.agent.api_key_env])
        source_hashes = tree_hashes(diagnosis_dir)
        source = load_source(diagnosis_dir, issue_id)
        write_json(output / 'config.json', config)
        write_json(output / 'source.json', {'diagnosis_dir': diagnosis_dir,
            'issue': source.conflict, 'input_digest': source.context.input_digest})
        state['phase'] = 'generation'
        emit(f'Generating a candidate patch for {issue_id}')
        patch, payload = generate_patch(source, generation, output / 'generation', environment,
                                       client=client, progress=emit)
        if tree_hashes(diagnosis_dir) != source_hashes:
            raise ValueError('Stage1 archive changed during patch generation')
        state.update(status='generated' if patch is not None else payload['status'],
                     phase='finished', reason=payload['reason'],
                     semantic_status='candidate_patch' if patch is not None else 'not_assessed',
                     generation_path=str(output / 'generation/generation.json'))
        if patch is not None:
            state['patch_path'] = str(output / 'generation/patch.json')
    except KeyboardInterrupt:
        state.update(status='cancelled')
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
