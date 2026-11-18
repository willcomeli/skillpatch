"""Bind model-generated supplement text to a target in the archived context."""
import hashlib
from method.skillpatch.patch_diagnosis.discovery_validation import _target
from .models import NOTE_EXAMPLE_FORMAT, FindingIssue, NoteExamplePatch, PatchSource, SkillPatch


def validate_generation(payload: dict, source: PatchSource, revision: int, *,
                        patch_format: str | None = None) -> SkillPatch | NoteExamplePatch | None:
    finding = isinstance(source.conflict, FindingIssue)
    if patch_format is None:
        sections = ("when_to_use", "how_to_interpret")
        patch_type = SkillPatch
    elif patch_format == NOTE_EXAMPLE_FORMAT:
        if finding or source.conflict.target is None:
            raise ValueError("Boundary-aware induction requires a Stage1 target")
        sections = ("note", "example")
        patch_type = NoteExamplePatch
    else:
        raise ValueError("Unsupported patch format")
    expected = {"status", "reason", *sections}
    if finding:
        expected.add("target")
    if not isinstance(payload, dict) or set(payload) != expected:
        actual = set(payload) if isinstance(payload, dict) else set()
        target_rule = ("For SOURCE OTHER FINDING, target is required: select an existing Skill using "
                       "scope, skill_ref and either quote or both start_char/end_char; "
                       "target must be null for a non-candidate result." if finding else
                       "For SOURCE SKILL IMPROVEMENT or SOURCE CONFLICT, the target is fixed; do not include a target field.")
        raise ValueError(
            f"Patch generation fields do not match the schema; required={sorted(expected)}, "
            f"missing={sorted(expected - actual)}, unexpected={sorted(actual - expected)}. {target_rule}")
    status = payload["status"]
    if not isinstance(status, str) or status not in {"candidate", "insufficient_evidence", "unpatchable"}:
        raise ValueError("Invalid patch generation status")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Patch generation must explain its decision")
    if status != "candidate":
        if any(payload[key] is not None for key in expected - {"status", "reason"}):
            raise ValueError("Non-candidate outputs must not contain a patch")
        return None
    for key in sections:
        if not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > 16000:
            raise ValueError("Both patch sections must be nonempty bounded text")
        if "<!-- skillpatch:" in payload[key] or "\x00" in payload[key]:
            raise ValueError("Patch text contains a reserved marker or null byte")
    target = source.conflict.target
    if finding:
        target = _target(payload["target"], {item.context_id: item for item in source.context.items},
                         "target", legacy=False)
    identity = (source.context.run_id, source.conflict.conflict_id, source.context.input_digest)
    if patch_format is not None:
        identity += (patch_format,)
    patch_id = "P" + hashlib.sha256("\n".join(identity).encode()).hexdigest()[:16] + f"-r{revision}"
    return patch_type(patch_id, source.context.run_id,
                      source.context.input_digest, source.conflict.conflict_id, source.conflict.type,
                      revision, target, **{key: payload[key] for key in sections})
