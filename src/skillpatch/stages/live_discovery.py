"""Diagnosis with read-only tools while the verified task sandbox is still alive."""

import asyncio
import base64
import json
import mimetypes
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from skillpatch.context import refresh_input_digest
from skillpatch.discovery_validation import validate_report
from skillpatch.environment import redact
from skillpatch.evidence_repair import build_evidence_repair_context
from skillpatch.evidence_views import EvidenceViews, normalize_terminal_text
from skillpatch.model_client import (
    ChatCompletionClient, DiagnosisConfig, DiagnosisProtocolError, diagnosis_secrets, parse_json_object,
)
from skillpatch.lifecycle import InspectionTools
from skillpatch.models import DiscoveryInput, EvidenceItem
from skillpatch.stages.discovery import render_report
from skillpatch.storage import jsonable, now, sha256_file, write_json


RUNTIME_PROMPTS = Path(__file__).resolve().parents[3] / "prompts" / "discovery_runtime.json"


def _runtime_prompts() -> dict[str, str]:
    prompts = parse_json_object(RUNTIME_PROMPTS.read_text(encoding="utf-8"))
    expected = {"initial_inspection", "final_budget", "image_observation", "phase_limitation",
                "repair", "context_compaction"}
    if set(prompts) != expected or any(not isinstance(value, str) or not value.strip() for value in prompts.values()):
        raise ValueError("Runtime diagnosis prompts must contain the six nonempty instruction templates")
    return prompts


def _message_size(messages: list[dict]) -> tuple[int, int]:
    """Count text characters separately from inline image bytes."""
    characters = 0
    image_bytes = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    characters += len(part.get("text", ""))
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if not url.startswith("data:image/") or ";base64," not in url:
                        raise ValueError("Only recorded local image data URLs are permitted")
                    image_bytes += len(base64.b64decode(url.split(",", 1)[1], validate=True))
                else:
                    raise ValueError("Unsupported diagnosis message content part")
        characters += len(json.dumps(message.get("tool_calls", []), ensure_ascii=False))
        characters += len(message.get("reasoning_content", ""))
    return characters, image_bytes


def _tool_content(result: dict, output: Path, secrets: tuple[str, ...]) -> tuple[str, list[dict]]:
    observation = result.get("text")
    if not isinstance(observation, str):
        raise ValueError("Inspection tools must return text")
    # The loop, rather than an individual inspector, binds model-visible evidence IDs.
    text = json.dumps({"observation": observation, "evidence": jsonable(result.get("evidence", [])),
                       "error": result.get("error"), "truncated": result.get("truncated", False)},
                      ensure_ascii=False, allow_nan=False)
    if redact(text, secrets) != text:
        raise ValueError("Known credential value appears in a tool observation")
    parts: list[dict] = []
    for value in result.get("images", []):
        path = Path(value).resolve()
        if not path.is_relative_to(output.resolve()) or not path.is_file():
            raise ValueError("Tool image must be an archived file inside this diagnosis")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Tool image exceeds the 8 MiB per-image limit")
        mime = mimetypes.guess_type(path.name)[0]
        if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise ValueError("Unsupported tool image format")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return text, parts


