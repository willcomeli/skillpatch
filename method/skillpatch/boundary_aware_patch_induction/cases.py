"""Schema and evidence checks for proposed INV and CON cases."""

from method.skillpatch.patch_diagnosis.discovery_validation import _choice, _list, _object, _strings, _text

CASE_BUDGET = 4
_CASE_FIELDS = {"pre_decision_state", "expected_use", "expected_behavior", "evidence_refs"}
_EXPECTED_USE = {"INV": "APPLY", "CON": "SKIP"}


def _validate_expected_behavior(case, expected_use, evidence_ids, path):
    _text(case["pre_decision_state"], f"{path}.pre_decision_state")
    _choice(case["expected_use"], {expected_use}, f"{path}.expected_use")
    _text(case["expected_behavior"], f"{path}.expected_behavior")
    refs = _strings(case["evidence_refs"], f"{path}.evidence_refs")
    if not refs or not set(refs) <= evidence_ids:
        raise ValueError(f"{path}.evidence_refs must name evidence in the Stage1 context")


def validate_boundary_cases(payload, evidence_ids):
    _object(payload, {"reference", "boundary_cases", "unresolved"}, "proposal")
    _strings(payload["unresolved"], "unresolved")
    cases = _list(payload["boundary_cases"], "boundary_cases")
    if len(cases) > CASE_BUDGET:
        raise ValueError("Boundary case budget exceeded")
    reference = payload["reference"]
    if reference is None and cases:
        raise ValueError("Boundary cases require an APPLY reference")
    if reference is not None:
        _object(reference, _CASE_FIELDS, "reference")
        _validate_expected_behavior(reference, "APPLY", evidence_ids, "reference")
    case_ids = set()
    for index, case in enumerate(cases):
        path = f"boundary_cases[{index}]"
        _object(case, _CASE_FIELDS | {"case_id", "type", "changed_factor", "held_fixed"}, path)
        case_id = _text(case["case_id"], f"{path}.case_id")
        if case_id in case_ids:
            raise ValueError("Duplicate boundary case ID")
        case_ids.add(case_id)
        kind = _choice(case["type"], set(_EXPECTED_USE), f"{path}.type")
        _text(case["changed_factor"], f"{path}.changed_factor")
        _strings(case["held_fixed"], f"{path}.held_fixed")
        _validate_expected_behavior(case, _EXPECTED_USE[kind], evidence_ids, path)
