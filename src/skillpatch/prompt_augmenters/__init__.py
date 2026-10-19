"""Harness-specific explicit Skill invocation prompts."""

from skillpatch.prompt_augmenters.base import PromptAugmentation, PromptAugmenter
from skillpatch.prompt_augmenters.registry import get_prompt_augmenter

__all__ = ["PromptAugmentation", "PromptAugmenter", "get_prompt_augmenter"]
