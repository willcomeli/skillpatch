"""Shared eligibility for diagnosis and evolution from normalized rewards."""

import math
from collections.abc import Mapping


def reward_eligibility(feedback) -> str:
    """Return eligible, succeeded or invalid; never infer reward from passed."""
    if isinstance(feedback, Mapping):
        status, metrics = feedback.get("status"), feedback.get("metrics")
    else:
        status = getattr(feedback, "status", None)
        metrics = getattr(feedback, "metrics", None)
    if status != "scored" or not isinstance(metrics, Mapping):
        return "invalid"
    reward = metrics.get("reward")
    if type(reward) not in (int, float):
        return "invalid"
    try:
        if not math.isfinite(reward):
            return "invalid"
    except OverflowError:
        return "invalid"
    return "succeeded" if reward == 1 else "eligible"
