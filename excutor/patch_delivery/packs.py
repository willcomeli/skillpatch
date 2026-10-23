"""Select a patched Skill pack without modifying baseline resources."""

from dataclasses import replace
from pathlib import Path

from excutor.executor_adapter.storage import hash_manifest, tree_hashes
from excutor.excutor_runners.patch_sidecars import SIDECAR_MANIFEST, SIDECAR_MARKDOWN, load_sidecars


def prepare_patch_request(request, patch_skills_dir: Path):
    if request.skill_mode != 'with-skill' or not request.explicit_skill_read:
        raise ValueError('Patch delivery requires with-skill and explicit_skill_read=True')
    baseline = request.skills_dir or request.task_dir / 'environment/skills'
    patched = patch_skills_dir.expanduser().absolute()
    base_hashes = tree_hashes(baseline)
    patch_hashes = tree_hashes(patched)
    reserved = {SIDECAR_MARKDOWN, SIDECAR_MANIFEST}

    def originals(hashes):
        return {name: value for name, value in hashes.items() if Path(name).name not in reserved}

    if originals(base_hashes) != originals(patch_hashes):
        raise ValueError('Patched pack must preserve all original Skill files and resources')
    roots = {path.parent for path in patched.glob('*/SKILL.md')}
    for name in patch_hashes:
        path = patched / name
        if path.name in reserved and path.parent not in roots:
            raise ValueError('Patch sidecars must belong to a top-level Skill')
    patched_skills = []
    for directory in sorted(roots):
        if load_sidecars(directory) is not None:
            patched_skills.append(directory.name)
    if not patched_skills:
        raise ValueError('Selected pack has no validated Skill patch sidecars')
    digest = hash_manifest(patch_hashes)
    return replace(request, skills_dir=patched), {
        'source_pack': str(patched.resolve()), 'baseline_pack': str(baseline.resolve()),
        'deployed_pack_digest': digest, 'patched_skills': patched_skills,
        'presentation_mode': 'sidecar_v1',
    }
