"""Stored supplements deploy through a foreign adapter without running an agent."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from skillpatch.models import RunRequest
from skillpatch.patch_deployment import prepare_patch_set_request
from skillpatch.patch_set_rendering import render_patch_set
from skillpatch.patch_sets import PatchSetStore
from skillpatch.patch_targets import bind_target
from skillpatch.patches import tree_hashes
import test_stage2 as fixtures


SKILL = ("---\nname: table\ndescription: Table editing\n---\n# Workflow\n"
         "1. Save the workbook.\n2. Recalculate formulas.\n")


class PatchDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.task = self.root / "task"
        self.skill = self.task / "skills/table"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text(SKILL)
        (self.skill / "helper.py").write_text("original helper\n")
        (self.task / "instructions.txt").write_text("Edit the table carefully.\n")
        (self.task / "input.csv").write_text("item,value\na,1\n")
        (self.task / "skills/other").mkdir()
        (self.task / "skills/other/SKILL.md").write_text("# Different Skill\nKeep this unchanged.\n")
        (self.task / "skills/other/script.py").write_text("other script\n")
        self.request = RunRequest("tables", self.task, "fixture-agent", "fixture-model", (),
                                  backend="table-engine")
        self.adapter = fixtures.TableAdapter()
        self.store = PatchSetStore(self.root / "registry", "reference/table")
        self.store.initialize(self.skill)
        self.target = bind_target(self.skill, "reference/table", "SKILL.md", "substep", "Recalculate formulas.")
        self.entry = {"entry_id": "P-first", "target": self.target,
                      "when_to_use": "When editing formula inputs.",
                      "how_to_interpret": "Refresh cached values while preserving formula expressions.",
                      "supports": [{"path": "fixture-receipt", "sha256": "fixture"}]}
        self.rendered = self.root / "rendered"
        render_patch_set(self.skill, [self.entry], self.rendered)
        self.first = self.store.commit([self.entry], self.rendered, {"receipt": "fixture"}, expected_parent=None)

    def deploy(self, *, request=None, adapter=None, output=None, **kwargs):
        return prepare_patch_set_request(request or self.request, adapter or self.adapter, self.store,
            output or self.root / "deployment", **kwargs)

    def test_unique_registered_unit_replaces_only_selected_skill(self):
        before = tree_hashes(self.task)
        original = tree_hashes(self.store.original_dir)
        active = self.store.current()
        request, receipt = self.deploy()
        self.assertEqual(receipt["version_id"], self.first["version_id"])
        self.assertEqual(receipt["presentation_mode"], "sidecar_v1")
        self.assertEqual(receipt["skill_prefix"], "table")
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.rendered))
        for name, digest in original.items():
            self.assertEqual(tree_hashes(request.skills_dir / "table")[name], digest)
        self.assertTrue((request.skills_dir / "table/SKILL_PATCHES.md").is_file())
        self.assertEqual(tree_hashes(request.skills_dir / "other"), tree_hashes(self.task / "skills/other"))
        self.assertEqual(tree_hashes(self.task), before)
        self.assertEqual(tree_hashes(request.task_dir), before)
        self.assertEqual(tree_hashes(self.store.original_dir), original)
        self.assertEqual(self.store.current(), active)
        self.assertEqual(request.skill_condition, f"patch-set:reference/table@{self.first['version_id']}")
        self.assertEqual(json.loads((self.root / "deployment/patch_set.json").read_text()), receipt)
        self.assertEqual(request.model, self.request.model)
        self.assertEqual(request.timeout_sec, self.request.timeout_sec)

    def test_prepared_request_can_be_prepared_again_for_normal_executor(self):
        request, receipt = self.deploy()
        final = self.adapter.prepare(request, self.root / "final-run/input")
        self.assertEqual(tree_hashes(final.skills_dir), receipt["deployed_pack_hashes"])
        self.assertEqual((final.task_dir / "input.csv").read_bytes(), (self.task / "input.csv").read_bytes())
        self.assertEqual(final.task_document.read_bytes(), (self.task / "instructions.txt").read_bytes())

    def test_adapter_task_identity_digest_may_use_its_native_convention(self):
        delegate = self.adapter

        class NativeDigestAdapter:
            def prepare(self, request, output):
                task = delegate.prepare(request, output)
                task.task_digest = "tables:native-task-version-7"
                return task

        _, receipt = self.deploy(adapter=NativeDigestAdapter())
        self.assertEqual(receipt["task_digest"], "tables:native-task-version-7")
        self.assertTrue(receipt["task_file_digest"].startswith("sha256:"))

    def test_matching_filename_with_other_content_is_rejected_even_with_path(self):
        (self.skill / "SKILL.md").write_text(SKILL + "Another original version.\n")
        with self.assertRaisesRegex(ValueError, "No Skill matches"):
            self.deploy()
        with self.assertRaisesRegex(ValueError, "not the registered original"):
            self.deploy(output=self.root / "explicit", skill_path="table/SKILL.md")

    def test_equal_documents_with_changed_script_are_not_same_skill(self):
        (self.skill / "helper.py").write_text("another resource version\n")
        with self.assertRaisesRegex(ValueError, "not the registered original"):
            self.deploy(skill_path="table/SKILL.md")

    def test_ambiguous_content_requires_explicit_path(self):
        shutil.copytree(self.skill, self.task / "skills/copy")
        with self.assertRaisesRegex(ValueError, "Multiple Skills match"):
            self.deploy()
        request, receipt = self.deploy(output=self.root / "explicit", skill_path="copy/SKILL.md")
        self.assertEqual(receipt["skill_prefix"], "copy")
        self.assertEqual(tree_hashes(request.skills_dir / "copy"), tree_hashes(self.rendered))
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.skill))

    def test_no_skill_rejected_before_any_output_is_created(self):
        with self.assertRaisesRegex(ValueError, "with-skill"):
            self.deploy(request=replace(self.request, skill_mode="no-skill"))
        self.assertFalse((self.root / "deployment").exists())

    def test_unsafe_output_does_not_mutate_sources(self):
        external = self.root / "external-skills"
        shutil.copytree(self.task / "skills", external)
        request = replace(self.request, skills_dir=external)
        before = tree_hashes(self.task)
        original = tree_hashes(self.store.registry_dir)
        for output in (self.task / "new-output", external / "new-output", self.store.registry_dir / "new-output"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(ValueError, "fresh directory outside"):
                    self.deploy(request=request, output=output)
                self.assertFalse(output.exists())
        self.assertEqual(tree_hashes(self.task), before)
        self.assertEqual(tree_hashes(self.store.registry_dir), original)

    def test_explicit_old_version_is_deployed_without_switching_current(self):
        second_entry = {**self.entry, "entry_id": "P-second", "when_to_use": "Whenever formula inputs change."}
        second_rendered = self.root / "rendered-second"
        render_patch_set(self.skill, [second_entry], second_rendered)
        second = self.store.commit([second_entry], second_rendered, {"receipt": "second"},
                                   expected_parent=self.first["version_id"])
        request, receipt = self.deploy(version_id=self.first["version_id"])
        self.assertEqual(receipt["version_id"], self.first["version_id"])
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.rendered))
        self.assertEqual(self.store.current(), second)

    def test_current_pointer_change_during_prepare_does_not_change_pinned_version(self):
        second_entry = {**self.entry, "entry_id": "P-second", "when_to_use": "Whenever formula inputs change."}
        second_rendered = self.root / "rendered-second"
        render_patch_set(self.skill, [second_entry], second_rendered)
        store = self.store
        first = self.first
        delegate = self.adapter

        class ChangingAdapter:
            def prepare(self, request, output):
                store.commit([second_entry], second_rendered, {"receipt": "second"},
                             expected_parent=first["version_id"])
                return delegate.prepare(request, output)

        request, receipt = self.deploy(adapter=ChangingAdapter())
        self.assertEqual(receipt["version_id"], self.first["version_id"])
        self.assertNotEqual(self.store.current()["version_id"], self.first["version_id"])
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.rendered))

    def test_known_deployed_input_can_be_restored_to_original(self):
        shutil.rmtree(self.skill)
        shutil.copytree(self.rendered, self.skill)
        request, receipt = self.deploy(version_id="original")
        self.assertIsNone(receipt["version_id"])
        self.assertEqual(receipt["presentation_mode"], "original")
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.store.original_dir))
        self.assertEqual(request.skill_condition, "patch-set:reference/table@original")
        self.assertEqual(self.store.current(), self.first)

    def test_external_skill_pack_uses_explicit_source_and_preserves_it(self):
        external = self.root / "external"
        shutil.copytree(self.task / "skills", external)
        before = tree_hashes(external)
        request, receipt = self.deploy(request=replace(self.request, skills_dir=external))
        self.assertEqual(tree_hashes(external), before)
        self.assertEqual(tree_hashes(request.skills_dir / "table"), tree_hashes(self.rendered))
        self.assertEqual(receipt["original_request"]["skills_dir"], str(external))

    def test_bad_relative_path_or_unknown_version_does_not_select_by_name(self):
        with self.assertRaises(ValueError):
            self.deploy(skill_path="../table/SKILL.md")
        with self.assertRaises(ValueError):
            self.deploy(output=self.root / "unknown", version_id="v-unknown")
        self.assertFalse((self.root / "unknown").exists())


if __name__ == "__main__":
    unittest.main()
