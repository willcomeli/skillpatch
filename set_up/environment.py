"""Load credentials without shell evaluation; redact known secrets in artifacts."""

import os
import re
import shlex
from pathlib import Path
from typing import Any, Mapping

_SECRET_NAME = re.compile(r"(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|AUTH)(?:_|$)", re.I)


def load_environment(env_file: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    if env_file is None:
        return environment
    for number, raw in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid environment assignment at line {number}")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"Invalid environment quoting at line {number}") from None
        if len(parts) > 1:
            raise ValueError(f"Quote environment values containing spaces at line {number}")
        literal = parts[0] if parts else ""
        if "${" in literal or "$(" in literal or "`" in literal:
            raise ValueError(f"Shell expansion is unsupported in environment file at line {number}")
        if not environment.get(name):
            environment[name] = literal
    return environment


def secret_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(sorted({value for name, value in environment.items()
                         if _SECRET_NAME.search(name) and len(value) >= 6}, key=len, reverse=True))


def redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def redact_artifacts(run_dir: Path, secrets: tuple[str, ...]) -> list[str]:
    """Redact captured text, preserving source snapshots and original evidence locations.

    The run directory is private while the backend writes raw files. Redaction
    occurs after the subprocess exits. This is known-value redaction, not a
    guarantee that arbitrary newly generated credentials can be detected.
    """
    changed = []
    if not secrets:
        return changed
    for path in run_dir.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.is_relative_to(run_dir / "input"):
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        cleaned = redact(original, secrets)
        if cleaned != original:
            path.write_text(cleaned, encoding="utf-8")
            changed.append(str(path.relative_to(run_dir)))
    return changed
