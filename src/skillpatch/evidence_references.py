"""Resolve run-qualified evidence selectors without model-authored quotations."""


def resolve_reference(ref, context, collection, view=None):
    # Archived reports remain readable; new prompts request selectors only.
    if isinstance(ref, dict) and set(ref) == {"evidence_id", "quote"}:
        return ref
    required = {"collection", "evidence_id"}
    if not isinstance(ref, dict) or set(ref) not in (required, required | {"start_char", "end_char"}):
        raise ValueError(f"{collection}: evidence selector requires collection, evidence_id and optionally both start_char/end_char")
    identifier = ref['evidence_id']
    if ref['collection'] != collection:
        raise ValueError(f"{collection}: evidence {identifier} belongs to the wrong declared collection {ref['collection']!r}")
    if not isinstance(identifier, str):
        raise ValueError(f"{collection}: evidence identifier must be a string")
    item = next((item for item in context.evidence if item.evidence_id == identifier), None)
    if item is None:
        raise ValueError(f"{collection}: unknown evidence ID {identifier}")
    start, end = ref.get('start_char', 0), ref.get('end_char', len(item.text))
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(item.text):
        raise ValueError(f"{collection}: evidence {identifier} has {len(item.text)} characters; require 0 <= start_char < end_char <= {len(item.text)} (end exclusive)")
    if view is not None and not any(a <= start and end <= b for a, b in view.presented.get(identifier, [])):
        raise ValueError(f"{collection}: evidence {identifier} range [{start}, {end}) is undisplayed; read this range before citing it")
    quote = item.text[start:end]
    if not quote.strip():
        raise ValueError(f"{collection}: evidence {identifier} selected range contains only whitespace")
    return {"evidence_id": identifier, "quote": quote}
