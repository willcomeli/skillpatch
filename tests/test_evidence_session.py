"""Offline tests of bounded Stage 2 evidence requests and presentation provenance."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from skillpatch.context import refresh_input_digest
from skillpatch.evidence_session import recorded_evidence_session
from skillpatch.model_client import DiagnosisConfig, DiagnosisProtocolError
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.storage import write_json


def final(payload=None):
    return {"role": "assistant", "content": json.dumps(payload or {"status": "uncertain"})}


def tool(identifier, name="read_evidence", **arguments):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def batch(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


class Client:
    def __init__(self, turns):
        self.turns, self.calls = iter(turns), []

    def turn(self, config, messages, environment, output, **kwargs):
        self.calls.append({"config": config, "messages": copy.deepcopy(messages), **kwargs})
        write_json(output / "model_call.json", {"usage": {"total_tokens": 11}})
        value = next(self.turns)
        if isinstance(value, Exception):
            raise value
        return value(self.calls[-1]) if callable(value) else value


class LegacyClient:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def complete(self, config, messages, environment, output):
        self.calls += 1
        return self.value


class EvidenceSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        prompt = self.root / "prompt.md"
        prompt.write_text("Return the supported judgment in a JSON object.")
        self.config = DiagnosisConfig("test-model", "https://invalid.test/v1", "TEST_KEY", prompt)
        self.output = self.root / "session"

    def context(self, suffix="source", extra=()):
        result = DiscoveryInput(suffix, "foreign", "task", "task-hash", "skill-hash", True, [
            EvidenceItem("Q000001", "task", "Preserve all input rows.", "task.md"),
            EvidenceItem("S000001", "skill", "Remove duplicates when unique rows are required.",
                         "skill.md", skill_id="table", relative_path="table/SKILL.md"),
            EvidenceItem("T000001", "trajectory", f"{suffix}: Removed duplicate rows.", "trace.jsonl"),
            EvidenceItem("F000001", "feedback", "The row count check failed.", "result.json"),
            *extra], [], "")
        refresh_input_digest(result)
        return result

    def run_session(self, client, *, contexts=None, **kwargs):
        return recorded_evidence_session(kwargs.pop("config", self.config), "Diagnose this target.",
            contexts or {"source": self.context()}, self.output, kwargs.pop("environment", {}),
            client=client, **kwargs)

    def read(self, relative):
        return json.loads((self.output / relative).read_text())

    def test_normalized_copy_and_small_fixture_fully_presented(self):
        context = self.context(extra=[EvidenceItem("T000002", "trajectory", "\x1b[31mSaved\x1b[0m output.", "t")])
        before = copy.deepcopy(context)
        captured = {}
        def validate(payload, views):
            captured.update(views)
            self.assertTrue(all(views["source"].fully_presented(item) for item in views["source"].context.evidence))
        self.run_session(Client([final()]), contexts={"source": context}, validate=validate)
        self.assertEqual(context, before)
        self.assertEqual(captured["source"].context.evidence[-1].text, "Saved output.")
        self.assertNotEqual(context.input_digest, captured["source"].context.input_digest)
        self.assertEqual(self.read("collections/source/raw_evidence.json")[-1]["text"], before.evidence[-1].text)
        self.assertEqual(self.read("session.json")["status"], "completed")
        self.assertEqual(self.read("usage.json")["total_tokens"], 11)
        self.assertTrue(self.read("prompt_version.json")["runtime_sha256"])

    def test_all_limitations_visible_in_first_request(self):
        context = self.context()
        context.limitations = [f"limitation {i}" for i in range(7)]
        client = Client([final()])
        self.run_session(client, contexts={"source": context})
        body = json.dumps(client.calls[0]["messages"])
        for limitation in context.limitations:
            self.assertIn(limitation, body)

    def test_complete_archive_fits_including_long_skill_and_trajectory(self):
        context = self.context(extra=[
            EvidenceItem("S000002", "skill", "Long skill instruction. " * 1200, "long-skill.md"),
            EvidenceItem("T000002", "trajectory", "Verified another row. " * 1400, "trace.jsonl")])
        captured = {}
        self.run_session(Client([final()]), contexts={"source": context},
                         validate=lambda payload, views: captured.update(views))
        self.assertTrue(all(captured["source"].fully_presented(item) for item in context.evidence))
        self.assertEqual(self.read("collections/source/evidence_access.json")["mode"], "full")

    def test_large_archive_still_presents_medium_trajectory_in_full(self):
        context = self.context(extra=[
            EvidenceItem("S000002", "skill", "X" * 90000, "schema.xsd"),
            EvidenceItem("T000002", "trajectory", "Executed another step. " * 350, "trace.jsonl")])
        captured = {}
        self.run_session(Client([final()]), contexts={"source": context},
                         config=replace(self.config, max_input_chars=80000),
                         validate=lambda payload, views: captured.update(views))
        self.assertNotIn("S000002", captured["source"].presented)
        self.assertTrue(captured["source"].fully_presented(context.evidence[-1]))

    def test_tools_scope_same_id_to_selected_collection_and_reply_in_batch(self):
        client = Client([batch(
            tool("b", collection="baseline", evidence_id="T000001"),
            tool("c", collection="candidate", evidence_id="T000001")), final()])
        self.run_session(client, contexts={name: self.context(name) for name in ("baseline", "candidate")})
        schemas = client.calls[0]["tools"]
        self.assertEqual({item["function"]["name"] for item in schemas},
                         {"read_evidence", "search_evidence", "read_evidence_index"})
        for item in schemas:
            params = item["function"]["parameters"]
            self.assertIn("collection", params["required"])
            self.assertEqual(params["properties"]["collection"]["enum"], ["baseline", "candidate"])
        messages = client.calls[1]["messages"]
        position = next(i for i, message in enumerate(messages) if message.get("tool_calls"))
        self.assertEqual([item["role"] for item in messages[position:position + 3]], ["assistant", "tool", "tool"])
        for offset, name in enumerate(("baseline", "candidate"), 1):
            reply = json.loads(messages[position + offset]["content"])
            self.assertEqual(reply["collection"], name)
            self.assertIn(name + ":", reply["result"]["text"])

    def test_missing_collection_and_cross_collection_id_are_local_tool_errors(self):
        contexts = {"baseline": self.context("baseline"), "candidate": self.context("candidate", [
            EvidenceItem("A000001", "artifact", "candidate-only", "a")])}
        client = Client([batch(tool("a", evidence_id="T000001"),
                              tool("b", collection="baseline", evidence_id="A000001")), final()])
        self.run_session(client, contexts=contexts)
        replies = [json.loads(item["content"]) for item in client.calls[1]["messages"] if item["role"] == "tool"]
        self.assertTrue(all(reply["evidence_displayed"] is False for reply in replies))
        self.assertEqual(self.read("session.json")["tool_calls"], 2)

    def test_large_auxiliary_xml_index_only_and_search_excerpts_count_as_read(self):
        xml = "<schema>" + "X" * 45000 + "MATCH-HERE" + "Y" * 30000 + "</schema>"
        context = self.context(extra=[EvidenceItem("S000002", "skill", xml, "schema.xsd", relative_path="table/schema.xsd")])
        client = Client([batch(tool("index", "read_evidence_index", collection="source"),
                              tool("search", "search_evidence", collection="source", evidence_id="S000002",
                                   query="MATCH-HERE", context_chars=3)), final()])
        seen = {}
        self.run_session(client, contexts={"source": context}, priority_ids={"source": ["S000001"]},
                         config=replace(self.config, max_input_chars=30000),
                         validate=lambda payload, views: seen.update(views))
        self.assertNotIn("MATCH-HERE", json.dumps(client.calls[0]["messages"]))
        self.assertIn("MATCH-HERE", json.dumps(client.calls[1]["messages"]))
        view = seen["source"]
        self.assertFalse(view.fully_presented(view.context.evidence[-1]))
        self.assertEqual(view.presented["S000002"], [(45005, 45021)])

    def test_oversized_tool_batch_compacted_without_claiming_unseen_page(self):
        context = self.context(extra=[EvidenceItem("A000001", "artifact", "unseen" * 7000, "huge.txt")])
        client = Client([batch(tool("large", collection="source", evidence_id="A000001", max_chars=20000)), final()])
        seen = {}
        self.run_session(client, contexts={"source": context}, config=replace(self.config, max_input_chars=14000),
                         validate=lambda payload, views: seen.update(views))
        self.assertNotIn("A000001", seen["source"].presented)
        self.assertEqual(self.read("session.json")["context_compactions"], 1)
        self.assertNotIn("tool", [item["role"] for item in client.calls[1]["messages"]])
        self.assertEqual(self.read("collections/source/evidence_access.json")["queued_ranges"], {})

    def test_kept_tool_page_is_visible_only_after_next_model_request(self):
        context = self.context(extra=[EvidenceItem("A000001", "artifact", "archived" * 4000, "huge.txt")])
        def inspect_request(call):
            access = self.read("collections/source/evidence_access.json")
            self.assertNotIn("A000001", access["presented_ranges"])
            self.assertIn("archived", json.dumps(call["messages"]))
            return final()
        client = Client([batch(tool("page", collection="source", evidence_id="A000001", start_char=10, max_chars=40)), inspect_request])
        self.run_session(client, contexts={"source": context}, config=replace(self.config, max_input_chars=20000))
        self.assertEqual(self.read("collections/source/evidence_access.json")["presented_ranges"]["A000001"], [[10, 50]])

    def test_tool_budget_forces_final_and_extra_calls_receive_errors(self):
        client = Client([batch(tool("a", collection="source", evidence_id="T000001"),
                              tool("b", collection="source", evidence_id="Q000001")), final()])
        self.run_session(client, config=replace(self.config, max_tool_calls=1))
        self.assertEqual(client.calls[1]["tool_choice"], "none")
        replies = [json.loads(item["content"]) for item in client.calls[1]["messages"] if item["role"] == "tool"]
        self.assertIn("budget exhausted", replies[1]["error"])
        self.assertEqual(self.read("session.json")["tool_calls"], 1)

    def test_invalid_final_can_be_corrected_within_repair_budget(self):
        client = Client([final({"bad": True}), final()])
        def validate(payload, views):
            if "status" not in payload:
                raise ValueError("status required")
        self.run_session(client, validate=validate)
        self.assertIn("status required", client.calls[1]["messages"][-1]["content"])
        self.assertEqual(self.read("session.json")["report_repairs"], 1)
        self.assertTrue((self.output / "calls/001/validation_error.json").is_file())

    def test_protocol_error_repair_and_http_error_not_retried(self):
        client = Client([DiagnosisProtocolError("missing valid assistant"), final()])
        self.run_session(client)
        self.assertEqual(len(client.calls), 2)
        self.output = self.root / "transport"
        transport = Client([ValueError("HTTP 429"), final()])
        with self.assertRaisesRegex(ValueError, "HTTP 429"):
            self.run_session(transport)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.read("session.json")["status"], "failed")

    def test_repair_exhaustion_uses_validated_deterministic_fallback(self):
        seen = []
        def validate(payload, views):
            seen.append(payload)
            if "status" not in payload:
                raise ValueError("missing status")
        client = Client([final({"bad": 1}), final({"bad": 2})])
        result = self.run_session(client, config=replace(self.config, max_report_repairs=1), validate=validate,
                                  budget_fallback=lambda cause: {"status": "insufficient_evidence", "reason": cause})
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(len(seen), 3)
        self.assertIn("report_repair_budget_exhausted", self.read("session.json")["cause"])
        self.assertEqual(self.read("budget_fallback.json")["origin"], "deterministic_program_fallback")

    def test_too_large_metadata_fallback_has_normalized_views_without_model_call(self):
        context = self.context()
        context.limitations = ["Missing verifier details " * 1000]
        client = Client([])
        seen = {}
        result = self.run_session(client, contexts={"source": context}, config=replace(self.config, max_input_chars=10000),
            validate=lambda payload, views: seen.update(views), budget_fallback=lambda cause: {"status": "uncertain"})
        self.assertEqual(result, {"status": "uncertain"})
        self.assertEqual(client.calls, [])
        self.assertIsNot(seen["source"].context, context)
        self.assertEqual(self.read("collections/source/evidence_access.json")["presented_ranges"], {})

    def test_budget_fallback_cannot_claim_supported_result(self):
        with self.assertRaisesRegex(ValueError, "cannot assert"):
            self.run_session(Client([]), config=replace(self.config, max_input_chars=1000),
                             budget_fallback=lambda cause: {"status": "candidate"})
        self.assertEqual(self.read("session.json")["status"], "failed")
        self.assertFalse((self.output / "payload.json").exists())

    def test_legacy_validation_failure_single_call_retains_raw_payload(self):
        client = LegacyClient('{"bad":true}')
        def reject(payload, views):
            raise ValueError("schema invalid")
        with self.assertRaisesRegex(ValueError, "schema invalid"):
            self.run_session(client, validate=reject)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.read("model_output.json"), {"bad": True})

    def test_credential_output_redacted_before_rejection(self):
        secret = "private-test-api-value-789"
        client = LegacyClient(json.dumps({"status": "candidate", "text": secret}))
        with self.assertRaisesRegex(ValueError, "credential value"):
            self.run_session(client, environment={"TEST_KEY": secret})
        self.assertNotIn(secret, (self.output / "model_output.txt").read_text())
        self.assertFalse((self.output / "payload.json").exists())
        self.assertEqual(client.calls, 1)

    def test_session_deadline_limits_request_timeout_and_fallback_is_uncertain(self):
        clock = [10.0]
        def consume_budget(call):
            self.assertEqual(call["config"].timeout_sec, 7)
            clock[0] = 18.0
            return final({"status": "candidate"})
        client = Client([consume_budget])
        with patch("skillpatch.evidence_session.time.monotonic", side_effect=lambda: clock[0]):
            result = self.run_session(client, session_timeout_sec=7,
                                      budget_fallback=lambda cause: {"status": "uncertain", "reason": cause})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.read("session.json")["cause"], "session_time_exhausted")
        self.assertEqual(len(client.calls), 1)

    def test_time_budget_only_exceeds_old_call_tool_and_repair_limits(self):
        turns = [batch(tool(f"a-{i}", collection="source", evidence_id="T000001"),
                       tool(f"b-{i}", collection="source", evidence_id="Q000001"))
                 for i in range(52)]
        turns += [DiagnosisProtocolError("missing valid assistant") for _ in range(3)]
        turns += [final({"bad": True}) for _ in range(3)]
        turns += [final({"status": "supported"}), AssertionError("valid output must stop the session")]
        client = Client(turns)
        def validate(payload, views):
            if "status" not in payload:
                raise ValueError("status required")
        config = replace(self.config, time_budget_only=True, max_rounds=50,
                         max_tool_calls=100, max_report_repairs=5)
        with patch("skillpatch.evidence_session.time.monotonic", return_value=10.0):
            result = self.run_session(client, config=config, validate=validate, session_timeout_sec=1800)
        self.assertEqual(result, {"status": "supported"})
        self.assertEqual(len(client.calls), 59)
        self.assertTrue(all(call["tool_choice"] == "auto" for call in client.calls))
        state = self.read("session.json")
        self.assertEqual(state["tool_calls"], 104)
        self.assertEqual(state["report_repairs"], 6)
        self.assertTrue(state["time_budget_only"])
        for name in ("max_model_calls", "max_rounds", "max_tool_calls", "max_report_repairs"):
            self.assertIsNone(state[name])
        for call in client.calls:
            statuses = [message["content"].split("\n", 1)[1] for message in call["messages"]
                        if isinstance(message.get("content"), str)
                        and message["content"].startswith("SESSION ACCESS AND BUDGETS\n")]
            status = json.loads(statuses[-1])
            self.assertIsNone(status["model_calls_remaining"])
            self.assertIsNone(status["inspection_calls_remaining"])
            self.assertEqual(status["seconds_remaining"], 1800)

    def test_time_budget_only_stops_at_deadline_during_repair(self):
        clock = [10.0]
        def consume_second(call):
            self.assertEqual(call["config"].timeout_sec, 13.0 - clock[0])
            clock[0] += 1
            return final({"bad": True})
        def validate(payload, views):
            if "status" not in payload:
                raise ValueError("status required")
        client = Client([consume_second] * 4)
        config = replace(self.config, time_budget_only=True, max_rounds=1,
                         max_tool_calls=1, max_report_repairs=0)
        with patch("skillpatch.evidence_session.time.monotonic", side_effect=lambda: clock[0]):
            result = self.run_session(client, config=config, validate=validate, session_timeout_sec=3,
                budget_fallback=lambda cause: {"status": "uncertain", "reason": cause})
        self.assertEqual(result["reason"], "session_time_exhausted")
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(self.read("session.json")["status"], "budget_exhausted")
        self.assertEqual(self.read("session.json")["report_repairs"], 2)

    def test_time_budget_only_includes_payload_validation_in_deadline(self):
        clock = [10.0]
        def validate(payload, views):
            if payload["status"] == "supported":
                clock[0] = 14.0
        with patch("skillpatch.evidence_session.time.monotonic", side_effect=lambda: clock[0]):
            result = self.run_session(Client([final({"status": "supported"})]),
                config=replace(self.config, time_budget_only=True), validate=validate, session_timeout_sec=3,
                budget_fallback=lambda cause: {"status": "uncertain", "reason": cause})
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.read("session.json")["cause"], "session_time_exhausted")

    def test_deadline_expired_during_message_copy_does_not_issue_request(self):
        clock = [10.0]
        deepcopy = copy.deepcopy
        def consume_copy_budget(value, *args, **kwargs):
            result = deepcopy(value, *args, **kwargs)
            if isinstance(value, list) and any(
                    isinstance(item, dict) and isinstance(item.get("content"), str)
                    and item["content"].startswith("SESSION ACCESS AND BUDGETS\n") for item in value):
                clock[0] = 14.0
            return result
        client = Client([final()])
        with patch("skillpatch.evidence_session.time.monotonic", side_effect=lambda: clock[0]), \
             patch("skillpatch.evidence_session.copy.deepcopy", side_effect=consume_copy_budget):
            result = self.run_session(client, config=replace(self.config, time_budget_only=True),
                session_timeout_sec=3,
                budget_fallback=lambda cause: {"status": "uncertain", "reason": cause})
        self.assertEqual(clock[0], 14.0)
        self.assertEqual(result["reason"], "session_time_exhausted")
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read("session.json")["status"], "budget_exhausted")

    def test_time_budget_only_keeps_context_limit_and_http_failures(self):
        client = Client([])
        result = self.run_session(client, config=replace(self.config, time_budget_only=True, max_input_chars=1000),
                                 budget_fallback=lambda cause: {"status": "uncertain"})
        self.assertEqual(result, {"status": "uncertain"})
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read("session.json")["cause"], "input_metadata_exceeds_context_budget")
        self.output = self.root / "transport"
        client = Client([ValueError("HTTP 402"), final()])
        with self.assertRaisesRegex(ValueError, "HTTP 402"):
            self.run_session(client, config=replace(self.config, time_budget_only=True))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.read("session.json")["status"], "failed")

    def test_time_budget_only_legacy_client_can_repair_invalid_output(self):
        class RepairingLegacyClient(LegacyClient):
            def complete(inner, config, messages, environment, output):
                inner.calls += 1
                return '{"bad":true}' if inner.calls == 1 else '{"status":"uncertain"}'
        client = RepairingLegacyClient(None)
        def validate(payload, views):
            if "status" not in payload:
                raise ValueError("status required")
        result = self.run_session(client, config=replace(self.config, time_budget_only=True), validate=validate)
        self.assertEqual(result, {"status": "uncertain"})
        self.assertEqual(client.calls, 2)
        self.assertEqual(self.read("session.json")["mode"], "completion_compatibility")

    def test_malformed_usage_cannot_mask_final_result(self):
        class MalformedUsage(Client):
            def turn(inner, config, messages, environment, output, **kwargs):
                result = super(MalformedUsage, inner).turn(config, messages, environment, output, **kwargs)
                write_json(output / "model_call.json", {"usage": {"total_tokens": "oops"}})
                return result
        self.assertEqual(self.run_session(MalformedUsage([final()])), {"status": "uncertain"})
        self.assertIsNone(self.read("usage.json")["total_tokens"])

    def test_invalid_priority_cannot_cross_collection(self):
        client = Client([])
        with self.assertRaisesRegex(ValueError, "absent collection"):
            self.run_session(client, priority_ids={"candidate": ["T000001"]})
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
