"""Time-bounded model sessions over named, immutable evidence archives."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from skillpatch.environment import redact
from skillpatch.evidence_views import EvidenceViews
from skillpatch.evidence_repair import build_evidence_repair_context
from skillpatch.model_client import (
    ChatCompletionClient, DiagnosisConfig, DiagnosisProtocolError, diagnosis_secrets, parse_json_object,
)
from skillpatch.models import DiscoveryInput
from skillpatch.storage import jsonable, now, sha256_file, write_json


RUNTIME_PROMPTS = Path(__file__).resolve().parents[2] / "prompts" / "evidence_session_runtime.json"
_COLLECTIONS = {"source", "baseline", "candidate"}


class _BudgetExceeded(ValueError):
    pass


def _dump(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _merge(target: dict, identifier: str, start: int, end: int) -> None:
    merged = []
    for a, b in sorted([*target.get(identifier, []), (start, end)]):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    target[identifier] = merged


def _take_pending(views: dict[str, EvidenceViews]) -> dict:
    ranges = {name: copy.deepcopy(view.pending) for name, view in views.items()}
    for view in views.values():
        view.discard_pending()
        view.persist()
    return ranges


def _mark_request(blocks: list[dict], views: dict[str, EvidenceViews]) -> None:
    """Commit only excerpts from blocks actually included in this request."""
    for view in views.values():
        view.discard_pending()
    for block in blocks:
        for name, identifiers in block.get("ranges", {}).items():
            for identifier, spans in identifiers.items():
                for start, end in spans:
                    _merge(views[name].pending, identifier, start, end)
    for view in views.values():
        view.commit_visible()


def _definitions(views: dict[str, EvidenceViews]) -> list[dict]:
    definitions = copy.deepcopy(next(iter(views.values())).definitions())
    for definition in definitions:
        parameters = definition["function"]["parameters"]
        parameters["properties"]["collection"] = {"type": "string", "enum": list(views)}
        parameters["required"] = ["collection", *parameters["required"]]
    return definitions


def _labels(item) -> dict:
    labels = {"evidence_id": item.evidence_id, "kind": item.kind, "text_chars": len(item.text)}
    for name in ("skill_id", "relative_path", "version", "availability", "source_role"):
        value = getattr(item, name)
        if value is not None:
            labels[name] = value
    return labels


def _initial_view(views: dict[str, EvidenceViews], priority: dict, budget: int) -> tuple[str, dict]:
    """Never fill the window with long auxiliary files or system/XML dumps."""
    metadata = {name: {"run_id": view.context.run_id, "input_digest": view.context.input_digest,
                       "trajectory_complete": view.context.trajectory_complete,
                       "evidence_items": len(view.context.evidence),
                       "limitations": view.context.limitations}
                for name, view in views.items()}
    text = "EVIDENCE COLLECTIONS\n" + _dump(metadata) + "\n"
    if budget < len(text) + 300:
        raise _BudgetExceeded("input_metadata_exceeds_context_budget")

    def excerpt_text(name, item, size):
        return "EXCERPT\n" + _dump({"collection": name, "excerpt": {
            **_labels(item), "start_char": 0, "end_char": size,
            "has_more": size < len(item.text), "text": item.text[:size]}}) + "\n"

    # Small archives need no paging, including long individual trajectory items.
    complete = text
    for name, view in views.items():
        for item in view.context.evidence:
            complete += excerpt_text(name, item, len(item.text))
            if len(complete) > budget:
                break
        if len(complete) > budget:
            break
    if len(complete) <= budget:
        for view in views.values():
            view.observe(view.context.evidence)
        return complete, _take_pending(views)

    index_allowance = max(100, (budget - len(text)) // 5 // len(views))
    for name, view in views.items():
        view.mode = "indexed"
        entries = []
        size = 0
        for item in view.context.evidence:
            entry = _labels(item)
            if size + len(_dump(entry)) > index_allowance:
                break
            entries.append(entry)
            size += len(_dump(entry))
        text += "INDEX\n" + _dump({"collection": name, "items": entries,
            "next_offset": len(entries) if len(entries) < len(view.context.evidence) else None}) + "\n"

    def rank(name, item):
        if item.kind == "task":
            return 0
        if item.evidence_id in priority.get(name, []) and item.kind == "skill":
            return 1
        if item.kind == "feedback":
            return 2
        if item.evidence_id in priority.get(name, []):
            return 3
        if item.kind == "trajectory":
            return 4
        return 5

    ordered = [(name, item) for name, view in views.items() for item in view.context.evidence]
    ordered.sort(key=lambda pair: rank(*pair))
    for name, item in ordered:
        selected = item.evidence_id in priority.get(name, [])
        if not selected:
            if item.kind in {"skill", "artifact"} and len(item.text) > 12000:
                continue
            if item.kind == "trajectory" and len(item.text) > 12000 and any(
                    token in item.text[:1000].lower() for token in ("system prompt", "<system", "<context", "<environment")):
                continue
        cap = 20000 if selected else (12000 if item.kind == "trajectory" else 8000)
        if item.kind == "trajectory" and not selected and len(item.text) > 12000:
            cap = 400
        remaining = budget - len(text)
        size = min(len(item.text), cap)
        if len(excerpt_text(name, item, size)) > remaining:
            size = min(size, max(0, (remaining - len(_dump(_labels(item))) - 250) // 6))
        if size <= 0:
            continue
        result = views[name].call("read_evidence", {"evidence_id": item.evidence_id, "max_chars": size})
        text += "EXCERPT\n" + _dump({"collection": name, "excerpt": parse_json_object(result["text"])}) + "\n"
    ranges = _take_pending(views)
    if len(text) > budget:
        raise _BudgetExceeded("initial_evidence_view_exceeds_context_budget")
    return text, ranges


def _coverage(views: dict[str, EvidenceViews]) -> dict:
    result = {}
    for name, view in views.items():
        result[name] = {
            "fully_presented": sum(view.fully_presented(item) for item in view.context.evidence),
            "total_items": len(view.context.evidence),
            "presented_ranges": {identifier: spans[-8:]
                                 for identifier, spans in list(view.presented.items())[-20:]},
            "additional_spans": {identifier: len(spans) - 8
                                 for identifier, spans in list(view.presented.items())[-20:] if len(spans) > 8},
            "additional_range_entries": max(0, len(view.presented) - 20),
        }
    return result


def _usage(output: Path) -> dict:
    records = []
    for path in sorted((output / "calls").glob("*/model_call.json")):
        try:
            records.append(parse_json_object(path.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            records.append({})
    result = {"recorded_calls": len(records)}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [record["usage"].get(name) if isinstance(record.get("usage"), dict) else None for record in records]
        result[name] = sum(values) if values and all(type(value) is int and value >= 0 for value in values) else None
    return result


def recorded_evidence_session(config: DiagnosisConfig, data: str, contexts: dict[str, DiscoveryInput],
                              output: Path, environment: Mapping[str, str], *, client=None,
                              priority_ids: dict[str, list[str]] | None = None,
                              validate: Callable[[dict, dict[str, EvidenceViews]], None] | None = None,
                              session_timeout_sec: int = 900,
                              budget_fallback: Callable[[str], dict] | None = None) -> dict:
    """Return checked JSON, raising on transport failure.

    Contexts are copied before normalization. Coverage means text included in a
    completed model request, not merely read locally or queued for a later turn.
    A timeout stops this session; an already issued remote request cannot be revoked.
    Bounded legacy clients retain their single-call validation behavior.
    """
    output = output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    secrets = diagnosis_secrets(config, environment)
    started = time.monotonic()
    deadline = started + session_timeout_sec
    time_budget_only = config.time_budget_only
    state = {"schema_version": 1, "status": "running", "started_at": now(), "model_calls": 0,
             "tool_calls": 0, "report_repairs": 0, "context_compactions": 0,
             "time_budget_only": time_budget_only,
             "max_rounds": None if time_budget_only else config.max_rounds,
             "max_tool_calls": None if time_budget_only else config.max_tool_calls,
             "max_report_repairs": None if time_budget_only else config.max_report_repairs,
             "session_timeout_sec": session_timeout_sec}
    views: dict[str, EvidenceViews] = {}
    messages: list[dict] = []
    blocks: list[dict] = []
    client = client or ChatCompletionClient()
    legacy = not callable(getattr(client, "turn", None))
    state["mode"] = "archived_evidence_tools"
    if legacy:
        state["mode"] = "completion_compatibility" if time_budget_only else "single_completion_compatibility"
    def save() -> None:
        write_json(output / "session.json", redact(state, secrets))

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise _BudgetExceeded("session_time_exhausted")
        return value

    def persist_payload(payload: dict) -> None:
        write_json(output / "model_output.json", payload)
        write_json(output / "payload.json", payload)

    def check_payload(payload: dict) -> None:
        if redact(payload, secrets) != payload:
            raise ValueError("Known credential value appears in decoded Stage 2 output")
        if validate is not None:
            validate(payload, views)

    def call_model(call_dir: Path, final_only: bool):
        history = copy.deepcopy(messages)

        def request_model():
            call_config = replace(config, timeout_sec=min(config.timeout_sec, remaining()))
            if legacy:
                return client.complete(call_config, history, environment, call_dir)
            return client.turn(call_config, history, environment, call_dir,
                               tools=definitions, tool_choice="none" if final_only else "auto")

        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(request_model)
            try:
                result = future.result(timeout=remaining())
            except FutureTimeout:
                if future.done():
                    raise  # A transport timeout, not a format-repair opportunity.
                future.cancel()
                raise _BudgetExceeded("session_time_exhausted") from None
            except _BudgetExceeded:
                future.cancel()
                raise
            remaining()
            return {"role": "assistant", "content": result} if legacy else result
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    try:
        save()
        if type(session_timeout_sec) is not int or session_timeout_sec <= 0:
            raise ValueError("session_timeout_sec must be a positive integer")
        if type(time_budget_only) is not bool:
            raise ValueError("time_budget_only must be a boolean")
        if (not contexts or not set(contexts) <= _COLLECTIONS
                or any(not isinstance(context, DiscoveryInput) for context in contexts.values())):
            raise ValueError("Evidence collections must be named source, baseline, or candidate")
        count_budgets = () if time_budget_only else (config.max_rounds, config.max_tool_calls)
        if any(type(value) is not int or value <= 0 for value in (*count_budgets, config.max_input_chars)):
            raise ValueError("Session round, tool and context budgets must be positive integers")
        if not time_budget_only and (type(config.max_report_repairs) is not int
                                     or not 0 <= config.max_report_repairs <= 5):
            raise ValueError("max_report_repairs must be between zero and five")
        max_calls = None if time_budget_only else (1 if legacy else config.max_rounds + config.max_report_repairs)
        state["max_model_calls"] = max_calls
        priority = priority_ids or {}
        if set(priority) - set(contexts):
            raise ValueError("Priority evidence refers to an absent collection")
        for name, identifiers in priority.items():
            known = {item.evidence_id for item in contexts[name].evidence}
            if not isinstance(identifiers, list) or any(not isinstance(item, str) or item not in known for item in identifiers):
                raise ValueError("Priority evidence IDs must belong to their selected collection")
        prompt = config.prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("Stage 2 prompt is empty")
        runtime = parse_json_object(RUNTIME_PROMPTS.read_text(encoding="utf-8"))
        if set(runtime) != {"initial_inspection", "context_compaction", "final_budget", "repair"} or any(
                not isinstance(value, str) or not value.strip() for value in runtime.values()):
            raise ValueError("Invalid evidence-session runtime prompts")
        data = data if isinstance(data, str) else _dump(data)
        raw_input = {"contexts": jsonable(contexts), "prompt": prompt, "data": data, "runtime": runtime}
        if redact(raw_input, secrets) != raw_input:
            raise ValueError("Known credential value appears in Stage 2 input")
        write_json(output / "config.json", config)
        write_json(output / "runtime_prompts.json", runtime)
        (output / "prompt.md").write_text(prompt, encoding="utf-8")
        (output / "data.txt").write_text(data, encoding="utf-8")
        write_json(output / "prompt_version.json", {"sha256": sha256_file(output / "prompt.md"),
            "runtime_sha256": sha256_file(output / "runtime_prompts.json")})
        for name, context in contexts.items():
            cloned = copy.deepcopy(context)
            if len({item.evidence_id for item in cloned.evidence}) != len(cloned.evidence):
                raise ValueError("Evidence identifiers must be unique within each collection")
            views[name] = EvidenceViews(cloned, output / "collections" / name)
            write_json(output / "collections" / name / "input.json", cloned)
        state["collections"] = {name: {"original_input_digest": contexts[name].input_digest,
            "view_input_digest": view.context.input_digest} for name, view in views.items()}
        definitions = _definitions(views)
        write_json(output / "tools.json", definitions)
        fixed = [{"role": "system", "content": prompt}, {"role": "user", "content": data + "\n" + runtime["initial_inspection"]}]
        # Account for tool schemas and reserve room for per-request coverage/budgets.
        window_budget = config.max_input_chars - len(_dump(fixed)) - len(_dump(definitions)) - 2500
        if window_budget < 1000:
            raise _BudgetExceeded("input_metadata_exceeds_context_budget")
        initial, ranges = _initial_view(views, priority, max(1000, window_budget // 2))
        base = {"messages": [*fixed, {"role": "user", "content": initial}], "ranges": ranges}
        blocks = [base]
        seen_call_ids = set()
        recent_tools = []
        repair_message = None
        while max_calls is None or state["model_calls"] < max_calls:
            round_number = state["model_calls"] + 1
            payload = None
            remaining()
            final_only = not time_budget_only and (round_number >= config.max_rounds
                                                   or state["tool_calls"] >= config.max_tool_calls)
            status = {"model_calls_remaining": None if time_budget_only else max_calls - state["model_calls"],
                      "inspection_calls_remaining": None if time_budget_only else max(0, config.max_tool_calls - state["tool_calls"]),
                      "seconds_remaining": round(remaining(), 2), "coverage": _coverage(views),
                      "recent_tools": recent_tools[-5:]}
            tail = [{"role": "user", "content": "SESSION ACCESS AND BUDGETS\n" + _dump(status)}]
            if final_only and not legacy:
                tail.append({"role": "system", "content": runtime["final_budget"]})
            if repair_message:
                tail.append({"role": "user", "content": repair_message})
            messages = [message for block in blocks for message in block["messages"]] + tail
            if len(_dump(messages)) + len(_dump(definitions)) > config.max_input_chars:
                write_json(output / "context_windows" / f"{state['context_compactions']:03d}.json", messages)
                latest = blocks[-1] if len(blocks) > 1 else None
                note = {"messages": [{"role": "user", "content": runtime["context_compaction"]}], "ranges": {}}
                blocks = [base, note]
                if latest is not None:
                    trial = [message for block in [*blocks, latest] for message in block["messages"]] + tail
                    if len(_dump(trial)) + len(_dump(definitions)) <= config.max_input_chars:
                        blocks.append(latest)
                state["context_compactions"] += 1
                messages = [message for block in blocks for message in block["messages"]] + tail
                if len(_dump(messages)) + len(_dump(definitions)) > config.max_input_chars:
                    raise _BudgetExceeded("bounded_context_exhausted")
            if redact(messages, secrets) != messages:
                raise ValueError("Known credential value appears in Stage 2 model messages")
            call_dir = output / "calls" / f"{round_number:03d}"
            call_dir.mkdir(parents=True)
            write_json(call_dir / "messages.json", messages)
            write_json(output / "messages.json", messages)
            state.update(model_calls=state["model_calls"] + 1, phase="model_call", input_chars=len(_dump(messages)))
            save()
            try:
                assistant = call_model(call_dir, final_only)
            except DiagnosisProtocolError as exc:
                if legacy and not time_budget_only:
                    raise
                error = str(exc)
                raw_path = call_dir / "model_output.txt"
                if raw_path.is_file():
                    (output / "model_output.txt").write_text(redact(raw_path.read_text(), secrets), encoding="utf-8")
                assistant = None
            else:
                _mark_request(blocks, views)
                write_json(call_dir / "assistant.json", redact(assistant, secrets))
                if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
                    raise ValueError("Session client must return an assistant message")
                calls = assistant.get("tool_calls") or []
                if calls:
                    if legacy or final_only:
                        error = "Tool calls are unavailable after the inspection budget is exhausted"
                    else:
                        turn = [assistant]
                        for call in calls:
                            call_id = call["id"]
                            if call_id in seen_call_ids:
                                raise ValueError("Repeated evidence tool call identifier")
                            seen_call_ids.add(call_id)
                            name = call["function"]["name"]
                            entry = {"call_id": call_id, "name": name, "started_at": now()}
                            try:
                                if not time_budget_only and state["tool_calls"] >= config.max_tool_calls:
                                    raise ValueError("Evidence tool-call budget exhausted")
                                state["tool_calls"] += 1
                                remaining()
                                args = parse_json_object(call["function"]["arguments"])
                                entry["arguments"] = args
                                collection = args.get("collection")
                                if not isinstance(collection, str) or collection not in views:
                                    raise ValueError("collection must select a named evidence archive")
                                result = views[collection].call(name, {key: value for key, value in args.items() if key != "collection"})
                                response = {"collection": collection, "result": parse_json_object(result["text"])}
                            except _BudgetExceeded:
                                raise
                            except ValueError as exc:
                                response = {"error": redact(str(exc), secrets), "evidence_displayed": False}
                            entry.update(finished_at=now(), result=response)
                            with (output / "tool_calls.jsonl").open("a", encoding="utf-8") as stream:
                                stream.write(_dump(redact(entry, secrets)) + "\n")
                            recent_tools.append({"call_id": call_id, "name": name, "arguments": entry.get("arguments"),
                                                 "error": response.get("error")})
                            turn.append({"role": "tool", "tool_call_id": call_id, "content": _dump(response)})
                        blocks.append({"messages": turn, "ranges": _take_pending(views)})
                        repair_message = None
                        continue
                else:
                    content = assistant.get("content")
                    if not isinstance(content, str):
                        raise ValueError("Session final response must contain text")
                    (output / "model_output.txt").write_text(redact(content, secrets), encoding="utf-8")
                    try:
                        payload = parse_json_object(content)
                        if redact(payload, secrets) != payload:
                            raise ValueError("Known credential value appears in decoded Stage 2 output")
                        persist_payload(payload)
                        check_payload(payload)
                    except ValueError as exc:
                        if "credential value" in str(exc) or (legacy and not time_budget_only):
                            raise
                        error = str(exc)
                        blocks.append({"messages": [assistant], "ranges": {}})
                    else:
                        if time_budget_only:
                            remaining()
                        write_json(output / "validation.json", {"status": "passed", "scope": "caller_schema_and_evidence_visibility",
                            "semantic_correctness": "not_verified"})
                        state.update(status="completed", phase="validation")
                        return payload
            write_json(call_dir / "validation_error.json", {"error": redact(error, secrets)})
            if not time_budget_only and state["report_repairs"] >= config.max_report_repairs:
                raise _BudgetExceeded("report_repair_budget_exhausted: " + redact(error, secrets))
            state["report_repairs"] += 1
            repair_message = runtime["repair"].replace("{error}", redact(error, secrets))
            repair_context = build_evidence_repair_context(
                error, payload, views, max_chars=min(6000, config.max_input_chars // 12),
            )
            if repair_context:
                repair_message += "\n" + repair_context
            repair_message = redact(repair_message, secrets)
            write_json(call_dir / "repair_feedback.json", {
                "error": redact(error, secrets), "message": repair_message,
            })
        raise _BudgetExceeded("model_round_budget_exhausted")
    except _BudgetExceeded as exc:
        reason = str(exc)
        state.update(status="budget_exhausted", cause=reason)
        if budget_fallback is None:
            raise
        try:
            payload = budget_fallback(reason)
            if not isinstance(payload, dict):
                raise ValueError("Budget fallback must return a JSON object")
            if (payload.get("status") not in (None, "insufficient_evidence", "uncertain", "pending")
                    or payload.get("target_status") not in (None, "uncertain")
                    or payload.get("baseline_status") not in (None, "uncertain")):
                raise ValueError("Budget exhaustion cannot assert a supported or successful result")
            check_payload(payload)
            persist_payload(payload)
            write_json(output / "budget_fallback.json", {"cause": reason, "payload": payload,
                "origin": "deterministic_program_fallback", "semantic_correctness": "not_verified"})
        except Exception as failure:
            state.update(status="failed", error=redact(f"{type(failure).__name__}: {failure}", secrets))
            raise
        return payload
    except KeyboardInterrupt:
        state.update(status="cancelled", error="Evidence session interrupted")
        raise
    except Exception as exc:
        state.update(status="failed", error=redact(f"{type(exc).__name__}: {exc}", secrets))
        raise
    finally:
        for view in views.values():
            view.discard_pending()
            view.persist()
        usage = _usage(output)
        state.update(finished_at=now(), elapsed_sec=round(time.monotonic() - started, 3),
                     usage=usage, coverage=_coverage(views))
        write_json(output / "usage.json", usage)
        save()
