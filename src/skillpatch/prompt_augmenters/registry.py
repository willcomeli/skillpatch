"""Select an explicit Skill prompt by harness, with no generic fallback."""

from skillpatch.prompt_augmenters.base import PromptAugmenter
from skillpatch.prompt_augmenters.openhands import OpenHandsReferenceAugmenter


def get_prompt_augmenter(agent: str) -> PromptAugmenter:
    if agent in ("openhands", "oh"):
        return OpenHandsReferenceAugmenter()
    raise ValueError(f"Unsupported prompt augmenter harness: {agent!r}; "
                     "currently supported: openhands (alias: oh)")
