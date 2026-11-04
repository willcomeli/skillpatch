"""Shared action parsing and base agent; benchmark prompts live with their adapters."""

import json
import re
from typing import Any, Dict, List

from .json_utils import extract_json_obj

def _format_recent_history(rows: List[Dict[str, Any]]) -> str:
    parts = []
    for row in rows or []:
        step = row.get("step", len(parts)) if isinstance(row, dict) else len(parts)
        observation = str((row or {}).get("observation", "")) if isinstance(row, dict) else ""
        action = str((row or {}).get("action", "")) if isinstance(row, dict) else str(row)
        if observation:
            parts.append(f"Step {step}: observation={observation} action={action}")
        else:
            parts.append(f"Step {step}: action={action}")
    return "\n".join(parts) if parts else "None"


def _extract_tagged_action(text: str) -> str:
    match = re.search(r"<action>\s*(.*?)\s*</action>", str(text or ""), flags=re.I | re.S)
    return match.group(1).strip() if match else ""


def _extract_tagged_thought(text: str) -> str:
    match = re.search(r"<think>\s*(.*?)\s*</think>", str(text or ""), flags=re.I | re.S)
    return match.group(1).strip() if match else ""


def _match_admissible_action(candidate: str, admissible_actions: List[str]) -> str:
    candidate = str(candidate or "").strip()
    actions = [str(x).strip() for x in admissible_actions or [] if str(x).strip()]
    if not actions:
        return candidate
    if candidate in actions:
        return candidate
    normalized = re.sub(r"\s+", " ", candidate).strip().lower()
    for action in actions:
        if re.sub(r"\s+", " ", action).strip().lower() == normalized:
            return action
    lower_text = str(candidate or "").lower()
    matches = [action for action in actions if action.lower() in lower_text]
    if len(matches) == 1:
        return matches[0]
    return candidate


def _fallback_action(text: str, admissible_actions: List[str]) -> str:
    tagged = _extract_tagged_action(text)
    if tagged:
        return _match_admissible_action(tagged, admissible_actions)
    actions = [str(x).strip() for x in admissible_actions or [] if str(x).strip()]
    lower_text = str(text or "").lower()
    matches = [action for action in actions if action.lower() in lower_text]
    if len(matches) == 1:
        return matches[0]
    return actions[0] if actions else ""


class ActionAgent:
    def __init__(self, llm: Any, system_prompt: str = ""):
        self.llm = llm
        self.system_prompt = system_prompt

    def act(self, task, context, world_knowledge=None, retrieved_memory_text="", recent_history=None, step_id=0):
        payload = {
            "task": task,
            "observation": context.get("observation", ""),
            "admissible_actions": context.get("admissible_actions") or [],
            "skills": retrieved_memory_text or "",
            "recent_history": recent_history or [],
            "step": step_id,
        }
        if world_knowledge:
            payload["world_knowledge"] = world_knowledge
        text = self.llm.chat(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )
        obj = extract_json_obj(text) or {}
        action = str(obj.get("action") or "").strip()
        if not action:
            action = _fallback_action(text, payload["admissible_actions"])
        return {
            "thought": str(obj.get("thought") or _extract_tagged_thought(text) or ""),
            "action": action,
        }
