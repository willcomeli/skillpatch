"""Evidence-view regression tests; all sources are local engineering fixtures."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skillpatch.discovery_validation import validate_report
from skillpatch.evidence_views import EvidenceViews, normalize_evidence_item, normalize_terminal_text
from test_discovery_validation import make_context, make_payload


class EvidenceViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name)
        self.context = make_context()

    def test_terminal_wrappers_are_removed_without_losing_source_or_whitespace(self):
        raw = '    g=1\n\x1b[?2004l\x1b[?2004h    best=2\n'
        self.context.evidence[3] = replace(self.context.evidence[3], text=raw)
        skill = self.context.evidence[1]
        view = EvidenceViews(self.context, self.output)
        self.assertEqual(self.context.evidence[3].text, '    g=1\n    best=2\n')
        self.assertEqual(self.context.evidence[3].source_path, '/run/events.jsonl')
        self.assertEqual(self.context.evidence[3].source_line, 2)
        self.assertEqual(self.context.evidence[1], skill)
        saved = json.loads((self.output / 'raw_evidence.json').read_text())
        self.assertEqual(saved[3]['text'], raw)
        record = json.loads((self.output / 'normalization.json').read_text())['entries'][0]
        self.assertEqual(record['evidence_id'], 'T000002')
        self.assertEqual(record['removed_raw_ranges'], [[8, 16], [16, 24]])
        self.assertEqual(view.raw[3].text, raw)

    def test_pure_normalization_matches_archived_view_without_mutating_original(self):
        original = replace(self.context.evidence[3], text='\x1b[31mRemoved duplicate rows.\x1b[0m',
                           observation={'original_metadata': 'retained'})
        expected = normalize_evidence_item(original)
        self.context.evidence[3] = original
        EvidenceViews(self.context, self.output)
        self.assertEqual(self.context.evidence[3], expected)
        self.assertEqual(original.observation, {'original_metadata': 'retained'})
        self.assertEqual(expected.observation['original_metadata'], 'retained')
        entries = json.loads((self.output / 'normalization.json').read_text())['entries']
        self.assertEqual(entries, [expected.observation['text_view']])
        self.assertIs(normalize_evidence_item(expected), expected)
        self.assertIs(normalize_evidence_item(self.context.evidence[1]), self.context.evidence[1])

    def test_normalization_preserves_tabs_newlines_and_link_labels(self):
        self.assertEqual(normalize_terminal_text('\tA\r\n\x1b[31mB\x1b[0m'), '\tA\r\nB')
        self.assertEqual(normalize_terminal_text('\x1b]8;;https://example.invalid\x07label\x1b]8;;\x07'), 'label')

    def test_large_input_is_indexed_but_full_text_remains_readable(self):
        raw = 'x' * 100000 + '\nneedle with exact spaces\n' + 'y' * 100000
        self.context.evidence[3] = replace(self.context.evidence[3], text=raw)
        view = EvidenceViews(self.context, self.output)
        text = view.render(5000)
        self.assertLessEqual(len(text), 5000)
        self.assertIn('PAGED EVIDENCE VIEW', text)
        self.assertEqual(self.context.evidence[3].text, raw)
        result = view.call('search_evidence', {'query': 'needle', 'evidence_id': 'T000002', 'context_chars': 40})
        matched = json.loads(result['text'])['matches'][0]
        self.assertEqual(matched['evidence_id'], 'T000002')
        self.assertIn('needle with exact spaces', matched['text'])
        page = json.loads(view.call('read_evidence', {'evidence_id': 'T000002', 'start_char': 100001,
                                                    'max_chars': 24})['text'])
        self.assertEqual(page['text'], raw[100001:100025])
        self.assertTrue(page['has_more'])
        index = json.loads(view.call('read_evidence_index', {'offset': 3, 'limit': 1})['text'])
        self.assertEqual(index['items'][0]['evidence_id'], 'T000002')

    def test_exact_quote_in_unread_archive_is_not_accepted_until_presented(self):
        self.context.evidence[3] = replace(self.context.evidence[3], text='x' * 30000 + 'Removed duplicate rows.')
        view = EvidenceViews(self.context, self.output)
        view.render(5000)
        view.commit_visible()
        payload = make_payload()
        payload['coverage_notes'] = ['Paged evidence coverage has limits.']
        validate_report(payload, self.context)
        with self.assertRaisesRegex(ValueError, 'undisplayed'):
            view.validate_visibility(payload)
        view.call('search_evidence', {'query': 'Removed duplicate rows.', 'evidence_id': 'T000002'})
        view.commit_visible()
        view.validate_visibility(payload)
        payload.update(status='no_supported_conflict', conflicts=[])
        with self.assertRaisesRegex(ValueError, 'coverage is incomplete'):
            view.validate_visibility(payload)
        payload['status'] = 'insufficient_evidence'
        view.validate_visibility(payload)

    def test_no_fuzzy_quote_matching_and_no_arbitrary_resource_access(self):
        view = EvidenceViews(self.context, self.output)
        view.render(5000)
        payload = make_payload()
        payload['coverage_notes'] = ['Original evidence retained.']
        payload['conflicts'][0]['behavior_evidence'][0]['quote'] = 'Removed some duplicate rows.'
        with self.assertRaisesRegex(ValueError, 'exact substring'):
            validate_report(payload, self.context)
        for arguments in ({'evidence_id': '/etc/passwd'}, {'evidence_id': 'T000002', 'max_chars': 999999},
                          {'evidence_id': 'T000002', 'start_char': True},
                          {'evidence_id': 'T000002', 'shell': 'cat /etc/passwd'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                view.call('read_evidence', arguments)


if __name__ == '__main__':
    unittest.main()
