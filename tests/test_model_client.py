"""Diagnosis configuration and single-call accounting; HTTP is always mocked."""

import io
import json
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from skillpatch.model_client import (
    ChatCompletionClient,
    DiagnosisConfig,
    DiagnosisProtocolError,
    load_diagnosis_config,
    parse_json_object,
)
from skillpatch.storage import write_json


class DiagnosisConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prompt = self.root / "prompts" / "discovery.md"
        self.prompt.parent.mkdir()
        self.prompt.write_text("Return one JSON object.", encoding="utf-8")
        self.config_path = self.root / "config" / "diagnosis.toml"
        self.config_path.parent.mkdir()

    def config_text(self, **changes):
        values = {
            "model": '"fixture-model"',
            "base_url": '"https://provider.invalid/v1"',
            "api_key_env": '"DIAGNOSIS_API_KEY"',
            "prompt_path": '"../prompts/discovery.md"',
        }
        values.update(changes)
        return "[diagnosis]\n" + "\n".join(
            f"{key} = {value}" for key, value in values.items() if value is not None
        ) + "\n"

    def load(self, text):
        self.config_path.write_text(text, encoding="utf-8")
        return load_diagnosis_config(self.config_path)

    def test_valid_config_resolves_prompt_relative_to_config(self):
        config = self.load(self.config_text(
            max_tokens="1024", timeout_sec="30", max_input_chars="9000",
            temperature="0.5", thinking='"disabled"',
            max_rounds="3", max_tool_calls="9",
            max_report_repairs="1",
        ))
        self.assertEqual(config.model, "fixture-model")
        self.assertEqual(config.base_url, "https://provider.invalid/v1")
        self.assertEqual(config.api_key_env, "DIAGNOSIS_API_KEY")
        self.assertEqual(config.prompt_path, self.prompt)
        self.assertEqual(config.max_tokens, 1024)
        self.assertEqual(config.timeout_sec, 30)
        self.assertEqual(config.max_input_chars, 9000)
        self.assertEqual(config.temperature, 0.5)
        self.assertEqual(config.thinking, "disabled")
        self.assertEqual(config.max_rounds, 3)
        self.assertEqual(config.max_tool_calls, 9)
        self.assertEqual(config.max_report_repairs, 1)

    def test_absolute_prompt_and_optional_defaults(self):
        config = self.load(self.config_text(prompt_path=json.dumps(str(self.prompt))))
        self.assertEqual(config.prompt_path, self.prompt)
        self.assertIsNone(config.thinking)
        self.assertGreater(config.max_tokens, 0)
        self.assertGreater(config.timeout_sec, 0)
        self.assertEqual(config.max_rounds, 50)
        self.assertEqual(config.max_tool_calls, 100)
        self.assertEqual(config.max_report_repairs, 2)
        self.assertFalse(config.time_budget_only)

    def test_time_budget_only_is_explicit_and_requires_boolean(self):
        self.assertTrue(self.load(self.config_text(time_budget_only="true")).time_budget_only)
        self.assertFalse(self.load(self.config_text(time_budget_only="false")).time_budget_only)
        for value in ('"true"', "1", "0", "[]"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "must be a boolean"):
                self.load(self.config_text(time_budget_only=value))

    def test_unknown_fields_missing_fields_and_tables_are_rejected(self):
        samples = (
            self.config_text(unexpected="true"),
            self.config_text(enable_tools="true"),
            self.config_text(model=None),
            self.config_text() + "\n[other]\nvalue=1\n",
            "diagnosis = 1\n",
            "[other]\nmodel='fixture-model'\n",
        )
        for text in samples:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.load(text)

    def test_wrong_types_and_invalid_numeric_options_are_rejected(self):
        samples = (
            ("model", "12"), ("base_url", "false"),
            ("api_key_env", "[]"), ("prompt_path", "{}"),
            ("model", '" "'),
            ("max_tokens", "true"), ("max_tokens", "0"),
            ("timeout_sec", "-1"), ("timeout_sec", "1.5"),
            ("max_input_chars", '"1000"'),
            ("temperature", "true"), ("temperature", "nan"),
            ("temperature", "inf"), ("temperature", "-0.1"),
            ("temperature", "2.1"), ("thinking", '"auto"'),
            ("thinking", "false"),
            ("max_rounds", "0"), ("max_rounds", "true"), ("max_rounds", "1.5"),
            ("max_tool_calls", "-1"), ("max_tool_calls", "false"),
            ("max_tool_calls", '"24"'),
            ("max_report_repairs", "-1"), ("max_report_repairs", "6"),
            ("max_report_repairs", "true"), ("max_report_repairs", "1.5"),
        )
        for field, value in samples:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.load(self.config_text(**{field: value}))

    def test_output_correction_can_be_disabled(self):
        self.assertEqual(self.load(self.config_text(max_report_repairs="0")).max_report_repairs, 0)

    def test_endpoint_and_credential_variable_are_validated(self):
        urls = (
            "ftp://provider.invalid", "https:///missing-host",
            "https://user:password@provider.invalid",
            "https://provider.invalid?token=test", "https://provider.invalid/#fragment",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.load(self.config_text(base_url=json.dumps(url)))
        for name in ("1KEY", "API-KEY", "API KEY", " "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(self.config_text(api_key_env=json.dumps(name)))

    def test_prompt_must_exist_as_a_file(self):
        for path in ("missing.md", "../prompts"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "prompt not found"):
                self.load(self.config_text(prompt_path=json.dumps(path)))


class JSONParsingTests(unittest.TestCase):
    def test_object_and_unicode_are_preserved(self):
        self.assertEqual(parse_json_object('{"说明":"原文", "items":[]}'),
                         {"说明": "原文", "items": []})

    def test_malformed_non_object_and_non_json_constants_are_rejected(self):
        for text in ("", "not JSON", "{} trailing", "```json\n{}\n```", "[]", "null",
                     '{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_json_object(text)

    def test_duplicate_keys_are_rejected_at_every_depth(self):
        for text in ('{"status":"a","status":"b"}',
                     '{"nested":{"quote":"a","quote":"b"}}',
                     '{"items":[{"x":1,"x":2}]}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
                parse_json_object(text)

    def test_numbers_that_overflow_float_are_rejected(self):
        for text in ('{"value":1e999}', '{"items":[-1e999]}'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "finite range"):
                parse_json_object(text)
        self.assertEqual(parse_json_object('{"value":1e300}'), {"value": 1e300})


class ChatCompletionClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "call"
        self.credential = "fixture-credential-value-never-real-123"
        self.environment = {"DIAGNOSIS_API_KEY": self.credential}
        self.config = DiagnosisConfig(
            model="fixture-requested-model", base_url="https://provider.invalid/v1/",
            api_key_env="DIAGNOSIS_API_KEY", prompt_path=self.root / "prompt.md",
            max_tokens=2048, timeout_sec=17, thinking="disabled",
        )
        self.messages = [
            {"role": "system", "content": "Return one JSON object."},
            {"role": "user", "content": "本次执行的证据。"},
        ]
        self.content = '{"status":"insufficient_evidence","conflicts":[]}'
        self.envelope = {
            "id": "fixture-response-id", "model": "fixture-returned-model",
            "choices": [{"finish_reason": "stop", "message": {"content": self.content}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 27, "total_tokens": 150},
        }
        self.tools = [{"type": "function", "function": {
            "name": "read_material", "description": "Read provided task material.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }}]
        self.tool_call = {"id": "call_fixture_1", "type": "function", "function": {
            "name": "read_material", "arguments": '{"path":"task/instruction.md"}',
        }}
        self.opener = MagicMock()
        self.response = MagicMock()
        self.response.status = 200
        self.response.__enter__.return_value = self.response
        self.opener.open.return_value = self.response
        patcher = patch("skillpatch.model_client.urllib.request.build_opener", return_value=self.opener)
        self.build_opener = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = ChatCompletionClient()

    def reply(self, envelope=None, raw=None):
        self.response.read.return_value = (
            raw if raw is not None else json.dumps(envelope or self.envelope, ensure_ascii=False)
        ).encode("utf-8")

    def complete(self, **kwargs):
        return self.client.complete(
            kwargs.get("config", self.config), kwargs.get("messages", self.messages),
            kwargs.get("environment", self.environment), self.output,
        )

    def turn(self, **kwargs):
        return self.client.turn(
            kwargs.get("config", self.config), kwargs.get("messages", self.messages),
            kwargs.get("environment", self.environment), self.output,
            tools=kwargs.get("tools"), tool_choice=kwargs.get("tool_choice"),
        )

    def record(self):
        return json.loads((self.output / "model_call.json").read_text(encoding="utf-8"))

    def assert_failed_record(self):
        record = self.record()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["started_at"])
        self.assertTrue(record["finished_at"])
        self.assertGreaterEqual(record["elapsed_sec"], 0)
        self.assertTrue(record["error"])
        return record

    def assert_no_stored_credential(self):
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.credential, path.read_text(encoding="utf-8"), str(path))

    def test_request_is_single_post_with_json_mode_and_auditable_usage(self):
        self.reply()
        self.assertEqual(self.complete(), self.content)
        self.build_opener.assert_called_once()
        self.opener.open.assert_called_once()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://provider.invalid/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.credential)
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 17})
        body = json.loads(request.data)
        self.assertEqual(body["messages"], self.messages)
        self.assertEqual(body["model"], self.config.model)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIs(body["stream"], False)
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(json.loads((self.output / "request.json").read_text()), body)
        record = self.record()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["usage"], self.envelope["usage"])
        self.assertEqual(record["requested_model"], "fixture-requested-model")
        self.assertEqual(record["returned_model"], "fixture-returned-model")
        self.assertEqual(record["response_id"], "fixture-response-id")
        self.assertEqual(record["http_status"], 200)
        self.assertEqual(record["finish_reason"], "stop")
        self.assertEqual(record["attempts"], 1)
        self.assert_no_stored_credential()
        handler = self.build_opener.call_args.args[0]
        self.assertIsNone(handler.redirect_request(request, None, 302, "redirect", {},
                                                  "https://other.invalid"))

    def test_missing_usage_remains_null_and_unspecified_thinking_is_omitted(self):
        self.envelope.pop("usage")
        self.reply()
        self.complete(config=replace(self.config, thinking=None))
        self.assertIsNone(self.record()["usage"])
        self.assertNotIn("thinking", json.loads(self.opener.open.call_args.args[0].data))

    def test_tool_turn_preserves_tool_calls_with_json_output_mode(self):
        self.envelope["choices"][0].update(
            finish_reason="tool_calls", message={"content": None, "tool_calls": [self.tool_call],
                                                  "provider_extra": "not conversation content"},
        )
        self.reply()

        result = self.turn(tools=self.tools, tool_choice="auto")

        self.assertEqual(result, {"role": "assistant", "content": None, "tool_calls": [self.tool_call]})
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["tools"], self.tools)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["thinking"], {"type": "disabled"})
        record = self.record()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["finish_reason"], "tool_calls")
        self.assertEqual(record["usage"], self.envelope["usage"])
        self.assertEqual(json.loads((self.output / "response.json").read_text()), self.envelope)
        self.assertFalse((self.output / "model_output.txt").exists())
        self.opener.open.assert_called_once()
        self.assert_no_stored_credential()

    def test_tool_history_and_text_finish_are_supported(self):
        messages = self.messages + [
            {"role": "assistant", "content": None, "tool_calls": [self.tool_call]},
            {"role": "tool", "tool_call_id": self.tool_call["id"], "content": "所请求材料的原文。"},
        ]
        self.reply()

        result = self.turn(tools=self.tools, messages=messages)

        self.assertEqual(result, {"role": "assistant", "content": self.content})
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["messages"], messages)
        self.assertNotIn("tool_choice", body)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual((self.output / "model_output.txt").read_text(), self.content)

    def test_provider_reasoning_field_is_preserved_for_tool_continuation(self):
        self.envelope["choices"][0].update(finish_reason="tool_calls", message={
            "content": None, "tool_calls": [self.tool_call], "reasoning_content": "provider continuation state",
        })
        self.reply()
        assistant = self.turn(tools=self.tools)
        self.assertEqual(assistant["reasoning_content"], "provider continuation state")
        self.output = self.root / "continuation"
        self.envelope["choices"][0].update(finish_reason="stop", message={"content": self.content})
        self.reply()
        history = self.messages + [assistant, {"role": "tool", "tool_call_id": self.tool_call["id"], "content": "observation"}]
        self.turn(tools=self.tools, messages=history)
        self.assertEqual(json.loads(self.opener.open.call_args.args[0].data)["messages"][-2]["reasoning_content"],
                         "provider continuation state")

    def test_forced_final_turn_disables_tools_and_enables_json_mode(self):
        self.reply()
        self.assertEqual(self.turn(tools=self.tools, tool_choice="none"),
                         {"role": "assistant", "content": self.content})
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["tool_choice"], "none")
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_spontaneous_final_on_auto_tool_turn_is_also_json_constrained(self):
        self.reply()
        result = self.turn(tools=self.tools, tool_choice="auto")
        self.assertEqual(result, {"role": "assistant", "content": self.content})
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["tools"], self.tools)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.opener.open.assert_called_once()

    def test_multimodal_tool_result_is_preserved_in_request(self):
        content = [
            {"type": "text", "text": "任务材料中的图像。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,Zml4dHVyZQ=="}},
        ]
        messages = self.messages + [
            {"role": "assistant", "content": None, "tool_calls": [self.tool_call]},
            {"role": "tool", "tool_call_id": self.tool_call["id"], "content": content},
        ]
        self.reply()

        self.turn(tools=self.tools, messages=messages)

        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body["messages"], messages)
        self.assertIsInstance(body["messages"][-1]["content"], list)
        self.assertEqual(body["messages"][-1]["content"], content)
        self.assertEqual(json.loads((self.output / "request.json").read_text())["messages"], messages)
        self.opener.open.assert_called_once()

    def test_unsolicited_or_forced_off_tool_calls_fail_with_record(self):
        self.envelope["choices"][0].update(
            finish_reason="tool_calls", message={"content": None, "tool_calls": [self.tool_call]},
        )
        self.reply()
        for index, arguments in enumerate(({}, {"tools": []},
                                           {"tools": self.tools, "tool_choice": "none"})):
            with self.subTest(arguments=arguments):
                self.output = self.root / f"disabled-{index}"
                self.opener.open.reset_mock()
                with self.assertRaisesRegex(ValueError, "tools are disabled"):
                    self.turn(**arguments)
                self.assertEqual(self.assert_failed_record()["finish_reason"], "tool_calls")
                self.opener.open.assert_called_once()

    def test_malformed_tool_calls_are_rejected_without_retry(self):
        samples = (
            None, [], "not a list", ["not an object"],
            [{**self.tool_call, "type": "unknown"}],
            [{**self.tool_call, "id": 1}], [{**self.tool_call, "id": " "}],
            [{**self.tool_call, "function": None}],
            [{**self.tool_call, "function": {"name": 1, "arguments": "{}"}}],
            [{**self.tool_call, "function": {"name": "", "arguments": "{}"}}],
            [{**self.tool_call, "function": {"name": "read_material", "arguments": {}}}],
            [{**self.tool_call, "function": {"name": "read_material"}}],
            [self.tool_call, self.tool_call],
        )
        for index, calls in enumerate(samples):
            with self.subTest(calls=calls):
                self.output = self.root / f"malformed-tool-{index}"
                self.opener.open.reset_mock()
                self.envelope["choices"][0].update(
                    finish_reason="tool_calls", message={"content": None, "tool_calls": calls},
                )
                self.reply()
                with self.assertRaises(DiagnosisProtocolError):
                    self.turn(tools=self.tools)
                self.assert_failed_record()
                self.assertTrue((self.output / "response.txt").exists())
                self.opener.open.assert_called_once()

    def test_inconsistent_tool_responses_are_rejected(self):
        samples = (
            ("stop", {"content": self.content, "tool_calls": [self.tool_call]}),
            ("tool_calls", {"content": [], "tool_calls": [self.tool_call]}),
            ("tool_calls", {"role": "tool", "content": None, "tool_calls": [self.tool_call]}),
            ("length", {"content": "partial", "tool_calls": [self.tool_call]}),
        )
        for index, (reason, message) in enumerate(samples):
            with self.subTest(reason=reason, message=message):
                self.output = self.root / f"inconsistent-{index}"
                self.opener.open.reset_mock()
                self.envelope["choices"][0].update(finish_reason=reason, message=message)
                self.reply()
                with self.assertRaises(DiagnosisProtocolError):
                    self.turn(tools=self.tools)
                self.assert_failed_record()
                self.opener.open.assert_called_once()

    def test_secret_in_tool_definition_is_not_sent(self):
        self.tools[0]["function"]["description"] = self.credential
        with self.assertRaisesRegex(ValueError, "credential value appears"):
            self.turn(tools=self.tools)
        self.build_opener.assert_not_called()
        self.assertFalse((self.output / "request.json").exists())
        self.assert_failed_record()
        self.assert_no_stored_credential()

    def test_http_errors_preserve_body_and_never_retry(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                self.output = self.root / str(status)
                self.opener.open.reset_mock()
                raw = '{"error":{"message":"Synthetic provider rejection"}}'
                self.opener.open.side_effect = urllib.error.HTTPError(
                    "https://provider.invalid/v1/chat/completions", status, "rejected", {},
                    io.BytesIO(raw.encode()),
                )
                self.addCleanup(self.opener.open.side_effect.close)
                with self.assertRaisesRegex(ValueError, f"HTTP {status}"):
                    self.complete()
                self.opener.open.assert_called_once()
                self.assertEqual((self.output / "response.txt").read_text(), raw)
                self.assertEqual(self.assert_failed_record()["http_status"], status)
                self.assert_no_stored_credential()

    def test_http_error_credential_echo_is_redacted(self):
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://provider.invalid", 403, "rejected", {},
            io.BytesIO(("Rejected " + self.credential).encode()),
        )
        self.addCleanup(self.opener.open.side_effect.close)
        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self.complete()
        self.assertIn("[REDACTED]", (self.output / "response.txt").read_text())
        self.assert_failed_record()
        self.assert_no_stored_credential()

    def test_http_error_json_escaped_credential_is_redacted(self):
        escaped_secret = "".join(f"\\u{ord(char):04x}" for char in self.credential)
        raw = json.dumps({"error": {"message": "Rejected " + self.credential}}).replace(
            self.credential, escaped_secret,
        )
        self.assertNotIn(self.credential, raw)
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://provider.invalid", 401, "rejected", {}, io.BytesIO(raw.encode()),
        )
        self.addCleanup(self.opener.open.side_effect.close)

        with self.assertRaisesRegex(ValueError, "HTTP 401"):
            self.complete()

        stored = (self.output / "response.txt").read_text()
        self.assertEqual(json.loads(stored), {"error": {"message": "Rejected [REDACTED]"}})
        self.assertNotIn(escaped_secret, stored)
        self.assertEqual(self.assert_failed_record()["http_status"], 401)
        self.assert_no_stored_credential()
        self.opener.open.assert_called_once()

    def test_network_failure_is_recorded_without_retry(self):
        self.opener.open.side_effect = urllib.error.URLError("fixture connection failure")
        with self.assertRaises(urllib.error.URLError):
            self.complete()
        self.opener.open.assert_called_once()
        self.assertIn("fixture connection failure", self.assert_failed_record()["error"])
        self.assertFalse((self.output / "response.txt").exists())

    def test_length_finish_preserves_partial_output_but_rejects_report(self):
        self.envelope["choices"][0].update(finish_reason="length", message={"content": '{"status":'})
        self.reply()
        with self.assertRaisesRegex(ValueError, "did not finish normally"):
            self.complete()
        self.assertEqual((self.output / "model_output.txt").read_text(), '{"status":')
        self.assertEqual(self.assert_failed_record()["finish_reason"], "length")
        self.opener.open.assert_called_once()

    def test_empty_and_non_text_content_are_rejected_with_records(self):
        for index, content in enumerate((None, "", " \n", [{"text": "not a string"}])):
            with self.subTest(content=content):
                self.output = self.root / f"empty-{index}"
                self.opener.open.reset_mock()
                self.envelope["choices"][0]["message"]["content"] = content
                self.reply()
                with self.assertRaisesRegex(ValueError, "empty/non-text"):
                    self.complete()
                self.assert_failed_record()
                self.opener.open.assert_called_once()

    def test_malformed_and_duplicate_envelopes_preserve_raw_response(self):
        samples = ("not JSON", '{"choices":[],"choices":[]}',
                   '{"choices":[{"message":{"content":"a","content":"b"}}]}')
        for index, raw in enumerate(samples):
            with self.subTest(raw=raw):
                self.output = self.root / f"invalid-{index}"
                self.opener.open.reset_mock()
                self.reply(raw=raw)
                with self.assertRaises(ValueError):
                    self.complete()
                self.assertEqual((self.output / "response.txt").read_text(), raw)
                self.assertFalse((self.output / "response.json").exists())
                self.assert_failed_record()
                self.opener.open.assert_called_once()

    def test_missing_credential_fails_before_network_and_retains_record(self):
        with self.assertRaisesRegex(ValueError, "Missing credential"):
            self.complete(environment={})
        self.build_opener.assert_not_called()
        self.assert_failed_record()

    def test_known_secret_in_input_is_not_sent_or_saved(self):
        messages = [{"role": "user", "content": "Evidence accidentally includes " + self.credential}]
        with self.assertRaisesRegex(ValueError, "credential value appears"):
            self.complete(messages=messages)
        self.build_opener.assert_not_called()
        self.assertFalse((self.output / "request.json").exists())
        self.assert_failed_record()
        self.assert_no_stored_credential()

    def test_selected_credential_is_protected_even_with_nonstandard_variable_name(self):
        config = replace(self.config, api_key_env="PROVIDER_ACCESS")
        messages = [{"role": "user", "content": "Evidence includes " + self.credential}]
        with self.assertRaisesRegex(ValueError, "credential value appears"):
            self.complete(config=config, environment={"PROVIDER_ACCESS": self.credential}, messages=messages)
        self.build_opener.assert_not_called()
        self.assertFalse((self.output / "request.json").exists())
        self.assert_failed_record()
        self.assert_no_stored_credential()

    def test_short_selected_credential_is_protected_before_network(self):
        self.credential = "k7!"
        config = replace(self.config, api_key_env="PROVIDER_ACCESS")
        messages = [{"role": "user", "content": "Evidence includes " + self.credential}]
        with self.assertRaisesRegex(ValueError, "credential value appears"):
            self.complete(config=config, environment={"PROVIDER_ACCESS": self.credential}, messages=messages)
        self.build_opener.assert_not_called()
        self.assertFalse((self.output / "request.json").exists())
        self.assert_failed_record()
        self.assert_no_stored_credential()

    def test_json_escaped_response_secret_is_redacted_in_all_artifacts(self):
        config = replace(self.config, api_key_env="PROVIDER_ACCESS")
        content = json.dumps({"summary": "Echo " + self.credential})
        self.envelope["choices"][0]["message"]["content"] = content
        escaped_secret = "".join(f"\\u{ord(char):04x}" for char in self.credential)
        raw = json.dumps(self.envelope).replace(self.credential, escaped_secret)
        self.assertNotIn(self.credential, raw)
        self.reply(raw=raw)

        result = self.complete(config=config, environment={"PROVIDER_ACCESS": self.credential})

        self.assertEqual(json.loads(result), {"summary": "Echo [REDACTED]"})
        self.assertTrue(self.record()["response_redacted"])
        self.assertEqual(self.record()["status"], "completed")
        self.assertEqual(self.record()["usage"], self.envelope["usage"])
        self.assert_no_stored_credential()
        response_text = (self.output / "response.txt").read_text()
        self.assertNotIn(escaped_secret, response_text)
        self.assertEqual(json.loads(response_text),
                         json.loads((self.output / "response.json").read_text()))
        self.opener.open.assert_called_once()

    def test_cancellation_is_recorded_without_retry(self):
        self.opener.open.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.complete()
        record = self.record()
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["attempts"], 1)
        self.assertTrue(record["finished_at"])
        self.opener.open.assert_called_once()

    def test_request_write_failure_keeps_failure_record_without_network(self):
        def fail_request(path, value):
            if path.name == "request.json":
                raise OSError("fixture request write failure")
            return write_json(path, value)

        with patch("skillpatch.model_client.write_json", side_effect=fail_request):
            with self.assertRaisesRegex(OSError, "request write failure"):
                self.complete()
        self.build_opener.assert_not_called()
        self.assertIn("request write failure", self.assert_failed_record()["error"])


if __name__ == "__main__":
    unittest.main()
