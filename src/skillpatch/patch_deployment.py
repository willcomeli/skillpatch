"""Deploy one explicitly registered patch set through any benchmark adapter."""

from dataclasses import replace
import os
from pathlib import Path
import shutil

from skillpatch.models import RunRequest
from skillpatch.patch_sets import PatchSetStore
from skillpatch.patch_targets import locate_skill_root
from skillpatch.patches import tree_hashes
from skillpatch.storage import hash_manifest, jsonable, now, write_json


def _path(path):
    result = Path(os.path.abspath(Path(path).expanduser()))
    for item in (result, *result.parents):
        if item.is_symlink():
            raise ValueError("Patch deployment paths cannot contain symbolic links")
    return result


def _unit_candidates(pack, known_digests, skill_path):
    if skill_path is not None:
        unit, prefix = locate_skill_root(pack, skill_path)
        hashes = tree_hashes(unit)
        if hash_manifest(hashes) not in known_digests:
            raise ValueError("Selected Skill is not the registered original or a known patch-set version")
        return unit, prefix, hashes
    matches = []
    for declaration in sorted(pack.rglob("SKILL.md")):
        if not declaration.is_file():
            continue
        unit = declaration.parent
        hashes = tree_hashes(unit)
        if hash_manifest(hashes) in known_digests:
            relative = unit.relative_to(pack).as_posix()
            matches.append((unit, "" if relative == "." else relative, hashes))
    if len(matches) != 1:
        reason = "No Skill matches" if not matches else "Multiple Skills match"
        raise ValueError(reason + " the registered resource version; use an exact skill_path to disambiguate")
    return matches[0]


