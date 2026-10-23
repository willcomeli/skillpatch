def _protected_inputs_equal(baseline, candidate, patch, *, presentation_mode="sidecar_v1"):
    a, b = baseline.prepared, candidate.prepared
    if a is None or b is None or a.skills_dir is None or b.skills_dir is None:
        return False
    if presentation_mode == "sidecar_v1":
        from excutor.excutor_runners.patch_sidecars import load_sidecars, SIDECAR_MARKDOWN, SIDECAR_MANIFEST
        from .targets import locate_skill_root
        # Every original file, including the target and its YAML description,
        # stays identical. Existing supplements are preserved in order, with
        # exactly this candidate appended to its own Skill's companion pair.
        unit_a, _ = locate_skill_root(a.skills_dir, patch.target.relative_path)
        unit_b, _ = locate_skill_root(b.skills_dir, patch.target.relative_path)
        before = load_sidecars(unit_a)
        after = load_sidecars(unit_b)
        expected = {"entry_id": patch.patch_id,
            "target": {"relative_path": (b.skills_dir / patch.target.relative_path).relative_to(unit_b).as_posix(),
                       "scope": patch.target.scope, "quote": patch.target.quote.replace("\r\n", "\n"),
                       "version": patch.target.version},
            "when_to_use": patch.when_to_use, "how_to_interpret": patch.how_to_interpret}
        if after is None or after["entries"] != ([*before["entries"]] if before else []) + [expected]:
            return False
        allowed_a = {(unit_a / name).relative_to(a.task_dir).as_posix()
                     for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)}
        allowed_b = {(unit_b / name).relative_to(b.task_dir).as_posix()
                     for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST)}
        return ({k: v for k, v in a.file_hashes.items() if k not in allowed_a}
                == {k: v for k, v in b.file_hashes.items() if k not in allowed_b})
    raise ValueError("Only sidecar presentation is supported")
