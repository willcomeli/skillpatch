"""Apply a task-specific verifier budget explicitly approved for a run."""

import copy
from functools import wraps
import json
from pathlib import Path

from excutor.executor_adapter.storage import write_json


def requested_policy(arguments):
    if '--config' not in arguments:
        return None
    config = Path(arguments[arguments.index('--config') + 1]).resolve()
    for folder in config.parents:
        marker = folder / 'VERIFIER_BUDGET_REQUESTED.json'
        if marker.is_file():
            return json.loads(marker.read_text())
    return None


def budgeted_verifier(native, policy):
    rules = policy['tasks']
    if rules != {'fix-build-agentops': {'original_seconds': 240, 'seconds': 900}}:
        raise ValueError('Verifier budget differs from the explicit AgentOps-only approval')

    @wraps(native)
    async def verify(env, task, paths, timing, planes, sandbox_user=None, workspace=None):
        rule = rules.get(task.name)
        if rule is None:
            return await native(env, task, paths, timing, planes,
                                sandbox_user=sandbox_user, workspace=workspace)
        original = task.config.verifier
        if original.timeout_sec != rule['original_seconds']:
            raise ValueError('Original AgentOps verifier budget changed')
        override = copy.copy(original)
        override.timeout_sec = rule['seconds']
        task.config.verifier = override
        write_json(paths.rollout_dir / 'verifier-budget-override.json', {
            'task': task.name, 'original_seconds': original.timeout_sec,
            'seconds': override.timeout_sec, 'scope': 'Explicit user-approved AgentOps follow-up scoring',
            'agent_and_other_task_budgets': 'unchanged', 'verifier_script': 'unchanged'})
        try:
            return await native(env, task, paths, timing, planes,
                                sandbox_user=sandbox_user, workspace=workspace)
        finally:
            task.config.verifier = original

    return verify


def enable(policy):
    from benchflow import rollout
    from benchflow.rollout import _setup
    if getattr(_setup, '_skillpatch_verifier_budget_policy', None) is not None:
        if _setup._skillpatch_verifier_budget_policy != policy:
            raise ValueError('Verifier budget policy changed inside a backend process')
        return
    wrapped = budgeted_verifier(_setup._verify_rollout, policy)
    _setup._verify_rollout = rollout._verify_rollout = wrapped
    _setup._skillpatch_verifier_budget_policy = policy
