"""The OpenHands reference condition, using only task-provided Skill names."""

from collections.abc import Sequence
import hashlib
from pathlib import Path
import re
from string import Formatter

from .base import PromptAugmentation


TEMPLATE_PATH = (Path(__file__).resolve().parents[1]
                 / "templates" / "execution" / "openhands_reference.md")
PATCH_TEMPLATE_PATH = TEMPLATE_PATH.with_name("openhands_skill_patches.md")
_SKILL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_REFERENCE_START = "Before solving the task, invoke and read these task-provided Skills:"
_LEGACY_START = "Before solving the task, invoke and read each of the following task-provided Skills."


class OpenHandsReferenceAugmenter:
    def __init__(self, template_path: Path | None = None,
                 patch_template_path: Path | None = None):
        self.template_path = TEMPLATE_PATH if template_path is None else Path(template_path)
        self.patch_template_path = (PATCH_TEMPLATE_PATH if patch_template_path is None
                                    else Path(patch_template_path))

    def augment(self, task_prompt: str, skill_names: Sequence[str], *,
                patch_skill_names: Sequence[str] = ()) -> PromptAugmentation:
        if not isinstance(task_prompt, str):
            raise ValueError("The task prompt must be a string")
        if isinstance(skill_names, (str, bytes)) or not isinstance(skill_names, Sequence):
            raise ValueError("Skill names must be a sequence of nonempty names")
        if not skill_names:
            raise ValueError("At least one task-provided Skill name is required")
        if any(not isinstance(name, str) or not _SKILL_NAME.fullmatch(name)
               for name in skill_names):
            raise ValueError("Skill names must be valid directory names using letters, digits, _, . or -")
        if len(set(skill_names)) != len(skill_names):
            raise ValueError("Task-provided Skill names must be unique")
        names = tuple(sorted(skill_names))
        if (isinstance(patch_skill_names, (str, bytes))
                or not isinstance(patch_skill_names, Sequence)
                or any(not isinstance(name, str) or name not in names for name in patch_skill_names)
                or len(set(patch_skill_names)) != len(patch_skill_names)):
            raise ValueError("Patch Skill names must be a unique subset of task-provided Skill names")
        patch_names = tuple(sorted(patch_skill_names))

        template_path = self.template_path.resolve()
        template_bytes = template_path.read_bytes()
        template = template_bytes.decode("utf-8")
        try:
            fields = [(field, spec, conversion)
                      for _, field, spec, conversion in Formatter().parse(template)
                      if field is not None]
        except ValueError as exc:
            raise ValueError("OpenHands prompt template has malformed placeholders") from exc
        if fields != [("skill_names", "", None)] or template.count("{skill_names}") != 1:
            raise ValueError("OpenHands prompt template requires exactly one {skill_names} placeholder")
        prefix = template.format(skill_names=", ".join(f"`{name}`" for name in names)).rstrip("\r\n")
        if not prefix.strip():
            raise ValueError("OpenHands prompt template must produce a nonempty prefix")
        patch_template_path = None
        patch_template_sha256 = None
        if patch_names:
            patch_template_path = self.patch_template_path.resolve()
            patch_template_bytes = patch_template_path.read_bytes()
            patch_template = patch_template_bytes.decode("utf-8")
            try:
                patch_fields = [(field, spec, conversion)
                                for _, field, spec, conversion in Formatter().parse(patch_template)
                                if field is not None]
            except ValueError as exc:
                raise ValueError("OpenHands patch prompt template has malformed placeholders") from exc
            if (patch_fields != [("skill_names", "", None)]
                    or patch_template.count("{skill_names}") != 1):
                raise ValueError("OpenHands patch prompt template requires exactly one {skill_names} placeholder")
            patch_prefix = patch_template.format(
                skill_names=", ".join(f"`{name}`" for name in patch_names)).rstrip("\r\n")
            prefix += "\n\n" + patch_prefix
            patch_template_sha256 = "sha256:" + hashlib.sha256(patch_template_bytes).hexdigest()

        # Check only the beginning: similar text inside the user's actual task is
        # ordinary task content and must never be removed or rewritten.
        beginning = task_prompt.lstrip()
        already_present = (beginning == prefix
                           or beginning.startswith(prefix + "\n")
                           or beginning.startswith(prefix + "\r\n"))
        if not already_present and beginning.startswith((_REFERENCE_START, _LEGACY_START)):
            raise ValueError("Task prompt already contains a different Skill invocation prefix; "
                             "use the original benchmark task instead")

        return PromptAugmentation(
            harness="openhands", variant="reference+patches" if patch_names else "reference", original_prompt=task_prompt,
            added_prompt="" if already_present else prefix,
            effective_prompt=task_prompt if already_present else prefix + "\n\n" + task_prompt,
            skill_names=names, template_path=template_path,
            template_sha256="sha256:" + hashlib.sha256(template_bytes).hexdigest(),
            applied=not already_present, reason="already_present" if already_present else None,
            patch_skill_names=patch_names, patch_template_path=patch_template_path,
            patch_template_sha256=patch_template_sha256,
        )
