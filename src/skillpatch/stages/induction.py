"""Generate a version-bound candidate after inspecting archived source evidence."""

import json
from pathlib import Path
from typing import Mapping

from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.evidence_views import EvidenceViews
from skillpatch.model_client import DiagnosisConfig
from skillpatch.patch_models import PatchSource
from skillpatch.output_language import validate_output_language
from skillpatch.patches import validate_generation
from skillpatch.storage import jsonable, write_json


def require_displayed_references(refs, view: EvidenceViews, collection: str) -> None:
    """A real quote in an archive is insufficient until that passage was shown."""
    evidence = {item.evidence_id: item for item in view.context.evidence}
    for ref in refs:
        item = evidence.get(ref["evidence_id"])
        if item is None or not any(ref["quote"] in item.text[start:end]
                                   for start, end in view.presented.get(item.evidence_id, [])):
            raise ValueError(f"{collection}: citation refers to undisplayed evidence {ref['evidence_id']}; "
                             "read that passage before citing it")


def require_behavior_references(refs, view: EvidenceViews, collection: str) -> None:
    """Require displayed behavior citations from a complete execution record.

    Relevant later repairs and counterevidence remain the model's responsibility;
    neither a whole cited event nor every later event must have been displayed.
    """
    trajectory_ids = {item.evidence_id for item in view.context.evidence if item.kind == "trajectory"}
    if not any(ref["evidence_id"] in trajectory_ids for ref in refs):
        raise ValueError(f"{collection}: a supported judgment needs trajectory evidence")
    if view.context.trajectory_complete is not True:
        raise ValueError(f"{collection}: incomplete execution evidence requires an uncertain judgment")
    require_displayed_references(refs, view, collection)


def generate_patch(source: PatchSource, config: DiagnosisConfig, output: Path,
                   environment: Mapping[str, str], *, revision: int = 0, history=None, client=None,
                   session_timeout_sec: int = 900):
    data = ("SOURCE CONFLICT (a candidate, not established truth)\n" +
            json.dumps(jsonable(source.conflict), ensure_ascii=False) +
            "\nPREVIOUS ATTEMPTS\n" + json.dumps(jsonable(history or []), ensure_ascii=False))
    viewed = {}

    def validate(payload, views):
        validate_generation(payload, source, revision)
        view = views["source"]
        viewed["source_digest"] = view.context.input_digest

    payload = recorded_evidence_session(
        config, data, {"source": source.context}, output, environment, client=client,
        priority_ids={"source": [source.conflict.target.skill_ref]}, validate=validate,
        session_timeout_sec=session_timeout_sec,
        budget_fallback=lambda reason: {"status": "insufficient_evidence",
            "reason": "Evidence inspection is incomplete: " + reason, "when_to_use": None, "how_to_interpret": None})
    patch = validate_generation(payload, source, revision)
    write_json(output / "generation.json", {"status": payload["status"], "reason": payload["reason"],
                                            "patch": patch, "semantic_correctness": "not_verified",
                                            "source_digest": viewed["source_digest"],
                                            "original_source_digest": source.context.input_digest})
    if patch is not None:
        write_json(output / "patch.json", patch)
    return patch, payload
