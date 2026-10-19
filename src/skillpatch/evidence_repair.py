"""Read-only, bounded context for repairing exact evidence citations.

This module neither validates a report nor changes evidence-access accounting.
Only text already presented to the model is repeated. Unseen pages remain behind
the normal evidence tools and their existing inspection budgets.
"""

import json
import re
from collections.abc import Mapping

from skillpatch.evidence_views import EvidenceViews


_GUIDANCE = (
    "Repair the cited reference, then reassess the judgment. Collection and evidence_id "
    "jointly identify an item: the same ID in source, baseline and candidate names different "
    "runs. For selector-based evaluation reports, submit collection, evidence_id and "
    "optional start_char/end_char (zero-based, end exclusive); the program extracts text. "
    "For legacy quote-based reports only, copy decoded evidence text exactly, including line "
    "prefixes such as +1| and +2|. Excerpts below repeat previously displayed text only. "
    "Partial excerpts do not establish full coverage. Use the suggested evidence tool "
    "within the remaining inspection budget to inspect passages needed to resolve "
    "the validation error. An exact citation "
    "does not establish that its content supports the judgment; inspect counterevidence "
    "and revise or qualify the judgment when necessary."
)
_COLLECTION_FIELD = {
    "source_evidence": "source", "baseline_evidence": "baseline",
    "candidate_evidence": "candidate", "reverse_conflicts": "candidate",
}


def _references(payload: object, default: str | None) -> list[dict]:
    """Recover model-supplied references without assuming a valid report schema."""
    references = []
    pending = [(payload, "report", default, 0)]
    visited = 0
    while pending and visited < 4000 and len(references) < 100:
        value, path, collection, depth = pending.pop()
        visited += 1
        if depth > 20:
            continue
        if isinstance(value, dict):
            if isinstance(value.get("collection"), str):
                collection = value["collection"]
            identifier = value.get("evidence_id", value.get("skill_ref"))
            if isinstance(identifier, str):
                references.append({"collection": collection, "evidence_id": identifier,
                                   "quote": value.get("quote", "") if isinstance(value.get("quote", ""), str) else "", "path": path})
            for key, child in reversed(list(value.items())):
                if isinstance(key, str):
                    pending.append((child, f"{path}.{key}",
                                    _COLLECTION_FIELD.get(key, collection), depth + 1))
        elif isinstance(value, list):
            for index in range(len(value) - 1, -1, -1):
                pending.append((value[index], f"{path}[{index}]", collection, depth + 1))
    return references


def _spans(view: EvidenceViews, identifier: str, size: int) -> list[tuple[int, int]]:
    return [(max(0, start), min(size, end))
            for start, end in view.presented.get(identifier, [])
            if start < end and end > 0 and start < size]


def _first_gap(spans: list[tuple[int, int]], size: int) -> int | None:
    offset = 0
    for start, end in sorted(spans):
        if start > offset:
            return offset
        offset = max(offset, end)
    return offset if offset < size else None


