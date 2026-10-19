"""SkillsBench reward direction and named-test comparisons for Stage 2."""

import math

from skillpatch.patch_models import FeedbackComparison


def compare_feedback(baseline, candidate, *, target=None) -> FeedbackComparison:
    if baseline.status != "scored" or candidate.status != "scored" or baseline.issues or candidate.issues:
        return FeedbackComparison("unavailable", limitations=["Valid scored feedback is required for both runs."])
    changes = []
    unavailable = False
    def tests(feedback):
        result = {}
        for item in feedback.tests:
            name, status = item.get("name"), item.get("status")
            if not isinstance(name, str) or not name or name in result or status not in {"passed", "failed"}:
                raise ValueError("Unmatched, duplicate or nonbinary test results cannot establish preservation")
            result[name] = status
        return result
    try:
        before, after = tests(baseline), tests(candidate)
    except ValueError as exc:
        return FeedbackComparison("unavailable", limitations=[str(exc)])
    if before.keys() != after.keys():
        unavailable = True
    for name in sorted(before.keys() & after.keys()):
        if before[name] != after[name]:
            short_name = name.split("::")[-1]
            related = target is None or any(name in ref.quote or (short_name and short_name in ref.quote)
                                           for ref in target.feedback_evidence)
            changes.append({"metric": "test:" + name, "baseline": before[name], "candidate": after[name],
                            "direction": "improved" if after[name] == "passed" else "regressed",
                            "target_related": related})
    for label in ("reward",):
        a, b = baseline.metrics.get(label), candidate.metrics.get(label)
        valid = all(type(x) in (int, float) and math.isfinite(x) for x in (a, b))
        if valid and a != b:
            changes.append({"metric": label, "baseline": a, "candidate": b,
                            "direction": "improved" if b > a else "regressed"})
        elif not valid:
            unavailable = True
    if baseline.passed is True and candidate.passed is not True:
        changes.append({"metric": "passed", "baseline": True, "candidate": candidate.passed,
                        "direction": "regressed" if candidate.passed is False else "unavailable"})
    limitations = ["Per-test coverage differs or numeric reward is unavailable."] if unavailable else []
    if any(c["direction"] == "regressed" for c in changes):
        status = "regressed"
    elif unavailable or any(c["direction"] == "unavailable" for c in changes):
        status = "unavailable"
    elif any(c["direction"] == "improved" and c.get("target_related", True) for c in changes):
        status = "improved"
    else:
        status = "unchanged"
        if any(c["direction"] == "improved" for c in changes):
            limitations.append("Named tests improved, but source conflict references do not establish target relevance.")
    return FeedbackComparison(status, changes, limitations)
