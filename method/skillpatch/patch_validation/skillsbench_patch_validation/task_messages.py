from pathlib import Path
from prompts.prompt_augmenters.skill_invocation import _artifact_content, _check_artifacts

def _validated_prompt_artifacts(run_dir: Path):
    # Import locally: execution archives also use patch source validation.
    from .records import load_execution
    from .source import load_request

    execution = load_execution(run_dir)
    request = load_request(run_dir / "request.json")
    content = _artifact_content(request, execution.prepared)
    if content is None:
        raise ValueError("No explicit prompt artifacts for this execution")
    files, manifest = content
    _check_artifacts(run_dir / "prompt", files, manifest)
    return files, manifest


def task_messages_equal(before_context, after_context, baseline_dir: Path, candidate_dir: Path) -> bool:
    """Allow only the separately audited patch-reading prefix to differ.

    The task snapshot and every other actual user message must still match.
    The OpenHands exception recognizes a complete canonical user message; it
    never removes matching substrings from arbitrary task text.
    """
    before = [(e.context_id, e.text) for e in before_context.items if e.kind == "task"]
    after = [(e.context_id, e.text) for e in after_context.items if e.kind == "task"]
    if before == after:
        return True
    if len(before) != len(after) or [item[0] for item in before] != [item[0] for item in after]:
        return False
    try:
        before_files, before_manifest = _validated_prompt_artifacts(Path(baseline_dir))
        after_files, after_manifest = _validated_prompt_artifacts(Path(candidate_dir))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if (before_manifest["harness"] != "openhands" or after_manifest["harness"] != "openhands"
            or before_files["original.md"] != after_files["original.md"]
            or before_manifest["template_sha256"] != after_manifest["template_sha256"]
            or before_manifest["skill_names"] != after_manifest["skill_names"]
            or not (before_manifest.get("patches") or after_manifest.get("patches"))):
        return False
    # SkillsBench exposes ACP user messages as the complete flattened event.
    # Any extra/changed event fields are conservatively rejected here.
    def expected_text(files):
        return "text:\n" + files["effective.md"] + "\n\ntype:\nuser_message"

    changed = [(left[1], right[1]) for left, right in zip(before, after) if left != right]
    return changed == [(expected_text(before_files), expected_text(after_files))]
