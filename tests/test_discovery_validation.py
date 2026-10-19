"""Tests only shape, provenance and reference validation, never diagnosis accuracy."""

import copy
import unittest
from dataclasses import replace

from skillpatch.discovery_validation import validate_report
from skillpatch.models import DiscoveryInput, EvidenceItem


def make_context():
    return DiscoveryInput(
        run_id="fresh-run", benchmark="test", task_id="task-a", task_digest="sha256:task",
        skill_digest="sha256:skills", trajectory_complete=True, limitations=[], input_digest="sha256:input",
        evidence=[
            EvidenceItem("Q000001", "task", "Preserve duplicate records for this task.", "/run/task.md"),
            EvidenceItem("S000001", "skill", "# Table cleanup\nRemove duplicate records when uniqueness is required.",
                         "/run/skills/table/SKILL.md", skill_id="table", relative_path="table/SKILL.md", version="sha256:skill"),
            EvidenceItem("T000001", "trajectory", "Read the table.", "/run/events.jsonl", source_line=1),
            EvidenceItem("T000002", "trajectory", "Removed duplicate rows.", "/run/events.jsonl", source_line=2),
            EvidenceItem("T000003", "trajectory", "Saved output.", "/run/events.jsonl", source_line=3),
            EvidenceItem("F000001", "feedback", "Row count test failed.", "/run/verifier.txt"),
        ],
    )


def make_payload():
    return {
        "status": "conflicts_found", "summary": "A candidate use conflict was found.",
        "conflicts": [{
            "conflict_id": "C000001", "type": "misuse",
            "target": {"scope": "substep", "skill_id": "table", "skill_ref": "S000001",
                       "quote": "Remove duplicate records when uniqueness is required."},
            "applicability_reason": "The task requests preservation.",
            "observed_behavior": "The trajectory reports removing duplicates.",
            "expected_behavior": "Preserve duplicate records.",
            "decision_evidence": [{"evidence_id": "Q000001", "quote": "Preserve duplicate records"}],
            "behavior_evidence": [{"evidence_id": "T000002", "quote": "Removed duplicate rows."}],
            "feedback_evidence": [{"evidence_id": "F000001", "quote": "Row count test failed."}],
            "trajectory_range": {"start": "T000001", "end": "T000003"},
            "feedback_assessment": "The feedback concerns row count but does not establish the diagnosis alone.",
            "uncertainty": [],
        }], "other_findings": [], "coverage_notes": [],
    }


class DiscoveryValidationTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context()
        self.payload = make_payload()

    def test_valid_report_binds_program_owned_provenance(self):
        result = validate_report(self.payload, self.context)
        self.assertEqual(result.run_id, self.context.run_id)
        self.assertEqual(result.input_digest, self.context.input_digest)
        self.assertEqual(result.semantic_status, "candidate_diagnosis")
        self.assertEqual(result.conflicts[0].target.version, "sha256:skill")
        self.assertEqual(result.conflicts[0].target.relative_path, "table/SKILL.md")
        self.assertEqual(result.conflicts[0].target.source_path, "/run/skills/table/SKILL.md")

    def test_both_conflict_types_and_target_scopes_are_representable(self):
        for kind in ("under_use", "misuse"):
            for scope in ("whole_skill", "substep"):
                with self.subTest(kind=kind, scope=scope):
                    self.payload["conflicts"][0]["type"] = kind
                    self.payload["conflicts"][0]["target"]["scope"] = scope
                    self.assertEqual(validate_report(self.payload, self.context).conflicts[0].type, kind)
        # These combinations deliberately test representation, not their meaning.

    def test_no_supported_conflict_requires_empty_conflicts(self):
        self.payload.update(status="no_supported_conflict", conflicts=[])
        self.assertEqual(validate_report(self.payload, self.context).conflicts, [])

    def test_status_and_conflict_presence_must_agree(self):
        for status, conflicts in (("conflicts_found", []), ("no_supported_conflict", make_payload()["conflicts"]),
                                  ("insufficient_evidence", make_payload()["conflicts"])):
            with self.subTest(status=status):
                self.payload.update(status=status, conflicts=conflicts)
                with self.assertRaisesRegex(ValueError, "conflicts:"):
                    validate_report(self.payload, self.context)

    def test_extra_fields_and_program_owned_fields_rejected(self):
        for key in ("run_id", "input_digest", "semantic_status", "unexpected-sensitive-field"):
            with self.subTest(key=key):
                payload = make_payload()
                payload[key] = "secret-value"
                with self.assertRaises(ValueError) as caught:
                    validate_report(payload, self.context)
                self.assertNotIn("secret-value", str(caught.exception))
                self.assertNotIn("unexpected-sensitive-field", str(caught.exception))

    def test_target_metadata_cannot_be_supplied_by_model(self):
        for key in ("version", "relative_path", "source_path"):
            with self.subTest(key=key):
                payload = make_payload()
                payload["conflicts"][0]["target"][key] = "invented"
                with self.assertRaisesRegex(ValueError, "target: fields"):
                    validate_report(payload, self.context)

    def test_missing_fields_and_wrong_types_rejected(self):
        variants = []
        missing = make_payload()
        del missing["conflicts"][0]["feedback_assessment"]
        variants.append(missing)
        empty = make_payload()
        empty["summary"] = "   "
        variants.append(empty)
        wrong = make_payload()
        wrong["conflicts"][0]["uncertainty"] = "none"
        variants.append(wrong)
        wrong_list = make_payload()
        wrong_list["conflicts"][0]["uncertainty"] = [None]
        variants.append(wrong_list)
        for payload in variants:
            with self.subTest(payload_kind=type(payload)):
                with self.assertRaises(ValueError):
                    validate_report(payload, self.context)

    def test_bad_enum_values_rejected(self):
        for field, value in (("type", "implementation_error"), ("type", [])):
            payload = make_payload()
            payload["conflicts"][0][field] = value
            with self.assertRaises(ValueError):
                validate_report(payload, self.context)

    def test_target_must_match_skill_identity_and_unique_quote(self):
        for field, value in (("skill_id", "another-skill"), ("skill_ref", "Q000001"),
                             ("quote", "Not actually in the source"), ("quote", " ")):
            with self.subTest(field=field):
                payload = make_payload()
                payload["conflicts"][0]["target"][field] = value
                with self.assertRaises(ValueError):
                    validate_report(payload, self.context)
        self.context.evidence[1] = replace(self.context.evidence[1], text="aaaaa")
        self.payload["conflicts"][0]["target"]["quote"] = "aaa"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            validate_report(self.payload, self.context)

    def test_target_requires_original_version(self):
        self.context.evidence[1] = replace(self.context.evidence[1], version=None)
        with self.assertRaisesRegex(ValueError, "bound_version"):
            validate_report(self.payload, self.context)

    def test_references_must_exist_and_quote_exactly(self):
        for reference in ({"evidence_id": "T999999", "quote": "Removed"},
                          {"evidence_id": "T000002", "quote": "removed duplicate rows."},
                          {"evidence_id": "T000002", "quote": ""},
                          {"evidence_id": "T000002", "quote": "Removed", "extra": True}):
            with self.subTest(reference=reference):
                self.payload["conflicts"][0]["behavior_evidence"] = [reference]
                with self.assertRaises(ValueError):
                    validate_report(self.payload, self.context)

    def test_decision_needs_task_or_trajectory_not_skill_alone(self):
        self.payload["conflicts"][0]["decision_evidence"] = [{"evidence_id": "S000001", "quote": "Table cleanup"}]
        validate_report(self.payload, self.context)
        self.payload["conflicts"][0]["decision_evidence"].append({"evidence_id": "T000001", "quote": "Read the table."})
        validate_report(self.payload, self.context)

    def test_behavior_and_feedback_reference_kinds(self):
        for field, reference in (("behavior_evidence", {"evidence_id": "F000001", "quote": "Row count"}),
                                 ("feedback_evidence", {"evidence_id": "T000002", "quote": "Removed"}),
                                 ("decision_evidence", {"evidence_id": "F000001", "quote": "Row count"})):
            with self.subTest(field=field):
                payload = make_payload()
                payload["conflicts"][0][field] = [reference]
                validate_report(payload, self.context)

    def test_behavior_and_decision_required_feedback_optional(self):
        for field in ("behavior_evidence", "decision_evidence"):
            payload = make_payload()
            payload["conflicts"][0][field] = []
            validate_report(payload, self.context)
        self.payload["conflicts"][0]["feedback_evidence"] = []
        self.assertEqual(validate_report(self.payload, self.context).conflicts[0].feedback_evidence, [])

    def test_trajectory_range_rejects_reverse_missing_and_excluded_behavior(self):
        for span in ({"start": "T000003", "end": "T000001"},
                     {"start": "T000001", "end": "T999999"},
                     {"start": "Q000001", "end": "T000003"},
                     {"start": "T000003", "end": "T000003"}):
            self.payload["conflicts"][0]["trajectory_range"] = span
            with self.assertRaisesRegex(ValueError, "trajectory_range"):
                validate_report(self.payload, self.context)

    def test_conflict_identifiers_are_unique(self):
        self.payload["conflicts"].append(copy.deepcopy(self.payload["conflicts"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate conflict"):
            validate_report(self.payload, self.context)

    def test_other_findings_remain_separate(self):
        self.payload.update(status="insufficient_evidence", conflicts=[])
        for category in ("implementation_error", "skill_content_issue", "evaluator_issue", "environment_issue", "uncertain"):
            self.payload["other_findings"] = [{"category": category, "description": "Needs review.",
                                              "evidence": [{"evidence_id": "F000001", "quote": "Row count"}]}]
            result = validate_report(self.payload, self.context)
            self.assertEqual(result.other_findings[0]["category"], category)
            self.assertEqual(result.conflicts, [])

    def test_limitations_require_coverage_notes(self):
        self.context.limitations = ["An attachment is unavailable."]
        validate_report(self.payload, self.context)
        self.payload["coverage_notes"] = ["The unavailable attachment limits the review."]
        validate_report(self.payload, self.context)

    def test_partial_or_unknown_trajectory_requires_insufficient_when_no_conflict(self):
        for complete in (False, None):
            with self.subTest(complete=complete):
                self.context.trajectory_complete = complete
                self.payload.update(status="no_supported_conflict", conflicts=[], coverage_notes=["Trajectory coverage is limited."])
                validate_report(self.payload, self.context)
                self.payload["status"] = "insufficient_evidence"
                validate_report(self.payload, self.context)

    def test_partial_trajectory_can_support_local_candidate_with_coverage_note(self):
        self.context.trajectory_complete = False
        self.payload["coverage_notes"] = ["Only the displayed execution interval was reviewed."]
        self.assertEqual(validate_report(self.payload, self.context).status, "conflicts_found")

    def test_nonfinite_values_and_non_json_types_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), {"not-a-list"}):
            self.payload["conflicts"][0]["uncertainty"] = [value]
            with self.assertRaisesRegex(ValueError, "payload:"):
                validate_report(self.payload, self.context)

    def test_duplicate_context_identifiers_rejected(self):
        self.context.evidence.append(self.context.evidence[0])
        with self.assertRaisesRegex(ValueError, "evidence identifiers"):
            validate_report(self.payload, self.context)

    def test_context_trajectory_order_must_be_unambiguous(self):
        self.context.evidence[2], self.context.evidence[3] = self.context.evidence[3], self.context.evidence[2]
        with self.assertRaisesRegex(ValueError, "increasing order"):
            validate_report(self.payload, self.context)

    def artifact(self, availability="initial"):
        item = EvidenceItem(
            "A000001", "artifact", "The original table contains duplicate records.",
            "/run/task/table.csv", availability=availability, source_role="task_material",
            observation={"tool": "read_file"},
        )
        self.context.evidence.append(item)
        return {"evidence_id": item.evidence_id, "quote": "contains duplicate records"}

    def test_initial_artifact_can_support_decision_without_task_quote(self):
        reference = self.artifact()
        self.payload["conflicts"][0]["decision_evidence"] = [reference]
        result = validate_report(self.payload, self.context)
        self.assertEqual(result.conflicts[0].decision_evidence[0].evidence_id, "A000001")

    def test_post_execution_unknown_and_unmarked_artifacts_cannot_support_decision(self):
        for availability in ("post_execution", "unknown", None):
            with self.subTest(availability=availability):
                self.context = make_context()
                self.payload = make_payload()
                self.payload["conflicts"][0]["decision_evidence"].append(self.artifact(availability))
                validate_report(self.payload, self.context)

    def test_artifact_identifier_and_availability_are_validated(self):
        self.artifact()
        original = self.context.evidence[-1]
        for changes in ({"evidence_id": "attachment-1"}, {"evidence_id": "A1"},
                        {"availability": "before"}, {"availability": []}):
            with self.subTest(changes=changes):
                self.context.evidence[-1] = replace(original, **changes)
                with self.assertRaises(ValueError):
                    validate_report(self.payload, self.context)

    def test_artifact_can_corroborate_behavior_but_does_not_replace_trajectory(self):
        for availability in ("initial", "post_execution", "unknown", None):
            with self.subTest(availability=availability):
                self.context = make_context()
                self.payload = make_payload()
                reference = self.artifact(availability)
                self.payload["conflicts"][0]["behavior_evidence"].append(reference)
                result = validate_report(self.payload, self.context)
                self.assertEqual(len(result.conflicts[0].behavior_evidence), 2)
                self.payload["conflicts"][0]["behavior_evidence"] = [reference]
                validate_report(self.payload, self.context)

    def test_range_checks_trajectory_only_but_still_rejects_excluded_trajectory(self):
        self.payload["conflicts"][0]["behavior_evidence"].append(self.artifact("post_execution"))
        self.payload["conflicts"][0]["trajectory_range"] = {"start": "T000002", "end": "T000002"}
        validate_report(self.payload, self.context)
        self.payload["conflicts"][0]["trajectory_range"] = {"start": "T000001", "end": "T000001"}
        with self.assertRaisesRegex(ValueError, "trajectory_range"):
            validate_report(self.payload, self.context)
        self.payload["conflicts"][0]["trajectory_range"] = {"start": "A000001", "end": "T000003"}
        with self.assertRaisesRegex(ValueError, "endpoints"):
            validate_report(self.payload, self.context)

    def test_artifact_is_not_feedback_or_skill_target(self):
        reference = self.artifact()
        self.payload["conflicts"][0]["feedback_evidence"] = [reference]
        validate_report(self.payload, self.context)
        self.payload = make_payload()
        self.payload["conflicts"][0]["target"]["skill_ref"] = "A000001"
        with self.assertRaisesRegex(ValueError, "expected a Skill"):
            validate_report(self.payload, self.context)

    def test_other_findings_accept_artifacts_with_exact_quotes(self):
        reference = self.artifact("unknown")
        self.payload["other_findings"] = [{
            "category": "uncertain", "description": "The observation time is unknown.",
            "evidence": [reference],
        }]
        result = validate_report(self.payload, self.context)
        self.assertEqual(result.other_findings[0]["evidence"], [reference])
        reference["quote"] = "A paraphrase absent from the source"
        with self.assertRaisesRegex(ValueError, "not an exact substring"):
            validate_report(self.payload, self.context)

    def test_model_cannot_override_artifact_availability_in_reference(self):
        reference = self.artifact("post_execution")
        reference["availability"] = "initial"
        self.payload["conflicts"][0]["decision_evidence"] = [reference]
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            validate_report(self.payload, self.context)


if __name__ == "__main__":
    unittest.main()
