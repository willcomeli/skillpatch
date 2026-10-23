"""Deterministic standalone interpretation files shared by Stage2 and Stage3.

Only the rule, its exact version-bound target, and its two interpretation fields
are deployed. Experiment receipts and execution evidence stay outside the Skill.
"""

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from excutor.executor_adapter.storage import sha256_file


SIDECAR_MARKDOWN = "SKILL_PATCHES.md"
SIDECAR_MANIFEST = "SKILL_PATCHES.json"
PRESENTATION_SIDECAR = "sidecar_v1"
PRESENTATION_INLINE = "inline_v1"


def _checked_target(skill_root: Path, target: dict) -> None:
    if set(target) != {"relative_path", "scope", "quote", "version"}:
        raise ValueError("Sidecar target fields do not match the schema")
    value = target["relative_path"]
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Sidecar target requires a canonical relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ValueError("Sidecar target requires a canonical relative path")
    path = skill_root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Sidecar target cannot contain symlinks")
        if path != skill_root / value and (path / "SKILL.md").is_file():
            raise ValueError("Sidecar target belongs to a nested Skill")
    if not path.is_file() or path.suffix.lower() != ".md":
        raise ValueError("Sidecar target must be a Markdown instruction file")
    if value in {SIDECAR_MARKDOWN, SIDECAR_MANIFEST}:
        raise ValueError("Sidecar target must refer to an original instruction")
    if target["scope"] not in {"whole_skill", "substep"}:
        raise ValueError("Invalid sidecar target scope")
    if target["scope"] == "whole_skill" and value != "SKILL.md":
        raise ValueError("Whole-Skill supplements require the root SKILL.md")
    if sha256_file(path) != target["version"]:
        raise ValueError("Sidecar target version differs from the original instruction")
    quote = target["quote"]
    if not isinstance(quote, str) or not quote.strip() or "\x00" in quote:
        raise ValueError("Sidecar target quote must be nonempty text")
    text = path.read_bytes().decode("utf-8")
    if text.replace("\r\n", "\n").count(quote.replace("\r\n", "\n")) != 1:
        raise ValueError("Sidecar target quote is absent or ambiguous")


def _validate_entries(skill_root: Path, entries: list[dict]) -> None:
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise ValueError("Sidecar Skill root must be a real directory")
    if not (skill_root / "SKILL.md").is_file() or (skill_root / "SKILL.md").is_symlink():
        raise ValueError("Sidecar Skill root must contain an original SKILL.md")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Sidecar entries must be a nonempty ordered list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
                "entry_id", "target", "when_to_use", "how_to_interpret"}:
            raise ValueError("Sidecar entry fields do not match the schema")
        identifier = entry["entry_id"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier):
            raise ValueError("Invalid sidecar patch entry ID")
        if identifier in seen:
            raise ValueError("Duplicate sidecar patch entry ID")
        seen.add(identifier)
        for field in ("when_to_use", "how_to_interpret"):
            value = entry[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 16000:
                raise ValueError("Both sidecar patch sections must be bounded nonempty text")
            if "\x00" in value or "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value:
                raise ValueError("Sidecar patch text contains a reserved marker or null byte")
        if not isinstance(entry["target"], dict):
            raise ValueError("Sidecar patch target must be an object")
        _checked_target(skill_root, entry["target"])


def _markdown(entries: list[dict]) -> tuple[str, list[dict]]:
    header = ("# Skill interpretation patches\n\n"
              "Read the original Skill and these supplements before applying its instructions. "
              "Each supplement applies only to its named target and when its condition holds. "
              "Keep the original Skill instructions for all other cases.\n\n")
    pieces = [header]
    spans = [{"origin": "sidecar_metadata", "entry_ids": [],
              "rendered_start": 0, "rendered_end": len(header)}]
    offset = len(header)
    for entry in entries:
        target = entry["target"]
        quote = target["quote"].replace("\r\n", "\n")
        fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", quote)), default=0))
        block = (f"## Patch {entry['entry_id']}\n\n"
                 f"Target file: {json.dumps(target['relative_path'], ensure_ascii=False)}\n\n"
                 f"Target scope: `{target['scope']}`\n\n"
                 f"Target version: `{target['version']}`\n\n"
                 f"### Target instruction\n\n{fence}text\n{quote}\n{fence}\n\n"
                 f"### When to use\n\n{entry['when_to_use'].strip()}\n\n"
                 f"### How to interpret\n\n{entry['how_to_interpret'].strip()}\n\n")
        block = block.replace("\r\n", "\n")
        pieces.append(block)
        spans.append({"origin": "patch", "entry_ids": [entry["entry_id"]],
                      "rendered_start": offset, "rendered_end": offset + len(block)})
        offset += len(block)
    return "".join(pieces), spans


def write_sidecars(skill_root: Path, entries: list[dict]) -> dict:
    """Add a manifest and Markdown file; refuse to overwrite either reserved path."""
    for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST):
        if (skill_root / name).exists() or (skill_root / name).is_symlink():
            raise ValueError("Original Skill already contains a reserved sidecar path")
    _validate_entries(skill_root, entries)
    markdown, spans = _markdown(entries)
    manifest = {"schema_version": 1, "presentation_mode": PRESENTATION_SIDECAR, "entries": entries}
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    (skill_root / SIDECAR_MARKDOWN).write_bytes(markdown.encode("utf-8"))
    (skill_root / SIDECAR_MANIFEST).write_bytes(encoded.encode("utf-8"))
    # These are deployed Skill instructions, read by a different sandbox user.
    # Keep experiment umasks from making them owner-only, without group/world writes.
    for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST):
        (skill_root / name).chmod(0o644)
    return {"sidecar_paths": [SIDECAR_MARKDOWN, SIDECAR_MANIFEST],
            "mappings": {SIDECAR_MARKDOWN: {"original_sha256": None,
                "rendered_sha256": "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                "spans": spans}}}


def load_sidecars(skill_root: Path) -> dict | None:
    """Load a verified sidecar pair, or None if neither reserved file exists."""
    paths = [skill_root / name for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)]
    if not any(path.exists() or path.is_symlink() for path in paths):
        return None
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise ValueError("Sidecar Markdown and manifest must be paired regular files")
    try:
        payload = json.loads(paths[1].read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid sidecar manifest JSON") from error
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "presentation_mode", "entries"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["presentation_mode"] != PRESENTATION_SIDECAR):
        raise ValueError("Unsupported sidecar manifest schema or presentation mode")
    _validate_entries(skill_root, payload["entries"])
    markdown, _ = _markdown(payload["entries"])
    if paths[0].read_bytes() != markdown.encode("utf-8"):
        raise ValueError("Sidecar Markdown differs from its declared interpretation patches")
    return payload
