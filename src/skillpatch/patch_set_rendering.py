"""Render explicitly separated supplements while retaining original provenance."""

import copy
import difflib
import json
import re
import shutil
from pathlib import Path

from skillpatch.patch_targets import _relative_path, bind_target
from skillpatch.patch_sidecars import (PRESENTATION_INLINE, PRESENTATION_SIDECAR,
                                      SIDECAR_MANIFEST, SIDECAR_MARKDOWN, write_sidecars)
from skillpatch.patches import tree_hashes
from skillpatch.skill_invocation import document_body
from skillpatch.storage import hash_manifest, sha256_file


def _block(entry: dict, newline: str) -> str:
    # Keep this byte-compatible with Stage2's render_patch for a single entry.
    text = (f"\n<!-- skillpatch:{entry['entry_id']} -->\n"
            f"### When to use\n\n{entry['when_to_use'].strip()}\n\n"
            f"### How to interpret\n\n{entry['how_to_interpret'].strip()}\n"
            f"<!-- /skillpatch:{entry['entry_id']} -->\n\n")
    return text.replace("\r\n", "\n").replace("\n", newline)


def _selection_edit(text: str, entries: list[dict]) -> dict:
    header = text[:len(text) - len(document_body(text))]
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
    for entry in entries:
        existing += (" When to use: " + " ".join(entry["when_to_use"].split())
                     + " How to interpret: " + " ".join(entry["how_to_interpret"].split()))
    return {"start": match.start(), "end": match.end(),
            "text": "description: " + json.dumps(existing, ensure_ascii=False),
            "origin": "selection_metadata", "entry_ids": [item["entry_id"] for item in entries]}


def _render_file(text: str, edits: list[dict]) -> tuple[str, list[dict]]:
    pieces = []
    spans = []
    original_offset = 0
    rendered_offset = 0
    # Sorting is stable: multiple entries at the same original line preserve
    # the versioned set's explicit order. Never sort by incidental patch IDs.
    for edit in sorted(edits, key=lambda item: item["start"]):
        if edit["start"] < original_offset:
            raise ValueError("Rendering edits overlap")
        if original_offset < edit["start"]:
            chunk = text[original_offset:edit["start"]]
            pieces.append(chunk)
            spans.append({"origin": "original", "original_start": original_offset,
                          "original_end": edit["start"], "rendered_start": rendered_offset,
                          "rendered_end": rendered_offset + len(chunk)})
            rendered_offset += len(chunk)
        pieces.append(edit["text"])
        spans.append({"origin": edit["origin"], "entry_ids": edit["entry_ids"],
                      "original_start": edit["start"], "original_end": edit["end"],
                      "rendered_start": rendered_offset,
                      "rendered_end": rendered_offset + len(edit["text"])})
        rendered_offset += len(edit["text"])
        original_offset = edit["end"]
    if original_offset < len(text):
        chunk = text[original_offset:]
        pieces.append(chunk)
        spans.append({"origin": "original", "original_start": original_offset,
                      "original_end": len(text), "rendered_start": rendered_offset,
                      "rendered_end": rendered_offset + len(chunk)})
    return "".join(pieces), spans


