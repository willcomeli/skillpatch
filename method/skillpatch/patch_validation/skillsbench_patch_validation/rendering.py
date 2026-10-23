"""Render a candidate in a separate pack while preserving all original resources."""
import difflib
import shutil
import stat
from pathlib import Path, PurePosixPath
from method.skillpatch.patch_generation.models import SkillPatch
from excutor.excutor_runners.patch_sidecars import (PRESENTATION_INLINE, PRESENTATION_SIDECAR,
    SIDECAR_MARKDOWN, SIDECAR_MANIFEST, load_sidecars, write_sidecars)
from excutor.executor_adapter.storage import sha256_file, hash_manifest

def tree_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Skill pack must be a real directory")
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError("Skill packs cannot contain links or special files")
        result[path.relative_to(root).as_posix()] = sha256_file(path)
    return result


def target_file(root: Path, patch: SkillPatch) -> Path:
    relative = PurePosixPath(patch.target.relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("Patch target must be a relative file inside the Skill pack")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Patch target is missing or outside its Skill pack")
    if sha256_file(path) != patch.target.version:
        raise ValueError("Patch base version has changed; re-diagnose instead of silently relocating")
    return path


def render_patch(pack: Path, patch: SkillPatch, output: Path, *,
                 presentation_mode: str = PRESENTATION_SIDECAR) -> dict:
    """Copy a pack and add a separate patch file without editing any original."""
    if presentation_mode == PRESENTATION_INLINE:
        raise ValueError("Validation supports sidecar presentation only")
    if presentation_mode != PRESENTATION_SIDECAR:
        raise ValueError("Unsupported patch presentation mode")
    # Stage2 needs an exact version-bound quote, not a Stage3 object identity.
    # Locating the owning Skill does not require headings or numbered steps.
    from .targets import locate_skill_root

    before = tree_hashes(pack)
    source = target_file(pack, patch)
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(pack.resolve()):
        raise ValueError("Candidate Skill pack must be a new directory outside its source")
    skill_root, prefix = locate_skill_root(pack, patch.target.relative_path)
    relative = source.relative_to(skill_root).as_posix()
    existing = load_sidecars(skill_root)
    entry = {"entry_id": patch.patch_id,
             "target": {"relative_path": relative, "scope": patch.target.scope,
                        "quote": patch.target.quote.replace("\r\n", "\n"), "version": patch.target.version},
             "when_to_use": patch.when_to_use, "how_to_interpret": patch.how_to_interpret}
    shutil.copytree(pack, output, symlinks=True)
    if tree_hashes(pack) != before or tree_hashes(output) != before:
        raise ValueError("Skill pack changed while copying")
    deployed = output / prefix if prefix else output
    entries = existing["entries"] + [entry] if existing else [entry]
    if existing:
        # These are verified generated artifacts, not original instructions.
        # Preserve their complete entry prefix and regenerate only the pair.
        for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST):
            (deployed / name).unlink()
    sidecars = write_sidecars(deployed, entries)
    after = tree_hashes(output)
    paths = [(PurePosixPath(prefix) / name).as_posix() for name in sidecars["sidecar_paths"]]
    if (any(after.get(name) != version for name, version in before.items() if name not in paths)
            or set(after) - set(before) != set(paths) - set(before) or tree_hashes(pack) != before):
        raise ValueError("Sidecar renderer changed an original Skill resource")
    diff = "".join("".join(difflib.unified_diff(
                   (pack / name).read_text().splitlines(True) if name in before else [],
                   (output / name).read_text().splitlines(True),
                   fromfile="baseline/" + name if name in before else "/dev/null", tofile="candidate/" + name))
                   for name in paths)
    return {"patch_id": patch.patch_id, "presentation_mode": PRESENTATION_SIDECAR,
            "base_digest": hash_manifest(before), "candidate_digest": hash_manifest(after),
            "base_hashes": before, "candidate_hashes": after,
            "target_path": patch.target.relative_path, "sidecar_paths": paths,
            "selection_description_updated": False, "limitations": [], "diff": diff}
