"""Stage3: consolidate supplements, validate changes, then activate a patch set."""

import copy
import hashlib
import json
from pathlib import Path

from skillpatch.adapters.registry import get_adapter
from skillpatch.environment import redact, secret_values
from skillpatch.model_client import load_diagnosis_config
from skillpatch.patch_set_rendering import map_rendered_quote, render_patch_set
from skillpatch.patch_sidecars import SIDECAR_MARKDOWN, SIDECAR_MANIFEST
from skillpatch.patch_sets import PatchSetStore
from skillpatch.patch_targets import bind_target, locate_skill_root
from skillpatch.patches import tree_hashes
from skillpatch.stage3_sources import load_accepted_queue, read_receipt, save_receipt
from skillpatch.stage3_validation import collect_cases, validate_patch_sets
from skillpatch.stages.consolidation_model import propose_consolidation
from skillpatch.stages.patch_validation import default_backend
from skillpatch.storage import hash_manifest, now, sha256_file, write_json


def _references(entries):
    result, seen = [], set()
    for entry in entries:
        for reference in entry.get("supports", []):
            key = (reference["path"], reference["sha256"])
            if key not in seen:
                result.append(reference); seen.add(key)
    return result


def _versions(store):
    if not store.registry_dir.exists():
        return []
    return [store.get_version(p.name) for p in sorted((store.registry_dir / "versions").glob("v-*"))]


def _protected_receipt_paths(receipt, *, stage3_output=None):
    """Protect whole historical episodes as well as their recorded input trees."""
    diagnosis = Path(receipt["diagnosis_dir"])
    source_run = diagnosis.parent.parent
    paths = [Path(receipt["queue_path"]).parent, diagnosis]
    # The source run is also the CLI's parent for new stage3/<episode> outputs.
    # Protect execution evidence within it without reserving that new sibling.
    paths.extend(source_run / name for name in ("input", "backend", "jobs", "prompt"))
    paths.extend(Path(path) for path in receipt.get("archive_trees", {}))
    for case in receipt.get("cases", []):
        for key in ("baseline_dir", "candidate_dir"):
            archived_run = Path(case[key])
            # A reused source is both historical execution evidence and the
            # CLI's output parent. Permit only a new direct stage3/<episode>
            # child, while the evidence paths above stay protected. Registry
            # placement and every other location retain whole-run protection.
            if (stage3_output is not None and key == "baseline_dir"
                    and case.get("baseline_origin") == "source" and case.get("role") == "source"
                    and archived_run.resolve() == source_run.resolve()
                    and stage3_output.resolve().parent == source_run.resolve() / "stage3"):
                continue
            paths.append(archived_run)
    return paths


def _binding(accepted, original, skill_key, versions):
    source_root, prefix = locate_skill_root(accepted.source.prepared.skills_dir,
                                             accepted.patch.target.relative_path)
    full_target = accepted.source.prepared.skills_dir / accepted.patch.target.relative_path
    relative = full_target.relative_to(source_root).as_posix()
    digest = hash_manifest(tree_hashes(source_root))
    origin_digest = hash_manifest(tree_hashes(original))
    provenance = {"kind": "original", "source_unit_digest": digest, "source_prefix": prefix}
    if digest == origin_digest:
        target = bind_target(original, skill_key, relative, accepted.patch.target.scope,
                             accepted.patch.target.quote)
    else:
        versions = [v for v in versions if v["skill_digest"] == digest]
        if not versions:
            raise ValueError("Source Skill is not the registered original or a known patch-set version")
        selected = versions[0]
        mapped = map_rendered_quote(source_root, selected["rendering"], relative, accepted.patch.target.quote)
        if mapped["origin"] == "original":
            target = bind_target(original, skill_key, relative, accepted.patch.target.scope, mapped["original_quote"])
        else:
            archived_target = mapped["target"]
            if archived_target["scope"] != accepted.patch.target.scope:
                raise ValueError("Patch quotation and registered target scopes differ")
            target = bind_target(original, skill_key, archived_target["relative_path"],
                                 archived_target["scope"], archived_target["source_quote"])
            if target["target_id"] != archived_target["target_id"]:
                raise ValueError("Mapped supplement changed the registered target object")
        provenance.update(kind=mapped["origin"], rendered_version=selected["version_id"], mapping=mapped)
    return target, provenance