def _usage_summary(output: Path) -> dict:
    records = []
    for path in sorted((output / "calls").glob("*/model_call.json")):
        try:
            records.append(parse_json_object(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            records.append({})
    summary: dict[str, Any] = {"model_calls": len(records), "per_call_usage_complete": bool(records)}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [item["usage"].get(name) if isinstance(item.get("usage"), dict) else None
                  for item in records]
        complete = bool(values) and all(type(value) is int and value >= 0 for value in values)
        summary[name] = sum(values) if complete else None
        if not complete:
            summary["per_call_usage_complete"] = False
    return summary


def _new_evidence(result: dict, context: DiscoveryInput) -> list[EvidenceItem]:
    """Validate the complete observation before appending any part of it."""
    items = result.get("evidence", [])
    if not isinstance(items, list):
        raise ValueError("Inspection evidence must be a list")
    identifiers = {item.evidence_id for item in context.evidence}
    for item in items:
        if (not isinstance(item, EvidenceItem) or item.kind != "artifact"
                or not isinstance(item.evidence_id, str)
                or not re.fullmatch(r"A[0-9]{6,}", item.evidence_id)
                or item.evidence_id in identifiers):
            raise ValueError("Inspection evidence must have unique new A000001 artifact identifiers")
        if not isinstance(item.text, str) or not isinstance(item.source_path, str):
            raise ValueError("Inspection evidence must contain text and a source path")
        if item.availability not in (None, "initial", "post_execution", "unknown"):
            raise ValueError("Inspection evidence has invalid availability")
        identifiers.add(item.evidence_id)
    return items


async def diagnose_live_context(context: DiscoveryInput, config: DiagnosisConfig, inspector: InspectionTools,
                                output: Path, environment: Mapping[str, str], *,
                                client: ChatCompletionClient | None = None,
                                timeout_sec: int = 900) -> dict:
    """Benchmark-independent loop. Inspectors own resource access and provenance."""
    output = output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    secrets = diagnosis_secrets(config, environment)
    deadline = time.monotonic() + timeout_sec
    time_only = config.time_budget_only
    state: dict[str, Any] = {
        "schema_version": 1, "run_id": context.run_id, "status": "running", "phase": "prepare",
        "started_at": now(), "output_dir": str(output), "mode": "live_post_verification",
        "model_calls": 0, "tool_calls": 0, "semantic_status": "not_assessed",
        "report_repairs": 0, "context_compactions": 0,
        "budget_mode": "time_only" if time_only else "counts_and_time",
        "timeout_sec": timeout_sec,
        "max_model_calls": None if time_only else config.max_rounds + config.max_report_repairs,
        "max_tool_calls": None if time_only else config.max_tool_calls,
        "max_report_repairs": None if time_only else config.max_report_repairs,
    }
    messages: list[dict] = []
    client = client or ChatCompletionClient()
    views: EvidenceViews | None = None

    def save() -> None:
        write_json(output / "result.json", redact(state, secrets))

    def record_tool(entry: dict) -> None:
        with (output / "tool_calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(redact(entry, secrets), ensure_ascii=False, allow_nan=False) + "\n")

    def repair(error: str, call_dir: Path, payload: object = None) -> None:
        """Request correction rather than parsing or executing textual tool syntax."""
        error = redact(error, secrets)
        write_json(call_dir / "validation.json", {"status": "failed", "error": error,
            "semantic_correctness": "not_verified"})
        with (output / "repairs.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"round": state["round"], "error": error,
                "repair_requested": time_only or state["report_repairs"] < config.max_report_repairs,
                "at": now()}, ensure_ascii=False) + "\n")
        if not time_only and state["report_repairs"] >= config.max_report_repairs:
            raise ValueError("Diagnosis output correction budget exhausted: " + error)
        state["report_repairs"] += 1
        feedback = runtime_prompts["repair"].format(error=error)
        repair_context = build_evidence_repair_context(
            error, payload, {"source": views} if views is not None else {},
            max_chars=min(6000, config.max_input_chars // 12), collection_argument=False,
        )
        if repair_context:
            feedback += "\n" + repair_context
        feedback = redact(feedback, secrets)
        write_json(call_dir / "repair_feedback.json", {"error": error, "message": feedback})
        messages.append({"role": "user", "content": feedback})
        save()

    def remaining_time() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Diagnosis time budget exhausted")
        return remaining

    async def within_budget(awaitable: Any) -> Any:
        try:
            remaining = remaining_time()
        except TimeoutError:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise
        result = await asyncio.wait_for(awaitable, timeout=remaining)
        remaining_time()
        return result

    try:
        save()
        if type(time_only) is not bool:
            raise ValueError("Diagnosis time_budget_only must be a boolean")
        if type(timeout_sec) is not int or timeout_sec <= 0:
            raise ValueError("Diagnosis time budget must be a positive integer")
        if not time_only and any(type(value) is not int or value <= 0 for value in
                                 (config.max_rounds, config.max_tool_calls)):
            raise ValueError("Diagnosis round, tool-call and time budgets must be positive integers")
        if not time_only and (type(config.max_report_repairs) is not int
                              or not 0 <= config.max_report_repairs <= 5):
            raise ValueError("Diagnosis output correction budget must be between zero and five")
        runtime_prompts = _runtime_prompts()
        if redact(runtime_prompts, secrets) != runtime_prompts:
            raise ValueError("Known credential value appears in runtime diagnosis prompts")
        catalog = await within_budget(inspector.prepare())
        if not isinstance(catalog, dict) or not isinstance(catalog.get("limitations", []), list):
            raise ValueError("Inspection catalog must be an object with a limitations list")
        if redact(jsonable(catalog), secrets) != jsonable(catalog):
            raise ValueError("Known credential value appears in the inspection catalog")
        context.limitations.extend(catalog.get("limitations", []))
        context.limitations.append(runtime_prompts["phase_limitation"])
        refresh_input_digest(context)
        prompt = config.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("Diagnosis prompt is empty")
        definitions = inspector.definitions()
        if not definitions:
            raise ValueError("Live diagnosis requires inspection tools")
        if any(item["function"]["name"] in EvidenceViews.names for item in definitions):
            raise ValueError("Inspector tool names conflict with archived evidence tools")
        if redact(jsonable(context), secrets) != jsonable(context):
            raise ValueError("Known credential value appears in the diagnosis input")
        views = EvidenceViews(context, output)
        if redact(jsonable(context), secrets) != jsonable(context):
            raise ValueError("Known credential value appears in normalized diagnosis evidence")
        definitions.extend(views.definitions())
        available_tools = {item["function"]["name"] for item in definitions}
        evidence_budget = (config.max_input_chars - len(prompt) - len(json.dumps(catalog, ensure_ascii=False))
                           - len(runtime_prompts["initial_inspection"]) - 12000) // 2
        messages = [{"role": "system", "content": prompt}, {
            "role": "user", "content": views.render(evidence_budget) + "\nLIVE INSPECTION CATALOG\n" +
            json.dumps(catalog, ensure_ascii=False, allow_nan=False) +
            "\n" + runtime_prompts["initial_inspection"],
        }]
        if redact(jsonable(context), secrets) != jsonable(context) or redact(messages, secrets) != messages:
            raise ValueError("Known credential value appears in the diagnosis input")
        write_json(output / "initial_input.json", context)
        write_json(output / "input.json", context)
        write_json(output / "catalog.json", catalog)
        write_json(output / "tools.json", definitions)
        write_json(output / "config.json", config)
        write_json(output / "runtime_prompts.json", runtime_prompts)
        (output / "prompt.md").write_text(prompt, encoding="utf-8")
        state.update(initial_input_digest=context.input_digest, prompt_sha256=sha256_file(output / "prompt.md"),
                     runtime_prompts_sha256=sha256_file(output / "runtime_prompts.json"))
        seen_call_ids: set[str] = set()
        round_number = 0
        while time_only or round_number < config.max_rounds + config.max_report_repairs:
            remaining_time()
            round_number += 1
            # All calls, including corrections, share this one absolute deadline.
            if not time_only and round_number > config.max_rounds + state["report_repairs"]:
                raise ValueError("Diagnosis round budget exhausted without a final report")
            final_only = not time_only and (round_number - state["report_repairs"] >= config.max_rounds
                                           or state["tool_calls"] >= config.max_tool_calls)
            if final_only:
                messages.append({"role": "system", "content": runtime_prompts["final_budget"]})
            characters, image_bytes = _message_size(messages)
            if characters > config.max_input_chars:
                # Compact only at a completed turn boundary. All original evidence,
                # tool observations and prior request windows remain archived.
                window = output / "context_windows" / f"{state['context_compactions'] + 1:03d}.json"
                write_json(window, messages)
                # Queued tool excerpts dropped from this window were never sent
                # to the model. They must not count as displayed citation text.
                views.discard_pending()
                images = [message for message in messages if message.get("role") == "user"
                          and isinstance(message.get("content"), list)]
                image_chars, _ = _message_size(images)
                budget = (config.max_input_chars - len(prompt) - image_chars
                          - len(json.dumps(catalog, ensure_ascii=False)) - 12000) // 2
                # Retain the latest correction instruction across context windows.
                correction = [message for message in messages if message.get("role") == "user"
                              and isinstance(message.get("content"), str)
                              and message["content"].startswith(runtime_prompts["repair"].split("{error}")[0])]
                messages = [{"role": "system", "content": prompt}, {"role": "user", "content":
                    views.render(budget) + "\nLIVE INSPECTION CATALOG\n" + json.dumps(catalog, ensure_ascii=False)
                    + "\n" + runtime_prompts["context_compaction"]}, *images, *correction[-1:]]
                if final_only:
                    messages.append({"role": "system", "content": runtime_prompts["final_budget"]})
                state["context_compactions"] += 1
                characters, image_bytes = _message_size(messages)
                if characters > config.max_input_chars:
                    raise ValueError("Bounded diagnosis context still exceeds max_input_chars")
            if image_bytes > 32 * 1024 * 1024:
                raise ValueError("Live diagnosis exceeds its 32 MiB image budget")
            state.update(phase="model_call", round=round_number, input_chars=characters, image_bytes=image_bytes)
            save()
            call_dir = output / "calls" / f"{round_number:03d}"
            call_dir.mkdir(parents=True)
            remaining = max(1, int(remaining_time()))
            call_config = replace(config, timeout_sec=min(config.timeout_sec, remaining))
            write_json(output / "messages.json", messages)
            if redact(messages, secrets) != messages:
                raise ValueError("Known credential value appears in the diagnosis conversation")
            views.commit_visible()

            def model_turn():
                # A thread-pool queue must not start a new request after expiry.
                remaining_time()
                state["model_calls"] += 1
                return client.turn(call_config, messages, environment, call_dir,
                                   tools=definitions, tool_choice="none" if final_only else "auto")

            try:
                assistant = await within_budget(asyncio.to_thread(model_turn))
            except DiagnosisProtocolError as exc:
                state["phase"] = "validation"
                repair("Assistant response protocol error: " + str(exc), call_dir)
                continue
            if redact(assistant, secrets) != assistant:
                raise ValueError("Known credential value appears in the diagnosis response")
            calls = assistant.get("tool_calls") or []
            if calls or (isinstance(assistant.get("content"), str) and assistant["content"].strip()):
                messages.append(assistant)
            if not calls:
                state["phase"] = "validation"
                content = assistant.get("content")
                if not isinstance(content, str) or not content.strip():
                    repair("Final diagnosis response is empty", call_dir)
                    continue
                (output / "model_output.txt").write_text(redact(content, secrets), encoding="utf-8")
                (call_dir / "diagnosis_output.txt").write_text(redact(content, secrets), encoding="utf-8")
                try:
                    payload = parse_json_object(content)
                except ValueError as exc:
                    detail = ("Tool-call markup was returned as ordinary text. Use the API's tool_calls "
                              "interface for tool requests; no markup was executed." if "DSML" in content
                              else "Final response is not a strict JSON object: " + str(exc))
                    repair(detail, call_dir)
                    continue
                if redact(payload, secrets) != payload:
                    raise ValueError("Known credential value appears in decoded diagnosis content")
                try:
                    report = validate_report(payload, context, require_english=True)
                except ValueError as exc:
                    repair(str(exc), call_dir, payload)
                    continue
                remaining_time()
                write_json(output / "validation.json", {"status": "passed",
                    "scope": "basic_schema_target_and_source_references",
                    "text_view": "terminal-escapes-v1", "semantic_correctness": "not_verified"})
                write_json(call_dir / "validation.json", {"status": "passed",
                    "semantic_correctness": "not_verified"})
                write_json(output / "report.json", report)
                (output / "report.md").write_text(render_report(report, context), encoding="utf-8")
                remaining_time()
                state.update(status="completed", diagnosis_status=report.status,
                             semantic_status=report.semantic_status, conflict_count=len(report.conflicts),
                             report_path=str(output / "report.json"))
                break
            if final_only:
                raise ValueError("Model requested tools after the inspection budget was exhausted")
            state["phase"] = "inspection"
            image_observations: list[dict] = []
            for call in calls:
                remaining_time()
                call_id = call["id"]
                if call_id in seen_call_ids:
                    raise ValueError("Repeated tool call identifier across diagnosis turns")
                seen_call_ids.add(call_id)
                name = call["function"]["name"]
                entry: dict[str, Any] = {"call_id": call_id, "name": name, "started_at": now()}
                if not time_only and state["tool_calls"] >= config.max_tool_calls:
                    result = {"text": "Tool-call budget exhausted; no inspection was performed.",
                              "evidence": [], "images": [], "error": "tool_budget_exhausted"}
                else:
                    state["tool_calls"] += 1
                    try:
                        arguments = parse_json_object(call["function"]["arguments"])
                        entry["arguments"] = arguments
                        if name not in available_tools:
                            raise ValueError("Unknown inspection tool; only declared tools are available")
                        result = (views.call(name, arguments) if name in EvidenceViews.names
                                  else await within_budget(inspector.call(name, arguments)))
                        remaining_time()
                    except TimeoutError:
                        raise
                    except (ValueError, OSError) as exc:
                        result = {"text": "Inspection failed: " + redact(str(exc), secrets),
                                  "evidence": [], "images": [], "error": type(exc).__name__}
                if not isinstance(result, dict):
                    raise ValueError("Inspection result must be an object")
                entry.update(finished_at=now(), result=jsonable(result))
                try:
                    if redact(jsonable(result), secrets) != jsonable(result):
                        raise ValueError("Known credential value appears in inspection output")
                    new_items = _new_evidence(result, context)
                    content, image_parts = _tool_content(result, output, secrets)
                    if image_parts and not new_items:
                        raise ValueError("Inspection images require recorded artifact evidence")
                except (ValueError, OSError) as exc:
                    entry.update(status="rejected", error=redact(str(exc), secrets),
                                 input_digest=context.input_digest)
                    record_tool(entry)
                    raise
                normalized_items = views.add(new_items)
                normalized_result = {**result, "text": normalize_terminal_text(result["text"]),
                                     "evidence": normalized_items}
                content, image_parts = _tool_content(normalized_result, output, secrets)
                views.observe(normalized_items)
                entry.update(status="recorded", input_digest=context.input_digest)
                record_tool(entry)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": content})
                if image_parts:
                    image_observations.append({"type": "text", "text": json.dumps({
                        "source": "inspection_artifact", "tool_call_id": call_id,
                        "evidence_ids": [item.evidence_id for item in new_items],
                        "image_paths_in_order": [str(value) for value in result.get("images", [])],
                    }, ensure_ascii=False)})
                    image_observations.extend(image_parts)
                write_json(output / "input.json", context)
                write_json(output / "messages.json", messages)
                save()
            if image_observations:
                # Chat Completions image input is supported in user messages. Finish
                # every tool response first so an image message never splits a batch.
                messages.append({"role": "user", "content": [{"type": "text", "text": runtime_prompts["image_observation"]},
                    *image_observations]})
                write_json(output / "messages.json", messages)
        else:
            raise ValueError("Diagnosis round budget exhausted without a final report")
    except TimeoutError:
        state.update(status="timeout", error="Live diagnosis exceeded its time budget")
    except asyncio.CancelledError:
        state.update(status="cancelled", error="Live diagnosis was cancelled")
        raise
    except (ValueError, OSError) as exc:
        state.update(status="failed", error=redact(str(exc), secrets))
        if state["phase"] == "validation":
            write_json(output / "validation.json", {"status": "failed", "error": state["error"],
                                                      "semantic_correctness": "not_verified"})
    finally:
        state.update(finished_at=now(), input_digest=context.input_digest,
                     evidence_count=len(context.evidence), usage=_usage_summary(output))
        if views is not None:
            views.persist()
            state["context_mode"] = views.mode
        if messages:
            write_json(output / "messages.json", redact(messages, secrets))
        write_json(output / "input.json", redact(jsonable(context), secrets))
        save()
    return state

