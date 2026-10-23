"""Connect prepared task skills to harness prompt augmenters and loading audits."""

import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from excutor.executor_adapter.models import PreparedTask, RunRequest
from excutor.excutor_runners.patch_sidecars import SIDECAR_MANIFEST, SIDECAR_MARKDOWN, load_sidecars
from .registry import get_prompt_augmenter
from excutor.executor_adapter.storage import sha256_file, write_json


def document_body(text: str) -> str:
    """Remove only an initial YAML metadata block; retain the instruction body."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in {"---", "..."}:
                return "".join(lines[index + 1:])
        raise ValueError("Unterminated task/skill metadata block")
    return text


def expected_skills(task: PreparedTask) -> list[dict[str, Any]]:
    if task.skills_dir is None:
        return []
    skills = []
    for path in sorted(task.skills_dir.glob("*/SKILL.md")):
        if path.is_symlink() or not path.resolve().is_relative_to(task.skills_dir.resolve()):
            raise ValueError("Skill file resolves outside the prepared skill pack")
        text = path.read_text(encoding="utf-8")
        body = document_body(text)
        frontmatter = text[:len(text) - len(body)]
        declarations = re.findall(r"^name[ \t]*:[ \t]*(.*)$", frontmatter, re.MULTILINE)
        if len(declarations) > 1:
            raise ValueError(f"Duplicate Skill name declarations: {path}")
        name = path.parent.name
        if declarations:
            # Match the SDK's plain/quoted scalar names, including inline comments.
            # Other YAML forms require a real parser; never silently invoke the
            # folder name when the declared loading name could differ.
            match = re.fullmatch(
                r'''(?:"([A-Za-z0-9_.-]+)"|'([A-Za-z0-9_.-]+)'|([A-Za-z0-9_.-]+))(?:[ \t]+\#.*)?[ \t]*''',
                declarations[0],
            )
            if match is None:
                raise ValueError(f"Unsupported Skill name declaration: {path}")
            name = next(value for value in match.groups() if value is not None)
        skills.append({"name": name,
                       "relative_path": path.relative_to(task.skills_dir).as_posix(),
                       "source_path": str(path), "version": sha256_file(path),
                       "body": body.strip()})
    return skills


def expected_skill_patches(task: PreparedTask, *, skills=None) -> list[dict[str, Any]]:
    """Locate validated separate patch documents in the deployed Skill directories."""
    if task.skills_dir is None:
        return []
    skills = expected_skills(task) if skills is None else skills
    known_roots = {Path(skill["source_path"]).parent for skill in skills}
    for path in task.skills_dir.rglob("*"):
        if path.name in {SIDECAR_MARKDOWN, SIDECAR_MANIFEST} and path.parent not in known_roots:
            raise ValueError("Separate patch files must belong to a supported top-level Skill; "
                             "nested or unrecognized Skill patch delivery is unsupported")
    patches = []
    for skill in skills:
        directory = Path(skill["source_path"]).parent
        if load_sidecars(directory) is None:
            continue
        path = directory / SIDECAR_MARKDOWN
        patches.append({
            "name": skill["name"], "relative_path": path.relative_to(task.skills_dir).as_posix(),
            "source_path": str(path), "version": sha256_file(path),
            "manifest_sha256": sha256_file(directory / SIDECAR_MANIFEST),
            "body": path.read_text(encoding="utf-8").strip(),
        })
    return patches


def _augmentation(request: RunRequest, task: PreparedTask):
    if not request.explicit_skill_read or request.skill_mode != "with-skill":
        if (request.skill_mode == "with-skill" and not request.explicit_skill_read
                and expected_skill_patches(task)):
            raise ValueError("Separate Skill patches require explicit_skill_read=True")
        return None
    augmenter = get_prompt_augmenter(request.agent)
    skills = expected_skills(task)
    if not skills:
        raise ValueError("Explicit skill reading requires at least one prepared skill")
    names = []
    for item in skills:
        folder_name = Path(item["relative_path"]).parts[0]
        if item["name"] != folder_name:
            raise ValueError("OpenHands reference requires matching Skill directory and declared name; "
                             f"found {folder_name!r} and {item['name']!r}")
        names.append(folder_name)
    body = document_body(task.task_document.read_text(encoding="utf-8"))
    if re.search(r"^##[ \t]+(?:Prompt(?:[ \t]*$)|Role:|Scene:|User-persona(?:[ \t]*$|:))", body, re.MULTILINE):
        raise ValueError("Explicit prompt augmentation currently requires a single plain task body; "
                         "BenchFlow Prompt/Role/Scene/User-persona sections are not supported")
    patches = expected_skill_patches(task, skills=skills)
    return augmenter.augment(body, names, patch_skill_names=[item["name"] for item in patches]), skills, patches


def _artifact_content(request: RunRequest, task: PreparedTask):
    result = _augmentation(request, task)
    if result is None:
        return None
    augmentation, skills, patches = result
    files = {"original.md": augmentation.original_prompt,
             "added.md": augmentation.added_prompt,
             "effective.md": augmentation.effective_prompt}
    manifest = {
        "schema_version": 1, "harness": augmentation.harness,
        "requested_agent": request.agent, "variant": augmentation.variant,
        "applied": augmentation.applied, "reason": augmentation.reason,
        "template_path": str(augmentation.template_path),
        "template_sha256": augmentation.template_sha256,
        "task_document": str(task.task_document), "task_document_sha256": sha256_file(task.task_document),
        "task_digest": task.task_digest, "skill_digest": task.skill_digest,
        "skill_names": list(augmentation.skill_names),
        "skills": [{key: value for key, value in item.items() if key != "body"} for item in skills],
        "files": {name: "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
                  for name, text in files.items()},
    }
    if patches:
        manifest.update({
            "patch_skill_names": list(augmentation.patch_skill_names),
            "patch_template_path": str(augmentation.patch_template_path),
            "patch_template_sha256": augmentation.patch_template_sha256,
            "patches": [{key: value for key, value in item.items() if key != "body"} for item in patches],
        })
    return files, manifest


def _check_artifacts(directory: Path, files: dict[str, str], manifest: dict) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Prompt artifact directory must be a real directory")
    for name, text in files.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.read_bytes() != text.encode("utf-8"):
            raise ValueError(f"Prepared prompt artifact changed: {name}")
    path = directory / "manifest.json"
    if path.is_symlink() or not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != manifest:
        raise ValueError("Prepared prompt manifest does not match the current task/harness/template")


def prepare_prompt_artifacts(request: RunRequest, task: PreparedTask, run_dir: Path) -> None:
    """Expose the original, added and effective prompts without invoking an agent."""
    content = _artifact_content(request, task)
    if content is None:
        return
    files, manifest = content
    directory = run_dir / "prompt"
    if directory.exists() or directory.is_symlink():
        _check_artifacts(directory, files, manifest)
        return
    directory.mkdir(parents=True, mode=0o700)
    for name, text in files.items():
        (directory / name).write_bytes(text.encode("utf-8"))
    write_json(directory / "manifest.json", manifest)


def explicit_prompt(request: RunRequest, task: PreparedTask, *, run_dir: Path | None = None) -> str | None:
    content = _artifact_content(request, task)
    if content is None:
        return None
    files, manifest = content
    if run_dir is not None and ((run_dir / "prompt").exists() or (run_dir / "prompt").is_symlink()):
        _check_artifacts(run_dir / "prompt", files, manifest)
    return files["effective.md"]


def check_patch_reads(request: RunRequest, task: PreparedTask,
                      events_path: Path | None) -> dict[str, Any]:
    """Recompute candidate reading evidence rather than trust a saved audit flag."""
    patches = expected_skill_patches(task)
    if not patches:
        return {"status": "not_applicable", "reason": "no_separate_patches"}
    if request.backend != "benchflow":
        return {"status": "not_applicable", "reason": "backend_or_harness_has_no_patch_read_audit",
                "patches": [item["relative_path"] for item in patches]}
    if request.agent not in {"openhands", "oh"}:
        return {"status": "unconfirmed", "reason": "harness_has_no_patch_read_audit",
                "patches": [item["relative_path"] for item in patches]}
    if not request.explicit_skill_read or request.skill_mode != "with-skill":
        return {"status": "unconfirmed", "reason": "separate_patches_require_explicit_skill_read"}
    audit = audit_skill_reads(task, events_path)
    return {"status": "passed" if audit["all_expected_instruction_bodies_observed"] else "unconfirmed",
            "reason": "successful_patch_file_reads", "patch_read_policy": "successful_file_read",
            "all_expected_patch_reads_observed": audit["all_expected_patch_reads_observed"],
            "patches": audit["patches"],
            "all_expected_bodies_observed": audit["all_expected_bodies_observed"],
            "all_expected_patch_bodies_observed": audit["all_expected_patch_bodies_observed"]}


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in {"data", "image", "image_url", "base64"}:
                yield from _strings(item)


def _patch_read_path(event, item, invocations):
    """Recognize successful OpenHands file reads, without checking file contents."""
    text = "\n".join(_strings(event.get("content", []))) + "\n" + "\n".join(_strings(event.get("rawOutput", [])))
    if not text.strip() or event.get("isError") or event.get("error"):
        return None
    paths = {"/skills/" + item["relative_path"], item["source_path"]}
    for invocation in invocations:
        if invocation["name"] == item["name"]:
            paths.update(str(Path(location) / "SKILL_PATCHES.md")
                         for location in invocation["reported_locations"])
    # Tool errors can be returned in ACP completed events. Do not mistake them
    # for successful file access. A later command's exit status is not the read's.
    if re.search(r"(?i)(no such file|permission denied|file not found|does not exist|not found at|is a directory|error opening)", text):
        return None
    title = event.get("title", "")
    if event.get("kind") == "read" and "Tool: file_editor" in text:
        match = re.search(r"Here's the result of running `cat -n` on ([^\n]+):", text)
        if match and match.group(1) in paths:
            return match.group(1)
    # ACP terminal events retain the executed command after '$' in the title.
    if event.get("kind") == "execute" and "Tool: terminal" in text and "$ " in title:
        command = title.split("$ ", 1)[1]
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None
        if not any(t in {";", "&&", "||", "|"} for t in tokens):
            if re.search(r"(?i)exit code:\s*[1-9][0-9]*", text):
                return None
        for index, token in enumerate(tokens):
            if token not in {"cat", "head", "tail", "sed"} or (index and tokens[index - 1] not in {";", "&&", "||", "|"}):
                continue
            args = []
            for value in tokens[index + 1:]:
                if value in {";", "&&", "||", "|", ">", ">>", "<"}:
                    break
                args.append(value)
            # sed must print, never edit; cat/head/tail are file-reading commands.
            if token == "sed" and (any(a.startswith("-i") for a in args) or not any("p" in a for a in args)):
                continue
            for value in args:
                if value in paths:
                    return value
    return None


def audit_skill_reads(task: PreparedTask, events_path: Path | None) -> dict[str, Any]:
    """Check original Skill bodies and successful reads of independent patches.

    This confirms visible reading, not comprehension or correct downstream use.
    A skill name in a prompt, catalog, title, or agent thought is not confirmation.
    """
    expected = expected_skills(task)
    patches = expected_skill_patches(task, skills=expected)
    matches: dict[str, list[dict[str, Any]]] = {item["relative_path"]: [] for item in expected}
    patch_matches: dict[str, list[dict[str, Any]]] = {item["relative_path"]: [] for item in patches}
    invocations = []
    if events_path is not None and events_path.is_file():
        # JSONL records are separated by physical newlines, not Unicode line
        # separator characters that may legitimately appear inside JSON strings.
        with events_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                record = json.loads(line)
                event = record.get("event", {})
                if event.get("type") != "tool_call" or event.get("status") != "completed":
                    continue
                # Content/rawOutput are observations; rawInput/title alone prove no reading.
                texts = list(_strings(event.get("content", []))) + list(_strings(event.get("rawOutput", [])))
                for item in patches:
                    path = _patch_read_path(event, item, invocations)
                    if path:
                        patch_matches[item["relative_path"]].append({
                            "event_id": record.get("event_id"), "source": record.get("source"),
                            "method": "successful_file_read", "reported_path": path})
                for text in texts:
                    names = re.findall(r"\[skill:\s*([^\]\n]+)\]", text)
                    locations = re.findall(r"This skill is located at `([^`]+)`", text)
                    for name in names:
                        invocations.append({"name": name, "event_id": record.get("event_id"),
                                            "source": record.get("source"), "reported_locations": locations})
                    original_text = text
                    for item in patches:
                        if item["body"]:
                            original_text = original_text.replace(item["body"], "")
                    for items, observations in ((expected, matches),):
                        for item in items:
                            body = item["body"]
                            # A whole-Skill quote inside the patch is binding
                            # metadata, not an observation of the original file.
                            observed_text = original_text if observations is matches else text
                            if body and body in observed_text:
                                observations[item["relative_path"]].append({
                                    "event_id": record.get("event_id"), "source": record.get("source"),
                                    "method": "invoke_skill" if observations is matches and item["name"] in names else "tool_body_read",
                                    "reported_locations": locations,
                                    "body_sha256": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
                                })
    names = {item["name"] for item in expected}
    result = {"schema_version": 2, "expected": [
        {key: value for key, value in item.items() if key != "body"}
        | {"body_read_confirmed": bool(matches[item["relative_path"]]),
           "observations": matches[item["relative_path"]]} for item in expected],
        "observed_invocations": invocations,
        "extra_invocations": [item for item in invocations if item["name"] not in names],
        "all_expected_bodies_observed": bool(expected) and all(matches.values()),
        "limitations": ["Only full matching bodies in completed tool observations confirm reading; "
                        "This full-body rule applies only to original Skills. Patch reads require a successful "
                        "file-reading tool call, including partial reads; legacy patch body flags mean read confirmation, not full coverage. "
                        "Reading does not establish correct use. Reported tool locations are observations, "
                        "not independent filesystem attestation."]}
    if patches:
        result.update({
            "patch_read_policy": "successful_file_read",
            "all_expected_patch_reads_observed": all(patch_matches.values()),
            "patches": [
                {key: value for key, value in item.items() if key != "body"}
                | {"read_confirmed": bool(patch_matches[item["relative_path"]]),
                   "body_read_confirmed": bool(patch_matches[item["relative_path"]]),
                   "observations": patch_matches[item["relative_path"]]} for item in patches],
            "all_expected_patch_bodies_observed": all(patch_matches.values()),
            "all_expected_instruction_bodies_observed": (
                result["all_expected_bodies_observed"] and all(patch_matches.values())),
        })
    return result