def render_patch_set(original_dir: Path, entries: list[dict], output: Path, *,
                     presentation_mode: str = PRESENTATION_SIDECAR) -> dict:
    """Render separate supplement files by default, retaining every original byte."""
    if presentation_mode == PRESENTATION_INLINE:
        return _render_inline_patch_set(original_dir, entries, output)
    if presentation_mode != PRESENTATION_SIDECAR:
        raise ValueError("Unsupported patch-set presentation mode")
    before = tree_hashes(original_dir)
    if "SKILL.md" not in before:
        raise ValueError("Original Skill directory must contain SKILL.md")
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(original_dir.resolve()):
        raise ValueError("Rendered Skill must be a new directory outside the original")
    if not isinstance(entries, list):
        raise ValueError("Patch set entries must be an ordered list")
    for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST):
        if (original_dir / name).exists() or (original_dir / name).is_symlink():
            raise ValueError("Original Skill already contains a reserved sidecar path")
    deployed = []
    verified = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Patch entry must be an object")
        identifier = entry.get("entry_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", identifier):
            raise ValueError("Invalid patch entry ID")
        if identifier in seen:
            raise ValueError("Duplicate patch entry ID")
        seen.add(identifier)
        for field in ("when_to_use", "how_to_interpret"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 16000:
                raise ValueError("Both patch sections must be bounded nonempty text")
            if "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value or "\x00" in value:
                raise ValueError("Patch text contains a reserved marker or null byte")
        target = entry.get("target")
        if not isinstance(target, dict):
            raise ValueError("Patch entry requires an original target binding")
        try:
            expected = bind_target(original_dir, target["skill_key"], target["relative_path"],
                                   target["scope"], target["source_quote"])
        except KeyError as error:
            raise ValueError("Incomplete original target binding") from error
        if target != expected:
            raise ValueError("Original target binding has changed or was tampered with")
        source = original_dir / target["relative_path"]
        if "<!-- skillpatch:" in source.read_text(encoding="utf-8"):
            raise ValueError("Original Skill already contains rendered patches")
        deployed.append({"entry_id": identifier,
                         "target": {"relative_path": target["relative_path"], "scope": target["scope"],
                                    "quote": target["source_quote"].replace("\r\n", "\n"),
                                    "version": sha256_file(source)},
                         "when_to_use": entry["when_to_use"], "how_to_interpret": entry["how_to_interpret"]})
        verified.append(copy.deepcopy(entry))
    mappings = {}
    for relative_path in before:
        if Path(relative_path).suffix.lower() == ".md":
            length = len((original_dir / relative_path).read_bytes().decode("utf-8"))
            mappings[relative_path] = {"original_sha256": before[relative_path],
                "rendered_sha256": before[relative_path],
                "spans": [{"origin": "original", "original_start": 0, "original_end": length,
                           "rendered_start": 0, "rendered_end": length}]}
    if tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed while preparing rendering")
    shutil.copytree(original_dir, output, symlinks=True)
    if tree_hashes(output) != before or tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed while copying")
    sidecars = write_sidecars(output, deployed) if deployed else {"sidecar_paths": [], "mappings": {}}
    mappings.update(sidecars["mappings"])
    after = tree_hashes(output)
    if (any(after.get(name) != digest for name, digest in before.items())
            or set(after) - set(before) != set(sidecars["sidecar_paths"])
            or tree_hashes(original_dir) != before):
        raise ValueError("Sidecar renderer changed an original Skill resource")
    diff = "".join("".join(difflib.unified_diff([], (output / name).read_text().splitlines(True),
                                                fromfile="/dev/null", tofile="rendered/" + name))
                   for name in sidecars["sidecar_paths"])
    return {"schema_version": 2, "presentation_mode": PRESENTATION_SIDECAR,
            "base_digest": hash_manifest(before), "base_hashes": before,
            "digest": hash_manifest(after), "hashes": after, "entries": verified,
            "mappings": mappings, "sidecar_paths": sidecars["sidecar_paths"],
            "selection_description_updated": False, "limitations": [], "diff": diff}


def _render_inline_patch_set(original_dir: Path, entries: list[dict], output: Path) -> dict:
    """Produce a new independent copy and character-level presentation mappings.

    An entry has entry_id, target (a bind_target result), when_to_use and
    how_to_interpret. Entries retain their list order at equal insertion offsets.
    Original files are never edited. Whole-Skill selection metadata follows the
    existing Stage2 presentation exactly, with the original span recorded.
    """
    before = tree_hashes(original_dir)
    if "SKILL.md" not in before:
        raise ValueError("Original Skill directory must contain SKILL.md")
    if output.exists() or output.is_symlink() or output.resolve().is_relative_to(original_dir.resolve()):
        raise ValueError("Rendered Skill must be a new directory outside the original")
    if not isinstance(entries, list):
        raise ValueError("Patch set entries must be an ordered list")
    verified = []
    seen = set()
    edits = {}
    texts = {}
    whole = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Patch entry must be an object")
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", entry_id):
            raise ValueError("Invalid patch entry ID")
        if entry_id in seen:
            raise ValueError("Duplicate patch entry ID")
        seen.add(entry_id)
        for key in ("when_to_use", "how_to_interpret"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 16000:
                raise ValueError("Both patch sections must be bounded nonempty text")
            if "<!-- skillpatch:" in value or "<!-- /skillpatch:" in value or "\x00" in value:
                raise ValueError("Patch text contains a reserved marker or null byte")
        target = entry.get("target")
        if not isinstance(target, dict):
            raise ValueError("Patch entry requires an original target binding")
        try:
            expected = bind_target(original_dir, target["skill_key"], target["relative_path"],
                                   target["scope"], target["source_quote"], presentation_mode=PRESENTATION_INLINE)
        except KeyError as error:
            raise ValueError("Incomplete original target binding") from error
        if any(target.get(key) != value for key, value in expected.items()):
            raise ValueError("Original target binding has changed or was tampered with")
        relative_path = target["relative_path"]
        if relative_path not in texts:
            texts[relative_path] = (original_dir / relative_path).read_bytes().decode("utf-8")
            if "<!-- skillpatch:" in texts[relative_path]:
                raise ValueError("Original Skill already contains rendered patches")
        newline = "\r\n" if "\r\n" in texts[relative_path] else "\n"
        offset = target["insertion_offset"]
        edits.setdefault(relative_path, []).append({"start": offset, "end": offset,
                                                   "text": _block(entry, newline),
                                                   "origin": "patch", "entry_ids": [entry_id]})
        if target["scope"] == "whole_skill":
            whole.append(entry)
        verified.append(copy.deepcopy(entry))
    if whole:
        edits.setdefault("SKILL.md", []).append(_selection_edit(texts["SKILL.md"], whole))
    rendered_files = {}
    mappings = {}
    diffs = []
    # Include untouched Markdown files so future references can also be traced
    # through a deployed version to their fixed original resource.
    for relative_path in before:
        if Path(relative_path).suffix.lower() != ".md":
            continue
        text = texts.get(relative_path)
        if text is None:
            text = (original_dir / relative_path).read_bytes().decode("utf-8")
        rendered, spans = _render_file(text, edits.get(relative_path, []))
        mappings[relative_path] = {"original_sha256": before[relative_path], "spans": spans}
        if relative_path in edits:
            rendered_files[relative_path] = rendered.encode("utf-8")
            diffs.append("".join(difflib.unified_diff(text.splitlines(True), rendered.splitlines(True),
                                                     fromfile="original/" + relative_path,
                                                     tofile="rendered/" + relative_path)))
    if tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed while preparing rendering")
    shutil.copytree(original_dir, output, symlinks=True)
    if tree_hashes(output) != before or tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed while copying")
    for relative_path, raw in rendered_files.items():
        (output / relative_path).write_bytes(raw)
    after = tree_hashes(output)
    if set(after) != set(before) or any(after[name] != digest for name, digest in before.items()
                                       if name not in rendered_files):
        raise ValueError("Renderer changed an unrelated original resource")
    if tree_hashes(original_dir) != before:
        raise ValueError("Original Skill changed during rendering")
    for relative_path, mapping in mappings.items():
        mapping["rendered_sha256"] = after[relative_path]
    return {"schema_version": 1, "presentation_mode": PRESENTATION_INLINE,
            "base_digest": hash_manifest(before), "base_hashes": before,
            "digest": hash_manifest(after), "hashes": after, "entries": verified,
            "mappings": mappings, "selection_description_updated": bool(whole),
            "limitations": (["Substep supplements are body instructions; no selection-level effect is claimed."]
                            if any(entry["target"]["scope"] == "substep" for entry in entries) else []),
            "diff": "".join(diffs)}


def map_rendered_quote(rendered_dir: Path, rendering: dict, relative_path: str, quote: str) -> dict:
    """Trace a unique deployed quote to original text or one added supplement.

    Quotes spanning provenance boundaries, or composed selection metadata for
    multiple supplements, are left unresolved. Callers must load this mapping
    from a verified stored rendering, not accept model-supplied mapping records.
    """
    _relative_path(relative_path)
    hashes = tree_hashes(rendered_dir)
    if hashes != rendering.get("hashes") or hash_manifest(hashes) != rendering.get("digest"):
        raise ValueError("Deployed rendering differs from its provenance record")
    mapping = rendering.get("mappings", {}).get(relative_path)
    if not isinstance(mapping, dict) or mapping.get("rendered_sha256") != hashes.get(relative_path):
        raise ValueError("No verified Markdown provenance for this target")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("Target quote must be nonempty")
    text = (rendered_dir / relative_path).read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    quote = quote.replace("\r\n", "\n").replace("\n", newline)
    if text.count(quote) != 1:
        raise ValueError("Deployed quote is absent or ambiguous")
    start = text.index(quote)
    end = start + len(quote)
    matching = [span for span in mapping["spans"]
                if span["rendered_start"] <= start and end <= span["rendered_end"]]
    if len(matching) != 1:
        raise ValueError("Deployed quote crosses provenance boundaries")
    span = matching[0]
    result = {"origin": span["origin"], "relative_path": relative_path,
              "rendered_start": start, "rendered_end": end}
    if span["origin"] == "original":
        original_start = span["original_start"] + start - span["rendered_start"]
        return {**result, "original_start": original_start,
                "original_end": original_start + len(quote), "original_quote": quote}
    if len(span.get("entry_ids", [])) != 1:
        raise ValueError("Quote in composed selection metadata cannot identify one patch")
    identifier = span["entry_ids"][0]
    entries = [entry for entry in rendering["entries"] if entry["entry_id"] == identifier]
    if len(entries) != 1:
        raise ValueError("Provenance entry is missing or ambiguous")
    return {**result, "entry_id": identifier, "target": copy.deepcopy(entries[0]["target"])}
