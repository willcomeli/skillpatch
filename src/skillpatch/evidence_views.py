"""Bounded model views of a complete, archived diagnosis evidence collection.

Original text is retained separately. Only terminal escape sequences are removed
from trajectory/feedback/inspection text; citations still match the resulting
evidence exactly. Large inputs expose an index and paged read/search tools.
"""

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

from skillpatch.context import refresh_input_digest, render_evidence
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.storage import jsonable, write_json


_TERMINAL_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])"
)
_VIEW_LIMITATION = (
    "Evidence text views remove terminal escape sequences only; original text and "
    "removal offsets are archived. Indexed or paged evidence is available through "
    "read_evidence, search_evidence and read_evidence_index. An index is not a full "
    "reading of its contents; report unresolved coverage gaps."
)


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_terminal_text(text: str) -> str:
    return _TERMINAL_ESCAPE.sub("", text)


def normalize_evidence_item(item: EvidenceItem) -> EvidenceItem:
    """Derive an exact text view and its provenance from the original evidence."""
    if item.kind not in {"trajectory", "feedback", "artifact"}:
        return item
    removed = [[match.start(), match.end()] for match in _TERMINAL_ESCAPE.finditer(item.text)]
    if not removed:
        return item
    text = normalize_terminal_text(item.text)
    record = {"evidence_id": item.evidence_id, "scheme": "terminal-escapes-v1",
              "raw_text_sha256": _hash(item.text), "view_text_sha256": _hash(text),
              "removed_raw_ranges": removed}
    return replace(item, text=text, observation={**item.observation, "text_view": record})


