"""ScienceWorld action prompting and score-aware history."""

import json
import re
from typing import Any, Dict, List

from ..runner_common.agents import (ActionAgent, _extract_tagged_action, _extract_tagged_thought,
                             _fallback_action, _match_admissible_action)
from .prompts import SCIENCEWORLD_ACTION_AGENT_PROMPT

def _format_score(value: Any) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return "unavailable"


def _format_scienceworld_recent_history(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No previous steps."
    parts: List[str] = []
    for index, raw in enumerate(rows):
        row = raw if isinstance(raw, dict) else {}
        before = row.get("score_before")
        after = row.get("score_after")
        delta = row.get("score_delta")
        if before is None or after is None or delta is None:
            score_line = "Score: unavailable"
        else:
            try:
                delta_text = f"{float(delta):+.4f}".rstrip("0").rstrip(".")
            except (TypeError, ValueError):
                delta_text = "unavailable"
            score_line = (
                f"Score: {_format_score(before)} -> {_format_score(after)} "
                f"(delta {delta_text})"
            )
        parts.append(
            "\n".join(
                [
                    f"Step {row.get('step', index)}",
                    f"Observation: {str(row.get('observation') or '')}",
                    f"Action: {str(row.get('action') or '')}",
                    f"Result: {str(row.get('next_observation') or row.get('env_feedback') or '')}",
                    score_line,
                ]
            )
        )
    return "\n\n".join(parts)


class ScienceWorldActionAgent(ActionAgent):
    def __init__(self, llm: Any):
        super().__init__(llm, system_prompt=SCIENCEWORLD_ACTION_AGENT_PROMPT)

    def act(self, task, context, world_knowledge=None, retrieved_memory_text="", recent_history=None, step_id=0):
        max_steps = context.get("max_steps")
        progress = (
            f"Step {int(step_id) + 1} of at most {int(max_steps)}."
            if max_steps not in (None, "")
            else f"Step {int(step_id) + 1}."
        )
        user_prompt = (
            f"## Task\n{str(task or '')}\n\n"
            f"## Task Family\n{str(context.get('task_family') or 'scienceworld')}\n\n"
            "## Skill Knowledge\n"
            f"{retrieved_memory_text or 'None'}\n\n"
            "## Progress\n"
            f"{progress}\n"
            f"Current score: {_format_score(context.get('current_score'))}\n\n"
            "## Recent History\n"
            f"{_format_scienceworld_recent_history(recent_history or [])}\n\n"
            "## Current Observation\n"
            f"{str(context.get('observation', ''))}\n\n"
        )
        admissible_actions = [str(x) for x in context.get("admissible_actions") or []]
        if admissible_actions:
            user_prompt += (
                "Candidate actions available in this state:\n"
                f"{json.dumps(admissible_actions, ensure_ascii=False)}\n\n"
            )
        user_prompt += "Now choose the next action."
        text = self.llm.chat(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        thought = _extract_tagged_thought(text)
        action = _extract_tagged_action(text)
        if admissible_actions:
            action = _match_admissible_action(action, admissible_actions)
            if not action:
                action = _fallback_action(text, admissible_actions)
        elif not action:
            match = re.search(r"Action:\s*(.+)", str(text or ""), flags=re.I)
            action = match.group(1).strip().strip("\"'`*") if match else ""
        return {"thought": thought, "action": action.strip()}
