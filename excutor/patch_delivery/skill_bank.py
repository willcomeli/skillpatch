"""Optional sidecar delivery through native Runners' Skill-text interface."""

from functools import wraps
from inspect import signature
from pathlib import Path

from excutor.executor_adapter.storage import hash_manifest, tree_hashes
from excutor.excutor_runners.patch_sidecars import SIDECAR_MARKDOWN, load_sidecars


class PatchedSkillBank:
    def __init__(self, bank, skill_dir):
        self.bank = bank
        self.skill_dir = Path(skill_dir).expanduser().absolute()
        self.digest = hash_manifest(tree_hashes(self.skill_dir))
        if load_sidecars(self.skill_dir) is None:
            raise ValueError('Selected Skill directory has no validated patch sidecars')
        self.original = (self.skill_dir / 'SKILL.md').read_text(encoding='utf-8')
        self.patch = (self.skill_dir / SIDECAR_MARKDOWN).read_text(encoding='utf-8')

    def __getattr__(self, name):
        return getattr(self.bank, name)

    def format_for_prompt(self, memory):
        if hash_manifest(tree_hashes(self.skill_dir)) != self.digest:
            raise ValueError('Patch Skill files changed after selection')
        original = self.bank.format_for_prompt(memory)
        if original != self.original:
            raise ValueError('Retrieved Skill text does not exactly match the selected patch base')
        return original + '\n\n## Skill interpretation supplements\n\n' + self.patch


def optional_skill_patches(method):
    """Add patch_skill_dir= to task and batch entry points; defaults are untouched."""
    method_signature = signature(method)

    @wraps(method)
    def run(*args, patch_skill_dir=None, **kwargs):
        if patch_skill_dir is None:
            return method(*args, **kwargs)
        bound = method_signature.bind(*args, **kwargs)
        if bound.arguments.get('checkpoint_path') or bound.arguments.get('checkpoint_dir'):
            raise ValueError('Patched execution requires a fresh run without native checkpoint reuse')
        bank = bound.arguments['skill_bank']
        if isinstance(bank, PatchedSkillBank):
            raise ValueError('Skill bank is already wrapped with patch delivery')
        patched = PatchedSkillBank(bank, patch_skill_dir)
        bound.arguments['skill_bank'] = patched
        result = method(*bound.args, **bound.kwargs)
        receipt = {'presentation_mode': 'skill_text_with_sidecar',
                   'skill_dir': str(patched.skill_dir.resolve()), 'skill_digest': patched.digest}
        for row in result if isinstance(result, list) else [result]:
            row['patch_deployment'] = dict(receipt)
        return result
    return run
