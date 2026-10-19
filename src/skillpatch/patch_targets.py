"""Conservative bindings to structural units in a fixed, original Skill.

Offsets are half-open Python Unicode character offsets, not byte offsets. Quotes
locate a unit; they are deliberately excluded from that unit's stable identity.
Historical deployed-file hashes belong in sidecars, never in these identities.
"""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from skillpatch.markdown_layout import document_lines, substep_insertion_offset
from skillpatch.patch_sidecars import PRESENTATION_INLINE, PRESENTATION_SIDECAR
from skillpatch.patches import tree_hashes
from skillpatch.skill_invocation import document_body
from skillpatch.storage import hash_manifest


def _relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Target must be a canonical relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or not path.parts:
        raise ValueError("Target must be a canonical relative path")
    return path


def _checked_file(root: Path, relative_path: str) -> Path:
    relative = _relative_path(relative_path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Skill root must be a real directory")
    path = root
    for component in relative.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("Skill target paths cannot contain symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Target is missing or outside the original Skill")
    return path


def locate_skill_root(pack: Path, target_relative_path: str) -> tuple[Path, str]:
    """Find the nearest enclosing SKILL.md without conflating same-named Skills."""
    path = _checked_file(pack, target_relative_path)
    directory = path.parent
    while True:
        declaration = directory / "SKILL.md"
        if declaration.is_symlink():
            raise ValueError("Skill declaration cannot be a symlink")
        if declaration.is_file():
            relative = directory.relative_to(pack).as_posix()
            return directory, "" if relative == "." else relative
        if directory == pack:
            break
        directory = directory.parent
    raise ValueError("Target has no enclosing original Skill declaration")


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _units(text: str, body_start: int) -> tuple[list[dict], list[dict]]:
    lines = document_lines(text)
    markers = []
    for index, line in enumerate(lines):
        if line["fenced"] or line["start"] < body_start:
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*[\r\n]*$", line["text"])
        step = re.match(r"^([ \t]*)([0-9]{1,9}[.)])[ \t]+(\S.*?)[\r\n]*$", line["text"])
        if heading:
            markers.append({"kind": "heading", "level": len(heading.group(1)),
                            "label": heading.group(2).strip(), "start": line["start"], "line": index})
        elif step:
            markers.append({"kind": "step", "indent": len(step.group(1).expandtabs(4)),
                            "label": step.group(2) + " " + step.group(3).strip(),
                            "start": line["start"], "line": index})
    for position, marker in enumerate(markers):
        end = len(text)
        for other in markers[position + 1:]:
            if marker["kind"] == "heading":
                closes = other["kind"] == "heading" and other["level"] <= marker["level"]
            else:
                closes = other["kind"] == "heading" or other["indent"] <= marker["indent"]
            if closes:
                end = other["start"]
                break
        if marker["kind"] == "step":
            # An unindented paragraph following a list is not silently absorbed
            # into its final step. Ambiguous lazy continuation is left unbound.
            for line in lines[marker["line"] + 1:]:
                if line["start"] >= end:
                    break
                if line["fenced"] or not line["text"].strip():
                    continue
                indentation = len(line["text"]) - len(line["text"].lstrip(" \t"))
                indentation = len(line["text"][:indentation].expandtabs(4))
                if indentation <= marker["indent"]:
                    end = line["start"]
                    break
        marker["end"] = end
    return markers, lines


def bind_target(original_dir: Path, skill_key: str, relative_path: str,
                scope: str, quote: str, *, presentation_mode: str = PRESENTATION_SIDECAR) -> dict:
    """Bind an exact unique quote to one unambiguous heading/numbered step.

    A nested child owns its own text; a quote spanning parent/child or sibling
    boundaries is rejected. Code and table text belong to their enclosing
    instruction unit; sidecars require no insertion location. Historical inline
    bindings retain their original layout restrictions and offsets.
    """
    if not isinstance(skill_key, str) or not skill_key.strip():
        raise ValueError("An explicit nonempty Skill identity is required")
    if presentation_mode not in {PRESENTATION_INLINE, PRESENTATION_SIDECAR}:
        raise ValueError("Unsupported target presentation mode")
    if scope not in {"whole_skill", "substep"}:
        raise ValueError("Unsupported patch target scope")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("Target quote must be nonempty")
    before = tree_hashes(original_dir)
    if "SKILL.md" not in before:
        raise ValueError("Original Skill directory must contain SKILL.md at its root")
    source = _checked_file(original_dir, relative_path)
    root, _ = locate_skill_root(original_dir, relative_path)
    if root != original_dir:
        raise ValueError("Target belongs to a nested Skill, not the declared original Skill")
    if source.suffix.lower() != ".md":
        raise ValueError("Supplement targets must be Markdown instructions")
    text = source.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    normalized_quote = quote.replace("\r\n", "\n").replace("\n", newline)
    if text.count(normalized_quote) != 1:
        raise ValueError("Target quote is absent or ambiguous in the original Skill")
    quote_start = text.index(normalized_quote)
    quote_end = quote_start + len(normalized_quote)
    body_start = len(text) - len(document_body(text))
    if scope == "whole_skill":
        if relative_path != "SKILL.md":
            raise ValueError("Whole-Skill targets require the root SKILL.md")
        start, end, label, kind = body_start, len(text), "whole_skill", "whole_skill"
        insertion_offset = body_start
    else:
        if quote_start < body_start:
            raise ValueError("Substep target cannot be in frontmatter")
        units, lines = _units(text, body_start)
        if presentation_mode == PRESENTATION_INLINE:
            insertion_offset = substep_insertion_offset(text, quote_start, quote_end, lines=lines)
        owners = [unit for unit in units if unit["start"] <= quote_start < unit["end"]]
        if not owners:
            raise ValueError("Quote has no unambiguous heading or numbered-step boundary")
        unit = max(owners, key=lambda item: item["start"])
        if quote_end > unit["end"] or any(quote_start < other["start"] < quote_end for other in units):
            raise ValueError("Target quote crosses structural object boundaries")
        start, end, label, kind = unit["start"], unit["end"], unit["label"], unit["kind"]
    base_digest = hash_manifest(before)
    canonical = {"skill_key": skill_key, "base_digest": base_digest, "scope": scope,
                 "relative_path": relative_path, "start": start, "end": end,
                 "unit_sha256": _sha(text[start:end]), "label": label, "unit_kind": kind}
    identity = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    result = {"schema_version": 1 if presentation_mode == PRESENTATION_INLINE else 2, **canonical,
              "target_id": "T" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
              "source_quote": normalized_quote}
    if presentation_mode == PRESENTATION_INLINE:
        result["insertion_offset"] = insertion_offset
    if tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed while binding its target")
    return result
