"""Codex CLI invocation and event parsing for file-based SkillPatch agents."""

import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def build_command(config, workspace: Path, output: Path):
    if config.command:
        command = list(config.command)
    else:
        # Prefer a compiled local harness, then the installed Codex CLI.
        candidates = [ROOT / 'harness/codex/codex-rs/target' / mode / 'codex'
                      for mode in ('release', 'debug')]
        binary = next((str(path) for path in candidates
                       if path.is_file() and os.access(path, os.X_OK)), None)
        binary = binary or shutil.which('codex')
        if not binary:
            raise ValueError('Codex CLI not found; install it or set diagnosis.command')
        command = [binary]
    provider = ('{ name = "SkillPatch method model", base_url = ' + json.dumps(config.base_url)
                + ', env_key = ' + json.dumps(config.api_key_env)
                + ', wire_api = "responses", requires_openai_auth = false }')
    command += ['exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
                '--skip-git-repo-check', '--color', 'never', '--sandbox', 'workspace-write',
                '-C', str(workspace), '-m', config.model,
                '-c', 'approval_policy="never"',
                '-c', 'cli_auth_credentials_store="ephemeral"',
                '-c', 'model_provider="skillpatch_method"',
                '-c', 'model_providers.skillpatch_method=' + provider,
                '--output-last-message', str(output / 'last_message.txt'), '-']
    return command


def collect_events(path: Path):
    """Require a completed turn and a final assistant message, not tool success."""
    completed = False
    text = None
    errors = []
    reconnect_errors = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind in {'turn.failed', 'error'}:
            error = event.get('error')
            detail = error.get('message') if isinstance(error, dict) else error
            message = detail or event.get('message') or 'unknown error'
            destination = reconnect_errors if kind == 'error' and message.startswith('Reconnecting...') else errors
            destination.append(f'Codex {kind}: {message}')
        elif kind == 'turn.completed':
            completed = True
        elif kind == 'item.completed':
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                message = item.get('text')
                if isinstance(message, str) and message.strip():
                    text = message
    if not (completed and text):
        errors.extend(reconnect_errors)
    return completed and bool(text), text, errors
