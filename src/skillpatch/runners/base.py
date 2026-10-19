from pathlib import Path
from typing import Mapping, Protocol

from skillpatch.models import BackendRun, PreparedTask, RunRequest


class RunBackend(Protocol):
    # An opted-in backend must await a VerifiedCallback before finally cleanup.
    # Its SDK stays behind VerifiedSession.inspector. Subprocess backends build
    # the callback in their worker through the same application composition.
    supports_live_diagnosis: bool

    def run(self, request: RunRequest, task: PreparedTask, run_dir: Path,
            environment: Mapping[str, str]) -> BackendRun: ...
