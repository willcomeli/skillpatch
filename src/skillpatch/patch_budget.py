"""Persist generation attempts per source conflict, including across episodes."""

import hashlib
import os
from pathlib import Path

from skillpatch.patch_sources import read_object
from skillpatch.storage import now, write_json


class PatchBudget:
    def __init__(self, source, limit: int):
        key = hashlib.sha256((source.context.run_id + "\n" + source.conflict.conflict_id).encode()).hexdigest()
        root = source.run_dir / "stage2" / "budgets"
        self.path = root / (key + ".json")
        self.lock = root / (key + ".lock")
        self.limit = limit
        self.entries = []
        self.held = False

    def acquire(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise ValueError("Source conflict budget is locked; concurrent attempts or an interrupted owner require review") from None
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()))
        self.held = True
        if self.path.exists():
            record = read_object(self.path)
            self.entries = record["entries"]
            if not isinstance(self.entries, list) or any(not isinstance(x, dict) for x in self.entries):
                raise ValueError("Source conflict budget record is invalid")
        return self

    def reserve(self, output: Path) -> int:
        if not self.held or len(self.entries) >= self.limit:
            raise ValueError("Source conflict exhausted its initial candidate plus allowed revisions")
        revision = len(self.entries)
        self.entries.append({"revision": revision, "episode_dir": str(output), "reserved_at": now()})
        write_json(self.path, {"schema_version": 1, "entries": self.entries})
        return revision

    def release(self):
        if self.held:
            self.lock.unlink()
            self.held = False
