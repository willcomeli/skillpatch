"""Read a generated candidate and reuse its exact SkillsBench source baseline."""
from pathlib import Path
from dataclasses import replace
from method_input_adapters.skillpatch import build_diagnosis_context
from excutor.executor_adapter.models import RunRequest
from excutor.executor_adapter.storage import jsonable, hash_manifest, tree_hashes
from method.skillpatch.patch_generation.source import read_json
from method.skillpatch.patch_consolidation.source import load_patch
from .records import load_execution


def load_request(path):
    raw = read_json(path)
    raw.pop('timeout_sec', None)  # Historical outer wall-clock limit is no longer used.
    raw.pop('diagnosis_config', None)  # Historical execution-only configuration.
    for name in ('task_dir','skills_dir'):
        if raw.get(name) is not None: raw[name] = Path(raw[name])
    raw['bench_command'] = tuple(raw['bench_command'])
    request = RunRequest(**raw)
    if request.benchmark != 'skillsbench' or request.backend != 'benchflow':
        raise ValueError('Patch validation currently supports only SkillsBench with BenchFlow')
    if request.skill_mode != 'with-skill' or not request.explicit_skill_read:
        raise ValueError('Validation requires with-skill and explicit Skill reading')
    return request


def load_validation_source(path):
    patch, source, episode = load_patch(path)
    if source.context.benchmark != 'skillsbench':
        raise ValueError('Patch validation currently supports only SkillsBench')
    baseline = load_execution(source.run_dir)
    current = build_diagnosis_context(source.run_dir)
    archived = read_json(source.diagnosis_dir/'input/context.json')
    current_data = jsonable(current)
    if any(current_data.get(key) != value for key,value in archived.items() if key != 'input_digest'):
        raise ValueError('Source execution context differs from the Stage1 archive')
    if baseline.prepared.task_digest != source.context.task_digest:
        raise ValueError('Source task digest differs from diagnosis')
    pack = baseline.prepared.skills_dir
    if pack is None or hash_manifest(tree_hashes(pack)) != source.context.skill_digest:
        raise ValueError('Source Skill snapshot differs from diagnosis')
    request = load_request(source.run_dir/'request.json')
    request = replace(request,task_dir=baseline.prepared.task_dir,skills_dir=pack)
    return patch,source,episode,baseline,request
