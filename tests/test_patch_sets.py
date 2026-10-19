"""Filesystem-integrity and version-transition tests; no network or containers."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skillpatch.patch_set_rendering import render_patch_set
from skillpatch.patch_sets import PatchSetStore
from skillpatch.patch_sets import _digest, _state
from skillpatch.patch_targets import bind_target
from skillpatch.patches import tree_hashes
from skillpatch.storage import hash_manifest, write_json


SKILL = ("---\nname: table\ndescription: Table editing\n---\n# Workflow\n"
         "1. Save the workbook.\n"
         "2. Recalculate formulas.\n   Inspect the recalculation result.\n")


class PatchSetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / "input-skill"
        (self.original / "scripts").mkdir(parents=True)
        (self.original / "empty").mkdir()
        (self.original / "SKILL.md").write_text(SKILL)
        (self.original / "scripts" / "recalc.py").write_bytes(b"print('original')\n")
        self.store = PatchSetStore(self.root / "registry", "reference/xlsx")
        self.base = self.store.initialize(self.original)
        self.target = bind_target(self.original, "reference/xlsx", "SKILL.md", "substep",
                                  "Recalculate formulas.")
        self.entry = {"entry_id": "P-first", "target": self.target,
                      "when_to_use": "After editing formula inputs.",
                      "how_to_interpret": "Preserve expressions and refresh calculated values.",
                      "supports": [{"receipt": "accepted-001", "sha256": "fixture"}]}
        self.rendered = self.root / "rendered"
        render_patch_set(self.original, [self.entry], self.rendered)
        self.evidence = {"decision": "accepted", "receipt": "accepted-001"}

    def commit(self, *, parent=None, entries=None, evidence=None):
        return self.store.commit([self.entry] if entries is None else entries, self.rendered,
                                 self.evidence if evidence is None else evidence,
                                 expected_parent=parent)

    def test_initialization_copies_single_skill_and_is_idempotent(self):
        self.assertIsNone(self.store.current())
        self.assertEqual(self.store.initialize(self.original), self.base)
        self.assertEqual(tree_hashes(self.original), tree_hashes(self.store.original_dir))
        self.assertTrue((self.store.original_dir / "empty").is_dir())
        (self.original / "scripts/recalc.py").write_text("external source changed")
        self.assertEqual((self.store.original_dir / "scripts/recalc.py").read_bytes(),
                         b"print('original')\n")
        with self.assertRaisesRegex(ValueError, "different immutable original"):
            self.store.initialize(self.original)

    def test_uninitialized_current_and_identity_path_safety(self):
        other = PatchSetStore(self.root / "registry", "../../other/xlsx")
        self.assertIsNone(other.current())
        self.assertEqual(other.registry_dir.parent, self.root / "registry")
        other.initialize(self.original)
        self.assertNotEqual(other.registry_dir, self.store.registry_dir)

    def test_sidecar_deployment_cannot_be_registered_as_an_original(self):
        with self.assertRaisesRegex(ValueError, "patch sidecars as its original"):
            PatchSetStore(self.root / "another-registry", "other/table").initialize(self.rendered)

    def test_commit_preserves_original_and_round_trips(self):
        original = tree_hashes(self.store.original_dir)
        version = self.commit()
        self.assertIsNone(version["parent_version"])
        self.assertEqual(self.store.current(), version)
        self.assertEqual(self.store.get_version(version["version_id"]), version)
        self.assertEqual(tree_hashes(self.store.original_dir), original)
        self.assertEqual(tree_hashes(self.store.skill_dir()), tree_hashes(self.rendered))
        self.assertEqual(self.store.history()[0]["operation"], "commit")
        self.assertEqual(version["rendering"]["presentation_mode"], "sidecar_v1")
        for name, digest in original.items():
            self.assertEqual(tree_hashes(self.store.skill_dir())[name], digest)

    def test_historical_inline_version_without_mode_is_read_as_inline(self):
        entry = copy.deepcopy(self.entry)
        entry["target"] = bind_target(self.original, "reference/xlsx", "SKILL.md", "substep",
                                      "Recalculate formulas.", presentation_mode="inline_v1")
        rendered = self.root / "legacy-inline"
        rendering = render_patch_set(self.original, [entry], rendered, presentation_mode="inline_v1")
        rendering.pop("presentation_mode", None)
        state = _state(rendered)
        manifest = {"schema_version": 1, "parent_version": None, "skill_key": self.store.skill_key,
                    "base_digest": self.base["base_digest"], "entries": [entry],
                    "skill_digest": hash_manifest(state["hashes"]), **state,
                    "evidence": self.evidence, "rendering": rendering, "created_at": "historical-fixture"}
        manifest["version_id"] = "v-" + _digest(manifest)
        directory = self.store.registry_dir / "versions" / manifest["version_id"]
        import shutil
        shutil.copytree(rendered, directory / "skill")
        write_json(directory / "manifest.json", manifest)

        self.assertEqual(self.store.get_version(manifest["version_id"]), manifest)
        self.assertNotEqual((directory / "skill/SKILL.md").read_bytes(),
                            (self.original / "SKILL.md").read_bytes())
        with self.assertRaisesRegex(ValueError, "binding|original plus declared supplements"):
            self.store.commit([entry], rendered, self.evidence, expected_parent=None)

    def test_exact_replay_idempotent_and_metadata_revision_append_only(self):
        first = self.commit()
        before = (self.store.version_dir(first["version_id"]) / "manifest.json").read_bytes()
        self.assertEqual(self.commit(parent=first["version_id"]), first)
        self.assertEqual(len(self.store.history()), 1)
        entries = copy.deepcopy(first["entries"])
        entries[0]["supports"].append({"receipt": "accepted-002"})
        second = self.commit(parent=first["version_id"], entries=entries,
                             evidence={"decision": "inherited", "receipt": "accepted-002"})
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(second["parent_version"], first["version_id"])
        self.assertEqual(first["skill_digest"], second["skill_digest"])
        self.assertEqual((self.store.version_dir(first["version_id"]) / "manifest.json").read_bytes(), before)

    def test_stale_commit_and_rollback_do_not_switch_current(self):
        first = self.commit()
        with self.assertRaisesRegex(ValueError, "parent changed"):
            self.commit()
        with self.assertRaisesRegex(ValueError, "parent changed"):
            self.store.rollback(first["version_id"], expected_parent=None)
        self.assertEqual(self.store.current(), first)

    def test_rollback_records_activation_without_modifying_versions(self):
        first = self.commit()
        second = self.commit(parent=first["version_id"], evidence={"receipt": "second"})
        selected = self.store.rollback(first["version_id"], expected_parent=second["version_id"])
        self.assertEqual(selected, first)
        self.assertEqual(self.store.current(), first)
        self.assertEqual(self.store.get_version(second["version_id"]), second)
        history = self.store.history()
        self.assertEqual([item["operation"] for item in history], ["rollback", "commit", "commit"])
        self.assertEqual(history[0]["previous_version"], second["version_id"])

    def test_protected_resource_and_original_text_changes_are_rejected(self):
        (self.rendered / "scripts/recalc.py").write_text("modified code")
        with self.assertRaisesRegex(ValueError, "original plus declared supplements"):
            self.commit()
        (self.rendered / "scripts/recalc.py").write_bytes(b"print('original')\n")
        document = self.rendered / "SKILL.md"
        document.write_text(document.read_text().replace("Save the workbook.", "Overwrite its formulas."))
        with self.assertRaisesRegex(ValueError, "original plus declared supplements"):
            self.commit()
        self.assertIsNone(self.store.current())

    def test_original_and_presentation_tampering_detected_on_read(self):
        first = self.commit()
        frozen = self.store.original_dir / "SKILL.md"
        frozen.write_text(SKILL + "tampering")
        with self.assertRaisesRegex(ValueError, "Immutable original"):
            self.store.current()
        frozen.write_text(SKILL)
        skill = self.store.skill_dir(first["version_id"]) / "SKILL.md"
        skill.write_text(skill.read_text() + "tampering")
        with self.assertRaises(ValueError):
            self.store.get_version(first["version_id"])

    def test_manifest_and_checkpoint_tampering_detected(self):
        first = self.commit()
        manifest = self.store.version_dir(first["version_id"]) / "manifest.json"
        data = json.loads(manifest.read_text())
        original_bytes = manifest.read_bytes()
        data["evidence"]["decision"] = "forged"
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.store.current()
        manifest.write_bytes(original_bytes)
        checkpoint = self.store.registry_dir / "checkpoints" / (self.store.history()[0]["checkpoint_id"] + ".json")
        data = json.loads(checkpoint.read_text())
        data["operation"] = "forged"
        checkpoint.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "checkpoint checksum"):
            self.store.current()

    def test_wrong_skill_binding_duplicate_ids_and_missing_support_rejected(self):
        wrong = copy.deepcopy(self.entry)
        wrong["target"]["skill_key"] = "another/xlsx"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.commit(entries=[wrong])
        with self.assertRaisesRegex(ValueError, "unique"):
            self.commit(entries=[self.entry, self.entry])
        wrong = copy.deepcopy(self.entry)
        wrong["supports"] = []
        with self.assertRaisesRegex(ValueError, "supporting receipt"):
            self.commit(entries=[wrong])

    def test_links_and_missing_skill_declaration_rejected(self):
        (self.original / "linked.py").symlink_to(self.original / "scripts/recalc.py")
        with self.assertRaises(ValueError):
            PatchSetStore(self.root / "other", "test").initialize(self.original)
        (self.original / "linked.py").unlink()
        (self.original / "SKILL.md").unlink()
        with self.assertRaisesRegex(ValueError, "SKILL.md"):
            PatchSetStore(self.root / "other", "test").initialize(self.original)
        for bad_id in ("../original", "v-123", "", "/tmp"):
            with self.assertRaises(ValueError):
                self.store.get_version(bad_id)

    def test_commit_lock_prevents_parallel_writer(self):
        other = PatchSetStore(self.root / "registry", "reference/xlsx")
        with self.store._lock():
            with self.assertRaisesRegex(ValueError, "busy"):
                other.commit([self.entry], self.rendered, self.evidence, expected_parent=None)
        self.assertIsNone(self.store.current())

    def test_internal_checkpoint_symlink_cannot_redirect_writes(self):
        outside = self.root / "outside-checkpoints"
        outside.mkdir()
        checkpoint_dir = self.store.registry_dir / "checkpoints"
        checkpoint_dir.rmdir()
        checkpoint_dir.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.commit()
        self.assertEqual(list(outside.iterdir()), [])

    def test_pointer_failure_keeps_previous_version_and_history(self):
        first = self.commit()
        import skillpatch.patch_sets as module
        real_replace = module.os.replace

        def fail_pointer(source, destination):
            if Path(destination).name == "current.json":
                raise OSError("simulated interrupted activation")
            return real_replace(source, destination)

        with patch.object(module.os, "replace", side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, "interrupted activation"):
                self.commit(parent=first["version_id"], evidence={"receipt": "second"})
        self.assertEqual(self.store.current(), first)
        self.assertEqual(len(self.store.history()), 1)

    def test_export_is_exact_and_never_overwrites(self):
        first = self.commit()
        destination = self.root / "export"
        receipt = self.store.export(destination)
        self.assertEqual(receipt["version_id"], first["version_id"])
        self.assertEqual(tree_hashes(destination), tree_hashes(self.rendered))
        with self.assertRaises(ValueError):
            self.store.export(destination)
        with self.assertRaises(ValueError):
            self.store.export(self.store.registry_dir / "export")


if __name__ == "__main__":
    unittest.main()
