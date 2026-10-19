"""Portable access to a verified execution while its environment is still alive.

Backends call the verified callback after persisting verification and await it
before cleanup. Concrete SDK objects stay behind the inspection interface.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from skillpatch.environment import redact
from skillpatch.models import BackendRun
from skillpatch.storage import now, write_json


class InspectionTools(Protocol):
    async def prepare(self) -> dict[str, Any]: ...

    def definitions(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class VerifiedSession:
    """Only valid until the backend's awaited verified callback returns.

``execution`` identifies the persisted original execution, and ``inspector``
offers observation tools. Metadata records capabilities and collection gaps.
The callback must not append diagnosis events to the original task trajectory.
"""

    execution: BackendRun
    inspector: InspectionTools
    metadata: dict[str, Any] = field(default_factory=dict)


VerifiedCallback = Callable[[VerifiedSession], Awaitable[dict[str, Any]]]


async def invoke_verified_callback(callback: VerifiedCallback, session: VerifiedSession,
                                   output: Path, *, timeout_sec: int,
                                   secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    """Isolate diagnosis failures from task results; cancellation still propagates.

The backend owns cleanup in its outer finally block, including when this helper
raises cancellation. The original execution and rewards are never rewritten.
"""
    state: dict[str, Any] = {
        "schema_version": 1, "status": "running", "started_at": now(),
        "phase": "post_verification_pre_cleanup", "mode": "live_post_verification",
    }
    write_json(output, state)
    try:
        if type(timeout_sec) is not int or timeout_sec <= 0:
            raise ValueError("diagnosis_timeout_sec must be a positive integer")
        result = await asyncio.wait_for(callback(session), timeout=timeout_sec)
        if not isinstance(result, dict) or result.get("status") not in {
            "completed", "skipped", "failed", "timeout", "cancelled", "unavailable",
        }:
            raise ValueError("Diagnosis callback returned an invalid result status")
        state.update(result)
    except asyncio.TimeoutError:
        state.update(status="timeout", error="Diagnosis exceeded diagnosis_timeout_sec")
    except asyncio.CancelledError:
        state.update(status="cancelled", error="Diagnosis was interrupted")
        raise
    except Exception as exc:
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        state["finished_at"] = now()
        state = redact(state, secrets)
        write_json(output, state)
    return state
