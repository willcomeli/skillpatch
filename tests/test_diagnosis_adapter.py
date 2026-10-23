"""Synthetic engineering fixtures for items assembly, never benchmark runs."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from method_input_adapters.skillpatch import build_diagnosis_context
from context import render_diagnosis_context
from excutor.executor_adapter.storage import hash_manifest, jsonable, sha256_file


class DiscoveryContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "synthetic-run"
        self.task = self.root / "input/tasks/demo"
        self.skills = self.task / "environment/skills"
        self.manifest_path = self.root / "input/manifest.json"
        self.canonical = self.root / "jobs/trial/trajectory/acp_trajectory.jsonl"
        self.events = self.root / "backend/events.jsonl"
        self.verifier = self.root / "jobs/trial/verifier"
        for name, text in {
            "task.md": "---\nmetadata: fixture\n---\nOriginal task document.\n",
            "environment/Dockerfile": "synthetic, never executed\n",
            "environment/skills/pack/SKILL.md": "---\nname: pack\n---\nDo the required check.\n",
            "environment/skills/pack/scripts/check.py": "SCRIPT_CONTENT_FOR_CONTEXT\n",
            "environment/skills/pack/references/guide.md": "REFERENCE_CONTENT_FOR_CONTEXT\n",
            "oracle/solve.sh": "ORACLE_BODY_MUST_NOT_ENTER_CONTEXT\n",
            "verifier/test_outputs.py": "VERIFIER_SOURCE_MUST_NOT_ENTER_CONTEXT\n",
            "verifier/test.sh": "synthetic verifier command\n",
        }.items():
            self.write(self.task / name, text)
        self.write(self.verifier / "test-stdout.txt", "Official failure: missing output\n")
        self.write(self.verifier / "reward.txt", "0\n")
        self.write(self.root / "jobs/trial/result.json", "{}")
        self.raw_events = [
            {"type": "user_message", "text": "Actual user task\nKeep the second line."},
            {"type": "tool_call", "kind": "skill", "status": "completed", "content": [{"type": "text", "text": "Skill body seen."}]},
            {"type": "agent_message", "text": "Finished."},
        ]
        self.write_events()
        self.result = {
            "schema_version": 1, "run_id": self.root.name, "benchmark": "skillsbench", "task_id": "demo",
            "status": "completed", "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:01:00Z",
            "prepared": {"benchmark": "skillsbench", "task_id": "demo", "task_dir": str(self.task),
                         "task_document": str(self.task / "task.md"), "skills_dir": str(self.skills),
                         "manifest_path": str(self.manifest_path)},
            "execution": {"status": "completed", "trajectory_path": str(self.canonical),
                          "normalized_trajectory_path": str(self.events), "trajectory_events": 3,
                          "trajectory_complete": True, "verifier_dir": str(self.verifier),
                          "result_path": str(self.root / "jobs/trial/result.json"),
                          "summary": {"trajectory_summary": {"steps": 3}, "usage_tracking": {"status": "unavailable"}}},
            "feedback": {"status": "scored", "metrics": {"reward": 0}, "passed": False,
                         "tests": [{"name": "output", "status": "failed", "message": "missing output"}],
                         "issues": [], "raw_paths": {"test-stdout.txt": str(self.verifier / "test-stdout.txt"),
                                                      "reward.txt": str(self.verifier / "reward.txt")}},
        }
        self.refresh_hashes()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def save_result(self):
        self.write(self.root / "result.json", json.dumps(self.result))

    def refresh_hashes(self):
        hashes = {path.relative_to(self.task).as_posix(): sha256_file(path) for path in sorted(self.task.rglob("*")) if path.is_file()}
        skill_hashes = {path.relative_to(self.skills).as_posix(): sha256_file(path) for path in sorted(self.skills.rglob("*")) if path.is_file()}
        self.result["prepared"].update(file_hashes=hashes, task_digest=hash_manifest(hashes), skill_digest=hash_manifest(skill_hashes))
        manifest = {
            "schema_version": 1, "benchmark": "skillsbench", "task_id": "demo",
            "source": {"task_dir": "/external/original/source/never-open-this"},
            "prepared": {"task_dir": str(self.task), "file_hashes": hashes, "task_digest": hash_manifest(hashes)},
            "skills": {"prepared_dir": str(self.skills), "skill_digest": hash_manifest(skill_hashes)},
        }
        self.write(self.manifest_path, json.dumps(manifest))
        self.save_result()

    def write_events(self):
        self.write(self.canonical, "\n".join(json.dumps(event) for event in self.raw_events))
        self.normalized = [{"event_id": f"T{index:06d}", "source": {"path": str(self.canonical), "line": index}, "event": event}
                           for index, event in enumerate(self.raw_events, 1)]
        self.save_events()

    def save_events(self):
        self.write(self.events, "\n".join(json.dumps(event) for event in self.normalized))

    def test_complete_context_has_actual_task_all_text_skills_and_feedback(self):
        data = build_diagnosis_context(self.root)
        items = {item.context_id: item for item in data.items}
        self.assertIn("Actual user task\nKeep the second line.", items["Q0001"].text)
        self.assertIn("metadata: fixture", items["Q0002"].text)
        self.assertEqual(items["T000001"].source_line, 1)
        self.assertIn("Official failure", render_diagnosis_context(data))
        skill_items = [item for item in data.items if item.kind == "skill"]
        self.assertEqual(len(skill_items), 3)
        self.assertEqual({item.skill_id for item in skill_items}, {"pack/SKILL.md"})
        self.assertIn("pack/scripts/check.py", {item.relative_path for item in skill_items})
        self.assertTrue(all(item.version == sha256_file(Path(item.source_path)) for item in skill_items))
        self.assertNotIn(str(self.root), render_diagnosis_context(data))



    def test_unfinished_run_is_rejected_by_loader_and_adapter(self):
        from method_input_adapters.skillpatch import get_diagnosis_adapter
        self.result["finished_at"] = None
        self.save_result()
        for build in (build_diagnosis_context,
                      get_diagnosis_adapter("skillsbench").build_diagnosis_context):
            with self.assertRaisesRegex(ValueError, "has not finished"):
                build(self.root)

    def test_digest_is_stable_and_covers_full_input_except_itself(self):
        data = build_diagnosis_context(self.root)
        payload = jsonable(data)
        del payload["input_digest"]
        expected = "sha256:" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        self.assertEqual(data.input_digest, expected)
        self.assertEqual(data.input_digest, build_diagnosis_context(self.root).input_digest)

    def test_snapshot_tampering_is_rejected(self):
        self.write(self.skills / "pack/SKILL.md", "tampered")
        with self.assertRaisesRegex(ValueError, "file hashes"):
            build_diagnosis_context(self.root)

    def test_added_snapshot_file_is_rejected(self):
        self.write(self.task / "extra.txt", "unrecorded")
        with self.assertRaisesRegex(ValueError, "file hashes"):
            build_diagnosis_context(self.root)

    def test_mismatched_task_digest_is_rejected(self):
        self.result["prepared"]["task_digest"] = "sha256:wrong"
        self.save_result()
        with self.assertRaisesRegex(ValueError, "Task digest"):
            build_diagnosis_context(self.root)

    def test_mismatched_skill_digest_is_rejected(self):
        self.result["prepared"]["skill_digest"] = "sha256:wrong"
        self.save_result()
        with self.assertRaisesRegex(ValueError, "Skill digest"):
            build_diagnosis_context(self.root)

    def test_run_identity_is_checked(self):
        self.result["run_id"] = "some-other-run"
        self.save_result()
        with self.assertRaisesRegex(ValueError, "run_id"):
            build_diagnosis_context(self.root)

    def test_external_trajectory_source_is_rejected(self):
        external = Path(self.temporary.name) / "old-trajectory.jsonl"
        self.write(external, json.dumps(self.raw_events[0]))
        self.normalized[0]["source"]["path"] = str(external)
        self.save_events()
        with self.assertRaisesRegex(ValueError, "outside this run"):
            build_diagnosis_context(self.root)

    def test_external_feedback_path_is_rejected_even_if_key_is_unknown(self):
        external = Path(self.temporary.name) / "secret.txt"
        self.write(external, "outside")
        self.result["feedback"]["raw_paths"]["unexpected"] = str(external)
        self.save_result()
        with self.assertRaisesRegex(ValueError, "outside this run"):
            build_diagnosis_context(self.root)

    def test_external_prepared_task_is_rejected(self):
        self.result["prepared"]["task_dir"] = self.temporary.name
        self.save_result()
        with self.assertRaisesRegex(ValueError, "outside this run"):
            build_diagnosis_context(self.root)

    def test_duplicate_event_ids_are_rejected(self):
        self.normalized[1]["event_id"] = "T000001"
        self.save_events()
        with self.assertRaisesRegex(ValueError, "event IDs"):
            build_diagnosis_context(self.root)

    def test_event_payload_must_match_canonical_line(self):
        self.normalized[0]["event"] = {"type": "user_message", "text": "invented"}
        self.save_events()
        with self.assertRaisesRegex(ValueError, "differs from the canonical"):
            build_diagnosis_context(self.root)

    def test_event_source_line_must_match(self):
        self.normalized[0]["source"]["line"] = 2
        self.save_events()
        with self.assertRaisesRegex(ValueError, "source line"):
            build_diagnosis_context(self.root)

    def test_event_count_must_match_executor(self):
        self.result["execution"]["trajectory_events"] = 4
        self.save_result()
        with self.assertRaisesRegex(ValueError, "event count"):
            build_diagnosis_context(self.root)

    def test_partial_and_unknown_trajectories_are_marked(self):
        for complete, phrase in ((False, "Trajectory is partial"), (None, "completeness is unknown")):
            with self.subTest(complete=complete):
                self.result["execution"]["trajectory_complete"] = complete
                self.save_result()
                data = build_diagnosis_context(self.root)
                self.assertIs(data.trajectory_complete, complete)
                self.assertTrue(any(phrase in item for item in data.limitations))

    def test_images_are_omitted_with_limitations_but_sibling_text_is_retained(self):
        self.raw_events[1]["content"] = [
            {"type": "text", "text": "Important visible tool output\nsecond line"},
            {"type": "image", "data": "SECRET_BASE64_IMAGE_PAYLOAD", "mimeType": "image/png", "caption": "Image caption"},
        ]
        self.write_events()
        data = build_diagnosis_context(self.root)
        rendered = render_diagnosis_context(data)
        self.assertNotIn("SECRET_BASE64_IMAGE_PAYLOAD", rendered)
        self.assertIn("Important visible tool output\nsecond line", rendered)
        self.assertIn("Image caption", rendered)
        self.assertTrue(any("T000002" in item and "non-text" in item for item in data.limitations))

    def test_image_url_placeholder_adds_limits_without_changing_event_text(self):
        text = "Image read successfully. Displaying image content.[Image: 1 URLs]\nOther text remains."
        self.raw_events[1]["content"] = [{"type": "text", "text": text}]
        self.write_events()
        data = build_diagnosis_context(self.root)
        event = next(item for item in data.items if item.context_id == "T000002")
        self.assertIn(text, event.text)
        self.assertIn(text, render_diagnosis_context(data))
        self.assertTrue(any("T000002" in item and "image placeholder" in item for item in data.limitations))
        self.assertIn(
            "trajectory_complete refers to recorded event coverage, not complete observability of underlying artifacts.",
            data.limitations,
        )
        self.assertTrue(data.trajectory_complete)

    def test_nontext_skill_assets_are_marked_not_sent(self):
        image = self.skills / "pack/assets/picture.png"
        self.write(image, "SYNTHETIC_IMAGE_PAYLOAD")
        self.refresh_hashes()
        data = build_diagnosis_context(self.root)
        self.assertNotIn("SYNTHETIC_IMAGE_PAYLOAD", render_diagnosis_context(data))
        self.assertTrue(any("picture.png" in item for item in data.limitations))

    def test_nontext_task_input_is_explicitly_marked_as_unseen(self):
        self.write(self.task / "environment/paper.pdf", "SYNTHETIC_PDF_PAYLOAD")
        self.refresh_hashes()
        data = build_diagnosis_context(self.root)
        self.assertNotIn("SYNTHETIC_PDF_PAYLOAD", render_diagnosis_context(data))
        self.assertTrue(any("Task asset environment/paper.pdf" in item for item in data.limitations))

    def test_oracle_and_verifier_source_never_become_text_evidence(self):
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path):
            if path.is_relative_to(self.task / "oracle") or path.is_relative_to(self.task / "verifier"):
                self.fail("Oracle/verifier source must be hashed only, never loaded as items text")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", guarded_read_bytes):
            data = build_diagnosis_context(self.root)
        rendered = render_diagnosis_context(data)
        self.assertNotIn("ORACLE_BODY_MUST_NOT_ENTER_CONTEXT", rendered)
        self.assertNotIn("VERIFIER_SOURCE_MUST_NOT_ENTER_CONTEXT", rendered)

    def test_unknown_feedback_files_are_not_opened_as_text(self):
        self.result["feedback"]["raw_paths"]["test_outputs.py"] = str(self.task / "verifier/test_outputs.py")
        self.save_result()
        self.assertNotIn("VERIFIER_SOURCE_MUST_NOT_ENTER_CONTEXT", render_diagnosis_context(build_diagnosis_context(self.root)))

    def test_oracle_cannot_masquerade_as_task_document(self):
        self.result["prepared"]["task_document"] = str(self.task / "oracle/solve.sh")
        self.save_result()
        with self.assertRaisesRegex(ValueError, "snapshot task.md"):
            build_diagnosis_context(self.root)

    def test_verifier_source_cannot_masquerade_as_skill_pack(self):
        self.result["prepared"]["skills_dir"] = str(self.task / "verifier")
        self.save_result()
        with self.assertRaisesRegex(ValueError, "environment/skills"):
            build_diagnosis_context(self.root)

    def test_feedback_must_come_from_same_trial_as_trajectory(self):
        other_verifier = self.root / "jobs/other/verifier"
        other_verifier.mkdir(parents=True)
        self.result["execution"]["verifier_dir"] = str(other_verifier)
        self.save_result()
        with self.assertRaisesRegex(ValueError, "trajectory's trial"):
            build_diagnosis_context(self.root)

    def test_inline_data_uri_is_removed_without_eating_following_text(self):
        self.raw_events[1]["content"] = [{"type": "text", "text": "data:image/png;base64,QUJDRA==\nKeep this following text"}]
        self.write_events()
        rendered = render_diagnosis_context(build_diagnosis_context(self.root))
        self.assertNotIn("QUJDRA==", rendered)
        self.assertIn("Keep this following text", rendered)

    def test_missing_reward_and_feedback_errors_remain_limitations(self):
        self.result["feedback"].update(status="error", metrics={}, issues=["verifier unavailable"])
        self.save_result()
        data = build_diagnosis_context(self.root)
        self.assertTrue(any("No verifier reward" in item for item in data.limitations))
        self.assertTrue(any("feedback status is error" in item for item in data.limitations))
        self.assertTrue(any("telemetry" in item for item in data.limitations))


if __name__ == "__main__":
    unittest.main()
