"""Source-task replay only: no dev split, semantic judge, or automatic activation."""

from dataclasses import replace
from pathlib import Path
import json
import shutil

from excutor.executor_adapter.storage import now, sha256_file, write_json, jsonable
from excutor.eviskill_runners.launcher import FileSkillBank, run_native
from excutor.patch_delivery.skill_bank import PatchedSkillBank
from excutor.excutor_runners.patch_sidecars import (
    SIDECAR_MANIFEST, SIDECAR_MARKDOWN, load_sidecars, write_sidecars,
)
from method.skillpatch.patch_consolidation.source import load_patch
from method_input_adapters.archive import load_archive
from method_input_adapters.skillpatch import build_diagnosis_context
from set_up.environment import redact, redact_artifacts, secret_values
from set_up.native_config import NativeRunConfig


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def archive_fingerprints(root):
    """Pin archive contents and link identities without traversing external data.

    Native AppWorld archives contain a runtime/data symlink to the dataset.
    Skill packs retain their separate, stricter no-links validation.
    """
    fingerprints = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            fingerprints[relative] = ('symlink', str(path.readlink()))
        elif path.is_file():
            fingerprints[relative] = ('file', sha256_file(path))
        elif not path.is_dir():
            raise ValueError('Execution archives cannot contain special files')
    return fingerprints


def load_source(patch_file):
    patch, source, episode = load_patch(patch_file)
    if source.context.benchmark != 'appworld':
        raise ValueError('AppWorld replay requires an AppWorld diagnosis')
    task_path = source.run_dir.resolve(strict=True)
    # Only managed native runs contain the execution settings and task/Skill snapshots.
    if task_path.parent.name != 'tasks':
        raise ValueError('Replay requires a native launcher tasks/task_*.json source archive')
    root = task_path.parent.parent
    state = read(root / 'result.json')
    if state.get('status') != 'completed' or state.get('errors'):
        raise ValueError('Source batch must have completed without execution errors')
    paths = [Path(value).resolve() for value in state['task_results']]
    if task_path not in paths:
        raise ValueError('Source task is not registered in its batch archive')
    index = paths.index(task_path)
    tasks = read(root / 'input/tasks.json')
    if len(tasks) != len(paths):
        raise ValueError('Source task snapshots do not match the saved result list')
    ref = tasks[index]
    task_id = ref.get('task_id') if isinstance(ref, dict) else str(ref)
    row = read(task_path)
    if task_id != source.context.task_id or row.get('task_id') != task_id:
        raise ValueError('Source task identity differs from its input snapshot')
    current = build_diagnosis_context(task_path)
    if jsonable(current) != read(source.diagnosis_dir / 'input/source_context.json'):
        raise ValueError('Source task no longer matches the diagnosed execution')
    baseline = load_archive(task_path)
    raw = read(root / 'config.json')
    if raw.get('benchmark') != 'appworld':
        raise ValueError('Source execution configuration is not AppWorld')
    for key in ('tasks_path', 'skill_path', 'output_root'):
        raw[key] = Path(raw[key]) if raw.get(key) else None
    config = NativeRunConfig(**raw)
    # Never re-read a possibly edited original Skill or the original task list.
    skill = root / 'input/SKILL.md'
    if not skill.is_file():
        raise ValueError('Source execution has no saved Skill snapshot')
    config = replace(config, skill_path=skill)
    previous = root / 'input/patched_skill'
    bank = FileSkillBank(skill)
    if previous.exists():
        bank = PatchedSkillBank(bank, previous)
    delivered = bank.format_for_prompt({})
    if delivered != row.get('retrieved_memory_text'):
        raise ValueError('Saved Skill and patches differ from the source delivered text')
    item = next(item for item in source.context.items if item.context_id == patch.target.skill_ref)
    if item.text != delivered or sha256_file(Path(patch.target.source_path)) != patch.target.version:
        raise ValueError('Patch target snapshot differs from the source delivered text')
    # Delivered memory may include existing supplements. New targets must bind
    # uniquely to original instructions, not to an earlier patch or its metadata.
    original = skill.read_text(encoding='utf-8')
    if original.count(patch.target.quote) != 1:
        raise ValueError('Patch quote must uniquely target the original Skill instructions')
    if patch.target.scope == 'whole_skill' and previous.exists():
        raise ValueError('Whole-Skill target includes prior supplements; re-diagnose a substep')
    return patch, source, episode, root, ref, config, baseline, previous if previous.exists() else None