class EvidenceViews:
    """Read-only access to collected evidence, independent of benchmark/harness.

    File/environment tools remain with the backend inspector. These three tools
    read only the evidence already collected for this diagnosis, using its IDs.
    """

    names = {"read_evidence", "search_evidence", "read_evidence_index"}

    def __init__(self, context: DiscoveryInput, output: Path):
        self.context, self.output = context, output
        self.raw = list(context.evidence)
        self.normalization: list[dict] = []
        self.presented: dict[str, list[tuple[int, int]]] = {}
        self.pending: dict[str, list[tuple[int, int]]] = {}
        self.mode = "full"
        context.evidence = [self._normalize(item) for item in context.evidence]
        context.limitations.append(_VIEW_LIMITATION)
        refresh_input_digest(context)
        self.persist()

    def _normalize(self, item: EvidenceItem) -> EvidenceItem:
        normalized = normalize_evidence_item(item)
        if normalized is not item:
            self.normalization.append(normalized.observation["text_view"])
        return normalized

    def add(self, items: list[EvidenceItem]) -> list[EvidenceItem]:
        self.raw.extend(items)
        normalized = [self._normalize(item) for item in items]
        self.context.evidence.extend(normalized)
        refresh_input_digest(self.context)
        self.persist()
        return normalized

    def persist(self) -> None:
        write_json(self.output / "raw_evidence.json", self.raw)
        write_json(self.output / "normalization.json", {
            "scope": "terminal_escape_sequences_only", "entries": self.normalization,
            "raw_archive": "raw_evidence.json",
        })
        write_json(self.output / "evidence_access.json", {
            "mode": self.mode, "presented_ranges": self.presented,
            "queued_ranges": self.pending,
            "fully_presented_trajectory_items": sum(self.fully_presented(item)
                for item in self.context.evidence if item.kind == "trajectory"),
            "trajectory_items": sum(item.kind == "trajectory" for item in self.context.evidence),
        })

    def _mark(self, item: EvidenceItem, start: int, end: int) -> None:
        self._record_range(self.pending, item.evidence_id, start, end)

    @staticmethod
    def _record_range(target: dict, identifier: str, start: int, end: int) -> None:
        ranges = sorted([*target.get(identifier, []), (start, end)])
        merged: list[tuple[int, int]] = []
        for a, b in ranges:
            if merged and a <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
            else:
                merged.append((a, b))
        target[identifier] = merged

    def commit_visible(self) -> None:
        """Commit only excerpts included in a model request, not queued tool output."""
        for identifier, ranges in self.pending.items():
            for start, end in ranges:
                self._record_range(self.presented, identifier, start, end)
        self.pending.clear()
        self.persist()

    def discard_pending(self) -> None:
        self.pending.clear()

    def fully_presented(self, item: EvidenceItem) -> bool:
        return (not item.text or self.presented.get(item.evidence_id) == [(0, len(item.text))])

    def observe(self, items: list[EvidenceItem]) -> None:
        for item in items:
            self._mark(item, 0, len(item.text))
        self.persist()

    @staticmethod
    def _labels(item: EvidenceItem) -> dict:
        labels: dict[str, Any] = {"evidence_id": item.evidence_id, "kind": item.kind,
                                  "text_chars": len(item.text)}
        for field in ("skill_id", "relative_path", "version", "availability", "source_role"):
            value = getattr(item, field)
            if value is not None:
                labels[field] = value
        return labels

    def _excerpt(self, item: EvidenceItem, start: int, end: int) -> dict:
        self._mark(item, start, end)
        return {**self._labels(item), "start_char": start, "end_char": end,
                "has_more": end < len(item.text), "text": item.text[start:end]}

    def render(self, budget: int) -> str:
        """Provide complete evidence when it fits; otherwise an explicit paged view."""
        if budget < 1500:
            raise ValueError("Diagnosis input budget is too small for an evidence index")
        full = render_evidence(self.context)
        if len(full) <= budget:
            for item in self.context.evidence:
                self._mark(item, 0, len(item.text))
            self.persist()
            return full
        self.mode = "indexed"
        header = render_evidence(replace(self.context, evidence=[]))
        note = (
            "\nPAGED EVIDENCE VIEW\nFull evidence text is archived, not discarded. "
            "The following excerpts are partial where has_more=true. Use read_evidence "
            "with start_char to continue, search_evidence to locate passages, and "
            "read_evidence_index to list further IDs. All quoted text must come from "
            "a displayed excerpt. Inspect material trajectory events before concluding.\n"
        )
        text = header + note
        if len(text) + 500 > budget:
            raise ValueError("Diagnosis metadata exceeds its bounded context budget")
        index_budget = min(25000, (budget - len(text)) // 4)
        entries = []
        used = 0
        for item in self.context.evidence:
            entry = {"evidence_id": item.evidence_id, "kind": item.kind,
                     "text_chars": len(item.text)}
            if item.skill_id is not None:
                entry["skill_id"] = item.skill_id
                entry["relative_path"] = item.relative_path
            line = json.dumps(entry, ensure_ascii=False)
            if used + len(line) + 1 > index_budget:
                break
            entries.append(line)
            used += len(line) + 1
        text += f"INDEX: {len(entries)} of {len(self.context.evidence)} items\n" + "\n".join(entries) + "\n"
        # Task, Skill and feedback excerpts get priority. Entire trajectory events
        # remain addressable instead of letting a huge terminal dump fill context.
        ordered = sorted(enumerate(self.context.evidence), key=lambda pair: (
            {"task": 0, "skill": 1, "feedback": 2, "artifact": 3, "trajectory": 4}.get(pair[1].kind, 5), pair[0]))
        for _, item in ordered:
            remaining = budget - len(text)
            label_size = len(json.dumps(self._labels(item), ensure_ascii=False)) + 250
            if remaining < label_size + 100:
                continue
            size = min(len(item.text), 20000 if item.kind != "trajectory" else 180,
                       (remaining - label_size) // 6)
            # JSON escaping is bounded by six characters per code point.
            if size <= 0:
                continue
            excerpt = self._excerpt(item, 0, size)
            text += "\nEVIDENCE EXCERPT\n" + json.dumps(excerpt, ensure_ascii=False) + "\n"
        if len(text) > budget:
            raise ValueError("Evidence projection exceeded its bounded context budget")
        self.persist()
        return text

    def definitions(self) -> list[dict]:
        specs = [
            ("read_evidence", "Read a page of archived evidence text by its existing Q/S/T/F/A ID. "
             "Use start_char to continue; quote the decoded text exactly.", {
                "evidence_id": {"type": "string"}, "start_char": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 20000}}, ["evidence_id"]),
            ("search_evidence", "Search archived evidence for a literal phrase (case insensitive). "
             "Results are exact source excerpts; a search does not establish complete coverage.", {
                "query": {"type": "string"}, "evidence_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["task", "skill", "trajectory", "feedback", "artifact"]},
                "max_matches": {"type": "integer", "minimum": 1, "maximum": 20},
                "context_chars": {"type": "integer", "minimum": 0, "maximum": 1000}}, ["query"]),
            ("read_evidence_index", "List archived evidence IDs, kinds and text lengths; "
             "index entries do not expose their full text.", {
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
        ]
        return [{"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required,
                           "additionalProperties": False}}} for name, description, properties, required in specs]

    @staticmethod
    def _integer(arguments: dict, key: str, default: int, minimum: int, maximum: int) -> int:
        value = arguments.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} is outside the permitted integer range")
        return value

    def call(self, name: str, arguments: dict) -> dict:
        definitions = {item["function"]["name"]: item["function"] for item in self.definitions()}
        spec = definitions.get(name)
        if spec is None or not isinstance(arguments, dict):
            raise ValueError("Unknown archived evidence tool")
        parameters = spec["parameters"]
        if set(arguments) - parameters["properties"].keys() or not set(parameters["required"]) <= arguments.keys():
            raise ValueError("Missing or unknown archived evidence tool arguments")
        evidence = {item.evidence_id: item for item in self.context.evidence}
        identifier = arguments.get("evidence_id")
        if identifier is not None and (not isinstance(identifier, str) or identifier not in evidence):
            raise ValueError("evidence_id must name an item from this diagnosis")
        if name == "read_evidence":
            item = evidence[identifier]
            start = self._integer(arguments, "start_char", 0, 0, len(item.text))
            size = self._integer(arguments, "max_chars", 12000, 1, 20000)
            data = self._excerpt(item, start, min(start + size, len(item.text)))
        elif name == "read_evidence_index":
            offset = self._integer(arguments, "offset", 0, 0, len(evidence))
            limit = self._integer(arguments, "limit", 50, 1, 100)
            data = {"items": [self._labels(item) for item in self.context.evidence[offset:offset + limit]],
                    "next_offset": offset + limit if offset + limit < len(evidence) else None}
        else:
            query = arguments["query"]
            if not isinstance(query, str) or not query.strip() or len(query) > 1000:
                raise ValueError("query must be a nonempty literal string of at most 1000 characters")
            kind = arguments.get("kind")
            if kind is not None and kind not in parameters["properties"]["kind"]["enum"]:
                raise ValueError("Invalid evidence kind")
            limit = self._integer(arguments, "max_matches", 5, 1, 20)
            radius = self._integer(arguments, "context_chars", 300, 0, 1000)
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            matches = []
            more = False
            items = [evidence[identifier]] if identifier is not None else self.context.evidence
            for item in items:
                if kind is not None and item.kind != kind:
                    continue
                for match in pattern.finditer(item.text):
                    if len(matches) == limit:
                        more = True
                        break
                    matches.append(self._excerpt(item, max(0, match.start() - radius),
                                                 min(len(item.text), match.end() + radius)))
                if more:
                    break
            data = {"matches": matches, "more_matches": more,
                    "coverage": "matched excerpts only; not an exhaustive reading"}
        self.persist()
        return {"text": json.dumps(data, ensure_ascii=False), "evidence": [], "images": [],
                "source": "archived_evidence", "truncated": False}

    def validate_visibility(self, payload: dict) -> None:
        """Citations must refer to text actually presented in this model session."""
        evidence = {item.evidence_id: item for item in self.context.evidence}
        references = []
        for conflict in payload["conflicts"]:
            target = conflict["target"]
            references.append({"evidence_id": target["skill_ref"], "quote": target["quote"]})
            for field in ("decision_evidence", "behavior_evidence", "feedback_evidence"):
                references.extend(conflict[field])
        for finding in payload["other_findings"]:
            references.extend(finding["evidence"])
        for ref in references:
            item = evidence[ref["evidence_id"]]
            if not any(ref["quote"] in item.text[start:end]
                       for start, end in self.presented.get(item.evidence_id, [])):
                raise ValueError("Citation refers to undisplayed evidence text; read that passage before quoting it")
        if payload["status"] == "no_supported_conflict" and any(
                item.kind == "trajectory" and not self.fully_presented(item) for item in self.context.evidence):
            raise ValueError("Trajectory text coverage is incomplete; use insufficient_evidence or inspect remaining pages")