def prepare_patch_set_request(request: RunRequest, adapter, store: PatchSetStore, output: Path,
                              *, version_id=None, skill_path: str | None = None) -> tuple[RunRequest, dict]:
    """Snapshot a task and compose its Skill pack with a pinned stored version.

    ``None`` selects the current version once, before adapter preparation;
    ``'original'`` explicitly selects the registered original. A Skill path is
    relative to the benchmark's prepared Skill pack, e.g. ``xlsx/SKILL.md``.
    Selection still requires exact registered contents when a path is supplied.
    This function never runs a backend or changes the active patch-set pointer.
    """
    if request.skill_mode != "with-skill":
        raise ValueError("Patch-set deployment requires with-skill mode")
    destination = _path(output)
    task_source = _path(request.task_dir)
    external_skills = _path(request.skills_dir) if request.skills_dir is not None else None
    registry = _path(store.registry_dir)
    protected = [task_source, _path(store.root), *([external_skills] if external_skills is not None else [])]
    if destination.exists() or any(destination.is_relative_to(root) for root in protected):
        raise ValueError("Patch deployment requires a fresh directory outside task, Skill and registry sources")

    # Pin the immutable version before calling an adapter, which can take time.
    original = store.original_dir
    base_digest = store.base_digest
    if version_id == "original":
        selected = None
    elif version_id is None:
        selected = store.current()
    else:
        selected = store.get_version(version_id)
    pinned_id = selected["version_id"] if selected is not None else None
    presentation_mode = selected["rendering"].get("presentation_mode", "inline_v1") if selected else "original"
    selected_unit = store.skill_dir(pinned_id) if selected is not None else original
    selected_hashes = tree_hashes(selected_unit)
    if presentation_mode == "sidecar_v1" and any(selected_hashes.get(name) != digest
            for name, digest in tree_hashes(original).items()):
        raise ValueError("Sidecar deployment changed an original Skill resource")
    selected_digest = hash_manifest(selected_hashes)
    known_digests = {base_digest}
    for path in sorted((registry / "versions").glob("v-*")):
        known_digests.add(store.get_version(path.name)["skill_digest"])
    task_before = tree_hashes(task_source)
    external_before = tree_hashes(external_skills) if external_skills is not None else None

    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    snapshot_root = destination / "input"
    prepared = adapter.prepare(request, snapshot_root)
    task_snapshot = _path(prepared.task_dir)
    if not task_snapshot.is_relative_to(snapshot_root):
        raise ValueError("Benchmark adapter did not create an independent task snapshot")
    task_hashes = tree_hashes(task_snapshot)
    if task_hashes != prepared.file_hashes:
        raise ValueError("Prepared task snapshot does not match its adapter manifest")
    if prepared.skills_dir is None:
        raise ValueError("Benchmark task has no Skill pack to deploy into")
    pack = _path(prepared.skills_dir)
    if not pack.is_relative_to(snapshot_root):
        raise ValueError("Benchmark adapter returned a Skill pack outside its new snapshot")
    before = tree_hashes(pack)
    if hash_manifest(before) != prepared.skill_digest:
        raise ValueError("Prepared Skill pack does not match its adapter manifest")
    unit, prefix, unit_hashes = _unit_candidates(pack, known_digests, skill_path)

    composed = destination / "skills"
    if prefix:
        shutil.copytree(pack, composed, symlinks=True)
        if tree_hashes(composed) != before:
            raise ValueError("Skill pack changed while copying for deployment")
        target = composed / prefix
        shutil.rmtree(target)
        shutil.copytree(selected_unit, target, symlinks=True)
    else:
        shutil.copytree(selected_unit, composed, symlinks=True)

    after = tree_hashes(composed)
    protected_hashes = {name: digest for name, digest in before.items()
                        if prefix and not name.startswith(prefix + "/")}
    expected = {**protected_hashes,
                **{(prefix + "/" + name if prefix else name): digest
                   for name, digest in selected_hashes.items()}}
    if after != expected:
        raise ValueError("Deployment changed another Skill or did not copy the pinned version exactly")
    if (tree_hashes(pack) != before or tree_hashes(unit) != unit_hashes
            or tree_hashes(task_snapshot) != task_hashes
            or tree_hashes(task_source) != task_before
            or (external_skills is not None and tree_hashes(external_skills) != external_before)):
        raise ValueError("Task or Skill inputs changed while preparing patch deployment")
    # Recheck the pinned snapshot, never reselect the potentially changed pointer.
    if selected is not None:
        store.get_version(pinned_id)
    elif tree_hashes(store.original_dir) != selected_hashes:
        raise ValueError("Registered original changed during deployment")
    if tree_hashes(selected_unit) != selected_hashes:
        raise ValueError("Pinned patch-set snapshot changed during deployment")

    deployed_request = replace(request, task_dir=task_snapshot, skills_dir=composed,
                               skill_condition=f"patch-set:{store.skill_key}@{pinned_id or 'original'}")
    manifest = jsonable({"schema_version": 1, "created_at": now(), "skill_key": store.skill_key,
        "presentation_mode": presentation_mode,
        "registry_dir": registry, "version_id": pinned_id, "base_digest": base_digest,
        "selection": "current" if version_id is None else "explicit",
        "skill_path": skill_path, "skill_prefix": prefix,
        "source_unit_digest": hash_manifest(unit_hashes), "source_unit_hashes": unit_hashes,
        "selected_unit_digest": selected_digest, "selected_unit_hashes": selected_hashes,
        "source_pack_digest": hash_manifest(before), "deployed_pack_digest": hash_manifest(after),
        "deployed_pack_hashes": after, "deployed_skills_dir": composed,
        "task_snapshot": task_snapshot, "task_digest": prepared.task_digest,
        "task_file_digest": hash_manifest(task_hashes),
        "prepared_manifest_path": prepared.manifest_path,
        "original_request": request, "deployed_request": deployed_request,
        "proof": {"selection_by_registered_content": True, "other_skill_hashes": protected_hashes,
                  "original_task_unchanged": True, "prepared_task_unchanged": True,
                  "registry_activation_performed": False, "version_pinned_before_preparation": True}})
    write_json(destination / "patch_set.json", manifest)
    return deployed_request, manifest
