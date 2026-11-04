"""ALFWorld action prompting and history formatting."""

from typing import Any, Dict, List

from ..runner_common.agents import (ActionAgent, _extract_tagged_action, _extract_tagged_thought,
                             _fallback_action, _match_admissible_action)
from .prompts import ALFWORLD_ACTION_AGENT_PROMPT, ALFWORLD_TEMPLATE, ALFWORLD_TEMPLATE_NO_HIS

def _format_alfworld_recent_history(rows: List[Dict[str, Any]]) -> str:
    parts = []
    for index, row in enumerate(rows or []):
        item = row if isinstance(row, dict) else {}
        step_num = int(item.get("step", index) or 0) + 1
        observation = str(item.get("observation") or "")
        action = str(item.get("action") or "")
        parts.append(
            f"[Observation {step_num}: '{observation}', Action {step_num}: '{action}']"
        )
    return "\n".join(parts)


class AlfWorldActionAgent(ActionAgent):
    def __init__(self, llm: Any):
        super().__init__(llm, system_prompt=ALFWORLD_ACTION_AGENT_PROMPT)

    def act(self, task, context, world_knowledge=None, retrieved_memory_text="", recent_history=None, step_id=0):
        admissible_actions = [str(x) for x in context.get("admissible_actions") or []]
        step_count = max(0, int(step_id))
        current_observation = str(context.get("observation") or "")
        admissible_text = "\n ".join(f"'{action}'" for action in admissible_actions)
        skill_prefix = ""
        if str(retrieved_memory_text or "").strip():
            skill_prefix = (
                "\n\n## Skill Knowledge\n"
                "Below is a skill document with learned strategies. "
                "Use these guidelines to inform your decisions:\n\n"
                f"{str(retrieved_memory_text).strip()}\n"
            )
        if step_count <= 0:
            environment_prompt = ALFWORLD_TEMPLATE_NO_HIS.format(
                current_observation=current_observation,
                admissible_actions=admissible_text,
            )
        else:
            history = _format_alfworld_recent_history(recent_history or [])
            environment_prompt = ALFWORLD_TEMPLATE.format(
                task_description=str(task or ""),
                step_count=step_count,
                history_length=len(recent_history or []),
                action_history=history,
                current_step=step_count + 1,
                current_observation=current_observation,
                admissible_actions=admissible_text,
            )
        user_prompt = skill_prefix + environment_prompt
        text = self.llm.chat(
            [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        thought = _extract_tagged_thought(text)
        action = _match_admissible_action(_extract_tagged_action(text), admissible_actions)
        if not action and admissible_actions:
            action = _fallback_action(text, admissible_actions)
            thought = thought or "No parsable action tag; using the first admissible action."
        return {"thought": thought, "action": action.strip()}
