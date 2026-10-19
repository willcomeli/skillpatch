"""Offline correction loops for run-scoped quotes and bounded evidence paging."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skillpatch.context import refresh_input_digest
from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.model_client import DiagnosisConfig
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.stages.induction import require_behavior_references
from skillpatch.stages.live_discovery import diagnose_live_context
from skillpatch.stages.patch_evaluation import validate_evaluation
from skillpatch.storage import write_json
from test_discovery_validation import make_context, make_payload


def final(payload):
    return {"role": "assistant", "content": json.dumps(payload)}


class RecordingClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def turn(self, config, messages, environment, output, **kwargs):
        call = {"config": config, "messages": copy.deepcopy(messages), **kwargs}
        self.calls.append(call)
        write_json(output / "model_call.json", {"usage": {"total_tokens": 1}})
        response = next(self.responses)
        return response(call) if callable(response) else response


class EmptyInspector:
    async def prepare(self):
        return {"resources": [], "limitations": []}

    def definitions(self):
        return [{"type": "function", "function": {"name": "read_text", "description": "Read a resource",
                "parameters": {"type": "object", "properties": {"resource_id": {"type": "string"}},
                               "required": ["resource_id"], "additionalProperties": False}}}]

    async def call(self, name, arguments):
        raise AssertionError("No live resource inspection is required by this fixture")


def context(name, text, *, identifier="T000021", extra=()):
    result = DiscoveryInput(name, "fixture", "task", "task-hash", "skill-hash", True, [
        EvidenceItem("Q000001", "task", "Preserve the input and check the generated output.", "task.md"),
        EvidenceItem(identifier, "trajectory", text, f"{name}/trace.jsonl"),
        *extra,
    ], [], "")
    refresh_input_digest(result)
    return result


def evaluation(baseline_quote, candidate_quote="The output check passed."):
    return {
        "baseline_status": "present", "target_status": "repaired",
        "explanation": "The baseline omitted the required check; the candidate performed it.",
        "baseline_evidence": [{"evidence_id": "T000021", "quote": baseline_quote}],
        "candidate_evidence": [{"evidence_id": "T000021", "quote": candidate_quote}],
        "reverse_conflicts": [], "revision_action": "none",
    }


def correction_text(call):
    return next(message["content"] for message in reversed(call["messages"])
                if message.get("role") == "user"
                and isinstance(message.get("content"), str)
                and "Validation error:" in message["content"])


class RepairFeedbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prompt = self.root / "prompt.md"
        prompt.write_text("Inspect the immutable evidence and return the required JSON in English.")
        self.config = DiagnosisConfig("fixture", "https://invalid.test", "FIXTURE_KEY", prompt,
                                      max_rounds=50, max_tool_calls=100, max_report_repairs=5)
        self.output = self.root / "session"

    def read(self, relative):
        return json.loads((self.output / relative).read_text())

    def run_evaluation(self, client, contexts, **kwargs):
        def validate(payload, views):
            validate_evaluation(payload, views["baseline"].context, views["candidate"].context, views=views)
        return recorded_evidence_session(kwargs.pop("config", self.config), "Evaluate the original target.",
            contexts, self.output, {}, client=client, validate=validate, **kwargs)

    def test_format_prefix_repair_exposes_exact_text_without_changing_archive(self):
        raw = "+1|import pandas as pd\n+2|from collections import defaultdict"
        stripped = "import pandas as pd\nfrom collections import defaultdict"
        contexts = {"baseline": context("baseline", raw),
                    "candidate": context("candidate", "The output check passed.")}
        before = copy.deepcopy(contexts)
        invalid, valid = evaluation(stripped), evaluation(raw)

        def corrected(call):
            message = correction_text(call)
            self.assertIn("+1|import pandas as pd", message)
            self.assertIn("+2|from collections import defaultdict", message)
            self.assertIn("baseline", message)
            self.assertIn("T000021", message)
            return final(valid)

        client = RecordingClient([final(invalid), corrected])
        result = self.run_evaluation(client, contexts)

        self.assertEqual(result["baseline_evidence"], valid["baseline_evidence"])
        self.assertEqual(contexts, before)
        self.assertEqual(self.read("collections/baseline/raw_evidence.json")[1]["text"], raw)
        state = self.read("session.json")
        self.assertEqual((state["model_calls"], state["tool_calls"], state["report_repairs"]), (2, 0, 1))
        self.assertEqual(self.read("collections/baseline/evidence_access.json")["presented_ranges"]["T000021"],
                         [[0, len(raw)]])
        self.assertEqual(self.read("calls/001/repair_feedback.json")["message"], correction_text(client.calls[1]))

    def test_source_quote_does_not_become_valid_for_same_id_in_baseline(self):
        source_quote = "The original run searched the obsolete index."
        baseline_quote = "The baseline run never checked the returned document."
        contexts = {"source": context("source", source_quote),
                    "baseline": context("baseline", baseline_quote),
                    "candidate": context("candidate", "The output check passed.")}
        invalid, valid = evaluation(source_quote), evaluation(baseline_quote)

        def still_wrong(call):
            self.assertIn(baseline_quote, correction_text(call))
            self.assertIn("baseline", correction_text(call))
            return final(invalid)

        client = RecordingClient([final(invalid), still_wrong, final(valid)])
        self.run_evaluation(client, contexts)

        self.assertEqual(self.read("session.json")["report_repairs"], 2)
        self.assertEqual(self.read("payload.json")["baseline_evidence"][0]["quote"], baseline_quote)
        for call in ("001", "002"):
            self.assertIn("not an exact quote from this run", self.read(f"calls/{call}/validation_error.json")["error"])
        self.assertEqual(self.read("collections/source/raw_evidence.json")[1]["text"], source_quote)

    def test_undisplayed_cited_passage_gets_page_hint_and_requires_actual_tool_read(self):
        unread_marker = "UNREAD_CITED_PASSAGE"
        raw = "B" * 28280 + unread_marker
        contexts = {"source": context("source", raw, identifier="T000071", extra=[
            EvidenceItem("A000001", "artifact", "X" * 90000, "large.xml")])}
        original = copy.deepcopy(contexts)
        payload = {"status": "candidate", "reason": "The source requires a condition clarification.",
                   "when_to_use": "When the task requires preserving the input.",
                   "how_to_interpret": "Check the preserved input before finalizing output."}

        def read_remainder(call):
            message = correction_text(call)
            self.assertIn("T000071", message)
            self.assertIn("read_evidence", message)
            self.assertRegex(message, r'"start_char"\s*:\s*20000')
            self.assertNotIn(unread_marker, message)
            access = self.read("collections/source/evidence_access.json")
            self.assertEqual(access["presented_ranges"]["T000071"], [[0, 20000]])
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "read-tail", "type": "function", "function": {"name": "read_evidence",
                "arguments": json.dumps({"collection": "source", "evidence_id": "T000071",
                                          "start_char": 20000, "max_chars": 20000})}}]}

        def validate(value, views):
            require_behavior_references([{"evidence_id": "T000071", "quote": unread_marker}],
                                        views["source"], "source")

        client = RecordingClient([final(payload), read_remainder, final(payload)])
        recorded_evidence_session(replace(self.config, max_input_chars=80000), "Inspect the source conflict.",
            contexts, self.output, {}, client=client, priority_ids={"source": ["T000071"]}, validate=validate)

        state = self.read("session.json")
        self.assertEqual((state["model_calls"], state["tool_calls"], state["report_repairs"]), (3, 1, 1))
        self.assertEqual(self.read("collections/source/evidence_access.json")["presented_ranges"]["T000071"],
                         [[0, len(raw)]])
        self.assertEqual(contexts, original)
        self.assertEqual(self.read("collections/source/raw_evidence.json")[1]["text"], raw)

    def test_five_corrections_remain_bounded_when_model_repeats_invalid_quote(self):
        contexts = {"baseline": context("baseline", "+1|first\n+2|second"),
                    "candidate": context("candidate", "The output check passed.")}
        invalid = evaluation("first\nsecond")
        fallback = {"baseline_status": "uncertain", "target_status": "uncertain",
                    "explanation": "The evidence could not be validated because the correction budget was exhausted.", "baseline_evidence": [],
                    "candidate_evidence": [], "reverse_conflicts": [],
                    "revision_action": "none"}
        client = RecordingClient([final(invalid)] * 6)
        result = self.run_evaluation(client, contexts, budget_fallback=lambda cause: fallback)

        state = self.read("session.json")
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual((state["model_calls"], state["tool_calls"], state["report_repairs"]), (6, 0, 5))
        self.assertEqual(result["target_status"], "uncertain")
        self.assertEqual(len(client.calls), 6)
        self.assertTrue(all("+1|first" in correction_text(call) for call in client.calls[1:]))

    def test_correction_survives_context_compaction_without_extra_tool_allowance(self):
        raw = "+1|first\n+2|second"
        contexts = {"baseline": context("baseline", raw),
                    "candidate": context("candidate", "The output check passed.")}
        invalid, valid = evaluation("first\nsecond"), evaluation(raw)
        invalid["explanation"] = "Long invalid explanation. " * 2000

        def corrected(call):
            self.assertIn("+1|first", correction_text(call))
            self.assertIn("+2|second", correction_text(call))
            return final(valid)

        client = RecordingClient([final(invalid), corrected])
        config = replace(self.config, max_input_chars=20000, max_tool_calls=1)
        self.run_evaluation(client, contexts, config=config)

        state = self.read("session.json")
        self.assertGreaterEqual(state["context_compactions"], 1)
        self.assertEqual(state["tool_calls"], 0)
        self.assertEqual(state["max_tool_calls"], 1)
        self.assertLessEqual(state["input_chars"], config.max_input_chars)
        self.assertEqual(self.read("collections/baseline/raw_evidence.json")[1]["text"], raw)


class LiveRepairFeedbackIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_report_repair_keeps_prefixes_after_context_compaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            prompt.write_text("Inspect the evidence and return the required JSON report in English.")
            config = DiagnosisConfig("fixture", "https://invalid.test", "FIXTURE_KEY", prompt,
                                     max_input_chars=30000, max_report_repairs=5)
            output = root / "live"
            source = make_context()
            raw = "+1|first changed line\n+2|second changed line"
            source.evidence = [replace(item, text=raw) if item.evidence_id == "T000002" else item
                               for item in source.evidence]
            refresh_input_digest(source)
            valid = make_payload()
            valid["coverage_notes"] = ["Inspection uses a normalized view of the archived evidence."]
            valid["conflicts"][0]["behavior_evidence"][0]["quote"] = raw
            invalid = copy.deepcopy(valid)
            invalid["conflicts"][0]["behavior_evidence"][0]["quote"] = "first changed line\nsecond changed line"
            invalid["summary"] = "Long invalid summary. " * 2000

            def corrected(call):
                text = next(message["content"] for message in reversed(call["messages"])
                    if message["role"] == "user" and isinstance(message["content"], str)
                    and message["content"].startswith("The previous diagnosis response"))
                self.assertIn("+1|first changed line", text)
                self.assertIn("+2|second changed line", text)
                self.assertIn("not an exact substring", text)
                return final(valid)

            client = RecordingClient([final(invalid), corrected])
            state = await diagnose_live_context(source, config, EmptyInspector(), output, {}, client=client)

            self.assertEqual(state["status"], "completed", state)
            self.assertEqual((state["model_calls"], state["tool_calls"], state["report_repairs"]), (2, 0, 1))
            self.assertGreaterEqual(state["context_compactions"], 1)
            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["conflicts"][0]["behavior_evidence"][0]["quote"], raw)
            self.assertEqual(next(item.text for item in source.evidence if item.evidence_id == "T000002"), raw)


if __name__ == "__main__":
    unittest.main()
