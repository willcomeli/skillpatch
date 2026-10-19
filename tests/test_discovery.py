"""Isolated Stage 1 orchestration checks; fixtures are not semantic ground truth."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from skillpatch.model_client import DiagnosisConfig
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.stages.discovery import run_discovery


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def complete(self, config, messages, environment, output):
        self.calls.append((config, copy.deepcopy(messages), dict(environment), output))
        if self.error is not None:
            raise self.error
        return json.dumps(self.response)


def context_fixture(run_id):
    return DiscoveryInput(
        run_id=run_id, benchmark="fixture", task_id="table-task", task_digest="sha256:task",
        skill_digest="sha256:skill-pack", trajectory_complete=True, limitations=[],
        input_digest="sha256:fixture-input",
        evidence=[
            EvidenceItem("Q0001", "task", "Preserve duplicate rows.", "/fixture/task.md"),
            EvidenceItem("S0001", "skill", "Remove duplicate rows only when uniqueness is required.",
                         "/fixture/table/SKILL.md", skill_id="table/SKILL.md",
                         relative_path="table/SKILL.md", version="sha256:skill-file"),
            EvidenceItem("T000001", "trajectory", "Read input rows.", "/fixture/events.jsonl", source_line=1),
            EvidenceItem("T000002", "trajectory", "Removed duplicate rows.", "/fixture/events.jsonl", source_line=2),
            EvidenceItem("F0001", "feedback", "The row count differs.", "/fixture/verifier.txt"),
        ],
    )


def payload_fixture():
    return {
        "status": "conflicts_found", "summary": "Candidate diagnosis from fixture evidence.",
        "conflicts": [{
            "conflict_id": "C000001", "type": "misuse",
            "target": {"scope": "substep", "skill_id": "table/SKILL.md", "skill_ref": "S0001",
                       "quote": "Remove duplicate rows only when uniqueness is required."},
            "applicability_reason": "The stated task requests preserving duplicates.",
            "observed_behavior": "The event reports removing duplicate rows.",
            "expected_behavior": "Preserve duplicates for this task.",
            "decision_evidence": [{"evidence_id": "Q0001", "quote": "Preserve duplicate rows."}],
            "behavior_evidence": [{"evidence_id": "T000002", "quote": "Removed duplicate rows."}],
            "feedback_evidence": [{"evidence_id": "F0001", "quote": "The row count differs."}],
            "trajectory_range": {"start": "T000001", "end": "T000002"},
            "feedback_assessment": "The feedback reports a row-count difference.",
            "uncertainty": ["Fixture claims have not received semantic verification."],
        }],
        "other_findings": [], "coverage_notes": [],
    }


def compact_payload_fixture(payload=None):
    source = copy.deepcopy(payload if payload is not None else payload_fixture())
    result = {name: source[name] for name in ("status", "summary", "conflicts")}
    for conflict in result["conflicts"]:
        for name in ("conflict_id", "applicability_reason", "observed_behavior", "expected_behavior",
                     "feedback_assessment", "trajectory_range", "feedback_evidence", "uncertainty"):
            conflict.pop(name)
        conflict["target"].pop("skill_id")
        conflict["diagnosis"] = (
            "The task requires preserving duplicates, so deduplication is inapplicable; the trajectory shows duplicates were removed. "
            "No direct feedback is cited here; attribution of the verifier failure requires further investigation."
        )
    return result


class DiscoveryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run_dir = self.root / "fresh-run"
        self.run_dir.mkdir()
        self.output = self.run_dir / "discovery" / "first"
        prompt = self.root / "prompt.md"
        prompt.write_text("Review the supplied evidence. Return the requested JSON report.\n", encoding="utf-8")
        self.config = DiagnosisConfig(model="test-model", base_url="https://not-called.invalid",
                                      api_key_env="CUSTOMVALUE", prompt_path=prompt)
        self.context = context_fixture(self.run_dir.name)
        self.client = FakeClient(response=payload_fixture())
        builder = patch("skillpatch.stages.discovery.build_discovery_input", return_value=self.context)
        self.build_input = builder.start()
        self.addCleanup(builder.stop)
        no_real_client = patch("skillpatch.stages.discovery.ChatCompletionClient",
                               side_effect=AssertionError("Tests must not construct a real API client"))
        no_real_client.start()
        self.addCleanup(no_real_client.stop)

    def discover(self, *, config=None, environment=None, prepare_only=False, client=None):
        return run_discovery(self.run_dir, config or self.config, self.output,
                             environment or {}, prepare_only=prepare_only,
                             client=client or self.client)

    def assert_no_report(self):
        self.assertFalse((self.output / "report.json").exists())
        self.assertFalse((self.output / "report.md").exists())

    def test_prepare_only_saves_inputs_without_model_call(self):
        state = self.discover(prepare_only=True)
        self.assertEqual(state["status"], "prepared")
        self.assertEqual(state["semantic_status"], "not_assessed")
        self.assertEqual(self.client.calls, [])
        self.assertTrue((self.output / "input.json").is_file())
        self.assertTrue((self.output / "messages.json").is_file())
        self.assertTrue((self.output / "prompt.md").is_file())
        self.assert_no_report()

    def test_input_limit_fails_without_truncating_evidence_or_calling_model(self):
        self.context.evidence[0] = replace(self.context.evidence[0], text="Keep this whole evidence. " * 30)
        state = self.discover(config=replace(self.config, max_input_chars=20))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "input")
        self.assertIn("max_input_chars", state["error"])
        self.assertGreater(state["input_chars"], 20)
        self.assertEqual(self.client.calls, [])
        saved = json.loads((self.output / "input.json").read_text())
        self.assertEqual(saved["evidence"][0]["text"], self.context.evidence[0].text)
        messages = json.loads((self.output / "messages.json").read_text())
        self.assertIn(self.context.evidence[0].text, messages[1]["content"])
        self.assert_no_report()

    def test_valid_response_saves_candidate_report_and_readable_markdown(self):
        state = self.discover()
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["semantic_status"], "candidate_diagnosis")
        self.assertEqual(state["conflict_count"], 1)
        self.assertEqual(len(self.client.calls), 1)
        report = json.loads((self.output / "report.json").read_text())
        self.assertEqual(report["run_id"], self.context.run_id)
        self.assertEqual(report["input_digest"], self.context.input_digest)
        self.assertEqual(report["semantic_status"], "candidate_diagnosis")
        self.assertEqual(report["conflicts"][0]["target"]["version"], "sha256:skill-file")
        markdown = (self.output / "report.md").read_text()
        self.assertIn("candidate_diagnosis", markdown)
        self.assertIn("C000001", markdown)
        self.assertIn("T000002", markdown)
        self.assertIn("Removed duplicate rows.", markdown)
        validation = json.loads((self.output / "validation.json").read_text())
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(validation["semantic_correctness"], "not_verified")
        persisted_state = json.loads((self.output / "result.json").read_text())
        self.assertEqual(persisted_state, state)

    def test_compact_response_saves_v2_report_with_bound_metadata_and_markdown(self):
        payload = compact_payload_fixture()
        state = self.discover(client=FakeClient(response=payload))

        self.assertEqual(state["status"], "completed", state)
        report = json.loads((self.output / "report.json").read_text())
        conflict = report["conflicts"][0]
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["run_id"], self.context.run_id)
        self.assertEqual(report["input_digest"], self.context.input_digest)
        self.assertEqual(conflict["conflict_id"], "C000001")
        self.assertEqual(conflict["target"]["skill_id"], "table/SKILL.md")
        self.assertEqual(conflict["target"]["version"], "sha256:skill-file")
        self.assertEqual(conflict["trajectory_range"], {"start": "T000002", "end": "T000002"})
        self.assertEqual(conflict["diagnosis"], payload["conflicts"][0]["diagnosis"])
        for field in ("feedback_evidence", "uncertainty"):
            self.assertEqual(conflict[field], [])
        for field in ("other_findings", "coverage_notes"):
            self.assertEqual(report[field], [])
        self.assertNotIn("observed_behavior", conflict)
        markdown = (self.output / "report.md").read_text()
        self.assertIn(conflict["diagnosis"], markdown)
        self.assertIn("Removed duplicate rows.", markdown)
        self.assertIn("Cited trajectory span", markdown)
        self.assertIn("does not establish complete inspection coverage", markdown)

    def test_another_collector_can_supply_the_shared_evidence_contract(self):
        received_paths = []

        def alternate_collector(path):
            received_paths.append(path)
            return replace(self.context, benchmark="another-fixture-benchmark")

        state = run_discovery(self.run_dir, self.config, self.output, {}, client=self.client,
                              input_builder=alternate_collector)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(received_paths, [self.run_dir])
        self.build_input.assert_not_called()
        saved = json.loads((self.output / "input.json").read_text())
        self.assertEqual(saved["benchmark"], "another-fixture-benchmark")

    def test_schema_failure_keeps_failed_state_and_emits_no_report(self):
        self.client.response["unexpected"] = True
        state = self.discover()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "validation")
        self.assertEqual(state["semantic_status"], "not_assessed")
        self.assertEqual(len(self.client.calls), 1)
        validation = json.loads((self.output / "validation.json").read_text())
        self.assertEqual(validation["status"], "failed")
        self.assertEqual(validation["semantic_correctness"], "not_verified")
        self.assert_no_report()

    def test_transport_failure_emits_no_report_and_is_not_retried(self):
        client = FakeClient(error=OSError("Fixture transport unavailable"))
        state = self.discover(client=client)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "model_call")
        self.assertEqual(len(client.calls), 1)
        self.assertIn("transport unavailable", state["error"])
        self.assertFalse((self.output / "validation.json").exists())
        self.assert_no_report()

    def test_existing_output_is_rejected_without_overwriting_it(self):
        self.output.mkdir(parents=True)
        marker = self.output / "existing.txt"
        marker.write_text("preserve me")
        with self.assertRaises(FileExistsError):
            self.discover()
        self.assertEqual(marker.read_text(), "preserve me")
        self.build_input.assert_not_called()
        self.assertEqual(self.client.calls, [])
        self.assertFalse((self.output / "result.json").exists())

    def test_cancellation_records_cancelled_state_without_report(self):
        client = FakeClient(error=KeyboardInterrupt())
        state = self.discover(client=client)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(state["phase"], "model_call")
        self.assertEqual(state["semantic_status"], "not_assessed")
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(state["finished_at"])
        self.assertEqual(json.loads((self.output / "result.json").read_text())["status"], "cancelled")
        self.assert_no_report()

    def test_custom_credential_in_input_is_rejected_before_saving_or_sending(self):
        value = "fixture-private-auth-material"
        self.context.evidence[0] = replace(self.context.evidence[0], text="Unexpected credential: " + value)
        state = self.discover(environment={"CUSTOMVALUE": value})
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["phase"], "input")
        self.assertEqual(self.client.calls, [])
        self.assertIn("credential", state["error"])
        for filename in ("input.json", "messages.json", "prompt.md"):
            self.assertFalse((self.output / filename).exists())
        for artifact in self.output.rglob("*"):
            if artifact.is_file():
                self.assertNotIn(value, artifact.read_text())
        self.assert_no_report()


if __name__ == "__main__":
    unittest.main()
