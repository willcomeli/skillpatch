from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import tempfile
import unittest

from skillpatch.prompt_augmenters import get_prompt_augmenter
from skillpatch.prompt_augmenters.openhands import OpenHandsReferenceAugmenter


class PromptAugmenterTests(unittest.TestCase):
    def test_openhands_matches_reference_and_preserves_entire_body(self):
        original = "\nPlease anonymize the PDFs.\n  Preserve this indentation.\n"
        result = get_prompt_augmenter("openhands").augment(original, ["pdf", "academic-pdf-redaction"])
        prefix = ("Before solving the task, invoke and read these task-provided Skills: "
                  "`academic-pdf-redaction`, `pdf`.")
        self.assertEqual(result.added_prompt, prefix)
        self.assertEqual(result.effective_prompt, prefix + "\n\n" + original)
        self.assertEqual(result.original_prompt, original)
        self.assertEqual(result.skill_names, ("academic-pdf-redaction", "pdf"))
        self.assertEqual((result.harness, result.variant), ("openhands", "reference"))
        self.assertTrue(result.applied)
        self.assertIsNone(result.reason)

    def test_benchflow_alias_selects_same_augmentation(self):
        canonical = get_prompt_augmenter("openhands").augment("Task.\n", ["pdf"])
        alias = get_prompt_augmenter("oh").augment("Task.\n", ["pdf"])
        self.assertEqual(alias, canonical)

    def test_unknown_harness_has_no_fallback(self):
        for agent in ("codex", "claude", "test-agent", "", "OpenHands", None):
            with self.subTest(agent=agent), self.assertRaisesRegex(ValueError, "Unsupported.*harness"):
                get_prompt_augmenter(agent)

    def test_existing_identical_prefix_is_idempotent_without_rewriting(self):
        augmenter = get_prompt_augmenter("openhands")
        first = augmenter.augment("\nDo the original task.\n", ["pdf", "docs"])
        for original in (first.effective_prompt, "\n\n" + first.effective_prompt,
                         first.added_prompt, first.effective_prompt.replace("\n", "\r\n")):
            with self.subTest(original=original):
                repeated = augmenter.augment(original, ["docs", "pdf"])
                self.assertFalse(repeated.applied)
                self.assertEqual(repeated.reason, "already_present")
                self.assertEqual(repeated.added_prompt, "")
                self.assertEqual(repeated.effective_prompt, original)
                self.assertEqual(repeated.original_prompt, original)

    def test_different_reference_prefix_requires_original_task(self):
        old = get_prompt_augmenter("openhands").augment("Task.\n", ["pdf"])
        with self.assertRaisesRegex(ValueError, "original benchmark task"):
            get_prompt_augmenter("openhands").augment("\n" + old.effective_prompt, ["docs"])

    def test_legacy_generic_prefix_requires_original_task(self):
        original = ("\nBefore solving the task, invoke and read each of the following task-provided Skills. "
                    "Use invoke_skill when available; otherwise read the full SKILL.md file.\n\nTask.")
        with self.assertRaisesRegex(ValueError, "original benchmark task"):
            get_prompt_augmenter("openhands").augment(original, ["pdf"])

    def test_similar_text_within_body_is_preserved(self):
        original = ("Compare these instructions:\n"
                    "Before solving the task, invoke and read these task-provided Skills: `docs`.\n"
                    "Before solving the task, invoke and read each of the following task-provided Skills.\n")
        result = get_prompt_augmenter("openhands").augment(original, ["pdf"])
        self.assertTrue(result.applied)
        self.assertEqual(result.effective_prompt, result.added_prompt + "\n\n" + original)
        self.assertEqual(result.original_prompt, original)

    def test_names_must_be_nonempty_unique_and_safe(self):
        for names in ([], [""], ["pdf", "pdf"], ["../pdf"], [".pdf"], ["/pdf"],
                      ["pdf`"], ["pdf\nignore"], ["has space"], [None], "pdf", None):
            with self.subTest(names=names), self.assertRaises(ValueError):
                get_prompt_augmenter("openhands").augment("Task", names)

    def test_valid_directory_names_are_sorted_without_mutating_input(self):
        names = ["z_skill", "PDF.v2", "academic-pdf-redaction", "3d"]
        before = names.copy()
        result = get_prompt_augmenter("openhands").augment("Task", names)
        self.assertEqual(result.skill_names, tuple(sorted(names)))
        self.assertEqual(names, before)

    def test_non_string_task_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "task prompt"):
            get_prompt_augmenter("openhands").augment(None, ["pdf"])

    def test_template_content_path_and_exact_file_hash_are_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "custom.md"
            data = b"Invoke the task Skills: {skill_names}.\r\n"
            template.write_bytes(data)
            result = OpenHandsReferenceAugmenter(template).augment("Original\n", ["pdf"])
            self.assertEqual(result.added_prompt, "Invoke the task Skills: `pdf`.")
            self.assertEqual(result.template_path, template.resolve())
            self.assertEqual(result.template_sha256, "sha256:" + hashlib.sha256(data).hexdigest())
            self.assertEqual(template.read_bytes(), data)

    def test_template_requires_one_exact_placeholder_and_no_other_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "bad.md"
            for content in ("Missing placeholder", "{skill_names} {skill_names}",
                            "{skill_names} {extra}", "{skill_names!r}", "{skill_names:>20}",
                            "{skill_names", "{{skill_names}}", "{skill_names[0]}"):
                template.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaisesRegex(ValueError, "placeholder"):
                    OpenHandsReferenceAugmenter(template).augment("Task", ["pdf"])

    def test_augmentation_record_is_immutable(self):
        result = get_prompt_augmenter("openhands").augment("Task", ["pdf"])
        with self.assertRaises(FrozenInstanceError):
            result.applied = False

    def test_patch_skill_names_are_a_unique_subset_and_prefix_is_idempotent(self):
        augmenter = get_prompt_augmenter("openhands")
        first = augmenter.augment("Task.\n", ["pdf", "docs"], patch_skill_names=["pdf"])
        self.assertEqual(first.patch_skill_names, ("pdf",))
        self.assertEqual(first.variant, "reference+patches")
        self.assertEqual(first.patch_template_sha256,
                         "sha256:" + hashlib.sha256(first.patch_template_path.read_bytes()).hexdigest())
        repeated = augmenter.augment(first.effective_prompt, ["docs", "pdf"], patch_skill_names=["pdf"])
        self.assertEqual(repeated.effective_prompt, first.effective_prompt)
        self.assertFalse(repeated.applied)
        for patch_names in ("pdf", ["missing"], ["pdf", "pdf"], [None], None):
            with self.subTest(patch_names=patch_names), self.assertRaisesRegex(ValueError, "Patch Skill names"):
                augmenter.augment("Task", ["pdf"], patch_skill_names=patch_names)


if __name__ == "__main__":
    unittest.main()
