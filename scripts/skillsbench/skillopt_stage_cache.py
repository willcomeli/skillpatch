"""Reuse completed SkillOpt stages, then let its native trainer rerun evaluation."""

import copy
from contextlib import contextmanager
from pathlib import Path
import shutil
from unittest.mock import patch

from . import experiment as exp


@contextmanager
def cached_stages(original, destination, baseline, initial, optimizer, config_path):
    step = original / 'steps/step_0001'
    required = ('candidate_skill.md', 'merged_patch.json', 'ranked_edits.json')
    if not all((step / name).is_file() for name in required):
        yield
        return
    from skillopt.config import flatten_config, load_config
    from skillopt.engine import trainer

    old = exp.read(original / 'config.json')
    expected = flatten_config(load_config(str(exp.ROOT / 'method/skillopt/configs/_base_/default.yaml')))
    expected.update(flatten_config(load_config(str(config_path))))
    runtime_fields = {'out_root', 'skill_init', 'model_backend', 'optimizer_backend', 'target_backend',
        'optimizer_model', 'target_model', 'reasoning_effort', 'openai_compatible_base_url',
        'openai_compatible_api_key'}
    for key, value in expected.items():
        if key not in runtime_fields and old.get(key) != value:
            raise ValueError(f'Cached SkillOpt configuration changed: {key}')
    if (old.get('skill_update_mode') != 'patch' or old['skill_init'] != str(initial)
            or old['optimizer_model'] != optimizer.model
            or old['openai_compatible_base_url'] != optimizer.base_url):
        raise ValueError('Cached SkillOpt input/model binding changed')
    binding = exp.read(original / 'selection_eval_baseline/source_binding.json')
    if binding['source'] != str(baseline) or not binding['reference_reused']:
        raise ValueError('Cached SkillOpt baseline differs from the current reference')
    start = (original / 'skills/skill_v0000.md').read_text()
    if start != initial.read_text():
        raise ValueError('Cached initial Skill differs from its frozen reference')
    merged = exp.read(step / 'merged_patch.json')
    ranked = exp.read(step / 'ranked_edits.json')
    candidate = (step / 'candidate_skill.md').read_text()
    patch_files = sorted((step / 'patches').glob('minibatch_*.json'))
    raw = [exp.read(path) for path in patch_files]
    failures, successes = trainer._normalise_patches(raw, update_mode='patch')
    if not patch_files or not (failures or successes):
        raise ValueError('Cached candidate lacks its completed reflection stages')
    native_apply = trainer.apply_patch_with_report
    if native_apply(start, ranked)[0] != candidate:
        raise ValueError('Cached candidate does not match native patch application')
    target = destination / 'steps/step_0001/patches'
    target.mkdir(parents=True, exist_ok=True)
    for path in patch_files:
        shutil.copyfile(path, target / path.name)
    calls = {'merge': 0, 'select': 0, 'apply': 0}

    def merge(skill, failure_patches, success_patches, **kwargs):
        if skill != start or failure_patches != failures or success_patches != successes:
            raise ValueError('Reflection inputs differ from cached SkillOpt stages')
        calls['merge'] += 1
        return copy.deepcopy(merged)

    def select(skill, merged_patch, **kwargs):
        if skill != start or merged_patch != merged or kwargs.get('max_edits') != old['edit_budget']:
            raise ValueError('Selection inputs differ from cached SkillOpt stages')
        calls['select'] += 1
        return copy.deepcopy(ranked)

    def apply(skill, selected):
        if skill != start or selected != ranked:
            raise ValueError('Patch application inputs differ from the saved candidate')
        result = native_apply(skill, selected)
        if result[0] != candidate:
            raise ValueError('Candidate Skill changed during stage recovery')
        calls['apply'] += 1
        return result

    audit = {'original_skill_folder': str(original),
        'candidate_sha256': exp.sha256_file(step / 'candidate_skill.md'),
        'stage_hashes': {str(path): exp.sha256_file(path)
                       for path in [*patch_files, step / 'merged_patch.json', step / 'ranked_edits.json']},
        'policy': 'Reuse cached reflection/aggregation/selection; native patch application, '
                  'execution, gate and trainer checkpoints still run; no changed training parameters',
        'original_stage_model_cost': 'Reused from original attempt; not a new model call'}
    exp.write_json(destination / 'cached-stages.json', audit)
    with patch.object(trainer, 'merge_patches', side_effect=merge), \
            patch.object(trainer, 'rank_and_select', side_effect=select), \
            patch.object(trainer, 'apply_patch_with_report', side_effect=apply):
        yield
    if calls != {'merge': 1, 'select': 1, 'apply': 1}:
        raise ValueError('Cached one-step recovery did not follow the original trainer stages')
    exp.write_json(destination / 'cached-stages-completed.json', calls)
