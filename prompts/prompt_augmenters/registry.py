"""Select an explicit Skill prompt by harness, with no generic fallback."""

from .base import PromptAugmenter
from .openhands import OpenHandsReferenceAugmenter


def get_prompt_augmenter(agent: str) -> PromptAugmenter:
    if agent in ("openhands", "oh"):
        return OpenHandsReferenceAugmenter()
    raise ValueError(f"Unsupported prompt augmenter harness: {agent!r}; "
                     "currently supported: openhands (alias: oh)")
