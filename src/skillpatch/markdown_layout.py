"""Shared Markdown fence boundaries for supplement placement and target binding.

All offsets refer to Unicode characters in the unchanged original document.
Fences may be indented under an instruction; their contents are never parsed as
headings, numbered steps or tables.
"""

import re

from skillpatch.skill_invocation import document_body


def document_lines(text: str) -> list[dict]:
    body_start = len(text) - len(document_body(text))
    lines = []
    offset = 0
    fence = None
    fence_start = None
    fence_ends = {}
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})([^\r\n]*)[\r\n]*$", line) if offset >= body_start else None
        in_fence = fence is not None
        owner = fence_start
        if fence is None and marker:
            # Backticks in the info string are not a valid opening fence.
            if marker.group(1)[0] != "`" or "`" not in marker.group(2):
                fence = marker.group(1)
                fence_start = owner = offset
                in_fence = True
        elif fence is not None and marker:
            mark = marker.group(1)
            if mark[0] == fence[0] and len(mark) >= len(fence) and not marker.group(2).strip():
                fence_ends[fence_start] = offset + len(line)
                fence = fence_start = None
        lines.append({"start": offset, "end": offset + len(line), "text": line,
                      "fenced": in_fence, "fence_start": owner})
        offset += len(line)
    if fence is not None:
        raise ValueError("Cannot place supplements in a document with an unterminated fence")
    for line in lines:
        if line["fenced"]:
            line["fence_end"] = fence_ends[line["fence_start"]]
    return lines


def substep_insertion_offset(text: str, quote_start: int, quote_end: int, *, lines=None) -> int:
    """Insert before the quoted line or its one enclosing, complete code block.

    The quote stays bound to its original content. A quote spanning prose and code,
    or more than one code block, is ambiguous and is not relocated.
    """
    body_start = len(text) - len(document_body(text))
    if quote_start < body_start:
        raise ValueError("Substep target cannot be in frontmatter")
    if not 0 <= quote_start < quote_end <= len(text):
        raise ValueError("Invalid target quote range")
    lines = document_lines(text) if lines is None else lines
    touched = [line for line in lines if line["start"] < quote_end and line["end"] > quote_start]
    if any(line["fenced"] for line in touched):
        owners = {line["fence_start"] for line in touched}
        if len(owners) != 1 or None in owners:
            raise ValueError("Substep quote crosses a code fence boundary")
        return touched[0]["fence_start"]
    if any(line["text"].lstrip().startswith("|") for line in touched):
        raise ValueError("Substep quote cannot be inside a Markdown table")
    return touched[0]["start"]