def _sidecar_bindings(entries, original):
    """Remove historical insertion details without changing object identity."""
    result = copy.deepcopy(entries)
    for entry in result:
        old = entry["target"]
        target = bind_target(original, old["skill_key"], old["relative_path"],
                             old["scope"], old["source_quote"])
        if target["target_id"] != old["target_id"]:
            raise ValueError("Sidecar conversion changed the registered target object")
        entry["target"] = target
    return result


def run_stage3(queue_path: Path, skill_key: str, registry: Path, config, output: Path,
               environment, *, original_skill: Path | None = None, prepare_only=False,
               client=None, adapter_factory=get_adapter, backend_factory=default_backend, progress=None):
    """One proposal at most; fresh validation is bounded separately from Stage2."""
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Stage3 output must be a new directory")
    # Reject output placement in incoming immutable artifacts before creating it.
    queue_path = queue_path.resolve(strict=True)
    if output.resolve().is_relative_to(queue_path.parent):
        raise ValueError("Stage3 output must be outside the historical Stage2 episode")
    store = PatchSetStore(registry, skill_key)
    if output.resolve().is_relative_to(store.root):
        raise ValueError("Stage3 output must be outside immutable registry storage")
    # Read-only validation first: even a failed command must not create files in
    # original Skills, historical inputs, diagnostics or execution evidence.
    accepted = load_accepted_queue(queue_path, adapter_factory=adapter_factory)
    source_unit, _ = locate_skill_root(accepted.source.prepared.skills_dir, accepted.patch.target.relative_path)
    initialized = store.registry_dir.exists()
    original = store.original_dir if initialized else (original_skill or source_unit).resolve(strict=True)
    current = store.current()
    receipts = [accepted.receipt, *[read_receipt(reference)
                for reference in _references(current["entries"] if current else [])]]
    protected = [original]
    if original_skill is not None:
        protected.append(original_skill.resolve(strict=True))
    output_protected = [*protected, *[path for receipt in receipts
                        for path in _protected_receipt_paths(receipt, stage3_output=output)]]
    registry_protected = [*protected, *[path for receipt in receipts
                          for path in _protected_receipt_paths(receipt)]]
    if (any(output.resolve().is_relative_to(p.resolve()) for p in output_protected)
            or any(Path(registry).resolve().is_relative_to(p.resolve()) for p in registry_protected)):
        raise ValueError("Stage3 output and registry must be outside original and historical artifacts")
    output.mkdir(parents=True, mode=0o700)
    emit = progress or (lambda _: None)
    secrets = secret_values(environment)
    state = {"schema_version": 1, "status": "running", "phase": "preflight", "started_at": now(),
             "skill_key": skill_key, "queue_path": str(queue_path), "output_dir": str(output),
             "original_skill_changed": False, "active_set_changed": False}
    def save():
        write_json(output / "result.json", redact(state, secrets))
    try:
        save()
        emit("Stage3: rechecking accepted patch and its actual validation evidence")
        archived_candidate, _ = locate_skill_root(accepted.candidate_pack, accepted.patch.target.relative_path)
        if output.resolve().is_relative_to(original) or Path(registry).resolve().is_relative_to(original):
            raise ValueError("Stage3 artifacts must be outside the original Skill")
        if original_skill is not None and tree_hashes(original_skill) != tree_hashes(original):
            raise ValueError("Requested original differs from registered original")
        if not initialized and (any((original / name).exists() or (original / name).is_symlink()
                                   for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST))
                or any("<!-- skillpatch:" in p.read_text(encoding="utf-8") for p in original.rglob("*.md"))):
            raise ValueError("Cannot register an already patched Skill as its original")
        original_hashes = tree_hashes(original)
        versions = _versions(store)
        parent = current["version_id"] if current else None
        current_mode = current["rendering"].get("presentation_mode", "inline_v1") if current else None
        selected_mode = "sidecar_v1"
        entries = copy.deepcopy(current["entries"] if current else [])
        for reference in _references(entries):
            read_receipt(reference)
        target, provenance = _binding(accepted, original, skill_key, versions)
        mapping = {"schema_version": 1, "diagnosis_dir": str(accepted.source.diagnosis_dir),
            "diagnosis_sha256": sha256_file(accepted.source.diagnosis_dir / "report.json"),
            "source_input_digest": accepted.source.context.input_digest,
            "deployed_target": accepted.patch.target, "original_target": target, "provenance": provenance}
        write_json(output / "target_binding.json", mapping)
        support = save_receipt(accepted, output / "receipts" / "incoming.json")
        incoming = {"entry_id": accepted.patch.patch_id, "target": target,
                    "when_to_use": accepted.patch.when_to_use, "how_to_interpret": accepted.patch.how_to_interpret,
                    "supports": [support]}
        write_json(output / "incoming.json", incoming)
        state.update(parent_version=parent, target_id=target["target_id"], base_digest=target["base_digest"],
                     coverage_notes=accepted.receipt["coverage_notes"], regression_coverage=accepted.receipt["regression_coverage"])
        references = _references(entries)
        cases = collect_cases(references, accepted, config, output, adapter_factory=adapter_factory)
        related = [e for e in entries if e["target"]["target_id"] == target["target_id"]]
        duplicate = next((e for e in related if all(e[k] == incoming[k] for k in ("when_to_use", "how_to_interpret"))), None)
        processed = any(read_receipt(r)["queue_sha256"] == accepted.queue_digest for r in references)
        # Source identity checks also prevent accidental migration between Skills.
        support_sources = []
        for reference in _references(related):
            receipt = read_receipt(reference)
            previous = load_accepted_queue(Path(receipt["queue_path"]), adapter_factory=adapter_factory)
            previous_target, _ = _binding(previous, original, skill_key, versions)
            if previous_target["target_id"] != target["target_id"]:
                raise ValueError("Related patch support belongs to another original object")
            support_sources.append(previous.source)
        proposal_config = load_diagnosis_config(config.consolidation_config)
        evaluation_config = load_diagnosis_config(config.evaluation_config)
        plan = {"source_patch_id": accepted.patch.patch_id, "parent_version": parent,
                "incoming_presentation_mode": accepted.presentation_mode,
                "current_presentation_mode": current_mode,
                "new_presentation_mode": "sidecar_v1",
                "target_id": target["target_id"], "same_target_entries": [e["entry_id"] for e in related],
                "max_proposal_sessions": int(bool(related) and duplicate is None and not processed),
                "max_validation_phases": 2, "max_validation_cases": config.max_validation_cases,
                "validation_cases": [{"id": c["id"], "role": c["role"], "benchmark": c["request"].benchmark,
                    "task": str(c["request"].task_dir)} for c in cases],
                "max_backend_runs": len(cases) * config.repetitions * 4,
                "max_evaluation_sessions": len(cases) * config.repetitions * 2,
                "repetitions": config.repetitions, "session_timeout_sec": config.model_session_timeout_sec,
                "proposal_tool_limit": proposal_config.max_tool_calls,
                "evaluation_tool_limit": evaluation_config.max_tool_calls,
                "config": config, "original_modified": False}
        write_json(output / "plan.json", plan)
        if prepare_only:
            state.update(status="prepared", decision="prepared", phase="preflight")
            return state
        if processed and not config.regression_configs:
            state.update(status="completed", phase="finished", decision="unchanged", reason="queue_already_integrated",
                         version_id=parent, presentation_mode=current_mode)
            return state
        if not initialized:
            store.initialize(original)
            original = store.original_dir
        evidence = {"mode": "stage2_inheritance", "receipt": support,
                    "target_binding_path": str(output / "target_binding.json"),
                    "target_binding_sha256": sha256_file(output / "target_binding.json"),
                    "coverage_notes": list(state["coverage_notes"]), "regression_coverage": state["regression_coverage"]}
        allowed = tuple(v["skill_digest"] for v in versions)
        if processed:
            # Rechecking coverage of an existing version retains its exact
            # presentation, including the historical inline protocol.
            selected_mode = current_mode
            selected_entries = entries
            selected_dir = output / "rendered" / "selected"
            selected_rendering = render_patch_set(original, selected_entries, selected_dir,
                                                   presentation_mode=selected_mode)
            if current is None or selected_rendering["digest"] != current["skill_digest"]:
                raise ValueError("Revalidation would change the already integrated presentation")
            evidence.update(mode="existing_set_revalidation", inherited_version=parent)
            state["action"] = "keep"
        elif duplicate is not None:
            # More evidence for the same text does not migrate an active
            # version to a different presentation without an experiment.
            selected_mode = current_mode
            duplicate["supports"].append(support)
            selected_entries = entries
            selected_dir = output / "rendered" / "selected"
            selected_rendering = render_patch_set(original, selected_entries, selected_dir,
                                                   presentation_mode=selected_mode)
            if current is None or selected_rendering["digest"] != current["skill_digest"]:
                raise ValueError("Duplicate evidence would change the actual presentation")
            evidence.update(mode="duplicate_evidence", inherited_version=parent,
                note="New receipt retains its original tested version; no new cross-task effect is claimed.")
            state["action"] = "deduplicate"
        else:
            if any(e["entry_id"] == incoming["entry_id"] for e in entries):
                raise ValueError("Patch ID reused with different content or target")
            entries = _sidecar_bindings(entries, original)
            related = [e for e in entries if e["target"]["target_id"] == target["target_id"]]
            unmerged = [*entries, incoming]
            unmerged_dir = output / "rendered" / "unmerged"
            unmerged_rendering = render_patch_set(original, unmerged, unmerged_dir)
            current_digest = current["skill_digest"] if current else target["base_digest"]
            # A matching incoming Stage2 deployment proves only its own checked
            # cases. Adding to an active set also requires preserving old source
            # behaviors, even when the new source ran with that exact active set.
            inherited = (accepted.presentation_mode == selected_mode and not entries
                         and provenance["source_unit_digest"] == current_digest
                         and hash_manifest(tree_hashes(archived_candidate)) == unmerged_rendering["digest"])
            if not inherited:
                state["phase"] = "combination_validation"
                save()
                baseline_skill = store.skill_dir(parent) if parent is not None else original
                comparison = validate_patch_sets(cases, baseline_skill, unmerged_dir, original, config,
                    output / "combination", environment, mode="combination", client=client,
                    adapter_factory=adapter_factory, backend_factory=backend_factory, progress=emit,
                    allowed_source_digests=allowed)
                state["combination_validation"] = comparison
                if comparison["decision"] != "accepted":
                    state.update(status="completed", decision=comparison["decision"], reason="new_combination_not_accepted")
                    return state
                evidence.update(mode="combination_validation", combination_validation=str(output / "combination/validation.json"),
                    combination_validation_sha256=sha256_file(output / "combination/validation.json"))
            selected_entries, selected_dir, selected_rendering = unmerged, unmerged_dir, unmerged_rendering
            state["action"] = "keep"
            if related:
                state["phase"] = "proposal"
                save()
                emit("Stage3: examining supplements for the same registered object")
                proposal = propose_consolidation(accepted.source, related, incoming, proposal_config,
                    output / "proposal", environment, sources=support_sources, original_skill_dir=original,
                    client=client, session_timeout_sec=config.model_session_timeout_sec)
                state["proposal"] = proposal
                if proposal["action"] == "defer":
                    state.update(status="completed", decision="pending", reason="consolidation_deferred")
                    return state
                if proposal["action"] == "merge":
                    joined = [*related, incoming]
                    binding = min(joined, key=lambda e: (e["target"]["start"],
                                                       e["target"]["source_quote"]))["target"]
                    body = {k: proposal[k] for k in ("when_to_use", "how_to_interpret")}
                    identity = json.dumps({"target": target["target_id"], "entries": [e["entry_id"] for e in joined], **body}, sort_keys=True)
                    merged = {"entry_id": "M" + hashlib.sha256(identity.encode()).hexdigest()[:32], "target": binding,
                              **body, "supports": _references(joined), "replaces": [e["entry_id"] for e in joined]}
                    selected_entries, inserted = [], False
                    for entry in unmerged:
                        if entry["target"]["target_id"] == target["target_id"]:
                            if not inserted:
                                selected_entries.append(merged); inserted = True
                        else:
                            selected_entries.append(entry)
                    selected_dir = output / "rendered" / "merged"
                    selected_rendering = render_patch_set(original, selected_entries, selected_dir)
                    state["phase"] = "preservation_validation"
                    save()
                    comparison = validate_patch_sets(cases, unmerged_dir, selected_dir, original, config,
                        output / "preservation", environment, client=client,
                        adapter_factory=adapter_factory, backend_factory=backend_factory, progress=emit,
                        allowed_source_digests=allowed)
                    state["preservation_validation"] = comparison
                    if comparison["decision"] != "accepted":
                        state.update(status="completed", decision=comparison["decision"], reason="merged_patch_set_not_accepted")
                        return state
                    evidence.update(mode="preservation_validation", preservation_validation=str(output / "preservation/validation.json"),
                        preservation_validation_sha256=sha256_file(output / "preservation/validation.json"))
                    state["action"] = "merge"
        if config.regression_configs and "preservation_validation" not in state:
            state["phase"] = "requested_coverage_validation"
            save()
            comparison = validate_patch_sets(cases, selected_dir, selected_dir, original, config,
                output / "coverage", environment, client=client, adapter_factory=adapter_factory,
                backend_factory=backend_factory, progress=emit, allowed_source_digests=allowed)
            state["preservation_validation"] = comparison
            if comparison["decision"] != "accepted":
                state.update(status="completed", decision=comparison["decision"], reason="requested_coverage_not_accepted")
                return state
            evidence.update(mode="requested_coverage_validation", preservation_validation=str(output / "coverage/validation.json"),
                preservation_validation_sha256=sha256_file(output / "coverage/validation.json"))
        write_json(output / "selected_entries.json", selected_entries)
        write_json(output / "rendering.json", selected_rendering)
        # Recheck all support records and original bytes immediately before commit.
        for reference in _references(selected_entries):
            read_receipt(reference)
        if tree_hashes(original) != original_hashes:
            raise ValueError("Original Skill changed during consolidation")
        for case in (state.get("combination_validation"), state.get("preservation_validation")):
            if case:
                evidence["regression_coverage"] = case["regression_coverage"]
        state["phase"] = "commit"
        save()
        evidence["presentation_mode"] = selected_mode
        manifest = store.commit(selected_entries, selected_dir, evidence, expected_parent=parent,
                                 presentation_mode=selected_mode)
        state.update(status="completed", phase="finished", decision="accepted", reason=evidence["mode"],
                     version_id=manifest["version_id"], skill_digest=manifest["skill_digest"],
                     presentation_mode=selected_mode,
                     active_set_changed=manifest["version_id"] != parent,
                     registry_dir=str(store.registry_dir), regression_coverage=evidence["regression_coverage"])
        return state
    except KeyboardInterrupt:
        state.update(status="cancelled", decision="pending", reason="interrupted")
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state.update(status="failed", decision="pending", reason="preflight_or_execution_error",
                     error=redact(f"{type(exc).__name__}: {exc}", secrets))
        return state
    finally:
        state["finished_at"] = now()
        save()