def _window(text: str, spans: list[tuple[int, int]], quote: str, limit: int):
    """Locate literal anchors for display only; never accept a modified quotation."""
    if not spans:
        return None
    selected = spans[0]
    anchor = selected[0]
    needles = [quote] if quote else []
    # A line from a copied code fragment can locate the original tool line even
    # when its +N| prefix was dropped. This does not relax substring validation.
    needles.extend(line.strip()[:200] for line in quote.splitlines()[:12]
                   if len(line.strip()) >= 5)
    located = False
    for needle in needles:
        if not needle:
            continue
        for start, end in spans:
            found = text.find(needle, start, end)
            if found >= 0:
                selected, anchor, located = (start, end), found, True
                break
        if located:
            break
    left, right = selected
    start = max(left, anchor - min(160, limit // 4))
    # Keep complete line starts when they fit without extending outside the
    # previously shown range. Very long lines remain explicitly partial.
    line_start = text.rfind("\n", left, anchor) + 1
    if start <= line_start <= anchor:
        start = max(left, line_start)
    end = min(right, start + limit)
    return {"start_char": start, "end_char": end, "text_chars": len(text),
            "text": text[start:end], "partial": start != 0 or end != len(text),
            "previously_displayed": True}


def _select(error: str, payload: object, views: Mapping[str, EvidenceViews]) -> list[dict]:
    default = next(iter(views)) if len(views) == 1 else None
    references = _references(payload, default)
    prefix = re.match(r"^(source|baseline|candidate)(?:[.\[][\w.\[\]0-9]*)?\s*:", error)
    collection = prefix.group(1) if prefix else None
    path = re.match(r"(report(?:\.[\w]+|\[\d+\])+):", error)
    if path:
        location = path.group(1)
        references = [ref for ref in references if location == ref["path"]
                      or location.startswith(ref["path"] + ".")]
    if collection:
        references = [ref for ref in references if ref["collection"] == collection]
    selected_views = {name: view for name, view in views.items()
                      if collection is None or name == collection}
    known = {name: {item.evidence_id: item for item in view.context.evidence}
             for name, view in selected_views.items()}
    ids = set()
    for items in known.values():
        for identifier in items:
            if re.search(r"(?<![\w])" + re.escape(identifier) + r"(?![\w])", error):
                ids.add(identifier)
    if ids:
        references = [ref for ref in references if ref["evidence_id"] in ids]
    coverage = any(word in error.lower() for word in ("undisplayed items", "unread:", "coverage", "read the complete"))
    selected = []
    for ref in references:
        name = ref["collection"]
        if name is None:
            # An unqualified ID from a multi-run report is insufficient to choose
            # a run. Never silently use whichever archive happens to be first.
            continue
        if name not in views:
            continue
        item = next((item for item in views[name].context.evidence
                     if item.evidence_id == ref["evidence_id"]), None)
        spans = _spans(views[name], ref["evidence_id"], len(item.text)) if item else []
        exact_visible = item is not None and any(ref["quote"] in item.text[a:b] for a, b in spans)
        if path or ids or not exact_visible:
            selected.append(ref)
    # Induction/consolidation outputs contain no citations. The validator can
    # nevertheless identify unread archived items by collection and ID.
    if ids:
        for identifier in sorted(ids, key=lambda value: error.find(value)):
            owners = [name for name, items in known.items() if identifier in items]
            if collection is None and len(owners) != 1:
                continue
            for name in owners:
                if not any(ref["collection"] == name and ref["evidence_id"] == identifier for ref in selected):
                    selected.append({"collection": name, "evidence_id": identifier,
                                     "quote": "", "path": None})
    if not selected and coverage:
        skill_only = "original skill" in error.lower() or "target context" in error.lower()
        for name, view in selected_views.items():
            for item in view.context.evidence:
                if item.kind == ("skill" if skill_only else "trajectory") and not view.fully_presented(item):
                    selected.append({"collection": name, "evidence_id": item.evidence_id,
                                     "quote": "", "path": None})
    result, seen = [], set()
    for ref in selected:
        key = (ref["collection"], ref["evidence_id"], ref["quote"])
        if key not in seen:
            result.append(ref)
            seen.add(key)
    return result


def build_evidence_repair_context(error: str, payload: object,
                                  views: Mapping[str, EvidenceViews], *,
                                  max_chars: int = 6000, max_items: int = 3,
                                  collection_argument: bool = True) -> str:
    """Return a bounded English JSON appendix without reading new evidence.

    The caller owns repair/tool budgets and redaction. ``collection_argument``
    is false for the single-collection Stage 1 read_evidence tool. Returning an
    empty string is valid for unrelated errors or an insufficient context budget.
    Original evidence excerpts and submitted quotes are never translated.
    """
    if type(max_chars) is not int or max_chars < 512 or type(max_items) is not int or max_items < 1:
        return ""
    if not isinstance(error, str) or not views:
        return ""
    if not any(word in error.lower() for word in ("evidence", "citation", "quote", "substring",
                                                  "trajectory", "original skill", "target context")):
        return ""
    references = _select(error[:12000], payload, views)
    if not references:
        return ""
    limit = min(max_items, 8)
    excerpt_limit, quote_limit = 1400, 900

    def render(count: int) -> str:
        records = []
        for ref in references[:count]:
            name, identifier, quote = ref["collection"], ref["evidence_id"], ref["quote"]
            view = views[name]
            item = next((item for item in view.context.evidence if item.evidence_id == identifier), None)
            record = {"collection": name, "evidence_id": identifier, "report_path": ref["path"],
                      "submitted_quote": quote[:quote_limit],
                      "submitted_quote_truncated": len(quote) > quote_limit}
            arguments = {"collection": name} if collection_argument else {}
            if item is None:
                record.update(issue="Unknown evidence ID in the declared collection; inspect its index.",
                              next_tool={"name": "read_evidence_index", "arguments": {**arguments, "limit": 50}})
                records.append(record)
                continue
            spans = _spans(view, identifier, len(item.text))
            excerpt = _window(item.text, spans, quote, excerpt_limit)
            gap = _first_gap(spans, len(item.text))
            record.update(excerpt=excerpt, fully_presented=gap is None)
            if quote and quote not in item.text:
                record["issue"] = "Submitted quote is not an exact substring of this collection's evidence."
                if any(quote in re.sub(r"(?m)^[+-]?\d+\|", "", item.text[a:b])
                       for a, b in spans):
                    record["format_hint"] = (
                        "The submitted quote matches previously displayed text only after removing "
                        "tool-added line prefixes. Those prefixes are part of the archived evidence: "
                        "restore them and retain the original whitespace in the exact quotation. "
                        "This display comparison does not make the submitted citation valid."
                    )
                matches = []
                for other_name, other_view in views.items():
                    if other_name == name:
                        continue
                    for other in other_view.context.evidence:
                        if other.evidence_id == identifier and any(
                                quote in other.text[a:b] for a, b in _spans(other_view, identifier, len(other.text))):
                            matches.append(other_name)
                if matches:
                    record["other_displayed_collections_with_this_quote"] = matches
                    record["run_warning"] = (
                        "The submitted quote appears under the same ID in another displayed run. "
                        "It does not support this declared run; inspect the declared collection."
                    )
            elif quote and not any(quote in item.text[a:b] for a, b in spans):
                record["issue"] = "The quoted passage has not been displayed; read it before citing it."
            elif gap is not None:
                record["issue"] = ("This evidence item has unread text; inspect any missing passage needed "
                                   "to resolve the reported validation error.")
            else:
                record["issue"] = "Recheck this cited passage against the reported validation error."
            start = gap if gap is not None else (excerpt["start_char"] if excerpt else 0)
            record["next_tool"] = {"name": "read_evidence", "arguments": {
                **arguments, "evidence_id": identifier, "start_char": start, "max_chars": 12000}}
            records.append(record)
        return json.dumps({"evidence_repair_context": {"instructions": (_GUIDANCE if max_chars >= 2500 else
            "Check collection and evidence_id. Select source offsets or preserve exact legacy quotes. "
            "Excerpts repeat displayed evidence only; use next_tool for missing text. "
            "Reassess whether the evidence supports the judgment."),
            "references": records, "omitted_references": len(references) - len(records)}}, ensure_ascii=True)

    count = min(limit, len(references))
    while count:
        result = render(count)
        if len(result) <= max_chars:
            return result
        if excerpt_limit > 100 or quote_limit > 100:
            excerpt_limit, quote_limit = max(100, excerpt_limit // 2), max(100, quote_limit // 2)
        else:
            count -= 1
    return ""
