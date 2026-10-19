"""Immutable Skill snapshots and append-only patch-set versions.

This store establishes byte/provenance integrity, not behavioral acceptance.
The Stage3 coordinator must verify experiment receipts before calling commit.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from skillpatch.patches import tree_hashes
from skillpatch.patch_sidecars import SIDECAR_MARKDOWN, SIDECAR_MANIFEST
from skillpatch.storage import hash_manifest, now, sha256_file


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _absolute(path):
    return Path(os.path.abspath(path))


def _no_links(path):
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Patch-set paths cannot contain symbolic links")


def _read(path):
    _no_links(path)
    if not path.is_file():
        raise ValueError(f"Patch-set record is missing: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid patch-set JSON record") from exc
    if not isinstance(value, dict):
        raise ValueError("Patch-set records must be JSON objects")
    return value


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path, value):
    _no_links(path)
    with path.open("xb") as stream:
        stream.write(_encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _state(path):
    _no_links(path)
    hashes = tree_hashes(path)
    directories = sorted(item.relative_to(path).as_posix() for item in path.rglob("*")
                         if stat.S_ISDIR(item.lstat().st_mode))
    return {"hashes": hashes, "directories": directories}


def _durable_tree(path):
    for item in path.rglob("*"):
        if item.is_file():
            with item.open("rb") as stream:
                os.fsync(stream.fileno())
    for item in sorted((p for p in path.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        _fsync_dir(item)
    _fsync_dir(path)


class PatchSetStore:
    """One explicitly identified original Skill and its versioned supplements.

    ``root`` may contain many Skills. A hashed identity prevents a Skill name
    from becoming a filesystem path. Initialization copies a *single* Skill
    directory with SKILL.md at its root. Locks cover short filesystem changes;
    no model calls or benchmark execution belong inside this class.
    """

    def __init__(self, root: Path, skill_key: str):
        if not isinstance(skill_key, str) or not skill_key.strip() or "\x00" in skill_key:
            raise ValueError("An explicit nonempty Skill identity is required")
        self.root = _absolute(root)
        self.skill_key = skill_key
        self.registry_dir = self.root / hashlib.sha256(skill_key.encode()).hexdigest()

    @contextmanager
    def _lock(self):
        _no_links(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / (self.registry_dir.name + ".lock")
        _no_links(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Invalid patch-set lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("Patch-set store is busy; retry after the active commit") from exc
            yield
        finally:
            os.close(fd)

    def initialize(self, original_dir: Path) -> dict:
        source = _absolute(original_dir)
        if self.root.is_relative_to(source):
            raise ValueError("Patch-set storage must be outside the original Skill")
        before = _state(source)
        if "SKILL.md" not in before["hashes"]:
            raise ValueError("Register one Skill directory with SKILL.md at its root")
        if any(name in before["hashes"] or (source / name).exists()
               for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)):
            raise ValueError("Cannot register a Skill containing patch sidecars as its original")
        with self._lock():
            if self.registry_dir.exists() or self.registry_dir.is_symlink():
                current = self._base()
                if before != {"hashes": current["hashes"], "directories": current["directories"]}:
                    raise ValueError("Skill identity already has a different immutable original")
                return current
            staging = Path(tempfile.mkdtemp(prefix=".initializing-", dir=self.root))
            try:
                shutil.copytree(source, staging / "original", symlinks=True)
                if _state(source) != before or _state(staging / "original") != before:
                    raise ValueError("Original Skill changed while copying")
                base = {"schema_version": 1, "skill_key": self.skill_key,
                        "base_digest": hash_manifest(before["hashes"]), **before,
                        "source_path": str(source), "created_at": now()}
                base["manifest_digest"] = "sha256:" + _digest(base)
                _write_new(staging / "base.json", base)
                (staging / "versions").mkdir()
                (staging / "checkpoints").mkdir()
                _durable_tree(staging)
                os.rename(staging, self.registry_dir)
                _fsync_dir(self.root)
                return base
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def _base(self):
        _no_links(self.registry_dir)
        base = _read(self.registry_dir / "base.json")
        payload = {key: value for key, value in base.items() if key != "manifest_digest"}
        if (base.get("schema_version") != 1 or base.get("skill_key") != self.skill_key
                or base.get("manifest_digest") != "sha256:" + _digest(payload)):
            raise ValueError("Original Skill manifest identity or checksum mismatch")
        state = _state(self.registry_dir / "original")
        if (state != {"hashes": base.get("hashes"), "directories": base.get("directories")}
                or hash_manifest(state["hashes"]) != base.get("base_digest")):
            raise ValueError("Immutable original Skill has changed")
        return base

    @property
    def original_dir(self) -> Path:
        self._base()
        return self.registry_dir / "original"

    @property
    def base_digest(self) -> str:
        return self._base()["base_digest"]

    def _version_path(self, version_id):
        if not isinstance(version_id, str) or not re.fullmatch(r"v-[0-9a-f]{64}", version_id):
            raise ValueError("Invalid patch-set version ID")
        result = self.registry_dir / "versions" / version_id
        _no_links(result)
        return result

    def _entries(self, entries, base):
        if not isinstance(entries, list):
            raise ValueError("Patch-set entries must be a list")
        result = json.loads(_encoded(entries))
        seen = set()
        for entry in result:
            if not isinstance(entry, dict):
                raise ValueError("Patch entries must be objects")
            key = entry.get("entry_id")
            target = entry.get("target")
            if not isinstance(key, str) or not key.strip() or key in seen:
                raise ValueError("Patch entry IDs must be nonempty and unique")
            seen.add(key)
            if (not isinstance(target, dict) or not target.get("target_id")
                    or target.get("skill_key") != self.skill_key
                    or target.get("base_digest") != base["base_digest"]):
                raise ValueError("Patch target does not belong to this original Skill")
            for field in ("when_to_use", "how_to_interpret"):
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    raise ValueError("Patch text requires both supplement fields")
            if not isinstance(entry.get("supports"), list) or not entry["supports"]:
                raise ValueError("Patch entries must retain supporting receipt references")
        return result

    def _validate_rendering(self, entries, skill_dir, base, *, presentation_mode):
        from skillpatch.patch_set_rendering import render_patch_set

        actual = _state(skill_dir)
        with tempfile.TemporaryDirectory(prefix="skillpatch-verify-") as temporary:
            expected_dir = Path(temporary) / "skill"
            rendering = render_patch_set(self.registry_dir / "original", entries, expected_dir,
                                          presentation_mode=presentation_mode)
            if actual != _state(expected_dir):
                raise ValueError("Rendered Skill differs from the original plus declared supplements")
        if _state(self.registry_dir / "original") != {"hashes": base["hashes"], "directories": base["directories"]}:
            raise ValueError("Immutable original changed during rendering verification")
        return actual, rendering

    def get_version(self, version_id: str) -> dict:
        base = self._base()
        directory = self._version_path(version_id)
        manifest = _read(directory / "manifest.json")
        payload = {key: value for key, value in manifest.items() if key != "version_id"}
        if (manifest.get("version_id") != version_id or "v-" + _digest(payload) != version_id
                or manifest.get("skill_key") != self.skill_key
                or manifest.get("base_digest") != base["base_digest"]):
            raise ValueError("Patch-set version identity or checksum mismatch")
        entries = self._entries(manifest.get("entries"), base)
        presentation_mode = manifest.get("rendering", {}).get("presentation_mode", "inline_v1")
        state, _ = self._validate_rendering(entries, directory / "skill", base,
                                            presentation_mode=presentation_mode)
        if (state != {"hashes": manifest.get("hashes"), "directories": manifest.get("directories")}
                or hash_manifest(state["hashes"]) != manifest.get("skill_digest")):
            raise ValueError("Immutable patch-set version has changed")
        return manifest

    def _pointer(self):
        path = self.registry_dir / "current.json"
        if not path.exists() and not path.is_symlink():
            return None
        pointer = _read(path)
        checkpoint = self._checkpoint(pointer.get("checkpoint_id"))
        if (checkpoint["version_id"] != pointer.get("version_id")
                or checkpoint["manifest_sha256"] != pointer.get("manifest_sha256")):
            raise ValueError("Active pointer does not match its durable checkpoint")
        return pointer

    def _checkpoint(self, checkpoint_id):
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"c-[0-9a-f]{64}", checkpoint_id):
            raise ValueError("Invalid patch-set checkpoint ID")
        value = _read(self.registry_dir / "checkpoints" / (checkpoint_id + ".json"))
        payload = {key: item for key, item in value.items() if key != "checkpoint_id"}
        if value.get("checkpoint_id") != checkpoint_id or "c-" + _digest(payload) != checkpoint_id:
            raise ValueError("Patch-set checkpoint checksum mismatch")
        return value

    def current(self) -> dict | None:
        if not self.registry_dir.exists() and not self.registry_dir.is_symlink():
            return None
        self._base()
        pointer = self._pointer()
        if pointer is None:
            return None
        manifest = self.get_version(pointer["version_id"])
        if sha256_file(self._version_path(manifest["version_id"]) / "manifest.json") != pointer["manifest_sha256"]:
            raise ValueError("Current version manifest checksum mismatch")
        return manifest

    def version_dir(self, version_id: str) -> Path:
        self.get_version(version_id)
        return self._version_path(version_id)

    def skill_dir(self, version_id: str | None = None) -> Path:
        if version_id is None:
            manifest = self.current()
            if manifest is None:
                return self.original_dir
            version_id = manifest["version_id"]
        return self.version_dir(version_id) / "skill"

    def _activate(self, manifest, previous, operation):
        old_pointer = self._pointer()
        checkpoint = {"schema_version": 1, "operation": operation,
                      "skill_key": self.skill_key, "version_id": manifest["version_id"],
                      "previous_version": previous,
                      "previous_checkpoint": old_pointer["checkpoint_id"] if old_pointer else None,
                      "manifest_sha256": sha256_file(self._version_path(manifest["version_id"]) / "manifest.json"),
                      "created_at": now()}
        checkpoint["checkpoint_id"] = "c-" + _digest(checkpoint)
        path = self.registry_dir / "checkpoints" / (checkpoint["checkpoint_id"] + ".json")
        _write_new(path, checkpoint)
        _fsync_dir(path.parent)
        pointer = {key: checkpoint[key] for key in ("checkpoint_id", "version_id", "manifest_sha256")}
        fd, temporary = tempfile.mkstemp(prefix=".current-", dir=self.registry_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_encoded(pointer) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.registry_dir / "current.json")
            _fsync_dir(self.registry_dir)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def commit(self, entries: list[dict], rendered_dir: Path, evidence: dict,
               expected_parent: str | None, *, presentation_mode="sidecar_v1") -> dict:
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("Commit requires explicit validation or inheritance evidence")
        evidence = json.loads(_encoded(evidence))
        with self._lock():
            base = self._base()
            current = self.current()
            parent = current["version_id"] if current else None
            if expected_parent != parent:
                raise ValueError("Patch-set parent changed; validate against the current set before committing")
            entries = self._entries(entries, base)
            source = _absolute(rendered_dir)
            state, rendering = self._validate_rendering(entries, source, base,
                                                        presentation_mode=presentation_mode)
            if (current is not None and current["entries"] == entries
                    and current["evidence"] == evidence
                    and current["skill_digest"] == hash_manifest(state["hashes"])):
                return current
            manifest = {"schema_version": 1, "parent_version": parent,
                        "skill_key": self.skill_key, "base_digest": base["base_digest"],
                        "entries": entries, "skill_digest": hash_manifest(state["hashes"]),
                        **state, "evidence": evidence, "rendering": rendering, "created_at": now()}
            manifest["version_id"] = "v-" + _digest(manifest)
            versions = self.registry_dir / "versions"
            _no_links(versions)
            staging = Path(tempfile.mkdtemp(prefix=".committing-", dir=versions))
            try:
                shutil.copytree(source, staging / "skill", symlinks=True)
                if _state(source) != state or _state(staging / "skill") != state:
                    raise ValueError("Rendered Skill changed while copying")
                _write_new(staging / "manifest.json", manifest)
                _durable_tree(staging)
                final = self._version_path(manifest["version_id"])
                if final.exists():
                    if self.get_version(manifest["version_id"]) != manifest:
                        raise ValueError("An immutable version already exists with different content")
                else:
                    os.rename(staging, final)
                    _fsync_dir(versions)
                self._activate(manifest, parent, "commit")
                return manifest
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def rollback(self, version_id: str, expected_parent: str | None) -> dict:
        with self._lock():
            current = self.current()
            parent = current["version_id"] if current else None
            if expected_parent != parent:
                raise ValueError("Patch-set parent changed before rollback")
            selected = self.get_version(version_id)
            if parent != version_id:
                self._activate(selected, parent, "rollback")
            return selected

    def history(self) -> list[dict]:
        """Return verified activations, newest first; abandoned checkpoints are omitted."""
        if self.current() is None:
            return []
        checkpoint_id = self._pointer()["checkpoint_id"]
        result, seen = [], set()
        while checkpoint_id is not None:
            if checkpoint_id in seen:
                raise ValueError("Patch-set checkpoint history contains a cycle")
            seen.add(checkpoint_id)
            item = self._checkpoint(checkpoint_id)
            manifest = self.get_version(item["version_id"])
            if sha256_file(self._version_path(manifest["version_id"]) / "manifest.json") != item["manifest_sha256"]:
                raise ValueError("Checkpoint references a modified manifest")
            result.append(item)
            checkpoint_id = item["previous_checkpoint"]
        return result

    def export(self, output: Path, version_id: str | None = None) -> dict:
        """Copy the selected presentation to a fresh directory, with a receipt returned."""
        selected = self.current() if version_id is None else self.get_version(version_id)
        source = (self._version_path(selected["version_id"]) / "skill"
                  if selected is not None else self.original_dir)
        destination = _absolute(output)
        _no_links(destination)
        if (destination.exists() or destination.is_relative_to(self.registry_dir)
                or self.registry_dir.is_relative_to(destination)):
            raise ValueError("Export requires a fresh directory outside the store")
        before = _state(source)
        destination.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            if _state(source) != before or _state(destination) != before:
                raise ValueError("Selected Skill changed while exporting")
        except Exception:
            if destination.exists() and not destination.is_symlink():
                shutil.rmtree(destination)
            raise
        return {"skill_key": self.skill_key, "base_digest": self.base_digest,
                "version_id": selected["version_id"] if selected else None,
                "presentation_mode": selected["rendering"].get("presentation_mode", "inline_v1") if selected else "original",
                "skill_digest": hash_manifest(before["hashes"]), "output": str(destination)}
