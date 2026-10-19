"""Adapter checks use synthetic tasks only; never launch a benchmark."""

import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from skillpatch.adapters.skillsbench import SkillsBenchAdapter
from skillpatch.models import BackendRun, RunRequest
from skillpatch.storage import hash_manifest, sha256_file


class SkillsBenchPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "native-task"
        for name, text in {
            "task.md": "---\nschema_version: '1.3'\n---\nOriginal task; no extra invocation instruction.\n",
            "environment/Dockerfile": "FROM synthetic-only\n",
            "environment/input.csv": "id,value\n1,42\n",
            "environment/skills/example/SKILL.md": "---\nname: example\n---\nUse the supplied data.\n",
            "environment/skills/example/scripts/helper.sh": "fixture script text\n",
            "environment/skills/example/assets/template.txt": "template asset\n",
            "environment/skills/reference.md": "supporting reference\n",
            "verifier/test.sh": "synthetic verifier text; do not execute\n",
            "oracle/solve.sh": "synthetic oracle text; do not execute\n",
        }.items():
            self.write(self.source / name, text)
        (self.source / "environment/empty-data").mkdir()
        self.destination = self.root / "run" / "input"
        self.adapter = SkillsBenchAdapter()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def request(self, **kwargs):
        return RunRequest(benchmark="skillsbench", task_dir=self.source, agent="fixture-agent",
                          model="fixture-model", bench_command=("fixture-bench",), **kwargs)

    def test_complete_isolated_copy_preserves_original_prompt_and_hashes(self):
        prepared = self.adapter.prepare(self.request(skill_condition="reference"), self.destination)
        self.assertEqual(prepared.task_id, "native-task")
        self.assertEqual(prepared.task_dir, self.destination / "tasks/native-task")
        self.assertEqual(prepared.task_document.read_bytes(), (self.source / "task.md").read_bytes())
        self.assertTrue((prepared.task_dir / "oracle/solve.sh").is_file())
        self.assertTrue((prepared.task_dir / "environment/empty-data").is_dir())
        self.assertTrue((prepared.skills_dir / "example/assets/template.txt").is_file())
        self.assertTrue((prepared.skills_dir / "reference.md").is_file())
        self.assertEqual(prepared.task_digest, hash_manifest(prepared.file_hashes))
        for name, digest in prepared.file_hashes.items():
            self.assertEqual(digest, sha256_file(prepared.task_dir / name))
            self.assertNotEqual((prepared.task_dir / name).stat().st_ino, (self.source / name).stat().st_ino)
        manifest = json.loads(prepared.manifest_path.read_text())
        self.assertEqual(manifest["source"]["file_hashes"], prepared.file_hashes)
        self.assertEqual(manifest["skills"]["skill_condition"], "reference")
        self.assertTrue(manifest["skills"]["requested_deployment"])
        self.assertEqual(prepared.skill_digest, manifest["skills"]["source_digest"])
        self.write(prepared.task_document, "snapshot changed")
        self.assertIn("Original task", (self.source / "task.md").read_text())

    def test_external_skillpack_replaces_only_snapshot(self):
        external = self.root / "external-skills"
        self.write(external / "custom/SKILL.md", "custom instructions")
        self.write(external / "custom/resources/table.csv", "a,b\n1,2\n")
        prepared = self.adapter.prepare(self.request(skills_dir=external), self.destination)
        self.assertFalse((prepared.skills_dir / "example").exists())
        self.assertTrue((prepared.skills_dir / "custom/resources/table.csv").is_file())
        self.assertTrue((self.source / "environment/skills/example/SKILL.md").is_file())
        manifest = json.loads(prepared.manifest_path.read_text())
        self.assertEqual(manifest["skills"]["source"], "external")
        self.assertNotEqual(manifest["source"]["task_digest"], prepared.task_digest)
        self.write(prepared.skills_dir / "custom/SKILL.md", "modified snapshot")
        self.assertEqual((external / "custom/SKILL.md").read_text(), "custom instructions")

    def test_no_skill_records_request_without_claiming_deployment(self):
        prepared = self.adapter.prepare(self.request(skill_mode="no-skill"), self.destination)
        self.assertIsNone(prepared.skills_dir)
        self.assertIsNone(prepared.skill_digest)
        self.assertTrue((prepared.task_dir / "environment/skills/example/SKILL.md").is_file())
        manifest = json.loads(prepared.manifest_path.read_text())
        self.assertFalse(manifest["skills"]["requested_deployment"])
        self.assertTrue(manifest["skills"]["source_file_hashes"])

    def test_missing_native_file_is_rejected_before_copy(self):
        (self.source / "verifier/test.sh").unlink()
        with self.assertRaisesRegex(ValueError, "required file"):
            self.adapter.prepare(self.request(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_with_skill_needs_skillpack_but_no_skill_does_not(self):
        shutil.rmtree(self.source / "environment/skills")
        with self.assertRaisesRegex(ValueError, "skill pack"):
            self.adapter.prepare(self.request(), self.destination)
        prepared = self.adapter.prepare(self.request(skill_mode="no-skill"), self.destination)
        self.assertIsNone(prepared.skills_dir)

    def test_root_skill_document_is_not_a_discoverable_pack(self):
        shutil.rmtree(self.source / "environment/skills")
        self.write(self.source / "environment/skills/SKILL.md", "root-level instructions")
        with self.assertRaisesRegex(ValueError, "discoverable skill pack"):
            self.adapter.prepare(self.request(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_deeply_nested_skill_is_not_a_discoverable_pack(self):
        external = self.root / "external-skills"
        self.write(external / "group/nested/SKILL.md", "nested instructions")
        with self.assertRaisesRegex(ValueError, "discoverable skill pack"):
            self.adapter.prepare(self.request(skills_dir=external), self.destination)
        self.assertFalse(self.destination.exists())

    def test_valid_direct_skill_allows_additional_directories_and_files(self):
        skills = self.source / "environment/skills"
        self.write(skills / "SKILL.md", "extra root document")
        self.write(skills / "group/nested/SKILL.md", "extra nested document")
        (skills / "empty").mkdir()
        prepared = self.adapter.prepare(self.request(), self.destination)
        self.assertTrue((prepared.skills_dir / "example/SKILL.md").is_file())
        self.assertTrue((prepared.skills_dir / "empty").is_dir())
        self.assertTrue((prepared.skills_dir / "group/nested/SKILL.md").is_file())

    def test_task_symlink_is_rejected(self):
        (self.source / "environment/link").symlink_to(self.source / "task.md")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            self.adapter.prepare(self.request(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_external_symlink_is_rejected(self):
        external = self.root / "external"
        external.symlink_to(self.source / "environment/skills", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.adapter.prepare(self.request(skills_dir=external), self.destination)

    def test_recursive_destination_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside the task source"):
            self.adapter.prepare(self.request(), self.source / "nested/input")
        self.assertFalse((self.source / "nested").exists())

    def test_destination_inside_external_pack_is_rejected(self):
        external = self.root / "external"
        self.write(external / "custom/SKILL.md", "custom")
        with self.assertRaisesRegex(ValueError, "inside the external skills source"):
            self.adapter.prepare(self.request(skills_dir=external), external / "run/input")

    def test_existing_destination_is_not_overwritten(self):
        self.write(self.destination / "keep.txt", "keep")
        with self.assertRaisesRegex(ValueError, "already exist"):
            self.adapter.prepare(self.request(), self.destination)
        self.assertEqual((self.destination / "keep.txt").read_text(), "keep")

    def test_source_change_during_copy_is_rejected(self):
        original_copytree = shutil.copytree

        def changing_copytree(source, target, *args, **kwargs):
            result = original_copytree(source, target, *args, **kwargs)
            if Path(source) == self.source:
                self.write(self.source / "task.md", "changed while copying")
            return result

        with patch("skillpatch.adapters.skillsbench.shutil.copytree", side_effect=changing_copytree):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.adapter.prepare(self.request(), self.destination)
        self.assertFalse((self.destination / "manifest.json").exists())


class SkillsBenchEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.verifier = Path(self.temporary.name) / "verifier"
        self.verifier.mkdir()
        self.adapter = SkillsBenchAdapter()

    def evaluate(self, summary=None, status="completed"):
        return self.adapter.evaluate(BackendRun(status=status, summary=summary or {}, verifier_dir=self.verifier))

    def test_zero_is_a_scored_failure_and_other_metrics_are_preserved(self):
        rewards = {"reward": 0, "partial_score": 0.25, "details": {"errors": ["missing field"]}}
        feedback = self.evaluate({"rewards": rewards})
        self.assertEqual(feedback.status, "scored")
        self.assertIs(feedback.passed, False)
        self.assertEqual(feedback.metrics, rewards)
        feedback.metrics["details"]["errors"].append("local edit")
        self.assertEqual(rewards["details"]["errors"], ["missing field"])

    def test_missing_reward_does_not_become_zero(self):
        (self.verifier / "test-stdout.txt").write_text("no reward produced")
        feedback = self.evaluate()
        self.assertEqual(feedback.status, "unavailable")
        self.assertIsNone(feedback.passed)
        self.assertEqual(feedback.metrics, {})
        self.assertIn("test-stdout.txt", feedback.raw_paths)

    def test_official_reward_file_fallback(self):
        (self.verifier / "reward.txt").write_text("1\n")
        feedback = self.evaluate()
        self.assertEqual(feedback.status, "scored")
        self.assertTrue(feedback.passed)
        self.assertEqual(feedback.metrics["reward"], 1)

    def test_contradictory_rewards_are_an_error(self):
        (self.verifier / "reward.txt").write_text("0")
        feedback = self.evaluate({"rewards": {"reward": 1}})
        self.assertEqual(feedback.status, "error")
        self.assertIsNone(feedback.passed)
        self.assertEqual(feedback.metrics["reward"], 1)
        self.assertIn("Conflicting", feedback.issues[0])

    def test_verifier_error_takes_precedence_over_reward(self):
        feedback = self.evaluate({"rewards": {"reward": 1}, "verifier_error": "timed out"})
        self.assertEqual(feedback.status, "error")
        self.assertIsNone(feedback.passed)
        self.assertEqual(feedback.metrics["reward"], 1)

    def test_invalid_reward_is_not_a_failed_task(self):
        for value in (None, "NaN", True, {"unexpected": 0}):
            with self.subTest(value=value):
                feedback = self.evaluate({"rewards": {"reward": value}})
                self.assertEqual(feedback.status, "error")
                self.assertIsNone(feedback.passed)

    def test_fractional_reward_is_preserved_without_binary_claim(self):
        feedback = self.evaluate({"rewards": {"reward": 0.5}})
        self.assertEqual(feedback.status, "scored")
        self.assertIsNone(feedback.passed)

    def test_ctrf_keeps_failed_test_message_and_summary(self):
        tests = [{"name": "required_field", "status": "failed", "message": "Expected field missing", "trace": "fixture traceback"}]
        ctrf = {"results": {"tests": tests, "summary": {"tests": 1, "passed": 0, "failed": 1}}}
        (self.verifier / "ctrf.json").write_text(json.dumps(ctrf))
        feedback = self.evaluate({"rewards": {"reward": 0}})
        self.assertEqual(feedback.tests, tests)
        self.assertEqual(feedback.metrics["ctrf_summary"], ctrf["results"]["summary"])
        self.assertIn("ctrf.json", feedback.raw_paths)

    def test_malformed_ctrf_is_reported(self):
        (self.verifier / "ctrf.json").write_text("not json")
        feedback = self.evaluate({"rewards": {"reward": 1}})
        self.assertEqual(feedback.status, "error")
        self.assertIsNone(feedback.passed)

    def test_nonfinite_metrics_report_error_and_remain_serializable(self):
        rewards = {"reward": 1, "details": {"scores": [float("nan"), float("inf"), float("-inf")]}}
        feedback = self.evaluate({"rewards": rewards})
        self.assertEqual(feedback.status, "error")
        self.assertIsNone(feedback.passed)
        self.assertEqual(feedback.metrics["reward"], 1)
        self.assertIn("rewards.details.scores[0]", feedback.issues[0])
        self.assertEqual(feedback.metrics["details"]["scores"], [
            "<non-finite float: nan>", "<non-finite float: inf>", "<non-finite float: -inf>",
        ])
        json.dumps(feedback.__dict__, allow_nan=False)

    def test_nonfinite_reward_itself_remains_serializable(self):
        feedback = self.evaluate({"rewards": {"reward": float("nan")}})
        self.assertEqual(feedback.status, "error")
        self.assertIsNone(feedback.passed)
        self.assertEqual(feedback.metrics["reward"], "<non-finite float: nan>")
        json.dumps(feedback.__dict__, allow_nan=False)

    def test_ctrf_rejects_nonfinite_json_constants_and_overflow(self):
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(literal=literal):
                (self.verifier / "ctrf.json").write_text(
                    '{"results":{"tests":[{"name":"test","duration":' + literal + '}]}}'
                )
                feedback = self.evaluate({"rewards": {"reward": 1}})
                self.assertEqual(feedback.status, "error")
                self.assertIsNone(feedback.passed)
                self.assertEqual(feedback.tests, [])
                self.assertIn("Cannot read verifier ctrf.json", feedback.issues[0])
                json.dumps(feedback.__dict__, allow_nan=False)

    def test_real_feedback_survives_agent_error(self):
        feedback = self.evaluate({"error": "agent execution failed", "rewards": {"reward": 0}}, status="error")
        self.assertEqual(feedback.status, "scored")
        self.assertIs(feedback.passed, False)

    def test_agent_selftest_is_not_verifier_feedback(self):
        agent = self.verifier.parent / "agent"
        agent.mkdir()
        (agent / "reward.txt").write_text("1")
        (agent / "test-stdout.txt").write_text("self-test passed")
        feedback = self.evaluate({"agent_result": {"reward": 1}})
        self.assertEqual(feedback.status, "unavailable")
        self.assertIsNone(feedback.passed)


if __name__ == "__main__":
    unittest.main()
