import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.models import BackendRun, EvaluationFeedback, EvidenceItem, ExecutionResult, PreparedTask, RunRequest
from skillpatch.patch_sidecars import SIDECAR_MARKDOWN, write_sidecars
from skillpatch.prompt_augmenters.openhands import OpenHandsReferenceAugmenter
from skillpatch.skill_invocation import (
    audit_skill_reads, check_patch_reads, explicit_prompt, prepare_prompt_artifacts, task_messages_equal,
)
from skillpatch.storage import hash_manifest, sha256_file, write_json


class SkillInvocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        skills = self.root / "environment" / "skills"
        (skills / "pdf").mkdir(parents=True)
        self.body = "Read relevant pages before editing.\nCheck applicable conditions."
        (skills / "pdf" / "SKILL.md").write_text("---\nname: pdf\n---\n" + self.body)
        task_doc = self.root / "task.md"
        task_doc.write_text("---\nname: example\n---\nPreserve all scientific content.\n")
        self.task = PreparedTask("fixture", "example", self.root, task_doc, skills,
                                 self.root / "manifest.json", "task-hash", "skill-hash")
        self.request = RunRequest("fixture", self.root, "openhands", "test-model", ("bench",))

    def test_prompt_adds_only_the_reference_sentence_and_preserves_task(self):
        before = self.task.task_document.read_bytes()
        prompt = explicit_prompt(self.request, self.task)
        self.assertEqual(prompt, "Before solving the task, invoke and read these task-provided Skills: `pdf`.\n\n"
                                 "Preserve all scientific content.\n")
        self.assertEqual(self.task.task_document.read_bytes(), before)

    def test_disabled_and_no_skill_do_not_override_task_prompt(self):
        self.assertIsNone(explicit_prompt(replace(self.request, explicit_skill_read=False), self.task))
        self.assertIsNone(explicit_prompt(replace(self.request, skill_mode="no-skill"), self.task))

    def test_preamble_is_loaded_from_the_central_template(self):
        template = self.root / "execution-template.md"
        template.write_text("Template-specific instruction.\n{skill_names}\n")
        with patch("skillpatch.skill_invocation.get_prompt_augmenter",
                   return_value=OpenHandsReferenceAugmenter(template)):
            prompt = explicit_prompt(self.request, self.task)
            self.assertTrue(prompt.startswith("Template-specific instruction."))
            self.assertIn("`pdf`", prompt)
            self.assertNotIn("{skill_names}", prompt)
            self.assertTrue(prompt.endswith("Preserve all scientific content.\n"))
            template.write_text("Missing placeholder")
            with self.assertRaisesRegex(ValueError, "placeholder"):
                explicit_prompt(self.request, self.task)

    def test_prepare_records_exact_prompts_and_rejects_drift(self):
        before = self.task.task_document.read_bytes()
        prepare_prompt_artifacts(self.request, self.task, self.root)
        folder = self.root / "prompt"
        prompt = explicit_prompt(self.request, self.task, run_dir=self.root)
        self.assertEqual((folder / "effective.md").read_text(), prompt)
        self.assertEqual((folder / "original.md").read_text(), "Preserve all scientific content.\n")
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest["harness"], "openhands")
        self.assertEqual(manifest["variant"], "reference")
        self.assertEqual(manifest["skill_names"], ["pdf"])
        self.assertEqual(self.task.task_document.read_bytes(), before)
        prepare_prompt_artifacts(self.request, self.task, self.root)
        (folder / "effective.md").write_text("Unexpected second prompt")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            explicit_prompt(self.request, self.task, run_dir=self.root)

    def test_disabled_or_no_skill_writes_no_prompt_artifacts(self):
        for request in (replace(self.request, explicit_skill_read=False),
                        replace(self.request, skill_mode="no-skill")):
            prepare_prompt_artifacts(request, self.task, self.root)
            self.assertFalse((self.root / "prompt").exists())

    def test_reference_rejects_mismatched_or_ambiguous_skill_names(self):
        path = self.task.skills_dir / "pdf/SKILL.md"
        for declaration in ("name: another-name", "name: >\n  pdf", "name: pdf\nname: another-name"):
            with self.subTest(declaration=declaration):
                path.write_text("---\n" + declaration + "\n---\n" + self.body)
                with self.assertRaisesRegex(ValueError, "name"):
                    explicit_prompt(self.request, self.task)

    def test_quoted_skill_names_and_inline_comments_match_loading_name(self):
        path = self.task.skills_dir / "pdf/SKILL.md"
        for declaration in ('name: "pdf" # toolkit', "name: 'pdf'", "name: pdf # toolkit"):
            with self.subTest(declaration=declaration):
                path.write_text("---\n" + declaration + "\n---\n" + self.body)
                self.assertIn("Skills: `pdf`.", explicit_prompt(self.request, self.task))

    def test_structured_benchflow_task_is_not_flattened_into_an_override(self):
        for heading in ("## Prompt", "## Role: coder", "## Scene: review", "## User-persona"):
            with self.subTest(heading=heading):
                self.task.task_document.write_text(heading + "\nPreserve original structure.")
                with self.assertRaisesRegex(ValueError, "single plain task body"):
                    explicit_prompt(self.request, self.task)

    def audit(self, events):
        path = self.root / "events.jsonl"
        path.write_text("".join(json.dumps({"event_id": f"T{index:06}",
            "source": {"path": "trace.jsonl", "line": index}, "event": event}) + "\n"
            for index, event in enumerate(events, 1)))
        return audit_skill_reads(self.task, path)

    def test_full_observation_matches_source_and_extra_invocations_remain_visible(self):
        result = self.audit([{"type": "tool_call", "status": "completed", "content": [
            {"text": "[skill: pdf]\nThis skill is located at `/skills/pdf`\n" + self.body},
            {"text": "[skill: builtin]\nThis skill is located at `/builtin`\nOther instructions"}]}])
        self.assertTrue(result["all_expected_bodies_observed"])
        observation = result["expected"][0]["observations"][0]
        self.assertEqual(observation["reported_locations"], ["/skills/pdf"])
        self.assertEqual(result["extra_invocations"][0]["name"], "builtin")

    def test_prompt_names_arguments_and_incomplete_calls_do_not_prove_reading(self):
        result = self.audit([
            {"type": "user_message", "text": self.body},
            {"type": "tool_call", "status": "completed", "rawInput": self.body},
            {"type": "tool_call", "status": "pending", "content": [{"text": self.body}]},
            {"type": "tool_call", "status": "completed", "content": [{"text": "[skill: pdf]"}]},
        ])
        self.assertFalse(result["all_expected_bodies_observed"])
        self.assertFalse(result["expected"][0]["body_read_confirmed"])

    def test_changed_body_does_not_match(self):
        result = self.audit([{"type": "tool_call", "status": "completed", "rawOutput": self.body[:-5]}])
        self.assertFalse(result["all_expected_bodies_observed"])

    def add_sidecar(self, task=None):
        task = task or self.task
        directory = task.skills_dir / "pdf"
        write_sidecars(directory, [{"entry_id": "clarify-applicability",
            "target": {"relative_path": "SKILL.md", "scope": "substep", "quote": self.body,
                       "version": sha256_file(directory / "SKILL.md")},
            "when_to_use": "When editing an anonymized document.",
            "how_to_interpret": "Inspect the document's metadata as well as its visible pages."}])
        return (directory / SIDECAR_MARKDOWN).read_text()

    def test_separate_patch_prompt_preserves_original_skill_and_records_provenance(self):
        original_skill = (self.task.skills_dir / "pdf/SKILL.md").read_bytes()
        self.add_sidecar()
        prepare_prompt_artifacts(self.request, self.task, self.root)
        prompt = explicit_prompt(self.request, self.task, run_dir=self.root)
        self.assertTrue(prompt.startswith("Before solving the task, invoke and read these task-provided Skills: `pdf`.\n\n"))
        self.assertIn("separate `SKILL_PATCHES.md` file: `pdf`", prompt)
        self.assertIn("actual Skill location", prompt)
        self.assertTrue(prompt.endswith("Preserve all scientific content.\n"))
        manifest = json.loads((self.root / "prompt/manifest.json").read_text())
        self.assertEqual(manifest["variant"], "reference+patches")
        self.assertEqual(manifest["patches"][0]["relative_path"], "pdf/SKILL_PATCHES.md")
        self.assertEqual((self.task.skills_dir / "pdf/SKILL.md").read_bytes(), original_skill)
        with self.assertRaisesRegex(ValueError, "explicit_skill_read"):
            explicit_prompt(replace(self.request, explicit_skill_read=False), self.task)
        (self.task.skills_dir / "pdf/SKILL_PATCHES.md").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Sidecar Markdown"):
            explicit_prompt(self.request, self.task, run_dir=self.root)

    def patch_read_event(self, text="1\tPartial patch text"):
        return {"type": "tool_call", "status": "completed", "kind": "read",
                "content": [{"text": "Tool: file_editor\nResult:\nHere's the result of running `cat -n` on /skills/pdf/SKILL_PATCHES.md:\n" + text}]}

    def test_partial_patch_read_passes_without_matching_original(self):
        self.add_sidecar()
        result = self.audit([self.patch_read_event()])
        self.assertTrue(result["all_expected_patch_reads_observed"])
        self.assertEqual(result["patches"][0]["observations"][0]["method"], "successful_file_read")

    def test_failed_wrong_path_and_directory_listing_are_not_patch_reads(self):
        self.add_sidecar()
        for event in [
            self.patch_read_event("Permission denied"),
            self.patch_read_event("No such file or directory"),
            {**self.patch_read_event(), "status": "failed"},
            {**self.patch_read_event(), "kind": "search"},
            {"type": "tool_call", "status": "completed", "kind": "execute",
             "title": "$ ls /skills/pdf/SKILL_PATCHES.md", "rawOutput": "Tool: terminal\nResult: SKILL_PATCHES.md"},
            {"type": "tool_call", "status": "completed", "kind": "execute",
             "title": "$ echo cat /skills/pdf/SKILL_PATCHES.md", "rawOutput": "Tool: terminal\nResult: cat /skills/pdf/SKILL_PATCHES.md"},
        ]:
            with self.subTest(event=event):
                self.assertFalse(self.audit([event])["all_expected_patch_reads_observed"])
        event = self.patch_read_event()
        event["content"][0]["text"] = event["content"][0]["text"].replace('/skills/pdf/', '/other/pdf/')
        self.assertFalse(self.audit([event])["all_expected_patch_reads_observed"])

    def test_terminal_partial_read_and_later_command_failure(self):
        self.add_sidecar()
        for command in ["head -n 2 /skills/pdf/SKILL_PATCHES.md", "cat /skills/pdf/SKILL_PATCHES.md 2>/dev/null; find / -name SKILL_PATCHES.md"]:
            event = {"type": "tool_call", "status": "completed", "kind": "execute",
                     "title": "$ " + command, "rawOutput": "Tool: terminal\nResult: partial text\nExit code: " + ("1" if ";" in command else "0")}
            self.assertTrue(self.audit([event])["all_expected_patch_reads_observed"])

    def test_patch_must_appear_in_completed_tool_observation(self):
        patch_text = self.add_sidecar()
        events = [{"type": "tool_call", "status": "completed", "rawOutput": self.body},
                  {"type": "user_message", "text": patch_text},
                  {"type": "tool_call", "status": "completed", "rawInput": patch_text},
                  {"type": "tool_call", "status": "pending", "rawOutput": patch_text}]
        audit = self.audit(events)
        self.assertTrue(audit["all_expected_bodies_observed"])
        self.assertFalse(audit["all_expected_patch_bodies_observed"])
        self.assertFalse(audit["all_expected_instruction_bodies_observed"])
        check = check_patch_reads(self.request, self.task, self.root / "events.jsonl")
        self.assertEqual(check["status"], "unconfirmed")
        events.append(self.patch_read_event())
        audit = self.audit(events)
        self.assertTrue(audit["all_expected_instruction_bodies_observed"])
        self.assertEqual(audit["patches"][0]["observations"][0]["event_id"], "T000005")
        check = check_patch_reads(self.request, self.task, self.root / "events.jsonl")
        self.assertEqual(check["status"], "passed")
        foreign = check_patch_reads(replace(self.request, backend="fixture"), self.task, self.root / "events.jsonl")
        self.assertEqual(foreign["status"], "not_applicable")
        unknown_harness = check_patch_reads(replace(self.request, agent="another-agent"), self.task,
                                            self.root / "events.jsonl")
        self.assertEqual(unknown_harness["status"], "unconfirmed")

    def test_unrecognized_nested_sidecar_is_never_silently_ignored(self):
        nested = self.task.skills_dir / "pdf/nested"
        nested.mkdir()
        (nested / "SKILL.md").write_text("---\nname: nested\n---\n" + self.body)
        write_sidecars(nested, [{"entry_id": "nested-patch",
            "target": {"relative_path": "SKILL.md", "scope": "substep", "quote": self.body,
                       "version": sha256_file(nested / "SKILL.md")},
            "when_to_use": "When editing metadata.", "how_to_interpret": "Inspect every relevant field."}])
        with self.assertRaisesRegex(ValueError, "top-level Skill"):
            explicit_prompt(self.request, self.task)
        with self.assertRaisesRegex(ValueError, "top-level Skill"):
            check_patch_reads(self.request, self.task, None)

    def test_unicode_line_separators_inside_json_strings_are_not_record_boundaries(self):
        path = self.root / "events.jsonl"
        payload = {"event_id": "T000001", "event": {
            "type": "tool_call", "status": "completed", "rawOutput": "\u2028\u0085" + self.body}}
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertTrue(audit_skill_reads(self.task, path)["all_expected_bodies_observed"])

    def test_target_quote_inside_patch_is_not_a_separate_original_skill_read(self):
        patch_text = self.add_sidecar()
        audit = self.audit([self.patch_read_event(patch_text)])
        self.assertTrue(audit["all_expected_patch_bodies_observed"])
        self.assertFalse(audit["all_expected_bodies_observed"])
        self.assertFalse(audit["all_expected_instruction_bodies_observed"])

    def prompt_archive(self, name, *, patched):
        directory = self.root / name
        task_dir = directory / "input/task"
        skill_dir = task_dir / "environment/skills/pdf"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: pdf\n---\n" + self.body)
        task_document = task_dir / "task.md"
        task_document.write_text("Original benchmark task.\n")
        task = PreparedTask("fixture", "task", task_dir, task_document, skill_dir.parent,
                            directory / "manifest.json", "task-digest", "skill-digest")
        if patched:
            self.add_sidecar(task)
        task.file_hashes = {p.relative_to(task_dir).as_posix(): sha256_file(p)
                            for p in task_dir.rglob("*") if p.is_file()}
        task.task_digest = hash_manifest(task.file_hashes)
        request = replace(self.request, task_dir=task_dir)
        write_json(directory / "request.json", request)
        write_json(directory / "result.json", ExecutionResult(
            name, "fixture", "task", "completed", "start", "end", prepared=task,
            execution=BackendRun("completed", trajectory_complete=True),
            feedback=EvaluationFeedback("scored", metrics={"reward": 0}, passed=False)))
        prepare_prompt_artifacts(request, task, directory)
        prompt = explicit_prompt(request, task)
        context = SimpleNamespace(evidence=[
            EvidenceItem("Q0001", "task", "text:\n" + prompt + "\n\ntype:\nuser_message", "trace"),
            EvidenceItem("Q0002", "task", "Unmodified task snapshot", "task.md")])
        return directory, context

    def test_comparison_allows_only_verified_patch_prompt_and_no_other_task_change(self):
        baseline, before = self.prompt_archive("baseline", patched=False)
        candidate, after = self.prompt_archive("candidate", patched=True)
        self.assertTrue(task_messages_equal(before, after, baseline, candidate))
        original = after.evidence[1]
        after.evidence[1] = replace(original, text="Changed task snapshot")
        self.assertFalse(task_messages_equal(before, after, baseline, candidate))
        after.evidence[1] = original
        actual_message = after.evidence[0]
        after.evidence[0] = replace(actual_message, text=actual_message.text + "\nExtra hidden task")
        self.assertFalse(task_messages_equal(before, after, baseline, candidate))
        after.evidence[0] = actual_message
        (candidate / "prompt/effective.md").write_text("Forged prefix")
        self.assertFalse(task_messages_equal(before, after, baseline, candidate))
