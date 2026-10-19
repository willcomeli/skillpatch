"""Consolidation proposals inspect every support without rewriting original Skills."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skillpatch.context import refresh_input_digest
from skillpatch.discovery_validation import validate_report
from skillpatch.evidence_views import EvidenceViews
from skillpatch.model_client import DiagnosisConfig, load_diagnosis_config
from skillpatch.patches import tree_hashes
from skillpatch.stages.consolidation_model import propose_consolidation, validate_proposal
from skillpatch.storage import hash_manifest, jsonable
from test_discovery_validation import make_context, make_payload
from test_stage2_evidence import TurnClient


def proposal(action="merge"):
    return {"action": action, "reason": "Preserve the original step applicability and exceptions while combining supplements.",
            "when_to_use": "When uniqueness is required." if action == "merge" else None,
            "how_to_interpret": "Preserve duplicates if the task requires them." if action == "merge" else None}


class ConsolidationModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        context = make_context()
        self.source = SimpleNamespace(context=context,
            conflict=validate_report(make_payload(), context).conflicts[0])
        self.entry = {"entry_id": "entry-original", "target": {"target_id": "table-original-step",
                      "skill_key": "table", "base_digest": "sha256:original-pack"},
                      "when_to_use": "When uniqueness is required.",
                      "how_to_interpret": "Preserve requested duplicates.", "supports": []}
        self.incoming = {**copy.deepcopy(self.entry), "entry_id": "entry-new"}
        self.config = DiagnosisConfig("fixture", "https://not-called.invalid", "FIXTURE_KEY",
            Path(__file__).resolve().parents[1] / "prompts/consolidation.md")
        self.calls = []

    def session(self, payload=None, *, unread=None, partial=None, fallback=False):
        def recorded(config, data, contexts, output, environment, **kwargs):
            self.calls.append({"data": data, "contexts": contexts, **kwargs})
            views = {name: EvidenceViews(copy.deepcopy(context), output / "collections" / name)
                     for name, context in contexts.items()}
            self.views = views
            if not fallback:
                for view in views.values():
                    view.observe([item for item in view.context.evidence if item.evidence_id != unread
                                  and (partial is None or item.evidence_id != partial[0])])
                    if partial is not None:
                        identifier, quote = partial
                        item = next(item for item in view.context.evidence if item.evidence_id == identifier)
                        view.call("read_evidence", {"evidence_id": identifier,
                                  "start_char": item.text.index(quote), "max_chars": len(quote)})
                    view.commit_visible()
            value = kwargs["budget_fallback"]("inspection limit") if fallback else copy.deepcopy(payload)
            kwargs["validate"](value, views)
            return value
        return recorded

    def run_proposal(self, payload=None, *, unread=None, partial=None, fallback=False, **kwargs):
        with patch("skillpatch.stages.consolidation_model.recorded_evidence_session",
                   self.session(payload or proposal(), unread=unread, partial=partial, fallback=fallback)):
            return propose_consolidation(self.source, [self.entry], self.incoming, self.config,
                                         self.root / "proposal", {}, **kwargs)

    def test_schema_rejects_rewrite_extra_fields_and_nonmerge_text(self):
        for value in [dict(proposal(), original_skill="rewrite"), dict(proposal(), action=[]),
                      dict(proposal(), reason=""), dict(proposal(), when_to_use=""),
                      dict(proposal(), how_to_interpret="<!-- /skillpatch:fake -->"),
                      dict(proposal("keep"), how_to_interpret="replacement")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_proposal(value)
        self.assertEqual(validate_proposal(proposal("keep"))["action"], "keep")

    def test_all_source_archives_are_namespaced_without_mutation(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        second.context.evidence[3] = replace(second.context.evidence[3], text="Second run removed duplicate rows.")
        second.conflict.behavior_evidence[0] = replace(second.conflict.behavior_evidence[0],
                                                       quote="removed duplicate rows.")
        refresh_input_digest(second.context)
        before = jsonable(self.source.context), jsonable(second.context)
        result = self.run_proposal(sources=[self.source, second], session_timeout_sec=19)
        self.assertEqual(result["action"], "merge")
        archive = self.calls[0]["contexts"]["source"]
        by_id = {item.evidence_id: item for item in archive.evidence}
        self.assertEqual(by_id["R0__T000002"].text, "Removed duplicate rows.")
        self.assertEqual(by_id["R1__T000002"].text, "Second run removed duplicate rows.")
        self.assertNotIn("R2__T000002", by_id)
        self.assertEqual((jsonable(self.source.context), jsonable(second.context)), before)
        metadata = json.loads((self.root / "proposal/source_map.json").read_text())
        self.assertEqual(metadata[1]["conflict"]["behavior_evidence"][0]["evidence_id"], "R1__T000002")
        self.assertEqual(metadata[1]["original_input_digest"], second.context.input_digest)
        self.assertEqual(self.calls[0]["session_timeout_sec"], 19)
        archived = json.loads((self.root / "proposal/proposal.json").read_text())
        self.assertFalse(archived["ready_for_activation"])
        self.assertEqual(archived["semantic_correctness"], "not_verified")

    def test_unread_uncited_later_behavior_does_not_block_merge(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        result = self.run_proposal(sources=[second], unread="R1__T000003")
        self.assertEqual(result["action"], "merge")
        self.assertNotIn("R1__T000003", self.views["source"].presented)

    def test_displayed_cited_passage_does_not_require_reading_the_entire_event(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        second.context.evidence[3] = replace(second.context.evidence[3],
            text="Earlier metadata. Removed duplicate rows. Other log output.")
        result = self.run_proposal(sources=[second], unread="R1__T000003",
                                  partial=("R1__T000002", "Removed duplicate rows."))
        self.assertEqual(result["action"], "merge")
        view = self.views["source"]
        item = next(item for item in view.context.evidence if item.evidence_id == "R1__T000002")
        self.assertFalse(view.fully_presented(item))

    def test_undisplayed_citation_from_any_support_still_blocks_merge(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        with self.assertRaisesRegex(ValueError, "R1__.*undisplayed evidence R1__T000002"):
            self.run_proposal(sources=[second], unread="R1__T000002")
        self.assertFalse((self.root / "proposal/proposal.json").exists())

    def test_each_source_completeness_is_checked_even_with_complete_incoming(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        second.context.trajectory_complete = False
        with self.assertRaisesRegex(ValueError, "R1__.*incomplete execution"):
            self.run_proposal(sources=[second])

    def test_original_skill_must_be_fully_read_not_only_target_quote(self):
        def partial(config, data, contexts, output, environment, **kwargs):
            view = EvidenceViews(copy.deepcopy(contexts["source"]), output / "source")
            view.observe([item for item in view.context.evidence if item.kind != "skill"])
            target = self.source.conflict.target
            item = next(item for item in view.context.evidence if item.evidence_id == "R0__" + target.skill_ref)
            start = item.text.index(target.quote)
            view.call("read_evidence", {"evidence_id": item.evidence_id, "start_char": start,
                                       "max_chars": len(target.quote)})
            view.commit_visible()
            kwargs["validate"](proposal(), {"source": view})
        with patch("skillpatch.stages.consolidation_model.recorded_evidence_session", partial):
            with self.assertRaisesRegex(ValueError, "complete original Skill"):
                propose_consolidation(self.source, [self.entry], self.incoming, self.config,
                                      self.root / "proposal", {})

    def test_cross_target_or_registered_original_version_cannot_be_merged(self):
        self.incoming["target"]["target_id"] = "different-object"
        with self.assertRaisesRegex(ValueError, "same target_id"):
            self.run_proposal()
        self.incoming["target"]["target_id"] = self.entry["target"]["target_id"]
        self.incoming["target"]["base_digest"] = "sha256:other-origin"
        with self.assertRaisesRegex(ValueError, "same target_id, skill_key and base_digest"):
            self.run_proposal()

    def test_registered_same_target_can_have_different_historical_deployments(self):
        second = copy.deepcopy(self.source)
        second.context.run_id = "second-run"
        second.conflict.target = replace(second.conflict.target, version="sha256:patched-deployment",
                                         relative_path="other-layout/table/SKILL.md", skill_id="deployed-table")
        second.context.evidence[1] = replace(second.context.evidence[1], version="sha256:patched-deployment",
                                             relative_path="other-layout/table/SKILL.md", skill_id="deployed-table")
        result = self.run_proposal(sources=[second])
        self.assertEqual(result["action"], "merge")
        mapping = json.loads((self.root / "proposal/source_map.json").read_text())
        self.assertEqual(mapping[1]["conflict"]["target"]["version"], "sha256:patched-deployment")

    def original_snapshot(self):
        original = self.root / "original"
        original.mkdir()
        (original / "SKILL.md").write_text("# Table\nKeep original global conditions.\n")
        (original / "steps.md").write_text("# Cleanup\nRemove duplicate records when uniqueness is required.\n")
        digest = hash_manifest(tree_hashes(original))
        for entry in (self.entry, self.incoming):
            entry["target"].update(base_digest=digest, relative_path="steps.md")
        return original

    def test_canonical_snapshot_is_exposed_separately_and_hash_bound(self):
        original = self.original_snapshot()
        before = tree_hashes(original)
        self.run_proposal(original_skill_dir=original)
        context = self.calls[0]["contexts"]["source"]
        canonical = [item for item in context.evidence if item.evidence_id.startswith("ORIGINAL__")]
        self.assertEqual({item.relative_path for item in canonical}, {"SKILL.md", "steps.md"})
        self.assertTrue(all(item.source_role == "canonical_original_skill" for item in canonical))
        self.assertEqual(tree_hashes(original), before)
        saved = json.loads((self.root / "proposal/original_skill.json").read_text())
        self.assertEqual(saved["base_digest"], self.entry["target"]["base_digest"])
        self.assertIn("ORIGINAL__S000001", self.calls[0]["priority_ids"]["source"])

    def test_unread_canonical_global_conditions_cannot_be_replaced_by_deployed_skill(self):
        original = self.original_snapshot()
        with self.assertRaisesRegex(ValueError, "complete canonical original"):
            self.run_proposal(original_skill_dir=original, unread="ORIGINAL__S000001")

    def test_changed_original_snapshot_is_rejected_before_model(self):
        original = self.original_snapshot()
        (original / "SKILL.md").write_text("changed global requirements")
        with self.assertRaisesRegex(ValueError, "registered base_digest"):
            self.run_proposal(original_skill_dir=original)
        self.assertEqual(self.calls, [])

    def test_budget_fallback_defers_without_reading_or_replacing_patch(self):
        result = self.run_proposal(fallback=True)
        self.assertEqual(result["action"], "defer")
        self.assertIsNone(result["when_to_use"])
        self.assertIsNone(result["how_to_interpret"])
        self.assertIn("inspection limit", result["reason"])

    def test_real_evidence_session_with_fake_model_never_activates(self):
        client = TurnClient([{"role": "assistant", "content": json.dumps(proposal())}])
        result = propose_consolidation(self.source, [self.entry], self.incoming, self.config,
                                      self.root / "real-session", {}, client=client)
        self.assertEqual(result["action"], "merge")
        self.assertEqual(len(client.calls), 1)
        text = json.dumps(client.calls[0][0], ensure_ascii=False)
        self.assertIn("R0__T000002", text)
        self.assertIn("original Skill is immutable", " ".join(client.calls[0][0][0]["content"].split()))
        self.assertFalse((self.root / "real-session/current.json").exists())

    def test_default_config_has_bounded_common_model_settings(self):
        config = load_diagnosis_config(Path(__file__).resolve().parents[1] / "configs/consolidation.toml")
        self.assertEqual((config.model, config.thinking), ("deepseek-flash", "disabled"))
        self.assertEqual((config.max_rounds, config.max_tool_calls, config.max_report_repairs), (50, 100, 2))


if __name__ == "__main__":
    unittest.main()
