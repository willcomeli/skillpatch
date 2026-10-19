"""Compact diagnosis and archived-report compatibility, without model calls."""

import copy
import unittest
from dataclasses import asdict, replace

from skillpatch.discovery_validation import validate_report, validate_saved_report
from test_discovery_validation import make_context, make_payload


def compact_payload():
    return {
        "status": "conflicts_found",
        "summary": "发现候选误用。",
        "conflicts": [{
            "type": "misuse",
            "target": {"scope": "substep", "skill_ref": "S000001",
                       "quote": "Remove duplicate records when uniqueness is required."},
            "diagnosis": "任务要求保留重复记录，去重条件不成立；轨迹却执行了去重，应保留原记录。行数反馈相关，但尚未证明因果。",
            "decision_evidence": [{"evidence_id": "Q000001", "quote": "Preserve duplicate records"}],
            "behavior_evidence": [{"evidence_id": "T000002", "quote": "Removed duplicate rows."}],
        }],
    }


class CompactReportTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context()
        self.payload = compact_payload()

    def test_position_references_extract_original_and_roundtrip_empty_span(self):
        c = self.payload["conflicts"][0]
        target = next(e for e in self.context.evidence if e.evidence_id == "S000001")
        quote = c["target"].pop("quote")
        start = target.text.index(quote)
        c["target"].update(start_char=start, end_char=start + len(quote))
        c["decision_evidence"] = [{"evidence_id": "Q000001", "start_char": 0, "end_char": 8}]
        c["behavior_evidence"] = []
        report = validate_report(self.payload, self.context, require_english=True)
        self.assertEqual(report.conflicts[0].target.quote, quote)
        self.assertEqual(report.conflicts[0].trajectory_range, {"start": "", "end": ""})
        self.assertEqual(asdict(validate_saved_report(asdict(report), self.context)), asdict(report))

    def test_position_references_reject_bad_range_and_foreign_collection(self):
        for ref in [{"evidence_id": "T999999"},
                    {"evidence_id": "T000002", "start_char": 0, "end_char": 999999},
                    {"evidence_id": "T000002", "start_char": True, "end_char": 5},
                    {"evidence_id": "T000002", "collection": "candidate"}]:
            with self.subTest(ref=ref):
                payload = compact_payload()
                payload["conflicts"][0]["behavior_evidence"] = [ref]
                with self.assertRaises(ValueError):
                    validate_report(payload, self.context)

    def test_minimal_output_binds_metadata_and_defaults_without_inventing_explanations(self):
        report = validate_report(self.payload, self.context)
        conflict = report.conflicts[0]
        self.assertEqual(report.schema_version, 2)
        self.assertEqual(conflict.conflict_id, "C000001")
        self.assertEqual(conflict.target.skill_id, "table")
        self.assertEqual(conflict.target.version, "sha256:skill")
        self.assertEqual(conflict.diagnosis, self.payload["conflicts"][0]["diagnosis"])
        self.assertEqual(conflict.trajectory_range, {"start": "T000002", "end": "T000002"})
        self.assertEqual(conflict.feedback_evidence, [])
        self.assertEqual(conflict.uncertainty, [])
        self.assertEqual(report.coverage_notes, [])
        self.assertEqual(report.other_findings, [])
        self.assertNotIn("applicability_reason", asdict(conflict))
        self.assertEqual(asdict(validate_saved_report(asdict(report), self.context)), asdict(report))

    def test_ids_and_citation_span_are_derived_from_all_cited_trajectory_events(self):
        conflict = self.payload["conflicts"][0]
        conflict["decision_evidence"].append({"evidence_id": "T000001", "quote": "Read the table."})
        conflict["behavior_evidence"].insert(0, {"evidence_id": "T000003", "quote": "Saved output."})
        self.payload["conflicts"].append(copy.deepcopy(conflict))
        report = validate_report(self.payload, self.context)
        self.assertEqual([c.conflict_id for c in report.conflicts], ["C000001", "C000002"])
        self.assertEqual(report.conflicts[0].trajectory_range, {"start": "T000001", "end": "T000003"})

    def test_compact_model_cannot_supply_bound_metadata(self):
        for key, value in (("conflict_id", "chosen"), ("trajectory_range", {"start": "T000001", "end": "T000003"})):
            payload = compact_payload()
            payload["conflicts"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "fields"):
                validate_report(payload, self.context)
        for key in ("skill_id", "source_path", "relative_path", "version"):
            payload = compact_payload()
            payload["conflicts"][0]["target"][key] = "invented"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "target: fields"):
                validate_report(payload, self.context)

    def test_optional_fields_are_still_typed_and_citations_still_checked(self):
        variants = [
            ("feedback_evidence", None),
            ("behavior_evidence", [{"evidence_id": "T000002", "quote": "Preserved duplicate rows."}]),
            ("uncertainty", "none"), ("diagnosis", " "),
        ]
        for key, value in variants:
            payload = compact_payload()
            payload["conflicts"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_report(payload, self.context)

    def test_post_execution_artifacts_still_cannot_establish_decision_conditions(self):
        artifact = replace(self.context.evidence[0], evidence_id="A000001", kind="artifact",
                           availability="post_execution")
        self.context.evidence.append(artifact)
        self.payload["conflicts"][0]["decision_evidence"] = [{"evidence_id": artifact.evidence_id, "quote": artifact.text}]
        validate_report(self.payload, self.context)

    def test_optional_coverage_notes_do_not_allow_hiding_known_gaps(self):
        self.context.limitations.append("Attachment unavailable")
        validate_report(self.payload, self.context)
        self.payload["coverage_notes"] = ["附件无法读取。"]
        validate_report(self.payload, self.context)
        self.context.trajectory_complete = False
        self.payload.update(status="no_supported_conflict", conflicts=[])
        validate_report(self.payload, self.context)
        self.payload["status"] = "insufficient_evidence"
        self.assertEqual(validate_report(self.payload, self.context).conflicts, [])

    def test_empty_report_needs_only_three_fields_when_no_gaps_are_recorded(self):
        payload = {"status": "no_supported_conflict", "summary": "未找到使用冲突。", "conflicts": []}
        self.assertEqual(validate_report(payload, self.context).status, "no_supported_conflict")


class ArchivedReportTests(unittest.TestCase):
    def setUp(self):
        self.context = make_context()
        self.v2 = asdict(validate_report(compact_payload(), self.context))
        self.v1 = make_payload()
        self.v1.update(run_id=self.context.run_id, input_digest=self.context.input_digest,
                       semantic_status="candidate_diagnosis", schema_version=1)
        self.v1["conflicts"][0]["conflict_id"] = "old-candidate-name"
        self.v1["conflicts"][0]["target"] = dict(self.v2["conflicts"][0]["target"])

    def test_v1_preserves_identity_span_and_all_four_explanations_without_mutating_archive(self):
        before = copy.deepcopy(self.v1)
        report = validate_saved_report(self.v1, self.context)
        conflict = report.conflicts[0]
        self.assertEqual(conflict.conflict_id, "old-candidate-name")
        self.assertEqual(conflict.trajectory_range, self.v1["conflicts"][0]["trajectory_range"])
        for key in ("applicability_reason", "observed_behavior", "expected_behavior", "feedback_assessment"):
            self.assertIn(self.v1["conflicts"][0][key], conflict.diagnosis)
        self.assertEqual(self.v1, before)
        # Converting an archive to the new representation must not lose its stable selection ID.
        reloaded = validate_saved_report(asdict(report), self.context)
        self.assertEqual(asdict(reloaded), asdict(report))

    def test_both_archive_versions_reject_changed_bindings_references_and_ranges(self):
        for saved in (self.v1, self.v2):
            for key in ("skill_id", "relative_path", "source_path", "version"):
                report = copy.deepcopy(saved)
                report["conflicts"][0]["target"][key] = "changed"
                with self.subTest(version=saved["schema_version"], field=key), self.assertRaises(ValueError):
                    validate_saved_report(report, self.context)
            report = copy.deepcopy(saved)
            report["conflicts"][0]["behavior_evidence"][0]["quote"] = "invented"
            with self.assertRaisesRegex(ValueError, "exact substring"):
                validate_saved_report(report, self.context)
            report = copy.deepcopy(saved)
            report["conflicts"][0]["trajectory_range"] = {"start": "T000003", "end": "T000003"}
            with self.assertRaisesRegex(ValueError, "trajectory_range"):
                validate_saved_report(report, self.context)
            report = copy.deepcopy(saved)
            report["conflicts"].append(copy.deepcopy(report["conflicts"][0]))
            with self.assertRaisesRegex(ValueError, "duplicate conflict"):
                validate_saved_report(report, self.context)

    def test_report_identity_version_and_semantic_status_cannot_be_spoofed(self):
        for key, value in (("run_id", "other"), ("input_digest", "other"), ("schema_version", 99),
                           ("schema_version", True), ("semantic_status", "verified_correct")):
            report = copy.deepcopy(self.v2)
            report[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_saved_report(report, self.context)


if __name__ == "__main__":
    unittest.main()
