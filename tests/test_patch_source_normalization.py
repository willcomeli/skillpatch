"""Stage 2 source binding for raw and normalized local evidence; no external calls."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skillpatch.context import refresh_input_digest
from skillpatch.discovery_validation import validate_report
from skillpatch.evidence_views import EvidenceViews
from skillpatch.executor import Executor
from skillpatch.models import DiscoveryInput, EvidenceItem, RunRequest
from skillpatch.patch_sources import load_patch_source, read_object
from skillpatch.storage import write_json
from test_discovery import compact_payload_fixture
from test_discovery_validation import make_payload
from test_stage2 import SKILL, TableAdapter, TableBackend


class EscapedTableAdapter(TableAdapter):
    def collect_evidence(self, run_dir, *, record_path=None):
        context = super().collect_evidence(run_dir, record_path=record_path)
        context.evidence = [
            replace(item, text='\x1b[31m' + item.text + '\x1b[0m',
                    observation={'source_detail': 'original'})
            if item.evidence_id in {'T000002', 'F000001'} else item
            for item in context.evidence
        ]
        refresh_input_digest(context)
        return context


class PatchSourceNormalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        task = self.root / 'table-task'
        (task / 'skills/table').mkdir(parents=True)
        (task / 'skills/table/SKILL.md').write_text(SKILL)
        (task / 'instructions.txt').write_text('Preserve duplicate records for this task.')
        self.adapter = EscapedTableAdapter()
        self.run_dir = self.root / 'source'
        request = RunRequest('tables', task, 'fixture-agent', 'fixture-model', (), backend='table-engine')
        Executor(self.adapter, TableBackend()).run(request, self.run_dir, {})
        self.diagnosis = self.run_dir / 'discovery/fixture'

    def archive(self, *, normalized=True):
        context = self.adapter.collect_evidence(self.run_dir)
        if normalized:
            EvidenceViews(context, self.diagnosis)
        payload = compact_payload_fixture(make_payload())
        payload['coverage_notes'] = ['终端转义字符已规范化，原始文本保留供核验。']
        report = validate_report(payload, context)
        write_json(self.diagnosis / 'input.json', context)
        write_json(self.diagnosis / 'report.json', report)
        write_json(self.diagnosis / 'result.json', {'status': 'completed', 'run_id': self.run_dir.name})
        return context

    def load(self):
        return load_patch_source(self.diagnosis, 'C000001', adapter=self.adapter)

    def tamper(self, identifier, change, *, rebind_digest=True):
        payload = read_object(self.diagnosis / 'input.json')
        item = next(item for item in payload['evidence'] if item['evidence_id'] == identifier)
        change(item)
        if rebind_digest:
            context = DiscoveryInput(**{**payload, 'evidence': [EvidenceItem(**item)
                                                               for item in payload['evidence']]})
            refresh_input_digest(context)
            payload['input_digest'] = context.input_digest
            report = read_object(self.diagnosis / 'report.json')
            report['input_digest'] = context.input_digest
            write_json(self.diagnosis / 'report.json', report)
        write_json(self.diagnosis / 'input.json', payload)

    def test_historical_raw_evidence_still_loads(self):
        context = self.archive(normalized=False)
        source = self.load()
        self.assertEqual(source.context.evidence, context.evidence)
        self.assertIn('\x1b', source.context.evidence[3].text)
        self.assertNotIn('text_view', source.context.evidence[3].observation)

    def test_normalized_trajectory_and_feedback_load_with_original_binding(self):
        context = self.archive()
        source = self.load()
        self.assertEqual(source.context.evidence, context.evidence)
        for identifier in ('T000002', 'F000001'):
            item = next(item for item in source.context.evidence if item.evidence_id == identifier)
            self.assertNotIn('\x1b', item.text)
            self.assertEqual(item.observation['text_view']['scheme'], 'terminal-escapes-v1')
            self.assertEqual(item.observation['source_detail'], 'original')
        self.assertEqual(source.conflict.conflict_id, 'C000001')

    def test_forged_view_provenance_is_rejected_even_with_rebound_input_digest(self):
        changes = {
            'unknown_scheme': lambda item: item['observation']['text_view'].update(scheme='terminal-escapes-v2'),
            'raw_hash': lambda item: item['observation']['text_view'].update(raw_text_sha256='sha256:fake'),
            'view_hash': lambda item: item['observation']['text_view'].update(view_text_sha256='sha256:fake'),
            'ranges': lambda item: item['observation']['text_view'].update(removed_raw_ranges=[[0, 1]]),
            'bool_offset': lambda item: item['observation']['text_view']['removed_raw_ranges'][0].__setitem__(0, False),
            'float_offset': lambda item: item['observation']['text_view']['removed_raw_ranges'][0].__setitem__(0, 0.0),
            'extra_view_metadata': lambda item: item['observation']['text_view'].update(extra='forged'),
            'missing_marker': lambda item: item['observation'].pop('text_view'),
            'changed_original_metadata': lambda item: item['observation'].update(source_detail='forged'),
            'source_path': lambda item: item.update(source_path='/unrelated/events.jsonl'),
            'version': lambda item: item.update(version='sha256:fake'),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.archive()
                self.tamper('T000002', change)
                with self.assertRaisesRegex(ValueError, 'Q/S/T/F evidence has changed'):
                    self.load()

    def test_real_content_changes_are_not_accepted_as_normalization(self):
        for identifier in ('T000002', 'F000001'):
            with self.subTest(evidence=identifier):
                self.archive()
                self.tamper(identifier, lambda item: item.update(text=item['text'][:-1]))
                with self.assertRaisesRegex(ValueError, 'Q/S/T/F evidence has changed'):
                    self.load()

    def test_normalization_marker_does_not_authorize_task_or_skill_changes(self):
        for identifier in ('Q000001', 'S000001'):
            with self.subTest(evidence=identifier):
                self.archive()
                self.tamper(identifier, lambda item: item['observation'].update(
                    text_view={'scheme': 'terminal-escapes-v1'}))
                with self.assertRaisesRegex(ValueError, 'Q/S/T/F evidence has changed'):
                    self.load()

    def test_input_digest_is_still_checked_before_normalization_compatibility(self):
        self.archive()
        self.tamper('T000002', lambda item: item['observation']['text_view'].update(
            scheme='forged'), rebind_digest=False)
        with self.assertRaisesRegex(ValueError, 'input digest does not match'):
            self.load()


if __name__ == '__main__':
    unittest.main()
