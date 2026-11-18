"""Generate one candidate patch from unified Stage1 outputs."""
from pathlib import Path
from excutor.executor_adapter.storage import json_digest, jsonable, now, tree_hashes, write_json
from method.skillpatch.session import run_session, session_secrets
from set_up.environment import redact, redact_artifacts, secret_values
from .models import INDUCTIONS, NOTE_EXAMPLE_FORMAT
from .patches import validate_generation
from .source import DiagnosisArchive, load_diagnosis


def run_stage2(diagnosis_dir: Path | DiagnosisArchive, issue_id: str, config, output: Path, environment, *,
               induction='standard', client=None, progress=None):
    if induction not in INDUCTIONS:
        raise ValueError('Unknown Stage2 induction method')
    output = output.expanduser().resolve()
    archive = diagnosis_dir if isinstance(diagnosis_dir, DiagnosisArchive) else None
    diagnosis_dir = archive.diagnosis_dir if archive else diagnosis_dir.expanduser().resolve(strict=True)
    if output.is_relative_to(diagnosis_dir):
        raise ValueError('Stage2 output must be outside the immutable Stage1 archive')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    emit = progress or (lambda _: None)
    secrets = secret_values(environment)
    state = {'status': 'running', 'phase': 'source', 'started_at': now(),
             'issue_id': issue_id, 'semantic_status': 'not_assessed'}
    try:
        generation = config.agent_config(environment)
        secrets = session_secrets(generation, environment)
        source_hashes = tree_hashes(diagnosis_dir)
        archive = archive or load_diagnosis(diagnosis_dir)
        if any(source_hashes.get(name) != digest for name, digest in archive.file_hashes.items()):
            raise ValueError('Stage1 archive changed since loading its diagnosis')
        source = archive.select(issue_id)
        issue = jsonable(source.conflict)
        state.update(archive_version=2, diagnosis_dir=str(diagnosis_dir),
                     source_input_digest=source.context.input_digest,
                     source_issue_digest=json_digest(issue),
                     generation_config=str(config.generation_config), phase='generation')
        emit(f'Generating a candidate patch for {issue_id}')
        folder = output / 'generation'
        if induction == 'boundary-aware':
            from method.skillpatch.boundary_aware_patch_induction.pipeline import run_boundary_induction

            state['induction'] = induction
            payload, revision = run_boundary_induction(
                source, generation, output, environment, prompts=config.induction_prompts(environment),
                client=client, progress=emit,
            )
            patch = validate_generation(payload, source, revision, patch_format=NOTE_EXAMPLE_FORMAT)
            state['revision'] = revision
        else:
            request = {'issue': issue, 'previous_attempts': [], 'issue_is_candidate': True, 'revision': 0}
            payload = run_session(generation, request, {'source': source.context}, folder, environment,
                                  context_paths={'source': diagnosis_dir / 'input/context.json'},
                                  client=client, progress=emit)
            patch = validate_generation(payload, source, revision=0)
        if tree_hashes(diagnosis_dir) != source_hashes:
            raise ValueError('Stage1 archive changed during patch generation')
        state.update(status='generated' if patch is not None else payload['status'],
                     phase='finished', reason=payload['reason'],
                     semantic_status='candidate_patch' if patch is not None else 'not_assessed',
                     semantic_correctness='not_verified', access_tracking='harness_read_ranges_not_verified')
        if patch is not None:
            raw = jsonable(patch)
            write_json(folder / 'patch.json', raw)
            state.update(patch_path=str(folder / 'patch.json'), patch_digest=json_digest(raw))
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
