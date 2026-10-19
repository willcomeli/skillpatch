"""Regression checks for existing repairs and immutable historical archives."""

import json
import shutil
import unittest
from pathlib import Path

from skillpatch.patch_sets import PatchSetStore
from skillpatch.patches import tree_hashes
from skillpatch.stage3_sources import read_receipt
from skillpatch.stages.consolidation import run_stage3
from skillpatch.storage import write_json
import test_consolidation as consolidation_fixtures
from test_stage2 import assessment, candidate


class InterferingFollowupBackend(consolidation_fixtures.FollowupBackend):
    """The new instruction fixes its own task but breaks an earlier one."""
    def run(self, request, task, run_dir, environment):
        result = super().run(request, task, run_dir, environment)
        if ("additional scenario" not in task.task_document.read_text()
                and "additional condition" in consolidation_fixtures.fixture_skill_text(task)):
            result.summary.update(loss=1, behavior="Removed duplicate rows.")
            result.normalized_trajectory_path.write_text("Read the table.\nRemoved duplicate rows.\nSaved output.\n")
        return result


class PurposeClient:
    """Return an explicit fixture judgment for each independent model purpose."""
    def __init__(self, *, old_repair_preserved):
        self.old_repair_preserved = old_repair_preserved
        self.calls = []

    def complete(self, config, messages, environment, output):
        purpose = config.prompt_path.stem
        self.calls.append(purpose)
        write_json(output / "model_call.json", {"status": "completed", "usage": {"total_tokens": 10}})
        if purpose == "consolidation":
            result = {"action": "keep", "reason": "Keep both supplements separately; verify their effects using execution evidence.",
                      "when_to_use": None, "how_to_interpret": None}
        elif purpose == "patch_evaluation":
            result = assessment()
        elif purpose == "consolidation_evaluation":
            result = assessment(repaired=self.old_repair_preserved, regression=True)
        else:
            raise AssertionError("Unexpected fixture model purpose: " + purpose)
        return json.dumps(result)


class ConsolidationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = consolidation_fixtures.ConsolidationTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.first_queue = self.fixture.queue()
        self.first, _ = self.fixture.integrate(self.first_queue)
        self.store = PatchSetStore(self.fixture.registry, self.fixture.key)

    def followup_queue(self, *, interferes=False):
        fixture = self.fixture
        task = fixture.root / "followup-task"
        shutil.copytree(fixture.request.task_dir, task)
        shutil.rmtree(task / "skills/table")
        shutil.copytree(self.store.skill_dir(), task / "skills/table")
        (task / "instructions.txt").write_text("Preserve duplicate records for this task. An additional scenario.")
        fixture.backend = (InterferingFollowupBackend() if interferes
                           else consolidation_fixtures.FollowupBackend())
        diagnosis = fixture.new_diagnosis(task, "followup-source")
        return fixture.queue("followup-episode", diagnosis=diagnosis,
                             body=candidate("preserve duplicates under the additional condition"))

    def integrate(self, queue, client, output=None):
        fixture = self.fixture
        fixture.backend.calls.clear()
        return run_stage3(queue, fixture.key, fixture.registry, fixture.stage3_config,
            output or fixture.root / "stage3-followup", fixture.environment, client=client,
            adapter_factory=lambda _: fixture.adapter, backend_factory=lambda _: fixture.backend)

    def test_stage2_on_current_set_still_checks_old_sources_before_keep(self):
        queue = self.followup_queue()
        client = PurposeClient(old_repair_preserved=True)
        result = self.integrate(queue, client)
        self.assertEqual(result["decision"], "accepted", result)
        self.assertEqual(result["action"], "keep")
        self.assertEqual(result["reason"], "combination_validation")
        self.assertEqual(len(self.fixture.backend.calls), 4)
        self.assertEqual(client.calls, ["patch_evaluation", "consolidation_evaluation", "consolidation"])
        records = result["combination_validation"]["cases"]
        self.assertEqual({record["mode"] for record in records}, {"repair", "preservation"})
        self.assertEqual({record["comparison"]["status"] for record in records}, {"improved", "unchanged"})

    def test_new_stage2_success_cannot_hide_old_task_regression(self):
        queue = self.followup_queue(interferes=True)
        client = PurposeClient(old_repair_preserved=False)
        result = self.integrate(queue, client)
        self.assertEqual(result["decision"], "rejected", result)
        self.assertEqual(result["reason"], "new_combination_not_accepted")
        self.assertEqual(len(self.fixture.backend.calls), 4)
        self.assertEqual(client.calls, ["patch_evaluation", "consolidation_evaluation"])
        self.assertNotIn("proposal", result)
        self.assertEqual(self.store.current()["version_id"], self.first["version_id"])
        self.assertFalse(result["active_set_changed"])

    def test_output_cannot_pollute_any_prior_support_archive_before_failing(self):
        fixture = self.fixture
        task = fixture.root / "another-source-task"
        shutil.copytree(fixture.request.task_dir, task)
        diagnosis = fixture.new_diagnosis(task, "another-source")
        queue = fixture.queue("another-episode", diagnosis=diagnosis)
        old_receipt = read_receipt(self.store.current()["entries"][0]["supports"][0])
        paths = [self.first_queue.parent, fixture.source.diagnosis_dir, fixture.source.prepared.task_dir,
                 *[fixture.source.run_dir / name for name in ("input", "backend", "jobs", "prompt")],
                 Path(old_receipt["cases"][0]["baseline_dir"]),
                 Path(old_receipt["cases"][0]["candidate_dir"])]
        before = tree_hashes(fixture.root)
        client = PurposeClient(old_repair_preserved=True)
        for index, parent in enumerate(paths):
            output = parent / f"forbidden-stage3-{index}"
            with self.subTest(parent=parent), self.assertRaisesRegex(ValueError, "outside original and historical"):
                self.integrate(queue, client, output)
            self.assertFalse(output.exists())
            self.assertEqual(tree_hashes(fixture.root), before)
            self.assertEqual(self.fixture.backend.calls, [])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.store.current()["version_id"], self.first["version_id"])

    def test_default_source_run_stage3_output_layout_preserves_existing_evidence(self):
        output = self.fixture.source.run_dir / "stage3" / "default-layout-episode"
        reference = self.store.current()["entries"][0]["supports"][0]
        receipt = read_receipt(reference)
        client = PurposeClient(old_repair_preserved=True)
        result = self.integrate(self.first_queue, client, output)
        self.assertEqual(result["decision"], "unchanged", result)
        self.assertTrue((output / "result.json").is_file())
        self.assertEqual(read_receipt(reference), receipt)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.fixture.backend.calls, [])

    def test_source_default_layout_exception_cannot_extend_existing_stage3_episode(self):
        historical = self.fixture.source.run_dir / "stage3" / "historical-episode"
        historical.mkdir(parents=True)
        (historical / "evidence.json").write_text('{"status": "archived"}')
        output = historical / "new-episode"
        before = tree_hashes(self.fixture.root)
        client = PurposeClient(old_repair_preserved=True)
        with self.assertRaisesRegex(ValueError, "outside original and historical"):
            self.integrate(self.first_queue, client, output)
        self.assertFalse(output.exists())
        self.assertEqual(tree_hashes(self.fixture.root), before)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.fixture.backend.calls, [])

    def test_source_default_layout_exception_does_not_allow_registry_in_source(self):
        fixture = self.fixture
        output = fixture.source.run_dir / "stage3" / "new-episode"
        registry = fixture.source.run_dir / "stage3" / "forbidden-registry"
        before = tree_hashes(fixture.root)
        client = PurposeClient(old_repair_preserved=True)
        fixture.backend.calls.clear()
        with self.assertRaisesRegex(ValueError, "outside original and historical"):
            run_stage3(self.first_queue, fixture.key, registry, fixture.stage3_config,
                output, fixture.environment, client=client,
                adapter_factory=lambda _: fixture.adapter, backend_factory=lambda _: fixture.backend)
        self.assertFalse(output.exists())
        self.assertFalse(registry.exists())
        self.assertEqual(tree_hashes(fixture.root), before)
        self.assertEqual(client.calls, [])
        self.assertEqual(fixture.backend.calls, [])

    def test_source_default_layout_symlink_cannot_redirect_into_source_root(self):
        source = self.fixture.source.run_dir
        (source / "stage3").symlink_to(source, target_is_directory=True)
        output = source / "stage3" / "forbidden-episode"
        client = PurposeClient(old_repair_preserved=True)
        with self.assertRaisesRegex(ValueError, "outside original and historical"):
            self.integrate(self.first_queue, client, output)
        self.assertFalse((source / "forbidden-episode").exists())
        self.assertEqual(client.calls, [])
        self.assertEqual(self.fixture.backend.calls, [])

    def test_output_cannot_be_created_elsewhere_inside_registry_root(self):
        output = self.fixture.registry / "unrelated-skill" / "forbidden-output"
        before = tree_hashes(self.fixture.registry)
        with self.assertRaisesRegex(ValueError, "outside immutable registry"):
            self.integrate(self.first_queue, PurposeClient(old_repair_preserved=True), output)
        self.assertFalse(output.exists())
        self.assertEqual(tree_hashes(self.fixture.registry), before)


if __name__ == "__main__":
    unittest.main()
