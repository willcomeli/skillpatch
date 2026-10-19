"""Portable diagnosis protocol tests; no benchmark, Docker, or HTTP calls."""

import asyncio
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.context import refresh_input_digest
from skillpatch.model_client import DiagnosisConfig, DiagnosisProtocolError
from skillpatch.models import EvidenceItem
from skillpatch.stages.live_discovery import diagnose_live_context
from skillpatch.storage import write_json
from test_discovery import compact_payload_fixture
from test_discovery_validation import make_context, make_payload


def tool_turn(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def tool_call(identifier="call-1", name="read_text", arguments='{"resource_id":"R1"}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def final_turn(payload=None):
    return {"role": "assistant", "content": json.dumps(payload or {
        "status": "insufficient_evidence", "summary": "Further evidence is needed.", "conflicts": [],
        "other_findings": [], "coverage_notes": ["Only available materials were inspected; attribution is unverified."],
    })}


class FakeClient:
    def __init__(self, responses, usage=None):
        self.responses = iter(responses)
        self.calls = []
        self.usage = usage

    def turn(self, config, messages, environment, output, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), "config": config, **kwargs})
        if self.usage == "malformed":
            (output / "model_call.json").write_text("not JSON")
        else:
            write_json(output / "model_call.json", {"usage": self.usage})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class FakeInspector:
    def __init__(self, outputs=(), catalog=None):
        self.outputs = iter(outputs)
        self.calls = []
        self.catalog = catalog or {"resources": [{"resource_id": "R1"}], "limitations": []}

    async def prepare(self):
        return self.catalog

    def definitions(self):
        return [{"type": "function", "function": {"name": "read_text", "description": "Read a resource",
                "parameters": {"type": "object", "properties": {"resource_id": {"type": "string"}},
                               "required": ["resource_id"], "additionalProperties": False}}}]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        result = next(self.outputs)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result()
        return result


class LiveDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "diagnosis"
        prompt = self.root / "prompt.md"
        prompt.write_text("Inspect evidence, then return a strict JSON diagnosis.")
        self.config = DiagnosisConfig("fixture", "https://provider.invalid", "KEY", prompt, max_report_repairs=0)
        self.context = make_context()
        refresh_input_digest(self.context)
        self.environment = {"KEY": "fixture-private-credential-very-long"}
        self.artifact = EvidenceItem("A000001", "artifact", "The original table contains duplicate records.",
                                     str(self.output / "artifacts" / "table.csv"), availability="initial",
                                     source_role="task_material", observation={"tool": "read_text"})

    def result(self, *items, **changes):
        result = {"text": "Read a table excerpt.", "evidence": list(items), "images": [], "truncated": False}
        result.update(changes)
        return result

    async def run_diagnosis(self, client, inspector=None, **kwargs):
        return await diagnose_live_context(self.context, self.config, inspector or FakeInspector(),
                                            self.output, self.environment, client=client, **kwargs)

    def read(self, filename):
        return json.loads((self.output / filename).read_text())

    async def test_tool_evidence_is_delivered_and_bound_to_final_report(self):
        payload = make_payload()
        payload["coverage_notes"] = ["The observation followed verification; the initial file hash was checked."]
        payload["conflicts"][0]["decision_evidence"] = [{"evidence_id": "A000001", "quote": "contains duplicate records"}]
        inspector = FakeInspector([self.result(self.artifact)])
        client = FakeClient([tool_turn(tool_call()), final_turn(payload)],
                            usage={"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12})
        initial_digest = self.context.input_digest
        original_events = [item for item in self.context.evidence if item.kind == "trajectory"]

        state = await self.run_diagnosis(client, inspector)

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(state["model_calls"], 2)
        self.assertNotEqual(state["input_digest"], initial_digest)
        self.assertEqual(self.read("report.json")["input_digest"], self.context.input_digest)
        tool_message = client.calls[1]["messages"][-1]
        observation = json.loads(tool_message["content"])
        self.assertEqual(observation["evidence"][0]["text"], self.artifact.text)
        self.assertEqual(observation["evidence"][0]["availability"], "initial")
        self.assertEqual(tool_message["tool_call_id"], "call-1")
        self.assertEqual(original_events, [item for item in self.context.evidence if item.kind == "trajectory"])
        self.assertEqual(state["usage"]["total_tokens"], 24)
        self.assertEqual(self.read("validation.json")["semantic_correctness"], "not_verified")
        journal = self.read("tool_calls.jsonl")
        self.assertEqual(journal["result"]["evidence"][0]["evidence_id"], "A000001")

    async def test_compact_tool_report_rejects_bad_quote_then_saves_exact_repair(self):
        self.config = replace(self.config, max_report_repairs=1)
        valid = compact_payload_fixture(make_payload())
        valid["coverage_notes"] = ["The observation followed verification; the initial file hash was checked."]
        valid["conflicts"][0]["decision_evidence"] = [
            {"evidence_id": "A000001", "quote": "contains duplicate records"}]
        invalid = copy.deepcopy(valid)
        invalid["conflicts"][0]["decision_evidence"][0]["quote"] = "Contains duplicate records"
        inspector = FakeInspector([self.result(self.artifact)])
        client = FakeClient([tool_turn(tool_call()), final_turn(invalid), final_turn(valid)])

        state = await self.run_diagnosis(client, inspector)

        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["model_calls"], 3)
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(state["report_repairs"], 1)
        self.assertEqual(len(inspector.calls), 1)
        self.assertEqual(self.read("calls/002/validation.json")["status"], "failed")
        self.assertIn("not an exact substring", client.calls[2]["messages"][-1]["content"])
        report = self.read("report.json")
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["conflicts"][0]["conflict_id"], "C000001")
        self.assertEqual(report["conflicts"][0]["decision_evidence"],
                         valid["conflicts"][0]["decision_evidence"])
        self.assertEqual(report["conflicts"][0]["feedback_evidence"], [])
        self.assertEqual(self.read("validation.json")["semantic_correctness"], "not_verified")

    async def test_language_is_guidance_not_a_report_gate(self):
        payload = make_payload()
        payload["summary"] = "发现一处候选冲突。"
        state = await self.run_diagnosis(FakeClient([final_turn(payload)]))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["report_repairs"], 0)
        self.assertEqual(self.read("report.json")["summary"], payload["summary"])

    async def test_bad_artifact_batch_is_rejected_atomically_before_next_turn(self):
        for index, invalid in enumerate((replace(self.artifact, evidence_id="A1"),
                                         replace(self.artifact, kind="trajectory"), self.artifact,
                                         replace(self.artifact, availability="invented"))):
            with self.subTest(index=index):
                self.output = self.root / f"bad-{index}"
                self.context = make_context()
                original = list(self.context.evidence)
                inspector = FakeInspector([self.result(self.artifact, invalid)])
                client = FakeClient([tool_turn(tool_call())])
                state = await self.run_diagnosis(client, inspector)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(self.context.evidence, original)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(self.read("tool_calls.jsonl")["status"], "rejected")

    async def test_inspection_cannot_overwrite_existing_evidence_id(self):
        self.context.evidence.append(self.artifact)
        inspector = FakeInspector([self.result(self.artifact)])
        client = FakeClient([tool_turn(tool_call())])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(sum(item.evidence_id == "A000001" for item in self.context.evidence), 1)

    async def test_bad_arguments_and_unknown_tool_produce_recorded_tool_errors(self):
        client = FakeClient([tool_turn(tool_call(arguments='{"x":1,"x":2}'),
                                      tool_call("call-2", name="undeclared_tool")), final_turn()])
        inspector = FakeInspector()
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(inspector.calls, [])
        messages = client.calls[1]["messages"]
        errors = [json.loads(item["content"])["error"] for item in messages if item["role"] == "tool"]
        self.assertEqual(errors, ["ValueError", "ValueError"])
        self.assertFalse(any(item.kind == "artifact" for item in self.context.evidence))

    async def test_inspector_argument_error_is_a_tool_observation_not_fake_evidence(self):
        inspector = FakeInspector([ValueError("resource_id must name a catalog entry")])
        client = FakeClient([tool_turn(tool_call()), final_turn()])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "completed")
        message = json.loads(client.calls[1]["messages"][-1]["content"])
        self.assertEqual(message["error"], "ValueError")
        self.assertEqual(message["evidence"], [])

    async def test_tool_budget_limits_actual_access_and_requests_final_json(self):
        self.config = replace(self.config, max_tool_calls=1)
        inspector = FakeInspector([self.result(self.artifact)])
        client = FakeClient([tool_turn(tool_call(), tool_call("call-2")), final_turn()])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(len(inspector.calls), 1)
        self.assertEqual(state["tool_calls"], 1)
        self.assertEqual(client.calls[1]["tool_choice"], "none")
        messages = client.calls[1]["messages"]
        responses = [json.loads(item["content"]) for item in messages if item["role"] == "tool"]
        self.assertEqual(responses[-1]["error"], "tool_budget_exhausted")

    async def test_round_budget_reserves_final_turn_and_rejects_more_tools(self):
        self.config = replace(self.config, max_rounds=1)
        inspector = FakeInspector()
        client = FakeClient([tool_turn(tool_call())])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(client.calls[0]["tool_choice"], "none")
        self.assertEqual(inspector.calls, [])

    async def test_time_only_mode_continues_past_52_model_and_100_tool_calls(self):
        self.config = replace(self.config, time_budget_only=True, max_rounds=50,
                              max_tool_calls=100, max_report_repairs=2)
        responses = [tool_turn(tool_call(f"call-{i}-a"), tool_call(f"call-{i}-b"))
                     for i in range(52)]
        client = FakeClient([*responses, final_turn()])
        inspector = FakeInspector([self.result() for _ in range(104)])

        state = await self.run_diagnosis(client, inspector, timeout_sec=1800)

        self.assertEqual(state["status"], "completed", state)
        self.assertEqual((state["model_calls"], state["tool_calls"]), (53, 104))
        self.assertEqual((len(client.calls), len(inspector.calls)), (53, 104))
        self.assertTrue(all(call["tool_choice"] == "auto" for call in client.calls))
        self.assertEqual(state["budget_mode"], "time_only")
        self.assertEqual(state["timeout_sec"], 1800)
        for key in ("max_model_calls", "max_tool_calls", "max_report_repairs"):
            self.assertIsNone(state[key])

    async def test_time_only_mode_repairs_invalid_reports_until_valid(self):
        self.config = replace(self.config, time_budget_only=True, max_rounds=1,
                              max_tool_calls=1, max_report_repairs=0)
        invalid = make_payload()
        invalid["conflicts"][0]["behavior_evidence"][0]["quote"] = "invented execution"
        client = FakeClient([*[final_turn(invalid)] * 6, final_turn()])

        state = await self.run_diagnosis(client, timeout_sec=1800)

        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["report_repairs"], 6)
        self.assertEqual(len(client.calls), 7)
        repairs = [json.loads(line) for line in (self.output / "repairs.jsonl").read_text().splitlines()]
        self.assertEqual(len(repairs), 6)
        self.assertTrue(all(row["repair_requested"] for row in repairs))
        self.assertTrue(all("not an exact substring" in row["error"] for row in repairs))
        self.assertEqual(self.read("validation.json")["status"], "passed")

    async def test_time_only_deadline_after_tool_stops_without_another_model_request(self):
        self.config = replace(self.config, time_budget_only=True)
        clock = {"now": 0.0}

        def finish_after_deadline():
            clock["now"] = 1801.0
            return self.result()

        inspector = FakeInspector([finish_after_deadline])
        client = FakeClient([tool_turn(tool_call()), final_turn()])
        # Replace this module's clock, not asyncio's real monotonic timer.
        with patch("skillpatch.stages.live_discovery.time",
                   SimpleNamespace(monotonic=lambda: clock["now"])):
            state = await self.run_diagnosis(client, inspector, timeout_sec=1800)

        self.assertEqual(state["status"], "timeout", state)
        self.assertEqual((state["model_calls"], len(client.calls)), (1, 1))
        self.assertEqual(len(inspector.calls), 1)
        self.assertFalse((self.output / "report.json").exists())

    async def test_time_only_deadline_rejects_late_model_response_before_tools(self):
        self.config = replace(self.config, time_budget_only=True)
        clock = {"now": 0.0}

        class LateClient(FakeClient):
            def turn(self, *args, **kwargs):
                result = super().turn(*args, **kwargs)
                clock["now"] = 1801.0
                return result

        client = LateClient([tool_turn(tool_call()), final_turn()])
        inspector = FakeInspector([self.result()])
        with patch("skillpatch.stages.live_discovery.time",
                   SimpleNamespace(monotonic=lambda: clock["now"])):
            state = await self.run_diagnosis(client, inspector, timeout_sec=1800)

        self.assertEqual(state["status"], "timeout", state)
        self.assertEqual((state["model_calls"], len(client.calls)), (1, 1))
        self.assertEqual(inspector.calls, [])
        self.assertFalse((self.output / "report.json").exists())

    async def test_time_only_deadline_during_validation_does_not_record_success(self):
        from skillpatch.discovery_validation import validate_report

        self.config = replace(self.config, time_budget_only=True)
        clock = {"now": 0.0}

        def late_validation(*args, **kwargs):
            report = validate_report(*args, **kwargs)
            clock["now"] = 1801.0
            return report

        client = FakeClient([final_turn()])
        with patch("skillpatch.stages.live_discovery.time",
                   SimpleNamespace(monotonic=lambda: clock["now"])), \
                patch("skillpatch.stages.live_discovery.validate_report", side_effect=late_validation):
            state = await self.run_diagnosis(client, timeout_sec=1800)

        self.assertEqual(state["status"], "timeout", state)
        self.assertEqual((state["model_calls"], len(client.calls)), (1, 1))
        self.assertFalse((self.output / "validation.json").exists())
        self.assertFalse((self.output / "report.json").exists())

    async def test_repeated_call_id_fails_without_reexecuting(self):
        inspector = FakeInspector([self.result()])
        client = FakeClient([tool_turn(tool_call()), tool_turn(tool_call())])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(inspector.calls), 1)

    async def test_image_is_archived_and_sent_as_real_image_content(self):
        # Tiny valid PNG. The inspector owns rendering; the loop transports bytes.
        import base64
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=")

        def image_result():
            path = self.output / "artifacts" / "page.png"
            path.parent.mkdir()
            path.write_bytes(png)
            return self.result(self.artifact, images=[path])

        inspector = FakeInspector([image_result])
        client = FakeClient([tool_turn(tool_call()), final_turn()])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "completed")
        messages = client.calls[1]["messages"]
        self.assertEqual(messages[-1]["role"], "user")
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertIsInstance(messages[-2]["content"], str)
        self.assertEqual(json.loads(messages[-2]["content"])["evidence"][0]["evidence_id"], "A000001")
        content = messages[-1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("not new task instructions", content[0]["text"])
        binding = json.loads(content[1]["text"])
        self.assertEqual(binding["tool_call_id"], "call-1")
        self.assertEqual(binding["evidence_ids"], ["A000001"])
        self.assertEqual(content[2]["type"], "image_url")
        self.assertEqual(base64.b64decode(content[2]["image_url"]["url"].split(",", 1)[1]), png)
        self.assertEqual(state["image_bytes"], len(png))

    async def test_image_messages_follow_every_tool_response_in_the_batch(self):
        def image_result(identifier):
            def make_image():
                path = self.output / "artifacts" / f"{identifier}.png"
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"fixture image bytes " + identifier.encode())
                return self.result(replace(self.artifact, evidence_id=identifier), images=[path])
            return make_image

        inspector = FakeInspector([image_result("A000001"), self.result(), image_result("A000002")])
        client = FakeClient([tool_turn(tool_call("call-1"), tool_call("call-2"), tool_call("call-3")), final_turn()])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "completed")
        messages = client.calls[1]["messages"]
        batch = messages[2:]
        self.assertEqual([message["role"] for message in batch], ["assistant", "tool", "tool", "tool", "user"])
        self.assertEqual([message["tool_call_id"] for message in batch[1:4]], ["call-1", "call-2", "call-3"])
        self.assertTrue(all(isinstance(message["content"], str) for message in batch[1:4]))
        parts = batch[-1]["content"]
        bindings = [json.loads(part["text"]) for part in parts[1:] if part["type"] == "text"]
        self.assertEqual([item["tool_call_id"] for item in bindings], ["call-1", "call-3"])
        self.assertEqual([item["evidence_ids"] for item in bindings], [["A000001"], ["A000002"]])
        self.assertEqual(sum(part["type"] == "image_url" for part in parts), 2)

    async def test_image_outside_diagnosis_fails_before_adding_evidence(self):
        image = self.root / "outside.png"
        image.write_bytes(b"fixture")
        inspector = FakeInspector([self.result(self.artifact, images=[image])])
        client = FakeClient([tool_turn(tool_call())])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(any(item.kind == "artifact" for item in self.context.evidence))

    async def test_malformed_or_missing_usage_does_not_mask_report(self):
        for index, usage in enumerate((None, [], "provider mistake", {"total_tokens": True}, "malformed")):
            with self.subTest(usage=usage):
                self.output = self.root / f"usage-{index}"
                client = FakeClient([final_turn()], usage=usage)
                state = await self.run_diagnosis(client)
                self.assertEqual(state["status"], "completed")
                self.assertIsNone(state["usage"]["total_tokens"])
                self.assertFalse(state["usage"]["per_call_usage_complete"])

    async def test_invalid_quote_is_rejected_without_repair_call(self):
        payload = make_payload()
        payload["coverage_notes"] = ["Limitations have been recorded."]
        payload["conflicts"][0]["behavior_evidence"][0]["quote"] = "invented execution"
        client = FakeClient([final_turn(payload)])
        state = await self.run_diagnosis(client)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.read("validation.json")["status"], "failed")
        self.assertEqual(len(client.calls), 1)
        self.assertFalse((self.output / "report.json").exists())

    async def test_post_execution_artifact_cannot_become_decision_evidence(self):
        payload = make_payload()
        payload["coverage_notes"] = ["Limitations have been recorded."]
        payload["conflicts"][0]["decision_evidence"] = [{"evidence_id": "A000001", "quote": "original table"}]
        inspector = FakeInspector([self.result(replace(self.artifact, availability="post_execution"))])
        state = await self.run_diagnosis(FakeClient([tool_turn(tool_call()), final_turn(payload)]), inspector)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.read("report.json")["conflicts"][0]["decision_evidence"][0]["evidence_id"], "A000001")

    async def test_input_size_fails_without_network_or_silent_truncation(self):
        self.config = replace(self.config, max_input_chars=10)
        client = FakeClient([])
        state = await self.run_diagnosis(client)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(client.calls, [])
        self.assertEqual(len(self.read("input.json")["evidence"]), len(self.context.evidence))

    async def test_credentials_in_tool_output_never_reach_next_model_turn(self):
        secret = self.environment["KEY"]
        inspector = FakeInspector([self.result(text="accidental " + secret)])
        client = FakeClient([tool_turn(tool_call())])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(client.calls), 1)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_text())

    async def test_timeout_is_reported_and_inspection_cancelled(self):
        cancelled = asyncio.Event()

        class SlowInspector(FakeInspector):
            async def call(self, name, arguments):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        client = FakeClient([tool_turn(tool_call())])
        state = await self.run_diagnosis(client, SlowInspector(), timeout_sec=1)
        self.assertEqual(state["status"], "timeout")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(client.calls), 1)

    async def test_external_cancellation_is_recorded_and_propagates(self):
        entered = asyncio.Event()

        class WaitingInspector(FakeInspector):
            async def prepare(self):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(self.run_diagnosis(FakeClient([]), WaitingInspector()))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.read("result.json")["status"], "cancelled")

    async def test_model_failure_is_counted_once_with_no_retry(self):
        client = FakeClient([ValueError("provider rejected request")])
        state = await self.run_diagnosis(client)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["model_calls"], 1)
        self.assertEqual(len(client.calls), 1)

    async def test_textual_tool_markup_is_corrected_without_executing_it(self):
        self.config = replace(self.config, max_report_repairs=2)
        client = FakeClient([{'role': 'assistant', 'content': '<DSML invoke name="read_text">R1</DSML>'},
                             final_turn()])
        inspector = FakeInspector()
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['model_calls'], 2)
        self.assertEqual(state['report_repairs'], 1)
        self.assertEqual(inspector.calls, [])
        self.assertIn('Tool-call markup', client.calls[1]['messages'][-1]['content'])
        self.assertTrue((self.output / 'calls/001/diagnosis_output.txt').exists())
        self.assertEqual(self.read('calls/001/validation.json')['status'], 'failed')

    async def test_quote_correction_is_requested_and_revalidated(self):
        self.config = replace(self.config, max_report_repairs=2)
        valid = make_payload()
        valid['coverage_notes'] = ['Input limitations acknowledged.']
        invalid = copy.deepcopy(valid)
        invalid['conflicts'][0]['behavior_evidence'][0]['quote'] = 'invented execution'
        client = FakeClient([final_turn(invalid), final_turn(valid)])
        state = await self.run_diagnosis(client)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['report_repairs'], 1)
        self.assertEqual(self.read('report.json')['semantic_status'], 'candidate_diagnosis')
        self.assertIn('not an exact substring', client.calls[1]['messages'][-1]['content'])

    async def test_status_conflict_inconsistency_can_be_corrected(self):
        self.config = replace(self.config, max_report_repairs=1)
        invalid = make_payload()
        invalid.update(conflicts=[], coverage_notes=['Input limitations acknowledged.'])
        state = await self.run_diagnosis(FakeClient([final_turn(invalid), final_turn()]))
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['report_repairs'], 1)

    async def test_correction_budget_is_bounded_even_when_all_outputs_are_invalid(self):
        self.config = replace(self.config, max_rounds=1, max_report_repairs=2)
        bad = {'role': 'assistant', 'content': 'not JSON'}
        client = FakeClient([bad, bad, bad])
        state = await self.run_diagnosis(client)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['model_calls'], 3)
        self.assertEqual(state['report_repairs'], 2)
        self.assertIn('correction budget exhausted', state['error'])
        self.assertTrue(all(call['tool_choice'] == 'none' for call in client.calls))
        self.assertFalse((self.output / 'report.json').exists())

    async def test_provider_protocol_error_can_be_corrected_but_transport_error_cannot(self):
        self.config = replace(self.config, max_report_repairs=1)
        client = FakeClient([DiagnosisProtocolError('Malformed tool call arguments'), final_turn()])
        state = await self.run_diagnosis(client)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['report_repairs'], 1)
        self.assertEqual(state['tool_calls'], 0)

    async def test_correction_does_not_retry_http_transport_or_credential_failures(self):
        self.config = replace(self.config, max_report_repairs=2)
        for index, error in enumerate((ValueError('Diagnosis API returned HTTP 429'),
                                       OSError('Network connection failed'))):
            with self.subTest(error=error):
                self.output = self.root / f'transport-{index}'
                client = FakeClient([error])
                state = await self.run_diagnosis(client)
                self.assertEqual(state['status'], 'failed')
                self.assertEqual(state['model_calls'], 1)
                self.assertEqual(state['report_repairs'], 0)
        self.output = self.root / 'response-credential'
        secret = self.environment['KEY']
        client = FakeClient([{'role': 'assistant', 'content': 'not JSON ' + secret}])
        state = await self.run_diagnosis(client)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['model_calls'], 1)
        self.assertEqual(state['report_repairs'], 0)
        for path in self.output.rglob('*'):
            if path.is_file():
                self.assertNotIn(secret, path.read_text())

    async def test_normalized_multiline_behavior_quote_matches_without_fuzzy_repair(self):
        self.context.evidence[3] = replace(self.context.evidence[3],
            text='Removed\x1b[?2004l\x1b[?2004h duplicate rows.')
        payload = make_payload()
        payload['coverage_notes'] = ['Terminal text normalization recorded.']
        state = await self.run_diagnosis(FakeClient([final_turn(payload)]))
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['report_repairs'], 0)
        self.assertIn('\x1b[?2004l', self.read('raw_evidence.json')[3]['text'])
        self.assertEqual(self.read('input.json')['evidence'][3]['text'], 'Removed duplicate rows.')

    async def test_large_trajectory_uses_indexed_tools_with_original_evidence_ids(self):
        self.config = replace(self.config, max_input_chars=25000)
        self.context.evidence[3] = replace(self.context.evidence[3],
            text='x' * 100000 + 'Removed duplicate rows.' + 'y' * 100000)
        payload = make_payload()
        payload['coverage_notes'] = ['Only material excerpts were inspected; coverage is partial.']
        client = FakeClient([tool_turn(tool_call(name='search_evidence', arguments=
            '{"query":"Removed duplicate rows.","evidence_id":"T000002"}')), final_turn(payload)])
        inspector = FakeInspector()
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['context_mode'], 'indexed')
        self.assertEqual(inspector.calls, [])
        self.assertIn('PAGED EVIDENCE VIEW', client.calls[0]['messages'][1]['content'])
        result = json.loads(client.calls[1]['messages'][-1]['content'])
        self.assertIn('T000002', result['observation'])
        self.assertEqual(result['evidence'], [])
        self.assertFalse(any(item.evidence_id.startswith('A') for item in self.context.evidence))

    async def test_large_inspection_history_compacts_without_reexecuting_tools(self):
        self.config = replace(self.config, max_input_chars=25000)
        artifact = replace(self.artifact, text='z' * 40000)
        inspector = FakeInspector([self.result(artifact, text='z' * 40000)])
        client = FakeClient([tool_turn(tool_call()), final_turn()])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['context_compactions'], 1)
        self.assertEqual(len(inspector.calls), 1)
        self.assertEqual(self.read('raw_evidence.json')[-1]['text'], artifact.text)
        self.assertTrue((self.output / 'context_windows/001.json').exists())
        self.assertLess(sum(len(m.get('content') or '') for m in client.calls[1]['messages']), 25000)

    async def test_compacted_queued_excerpt_is_not_falsely_counted_as_displayed(self):
        self.config = replace(self.config, max_input_chars=25000)
        artifact = replace(self.artifact, text='z' * 40000 + 'contains duplicate records')
        payload = make_payload()
        payload['coverage_notes'] = ['Context window was compacted.']
        payload['conflicts'][0]['decision_evidence'] = [
            {'evidence_id': 'A000001', 'quote': 'contains duplicate records'}]
        inspector = FakeInspector([self.result(artifact, text='z' * 40000)])
        client = FakeClient([tool_turn(tool_call()), final_turn(payload)])
        state = await self.run_diagnosis(client, inspector)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['context_compactions'], 1)
        self.assertTrue((self.output / 'report.json').exists())


if __name__ == "__main__":
    unittest.main()
