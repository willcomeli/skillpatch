"""Auditable Chat Completions turns for the diagnosis agent, without retries."""

import json
import math
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from skillpatch.environment import redact, secret_values
from skillpatch.storage import now, write_json


@dataclass(frozen=True)
class DiagnosisConfig:
    model: str
    base_url: str
    api_key_env: str
    prompt_path: Path
    max_tokens: int = 8192
    timeout_sec: int = 240
    max_input_chars: int = 600000
    temperature: float = 0.0
    thinking: str | None = None
    max_rounds: int = 50
    max_tool_calls: int = 100
    max_report_repairs: int = 2
    # Session deadlines still apply; disable turn/tool/repair count ceilings.
    time_budget_only: bool = False


class DiagnosisProtocolError(ValueError):
    """A provider returned a response that cannot be used as an assistant turn.

    The live loop may request a corrected response within its bounded budget.
    Transport, credential and HTTP failures are not retried by this exception.
    """


def load_diagnosis_config(path: Path) -> DiagnosisConfig:
    path = path.resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"diagnosis"} or not isinstance(data["diagnosis"], dict):
        raise ValueError("Diagnosis config must have exactly one [diagnosis] table")
    values = data["diagnosis"].copy()
    required = {"model", "base_url", "api_key_env", "prompt_path"}
    allowed = required | {"max_tokens", "timeout_sec", "max_input_chars", "temperature", "thinking",
                          "max_rounds", "max_tool_calls", "max_report_repairs", "time_budget_only"}
    if not required <= values.keys() or values.keys() - allowed:
        raise ValueError("Missing or unknown diagnosis configuration fields")
    for name in required:
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError(f"diagnosis.{name} must be a nonempty string")
    values["prompt_path"] = (path.parent / values["prompt_path"]).resolve()
    config = DiagnosisConfig(**values)
    if type(config.time_budget_only) is not bool:
        raise ValueError("diagnosis.time_budget_only must be a boolean")
    for name in ("max_tokens", "timeout_sec", "max_input_chars", "max_rounds", "max_tool_calls"):
        if type(getattr(config, name)) is not int or getattr(config, name) <= 0:
            raise ValueError(f"diagnosis.{name} must be a positive integer")
    if type(config.max_report_repairs) is not int or not 0 <= config.max_report_repairs <= 5:
        raise ValueError("diagnosis.max_report_repairs must be an integer between 0 and 5")
    if (type(config.temperature) not in (int, float) or not math.isfinite(config.temperature)
            or not 0 <= config.temperature <= 2):
        raise ValueError("diagnosis.temperature must be finite and between 0 and 2")
    if config.thinking not in (None, "enabled", "disabled"):
        raise ValueError("diagnosis.thinking must be enabled or disabled")
    parsed = urllib.parse.urlsplit(config.base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("diagnosis.base_url must be an HTTP(S) URL without credentials/query/fragment")
    if not config.api_key_env.replace("_", "a").isalnum() or config.api_key_env[0].isdigit():
        raise ValueError("diagnosis.api_key_env must name an environment variable")
    if not config.prompt_path.is_file():
        raise ValueError(f"Diagnosis prompt not found: {config.prompt_path}")
    return config


def parse_json_object(text: str) -> dict[str, Any]:
    """Reject duplicate keys and non-JSON numeric constants instead of repairing output."""
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value: str) -> Any:
        raise ValueError(f"Invalid JSON numeric constant: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("JSON number exceeds finite range")
        return parsed

    data = json.loads(text, object_pairs_hook=object_pairs, parse_constant=invalid_constant,
                      parse_float=finite_float)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def diagnosis_secrets(config: DiagnosisConfig, environment: Mapping[str, str]) -> tuple[str, ...]:
    values = set(secret_values(environment))
    if environment.get(config.api_key_env):
        values.add(environment[config.api_key_env])
    return tuple(sorted(values, key=len, reverse=True))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Never forward the credential header to another endpoint automatically.
        return None


class ChatCompletionClient:
    def complete(self, config: DiagnosisConfig, messages: list[dict[str, Any]],
                 environment: Mapping[str, str], output: Path) -> str:
        """Run one text-only JSON completion using the same recorded turn protocol."""
        return self.turn(config, messages, environment, output)["content"]

    def turn(self, config: DiagnosisConfig, messages: list[dict[str, Any]],
             environment: Mapping[str, str], output: Path, *,
             tools: list[dict[str, Any]] | None = None,
             tool_choice: str | dict[str, Any] | None = None) -> dict[str, Any]:
        """Return an assistant message; persist this turn even on failure. No retries.

        The caller owns tool execution, round budgets, and a separate output directory
        for each turn. Tools are permitted only when supplied and not forced off.
        """
        secrets = diagnosis_secrets(config, environment)
        started = time.monotonic()
        record: dict[str, Any] = {"status": "failed", "started_at": now(),
                                  "requested_model": config.model, "attempts": 1}
        try:
            key = environment.get(config.api_key_env)
            if not key:
                raise ValueError(f"Missing credential environment variable: {config.api_key_env}")
            body: dict[str, Any] = {"model": config.model, "messages": messages,
                                    "max_tokens": config.max_tokens, "temperature": config.temperature,
                                    "stream": False, "response_format": {"type": "json_object"}}
            allow_tools = bool(tools) and tool_choice != "none"
            # An auto tool turn can also return the final report. Keep its
            # content constrained to JSON on every turn, including early stops.
            if tools is not None:
                body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
            if config.thinking is not None:
                body["thinking"] = {"type": config.thinking}
            # Refuse to transmit known credential values found in the evidence.
            if redact(body, secrets) != body:
                raise ValueError("Known credential value appears in the diagnosis input")
            write_json(output / "request.json", body)
            request = urllib.request.Request(
                config.base_url.rstrip("/") + "/chat/completions",
                data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                method="POST",
            )
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(request, timeout=config.timeout_sec) as response:
                    record["http_status"] = response.status
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                record["http_status"] = exc.code
                with exc:
                    raw_error = exc.read().decode("utf-8", errors="replace")
                raw_error = redact(raw_error, secrets)
                try:
                    decoded_error = parse_json_object(raw_error)
                    cleaned_error = redact(decoded_error, secrets)
                    if cleaned_error != decoded_error:
                        raw_error = json.dumps(cleaned_error, ensure_ascii=False, allow_nan=False)
                except ValueError:
                    pass  # Preserve non-JSON provider errors as text.
                (output / "response.txt").write_text(raw_error, encoding="utf-8")
                raise ValueError(f"Diagnosis API returned HTTP {exc.code}; see response.txt") from None
            raw = redact(raw, secrets)
            (output / "response.txt").write_text(raw, encoding="utf-8")
            envelope = parse_json_object(raw)
            cleaned = redact(envelope, secrets)
            if cleaned != envelope:
                # A provider may JSON-escape a value that was invisible in raw text.
                raw = json.dumps(cleaned, ensure_ascii=False, allow_nan=False)
                (output / "response.txt").write_text(raw, encoding="utf-8")
                record["response_redacted"] = True
            envelope = cleaned
            write_json(output / "response.json", envelope)
            record["returned_model"] = envelope.get("model")
            record["response_id"] = envelope.get("id")
            record["usage"] = envelope.get("usage")  # Missing remains null, never fabricated as zero.
            choices = envelope.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise DiagnosisProtocolError("Diagnosis API must return exactly one choice")
            choice = choices[0]
            record["finish_reason"] = choice.get("finish_reason")
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                (output / "model_output.txt").write_text(content, encoding="utf-8")
            finish_reason = choice.get("finish_reason")
            if finish_reason not in ("stop", "tool_calls"):
                raise DiagnosisProtocolError("Diagnosis completion did not finish normally; partial output is not a report")
            if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                raise DiagnosisProtocolError("Diagnosis API must return an assistant message")
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            # Some compatible providers require this field on subsequent tool turns.
            if isinstance(message.get("reasoning_content"), str):
                assistant["reasoning_content"] = message["reasoning_content"]
            calls = message.get("tool_calls")
            if finish_reason == "tool_calls":
                if not allow_tools:
                    raise DiagnosisProtocolError("Diagnosis API returned tool calls when tools are disabled")
                if content is not None and not isinstance(content, str):
                    raise DiagnosisProtocolError("Diagnosis tool-call content must be text or null")
                if not isinstance(calls, list) or not calls:
                    raise DiagnosisProtocolError("Diagnosis tool_calls must be a nonempty list")
                normalized_calls = []
                call_ids = set()
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise DiagnosisProtocolError("Diagnosis tool call must have type function")
                    call_id = call.get("id")
                    function = call.get("function")
                    if not isinstance(call_id, str) or not call_id.strip():
                        raise DiagnosisProtocolError("Diagnosis tool call id must be a nonempty string")
                    if call_id in call_ids:
                        raise DiagnosisProtocolError("Diagnosis tool call ids must be unique within a turn")
                    call_ids.add(call_id)
                    if not isinstance(function, dict):
                        raise DiagnosisProtocolError("Diagnosis tool call function must be an object")
                    name, arguments = function.get("name"), function.get("arguments")
                    if not isinstance(name, str) or not name.strip():
                        raise DiagnosisProtocolError("Diagnosis tool call name must be a nonempty string")
                    if not isinstance(arguments, str):
                        raise DiagnosisProtocolError("Diagnosis tool call arguments must be a string")
                    normalized_calls.append({"id": call_id, "type": "function",
                                             "function": {"name": name, "arguments": arguments}})
                assistant["tool_calls"] = normalized_calls
            else:
                if calls is not None and calls != []:
                    raise DiagnosisProtocolError("Diagnosis API returned tool calls without tool_calls finish reason")
                if not isinstance(content, str) or not content.strip():
                    raise DiagnosisProtocolError("Diagnosis API returned empty/non-text content")
            record["status"] = "completed"
            return assistant
        except KeyboardInterrupt:
            record["status"] = "cancelled"
            raise
        except (OSError, ValueError) as exc:
            record["error"] = redact(str(exc), secrets)
            raise
        finally:
            record["finished_at"] = now()
            record["elapsed_sec"] = round(time.monotonic() - started, 3)
            write_json(output / "model_call.json", redact(record, secrets))
