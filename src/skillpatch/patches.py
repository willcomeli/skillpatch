"""Version-bound interpretation sidecars and historical inline presentations."""

import difflib
import hashlib
import json
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

from skillpatch.markdown_layout import substep_insertion_offset
from skillpatch.patch_models import PatchSource, SkillPatch
from skillpatch.patch_sidecars import (PRESENTATION_INLINE, PRESENTATION_SIDECAR,
                                      SIDECAR_MANIFEST, SIDECAR_MARKDOWN, load_sidecars, write_sidecars)
from skillpatch.skill_invocation import document_body
from skillpatch.storage import hash_manifest, sha256_file


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


def validate_generation(payload: dict, source: PatchSource, revision: int) -> SkillPatch | None:
    if set(payload) != {"status", "reason", "when_to_use", "how_to_interpret"}:
        raise ValueError("Patch generation fields do not match the schema")
    if payload["status"] not in {"candidate", "insufficient_evidence", "unpatchable"}:
        raise ValueError("Invalid patch generation status")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Patch generation must explain its decision")
    if payload["status"] != "candidate":
        if payload["when_to_use"] is not None or payload["how_to_interpret"] is not None:
            raise ValueError("Non-candidate outputs must not contain a patch")
        return None
    for key in ("when_to_use", "how_to_interpret"):
        if not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > 16000:
            raise ValueError("Both patch sections must be nonempty bounded text")
        if "<!-- skillpatch:" in payload[key] or "\x00" in payload[key]:
            raise ValueError("Patch text contains a reserved marker or null byte")
    identity = "\n".join((source.context.run_id, source.conflict.conflict_id, source.context.input_digest))
    patch_id = "P" + hashlib.sha256(identity.encode()).hexdigest()[:16] + f"-r{revision}"
    return SkillPatch(patch_id, source.context.run_id,
                      source.context.input_digest, source.conflict.conflict_id, source.conflict.type,
                      revision, source.conflict.target, payload["when_to_use"], payload["how_to_interpret"])


def render_patch(pack: Path, patch: SkillPatch, output: Path, *,
                 presentation_mode: str = PRESENTATION_SIDECAR) -> dict:
    """Copy a pack and add a separate patch file without editing any original."""
    if presentation_mode == PRESENTATION_INLINE:
        return _render_inline_patch(pack, patch, output)
    if presentation_mode != PRESENTATION_SIDECAR:
        raise ValueError("Unsupported patch presentation mode")
    # Stage2 needs an exact version-bound quote, not a Stage3 object identity.
    # Locating the owning Skill does not require headings or numbered steps.
    from skillpatch.patch_targets import locate_skill_root

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


def _render_inline_patch(pack: Path, patch: SkillPatch, output: Path) -> dict:
    before = tree_hashes(pack)
    source = target_file(pack, patch)
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(pack.resolve()):
        raise ValueError("Candidate Skill pack must be a new directory outside its source")
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    quote = patch.target.quote.replace("\r\n", "\n").replace("\n", newline)
    if text.count(quote) != 1:
        raise ValueError("Target quote is absent or ambiguous in the complete original file")
    block = (f"\n<!-- skillpatch:{patch.patch_id} -->\n"
             f"### When to use\n\n{patch.when_to_use.strip()}\n\n"
             f"### How to interpret\n\n{patch.how_to_interpret.strip()}\n"
             f"<!-- /skillpatch:{patch.patch_id} -->\n\n")
    block = block.replace("\r\n", "\n").replace("\n", newline)
    if patch.target.scope == "whole_skill":
        body = document_body(text)
        offset = len(text) - len(body)
    else:
        start = text.index(quote)
        # A code quote keeps its exact target while its supplement is placed
        # before the opening fence, leaving the entire example unchanged.
        offset = substep_insertion_offset(text, start, start + len(quote))
    rendered = text[:offset] + block + text[offset:]
    selection_updated = False
    limitations = []
    if patch.target.scope == "whole_skill" and source.name != "SKILL.md":
        raise ValueError("Whole-Skill supplements require a SKILL.md target with selection metadata")
    if patch.target.scope == "whole_skill":
        body = document_body(rendered)
        header_length = len(rendered) - len(body)
        header = rendered[:header_length]
        # Only a single-line YAML description can be changed safely without a
        # YAML dependency; ambiguous/multiline metadata fails explicitly.
        matches = list(re.finditer(r"^description:[^\r\n]*", header, re.MULTILINE))
        if len(matches) != 1 or matches[0].group().split(":", 1)[1].strip() in {"", "|", ">", "|-", ">-"}:
            raise ValueError("Whole-Skill patch requires a supported single-line description for selection visibility")
        match = matches[0]
        existing = match.group().split(":", 1)[1].strip()
        if existing.startswith(('"', "'")):
            if existing[0] == '"':
                existing = json.loads(existing)
            elif existing.endswith("'"):
                existing = existing[1:-1].replace("''", "'")
            else:
                raise ValueError("Malformed Skill description")
        explanation = (existing + " When to use: " + " ".join(patch.when_to_use.split())
                       + " How to interpret: " + " ".join(patch.how_to_interpret.split()))
        rendered = rendered[:match.start()] + "description: " + json.dumps(explanation, ensure_ascii=False) + rendered[match.end():]
        selection_updated = True
    if patch.target.scope == "substep":
        limitations.append("Substep supplement is visible in the body; selection-level effects are not claimed.")
    shutil.copytree(pack, output, symlinks=True)
    if tree_hashes(pack) != before or tree_hashes(output) != before:
        raise ValueError("Skill pack changed while copying")
    destination = output / patch.target.relative_path
    destination.write_bytes(rendered.encode("utf-8"))
    after = tree_hashes(output)
    for name, version in before.items():
        if name != patch.target.relative_path and after.get(name) != version:
            raise ValueError("Patch renderer changed an unrelated Skill resource")
    diff = "".join(difflib.unified_diff(text.splitlines(True), rendered.splitlines(True),
                                      fromfile="baseline/" + patch.target.relative_path,
                                      tofile="candidate/" + patch.target.relative_path))
    return {"patch_id": patch.patch_id, "presentation_mode": PRESENTATION_INLINE,
            "base_digest": hash_manifest(before),
            "candidate_digest": hash_manifest(after), "base_hashes": before, "candidate_hashes": after,
            "target_path": patch.target.relative_path, "selection_description_updated": selection_updated,
            "insertion_offset": offset,
            "limitations": limitations, "diff": diff}
