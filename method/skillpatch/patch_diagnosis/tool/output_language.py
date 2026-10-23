"""Guard newly authored output without translating or changing source evidence.

Prompts require English prose. This deterministic check rejects Han characters;
it does not attempt to identify every possible non-English language. Call it only
when accepting fresh model output, never when reopening historical archives.
"""

import re


_HAN = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff\U00030000-\U000323af]")
_LEGACY_DESCRIPTION_FIELDS = ("applicability_reason", "observed_behavior",
                              "expected_behavior", "feedback_assessment")


def validate_output_language(payload: dict, kind: str) -> None:
    """Reject Chinese in authored fields; exact quotes and bound metadata are exempt.

    Schema validators remain responsible for shape and field types. Error messages
    identify the field without copying rejected prose into generated artifacts.
    """
    def check(value, path):
        if isinstance(value, str) and _HAN.search(value):
            raise ValueError(f"{path}: write model-authored text in English; "
                             "Chinese characters are not permitted in this field. "
                             "Preserve source text only in exact evidence quote fields.")
        if isinstance(value, list):
            for index, item in enumerate(value):
                check(item, f"{path}[{index}]")

    def fields(value, names, prefix=""):
        if isinstance(value, dict):
            for name in names:
                check(value.get(name), prefix + name)

    if not isinstance(payload, dict):
        return
    if kind == "discovery":
        fields(payload, ("summary", "coverage_notes"))
        for index, conflict in enumerate(payload.get("conflicts", []) or []):
            fields(conflict, ("conflict_id", "diagnosis", "uncertainty", *_LEGACY_DESCRIPTION_FIELDS),
                   f"conflicts[{index}].")
        for index, finding in enumerate(payload.get("other_findings", []) or []):
            fields(finding, ("description",), f"other_findings[{index}].")
    elif kind in {"generation", "consolidation"}:
        fields(payload, ("reason", "when_to_use", "how_to_interpret"))
    elif kind == "evaluation":
        fields(payload, ("explanation",))
        for index, finding in enumerate(payload.get("reverse_conflicts", []) or []):
            fields(finding, ("explanation",), f"reverse_conflicts[{index}].")
    else:
        raise ValueError("Unknown model output language contract")
