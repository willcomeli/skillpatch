"""Bind model-generated supplement text to a target in the archived context."""
import hashlib
from method.skillpatch.patch_diagnosis.discovery_validation import _target
from .models import FindingIssue, PatchSource, SkillPatch


def validate_generation(payload: dict, source: PatchSource, revision: int) -> SkillPatch | None:
    finding = isinstance(source.conflict, FindingIssue)
    expected = {"status", "reason", "when_to_use", "how_to_interpret"}
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
    if payload["status"] not in {"candidate", "insufficient_evidence", "unpatchable"}:
        raise ValueError("Invalid patch generation status")
    if not isinstance(payload["reason"], str) or not payload["reason"].strip():
        raise ValueError("Patch generation must explain its decision")
    if payload["status"] != "candidate":
        if (payload["when_to_use"] is not None or payload["how_to_interpret"] is not None
                or (finding and payload["target"] is not None)):
            raise ValueError("Non-candidate outputs must not contain a patch")
        return None
    for key in ("when_to_use", "how_to_interpret"):
        if not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > 16000:
            raise ValueError("Both patch sections must be nonempty bounded text")
        if "<!-- skillpatch:" in payload[key] or "\x00" in payload[key]:
            raise ValueError("Patch text contains a reserved marker or null byte")
    target = source.conflict.target
    if finding:
        target = _target(payload["target"], {item.context_id: item for item in source.context.items},
                         "target", legacy=False)
    identity = "\n".join((source.context.run_id, source.conflict.conflict_id, source.context.input_digest))
    patch_id = "P" + hashlib.sha256(identity.encode()).hexdigest()[:16] + f"-r{revision}"
    return SkillPatch(patch_id, source.context.run_id,
                      source.context.input_digest, source.conflict.conflict_id, source.conflict.type,
                      revision, target, payload["when_to_use"], payload["how_to_interpret"])
