"""AppWorld Python-action extraction and conversation construction."""

import json
import re
from typing import Any, Dict, List

from ..runner_common.agents import ActionAgent, _extract_tagged_thought
from ..runner_common.json_utils import extract_json_obj
from .prompts import APPWORLD_ACTION_AGENT_PROMPT, APPWORLD_ACTION_FEW_SHOT_MESSAGES

APPWORLD_FALLBACK_CODE = "print(apis.api_docs.show_app_descriptions())"

def extract_appworld_code(text: str) -> str:
    raw = str(text or "").strip()
    action_match = re.search(r"<action>\s*(.*?)\s*</action>", raw, flags=re.I | re.S)
    action_text = action_match.group(1).strip() if action_match else raw
    fenced = re.search(r"```(?:python|py)\s*\n?(.*?)```", action_text, flags=re.I | re.S)
    if fenced:
        code = fenced.group(1).strip()
        if code:
            return code
    partial = re.search(r"```(?:python|py)\s*\n?(.*)$", action_text, flags=re.I | re.S)
    if partial:
        code = partial.group(1).strip()
        if code:
            return code
    obj = extract_json_obj(action_text) or {}
    code = str(obj.get("code") or "").strip() if isinstance(obj, dict) else ""
    if code:
        return code
    try:
        compile(action_text, "<appworld-action>", "exec")
    except (SyntaxError, ValueError, TypeError):
        return APPWORLD_FALLBACK_CODE
    return action_text or APPWORLD_FALLBACK_CODE


def _appworld_reasoning_without_code(text: str) -> str:
    raw = str(text or "").strip()
    tagged_thought = _extract_tagged_thought(raw)
    if tagged_thought:
        return tagged_thought
    raw = re.sub(r"```(?:python|py)\s*\n?.*?```", "", raw, flags=re.I | re.S)
    raw = re.sub(r"```(?:python|py)\s*\n?.*$", "", raw, flags=re.I | re.S)
    raw = re.sub(r"</?action>", "", raw, flags=re.I)
    obj = extract_json_obj(str(text or "")) or {}
    if isinstance(obj, dict) and obj.get("thought"):
        return str(obj.get("thought") or "").strip()
    return raw.strip()


class AppWorldActionAgent(ActionAgent):
    def __init__(self, llm: Any):
        super().__init__(llm, system_prompt=APPWORLD_ACTION_AGENT_PROMPT)

    def act(self, task, context, world_knowledge=None, retrieved_memory_text="", recent_history=None, step_id=0):
        supervisor = context.get("supervisor") if isinstance(context.get("supervisor"), dict) else {}
        app_descriptions = context.get("app_descriptions") or []
        max_steps = context.get("max_steps")
        task_context = {
            "task": str(task or ""),
            "supervisor": supervisor,
            "datetime": str(context.get("datetime") or ""),
            "app_descriptions": app_descriptions,
            "interaction": int(step_id) + 1,
            "max_interactions": max_steps,
            "skill_md": str(retrieved_memory_text or ""),
        }
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": self.system_prompt},
            *[dict(message) for message in APPWORLD_ACTION_FEW_SHOT_MESSAGES],
            {
                "role": "user",
                "content": (
                    "Actual AppWorld task context:\n"
                    + json.dumps(task_context, ensure_ascii=False, indent=2)
                    + "\n\nGenerate the first Python interaction."
                ),
            },
        ]
        history = [row for row in (recent_history or []) if isinstance(row, dict)]
        for row in history:
            reasoning = str(row.get("thought") or "")
            code = str(row.get("action") or "")
            assistant_content = (reasoning + "\n\n" if reasoning else "") + f"```python\n{code}\n```"
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Output:\n```\n{str(row.get('next_observation') or '')}\n```\n\n"
                        "Continue the actual task with exactly one next Python code block."
                    ),
                }
            )
        text = self.llm.chat(messages)
        return {
            "thought": _appworld_reasoning_without_code(text),
            "action": extract_appworld_code(text),
        }