def render_candidate(patch, config, previous, output):
    if previous is not None:
        shutil.copytree(previous, output)
        entries = load_sidecars(output)['entries']
        for name in (SIDECAR_MANIFEST, SIDECAR_MARKDOWN):
            (output / name).unlink()
    else:
        output.mkdir()
        shutil.copyfile(config.skill_path, output / 'SKILL.md')
        entries = []
    entry = {'entry_id': patch.patch_id, 'target': {
        'relative_path': 'SKILL.md', 'scope': patch.target.scope,
        'quote': patch.target.quote, 'version': sha256_file(output / 'SKILL.md')},
        'when_to_use': patch.when_to_use, 'how_to_interpret': patch.how_to_interpret}
    write_sidecars(output, [*entries, entry])
    return {'archived_target': jsonable(patch.target), 'deployed_target': entry['target'],
            'binding': 'exact delivered text verified; unique quote rebound to saved original SKILL.md',
            'previous_patch_count': len(entries), 'candidate_patch_count': len(entries) + 1}


def run_validation(patch_file: Path, output: Path, environment, *, prepare_only=False):
    patch, source, episode, root, ref, config, baseline, previous = load_source(patch_file)
    output = output.expanduser().resolve()
    protected = (root, source.diagnosis_dir, episode)
    if any(output.is_relative_to(path.resolve()) for path in protected):
        raise ValueError('Replay output must be outside execution, diagnosis and generation archives')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = secret_values(environment)
    state = {'status': 'running', 'started_at': now(), 'benchmark': 'appworld',
             'patch_id': patch.patch_id, 'task_id': baseline.task_id,
             'baseline_strategy': 'reuse_source_execution', 'validation_split_used': False,
             'acceptance': 'not_decided', 'active_version_changed': False}
    try:
        pinned = {path: archive_fingerprints(path) for path in protected}
        binding = render_candidate(patch, config, previous, output / 'candidate_skill')
        write_json(output / 'binding.json', binding)
        write_json(output / 'patch.json', patch)
        write_json(output / 'tasks.json', [ref])
        config = replace(config, tasks_path=output / 'tasks.json', output_root=output)
        write_json(output / 'config.json', redact(jsonable(config), secrets))
        plan = {'source_task': source.run_dir, 'source_task_sha256': sha256_file(source.run_dir),
                'diagnosis_dir': source.diagnosis_dir, 'generation_dir': episode,
                'task_ref': ref, 'execution_config': jsonable(config),
                'baseline_success': baseline.success, 'baseline_score': baseline.score,
                'candidate_skill': output / 'candidate_skill', 'max_task_executions': 1,
                'limitations': ['One source-task replay does not establish generalization.',
                    'External dataset, server package and environment-resolved model endpoint are not pinned by historical native archives.']}
        write_json(output / 'plan.json', redact(jsonable(plan), secrets))
        result = run_native(config, output / 'candidate', environment, prepare_only=prepare_only,
                            patch_skill_dir=output / 'candidate_skill')
        if result['status'] == 'cancelled':
            state['status'] = 'cancelled'
        elif result['status'] != ('prepared' if prepare_only else 'completed'):
            raise ValueError(f"Candidate execution failed: {result['status']}; see candidate/result.json")
        elif prepare_only:
            state['status'] = 'prepared'
        else:
            if len(result['task_results']) != 1:
                raise ValueError('Replay must produce exactly one task result')
            candidate_path = Path(result['task_results'][0])
            candidate = load_archive(candidate_path)
            candidate_row = read(candidate_path)
            candidate_bank = PatchedSkillBank(FileSkillBank(config.skill_path), output / 'candidate_skill')
            if (candidate.task_id != baseline.task_id or candidate.instruction != baseline.instruction
                    or candidate_row.get('retrieved_memory_text') != candidate_bank.format_for_prompt({})
                    or not candidate_row.get('patch_deployment')):
                raise ValueError('Candidate task or delivered patch differs from the planned replay')
            outcome = ('repaired' if not baseline.success and candidate.success else
                       'regressed' if baseline.success and not candidate.success else
                       'still_failed' if not candidate.success else 'still_succeeded')
            comparison = {'outcome': outcome, 'baseline_success': baseline.success,
                          'candidate_success': candidate.success, 'baseline_score': baseline.score,
                          'candidate_score': candidate.score, 'score_delta': candidate.score - baseline.score,
                          'baseline_trajectory': str(source.run_dir), 'candidate_trajectory': str(candidate_path)}
            write_json(output / 'comparison.json', comparison)
            state.update(status='completed', comparison=comparison)
        if any(archive_fingerprints(path) != hashes for path, hashes in pinned.items()):
            raise ValueError('Historical input archives changed during replay')
    except KeyboardInterrupt:
        state['status'] = 'cancelled'
    except (OSError, ValueError, KeyError, TypeError) as error:
        state.update(status='failed', error=redact(str(error), secrets))
    finally:
        state['finished_at'] = now()
        try:
            redact_artifacts(output, secrets)
        except OSError as error:
            state.update(status='failed', error=redact(str(error), secrets))
        write_json(output / 'result.json', redact(state, secrets))
    return state
