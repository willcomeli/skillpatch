"""A harness-specific transformation of the original task prompt."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class PromptAugmentation:
    harness: str
    variant: str
    original_prompt: str
    added_prompt: str
    effective_prompt: str
    skill_names: tuple[str, ...]
    template_path: Path
    template_sha256: str
    applied: bool
    reason: str | None = None
    patch_skill_names: tuple[str, ...] = ()
    patch_template_path: Path | None = None
    patch_template_sha256: str | None = None


class PromptAugmenter(Protocol):
    def augment(self, task_prompt: str, skill_names: Sequence[str], *,
                patch_skill_names: Sequence[str] = ()) -> PromptAugmentation:
        """Return the effective prompt and provenance without changing the source."""
        ...
