"""Recheck generated patches and bind exact archived targets without a registry."""
from pathlib import PurePosixPath

from excutor.executor_adapter.storage import json_digest
from method.skillpatch.patch_generation.source import load_patch  # compatibility for existing experiment callers


def bind_target(patch, source, skill_key):
    """Conservative identity: same explicit Skill key and exact declaration/target/anchor.

    This binds available archived text, not an independently recovered whole-pack version.
    Different anchors are left separate even when they might describe one structural step.
    """
    if not isinstance(skill_key, str) or not skill_key.strip():
        raise ValueError('An explicit skill_key is required')
    target = patch.target
    item = next((item for item in source.context.items if item.context_id == target.skill_ref), None)
    declaration = next((item for item in source.context.items
                        if item.kind == 'skill' and item.relative_path == target.skill_id), None)
    if item is None or declaration is None or not declaration.text.strip():
        raise ValueError('Target and main SKILL.md must both be present in the context')
    def relative(value):
        if not isinstance(value, str) or not value or '\\' in value:
            raise ValueError('Invalid archived Skill path')
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != value:
            raise ValueError('Skill path must be canonical and relative')
        return path
    skill = relative(target.skill_id)
    if skill.name != 'SKILL.md':
        raise ValueError('Skill identity must name its main SKILL.md')
    file = relative(target.relative_path)
    try:
        local = file.relative_to(skill.parent).as_posix()
    except ValueError:
        raise ValueError('Patch target lies outside its declared Skill') from None
    if item.kind != 'skill' or item.skill_id != target.skill_id or item.relative_path != target.relative_path:
        raise ValueError('Target metadata disagrees with context item')
    if target.quote not in item.text:
        raise ValueError('Target anchor is absent from its archived Skill text')
    binding = {'skill_key': skill_key, 'relative_path': local, 'scope': target.scope,
               'quote': target.quote, 'target_text_digest': json_digest(item.text),
               'declaration_text_digest': json_digest(declaration.text)}
    return {**binding, 'target_id': json_digest(binding),
            'binding_scope': 'exact_archived_text_and_anchor; not_full_skill_pack'}
