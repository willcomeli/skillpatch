"""Original-object binding and reversible presentation, without model calls."""

import copy
from functools import partial
import tempfile
import unittest
from pathlib import Path

from skillpatch.models import SkillTarget
from skillpatch.patch_models import SkillPatch
from skillpatch.patch_set_rendering import map_rendered_quote, render_patch_set as default_render_patch_set
from skillpatch.patch_targets import bind_target as default_bind_target, locate_skill_root
from skillpatch.patches import render_patch as default_render_patch, tree_hashes
from skillpatch.storage import hash_manifest, sha256_file


SKILL = ('---\nname: workbook\ndescription: "Workbook operations"\n---\n'
         '# Workbook\nUse formulas carefully.\n\n## Workflow\n'
         '4. Save the workbook.\n'
         '5. Recalculate existing formulas.\n'
         '   Cached values must be refreshed.\n'
         '   ```sh\n   tool recalculate\n   # Fake heading\n   99. Fake step\n   ```\n'
         '6. Verify the output.\n   Check the reported errors.\n\n'
         '## Formatting\nRetain the requested styles.\n')


# Keep every historical byte/provenance regression exercised explicitly. New
# standalone behavior is covered separately in test_patch_sidecars.py.
bind_target = partial(default_bind_target, presentation_mode='inline_v1')
render_patch = partial(default_render_patch, presentation_mode='inline_v1')
render_patch_set = partial(default_render_patch_set, presentation_mode='inline_v1')


class PatchTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / 'original'
        self.original.mkdir()
        (self.original / 'SKILL.md').write_text(SKILL, encoding='utf-8')
        (self.original / 'scripts').mkdir()
        (self.original / 'scripts/recalculate.py').write_bytes(b'original implementation\n')
        (self.original / 'references').mkdir()
        (self.original / 'references/notes.md').write_text('# Notes\nAdditional instruction.\n')
        self.before = tree_hashes(self.original)

    def bind(self, quote='5. Recalculate existing formulas.', **kwargs):
        return bind_target(self.original, kwargs.get('skill_key', 'publisher/workbook'),
                           kwargs.get('relative_path', 'SKILL.md'), kwargs.get('scope', 'substep'), quote)

    def entry(self, quote='5. Recalculate existing formulas.', entry_id='P-original-r0', **kwargs):
        return {'entry_id': entry_id, 'target': self.bind(quote, **kwargs),
                'when_to_use': 'When a formula depends on a modified input.',
                'how_to_interpret': 'Retain the expression and refresh its cached result.'}

    def old_patch(self, entry):
        target = entry['target']
        path = self.original / target['relative_path']
        return SkillPatch(entry['entry_id'], 'source-run', 'sha256:source', 'C000001', 'under_use', 0,
                          SkillTarget(target['scope'], 'workbook', 'S000001', target['source_quote'],
                                      target['relative_path'], sha256_file(path), str(path)),
                          entry['when_to_use'], entry['how_to_interpret'])

    def test_different_quotes_in_same_numbered_step_share_identity(self):
        first = self.bind()
        second = self.bind('Cached values must be refreshed.')
        self.assertEqual(first['target_id'], second['target_id'])
        self.assertEqual(first['start'], second['start'])
        self.assertNotEqual(first['insertion_offset'], second['insertion_offset'])
        self.assertEqual(SKILL[first['start']:first['end']],
                         SKILL[SKILL.index('5. Recalculate'):SKILL.index('6. Verify')])

    def test_step_heading_whole_and_explicit_id_are_distinct(self):
        targets = [self.bind(), self.bind('Check the reported errors.'),
                   self.bind('Retain the requested styles.'),
                   self.bind(scope='whole_skill'), self.bind(skill_key='other/workbook')]
        self.assertEqual(len({target['target_id'] for target in targets}), 5)

    def test_resource_change_creates_new_original_version_identity(self):
        before = self.bind()
        (self.original / 'scripts/recalculate.py').write_bytes(b'changed implementation\n')
        after = self.bind()
        self.assertNotEqual(before['base_digest'], after['base_digest'])
        self.assertNotEqual(before['target_id'], after['target_id'])

    def test_missing_and_duplicate_quotes_are_not_guessed(self):
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.bind('not in this document')
        path = self.original / 'SKILL.md'
        path.write_text(SKILL + '\n5. Recalculate existing formulas.\n')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.bind()

    def test_quote_crossing_sibling_objects_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            self.bind('Save the workbook.\n5. Recalculate existing formulas.')

    def test_nested_numbered_object_has_own_identity_and_crossing_is_rejected(self):
        text = '# Workflow\n1. Parent condition.\n   1. Child condition.\n      Child detail.\n2. Next.\n'
        (self.original / 'SKILL.md').write_text(text)
        parent = self.bind('Parent condition.')
        child = self.bind('Child detail.')
        self.assertNotEqual(parent['target_id'], child['target_id'])
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            self.bind('Parent condition.\n   1. Child condition.')

    def test_code_quotes_keep_their_owning_step_identity_and_insert_before_fence(self):
        bound = self.bind()
        self.assertEqual(SKILL[bound['end']:].splitlines()[0], '6. Verify the output.')
        for quote in ('# Fake heading', '99. Fake step', 'tool recalculate'):
            with self.subTest(quote=quote):
                code = self.bind(quote)
                self.assertEqual(code['target_id'], bound['target_id'])
                self.assertEqual((code['start'], code['end']), (bound['start'], bound['end']))
                self.assertEqual(code['source_quote'], quote)
                self.assertEqual(code['insertion_offset'], SKILL.index('   ```sh'))

    def test_existing_plain_text_target_ids_remain_stable(self):
        expected = {
            '5. Recalculate existing formulas.':
                'T5e8673ed51bda30dd73b5df4da562ad778476f1eb44cd0203d692ebcab332a68',
            'Cached values must be refreshed.':
                'T5e8673ed51bda30dd73b5df4da562ad778476f1eb44cd0203d692ebcab332a68',
            'Check the reported errors.':
                'T9f38e98aabad134465ffb1aac78c416814f6f127198d895dc0c93e32fa8c7cd3',
            'Retain the requested styles.':
                'Tfb87bee9fb17765f8c5bf10b068bf40c2febf881232d39b6e6a94e5f688c86c8',
        }
        for quote, target_id in expected.items():
            with self.subTest(quote=quote):
                self.assertEqual(self.bind(quote)['target_id'], target_id)

    def test_code_quote_belongs_to_the_enclosing_heading_when_no_numbered_step_exists(self):
        text = ('# Workflow\nUse this procedure.\n\n## Recalculation\nRefresh existing formulas.\n'
                '```python\n# Fake heading\n99. Fake step\n|expression|\n```\n'
                '## Verification\nCheck the result.\n')
        (self.original / 'SKILL.md').write_bytes(text.encode())
        owner = self.bind('Refresh existing formulas.')
        self.assertEqual(owner['unit_kind'], 'heading')
        self.assertEqual(owner['label'], 'Recalculation')
        self.assertEqual(owner['end'], text.index('## Verification'))
        for quote in ('# Fake heading', '99. Fake step', '|expression|'):
            with self.subTest(quote=quote):
                code = self.bind(quote)
                self.assertEqual(code['target_id'], owner['target_id'])
                self.assertEqual(code['insertion_offset'], text.index('```python'))

    def test_code_supplement_is_identical_between_stages_and_preserves_original_bytes(self):
        code_block = '   ```sh\n   tool recalculate\n   # Fake heading\n   99. Fake step\n   ```\n'
        for newline in ('\n', '\r\n'):
            for quote in ('tool recalculate', code_block):
                with self.subTest(newline=newline, quote=quote):
                    text = SKILL.replace('\n', newline)
                    (self.original / 'SKILL.md').write_bytes(text.encode())
                    before = tree_hashes(self.original)
                    entry = self.entry(quote)
                    patch = self.old_patch(entry)
                    self.assertEqual(patch.target.quote, quote.replace('\n', newline))
                    self.assertEqual(patch.target.version, sha256_file(self.original / 'SKILL.md'))
                    stage2 = self.root / f'stage2-{len(list(self.root.iterdir()))}'
                    stage3 = self.root / f'stage3-{len(list(self.root.iterdir()))}'
                    render_patch(self.original, patch, stage2)
                    result = render_patch_set(self.original, [entry], stage3)
                    self.assertEqual(tree_hashes(stage2), tree_hashes(stage3))
                    rendered = (stage3 / 'SKILL.md').read_bytes().decode()
                    offset = text.index('   ```sh')
                    self.assertTrue(rendered.startswith(text[:offset] + newline + '<!-- skillpatch:'))
                    self.assertTrue(rendered.endswith(text[offset:]))
                    self.assertIn(code_block.replace('\n', newline), rendered)
                    if newline == '\r\n':
                        self.assertNotIn('\n', rendered.replace('\r\n', ''))
                    original_spans = ''.join(
                        rendered[span['rendered_start']:span['rendered_end']]
                        for span in result['mappings']['SKILL.md']['spans']
                        if span['origin'] == 'original')
                    self.assertEqual(original_spans.encode(), text.encode())
                    provenance = map_rendered_quote(stage3, result, 'SKILL.md', patch.target.quote)
                    self.assertEqual(provenance['origin'], 'original')
                    self.assertEqual(provenance['original_quote'], patch.target.quote)
                    self.assertEqual(self.bind(provenance['original_quote'])['target_id'],
                                     entry['target']['target_id'])
                    self.assertEqual(tree_hashes(self.original), before)

    def test_multiple_code_supplements_share_one_insertion_point_and_keep_input_order(self):
        first = self.entry('99. Fake step', entry_id='P-z')
        second = self.entry('tool recalculate', entry_id='P-a')
        self.assertEqual(first['target']['target_id'], second['target']['target_id'])
        self.assertEqual(first['target']['insertion_offset'], second['target']['insertion_offset'])
        output = self.root / 'multiple-code'
        render_patch_set(self.original, [first, second], output)
        rendered = (output / 'SKILL.md').read_bytes().decode()
        self.assertLess(rendered.index('<!-- skillpatch:P-z -->'), rendered.index('<!-- skillpatch:P-a -->'))
        self.assertLess(rendered.index('<!-- /skillpatch:P-a -->'), rendered.index('   ```sh'))
        self.assertTrue(rendered.endswith(SKILL[SKILL.index('   ```sh'):]))
        self.assertEqual(tree_hashes(self.original), self.before)

    def test_fence_matching_ignores_short_other_marker_and_nonempty_closing_lines(self):
        for marker, other in (('`', '~'), ('~', '`')):
            with self.subTest(marker=marker):
                opening = marker * 4 + 'python'
                text = ('# Workflow\n1. Recalculate.\n'
                        f'   {opening}\n   # Fake heading\n   99. Fake step\n'
                        f'   |expression|\n   {marker * 3}\n   {other * 5}\n'
                        f'   {marker * 4} trailing text\n   target_unique()\n'
                        f'   {marker * 5}\n2. Next.\n')
                (self.original / 'SKILL.md').write_bytes(text.encode())
                owner = self.bind('Recalculate.')
                self.assertEqual(owner['end'], text.index('2. Next.'))
                for quote in ('# Fake heading', '99. Fake step', '|expression|', 'target_unique()'):
                    entry = self.entry(quote)
                    self.assertEqual(entry['target']['target_id'], owner['target_id'])
                    self.assertEqual(entry['target']['insertion_offset'], text.index('   ' + opening))
                    stage2 = self.root / f'code-marker-stage2-{len(list(self.root.iterdir()))}'
                    stage3 = self.root / f'code-marker-stage3-{len(list(self.root.iterdir()))}'
                    render_patch(self.original, self.old_patch(entry), stage2)
                    render_patch_set(self.original, [entry], stage3)
                    self.assertEqual(tree_hashes(stage2), tree_hashes(stage3))
                    self.assertTrue((stage3 / 'SKILL.md').read_text().endswith(text[text.index('   ' + opening):]))

    def test_quotes_crossing_code_and_prose_or_multiple_code_blocks_are_rejected(self):
        text = ('# Workflow\n1. Recalculate.\n   Before code.\n'
                '   ```sh\n   first_command\n   ```\n'
                '   Between code.\n'
                '   ~~~sh\n   second_command\n   ~~~\n'
                '   After code.\n2. Next.\n')
        (self.original / 'SKILL.md').write_bytes(text.encode())
        quotes = (
            'Before code.\n   ```sh\n   first_command',
            'first_command\n   ```\n   Between code.',
            'first_command\n   ```\n   Between code.\n   ~~~sh\n   second_command',
        )
        for index, quote in enumerate(quotes):
            with self.subTest(quote=quote):
                with self.assertRaisesRegex(ValueError, 'fence|code'):
                    self.bind(quote)
                entry = self.entry('Recalculate.')
                entry['target']['source_quote'] = quote
                with self.assertRaisesRegex(ValueError, 'fence|code'):
                    render_patch(self.original, self.old_patch(entry), self.root / f'crossing-{index}')

    def test_unclosed_code_target_is_rejected_by_both_rendering_paths(self):
        text = '# Workflow\n1. Recalculate.\n   ````python\n   target_unique()\n   ```\n'
        (self.original / 'SKILL.md').write_bytes(text.encode())
        with self.assertRaisesRegex(ValueError, 'unterminated|unclosed'):
            self.bind('target_unique()')
        target = SkillTarget('substep', 'workbook', 'S000001', 'target_unique()',
                             'SKILL.md', sha256_file(self.original / 'SKILL.md'),
                             str(self.original / 'SKILL.md'))
        patch = SkillPatch('P-unclosed-r0', 'source-run', 'sha256:source', 'C000001',
                           'under_use', 0, target, 'When needed.', 'Retain the code.')
        with self.assertRaisesRegex(ValueError, 'unterminated|unclosed'):
            render_patch(self.original, patch, self.root / 'unclosed')

    def test_unterminated_fences_and_frontmatter_are_not_bound_as_steps(self):
        with self.assertRaisesRegex(ValueError, 'frontmatter'):
            self.bind('description: "Workbook operations"')
        (self.original / 'SKILL.md').write_text(SKILL + '\n~~~python\nprint(1)\n')
        with self.assertRaisesRegex(ValueError, 'unterminated'):
            self.bind()

    def test_no_structural_boundary_or_table_quote_is_rejected(self):
        (self.original / 'SKILL.md').write_text('Plain text without a structural object.\n')
        with self.assertRaisesRegex(ValueError, 'boundary'):
            self.bind('Plain text')
        (self.original / 'SKILL.md').write_text('# Data\n| Field | Value |\n| --- | --- |\n')
        with self.assertRaisesRegex(ValueError, 'table'):
            self.bind('Field')

    def test_nearest_skill_root_and_canonical_paths(self):
        self.assertEqual(locate_skill_root(self.root, 'original/scripts/recalculate.py'),
                         (self.original, 'original'))
        self.assertEqual(locate_skill_root(self.original, 'SKILL.md'), (self.original, ''))
        nested = self.original / 'references/nested'
        nested.mkdir()
        (nested / 'SKILL.md').write_text('# Nested\nA different Skill.\n')
        self.assertEqual(locate_skill_root(self.original, 'references/nested/SKILL.md'),
                         (nested, 'references/nested'))
        with self.assertRaisesRegex(ValueError, 'nested Skill'):
            self.bind('A different Skill.', relative_path='references/nested/SKILL.md')
        for path in ('../original/SKILL.md', '/SKILL.md', './SKILL.md', 'scripts//file', 'scripts\\file'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                locate_skill_root(self.original, path)

    def test_links_are_rejected_and_binding_is_read_only(self):
        self.bind()
        self.assertEqual(tree_hashes(self.original), self.before)
        (self.original / 'references/linked').symlink_to(self.original / 'scripts', target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            locate_skill_root(self.original, 'references/linked/recalculate.py')
        with self.assertRaisesRegex(ValueError, 'links'):
            self.bind()

    def test_single_entry_is_byte_identical_to_stage2_including_whole_skill_and_crlf(self):
        for index, (scope, newline) in enumerate((('substep', '\n'), ('whole_skill', '\n'),
                                                 ('substep', '\r\n'), ('whole_skill', '\r\n'))):
            with self.subTest(scope=scope, newline=newline):
                (self.original / 'SKILL.md').write_bytes(SKILL.replace('\n', newline).encode())
                entry = self.entry(scope=scope)
                expected = self.root / f'old-{index}'
                actual = self.root / f'new-{index}'
                render_patch(self.original, self.old_patch(entry), expected)
                result = render_patch_set(self.original, [entry], actual)
                self.assertEqual(tree_hashes(expected), tree_hashes(actual))
                self.assertEqual(result['digest'], hash_manifest(tree_hashes(actual)))
                self.assertEqual(result['selection_description_updated'], scope == 'whole_skill')

    def test_multiple_entries_keep_order_and_resources_and_original_bytes(self):
        one = self.entry(entry_id='P-z')
        two = self.entry(entry_id='P-a')
        two['when_to_use'] = 'Another specific condition.'
        output = self.root / 'rendered'
        result = render_patch_set(self.original, [one, two], output)
        rendered = (output / 'SKILL.md').read_text()
        self.assertLess(rendered.index('<!-- skillpatch:P-z -->'), rendered.index('<!-- skillpatch:P-a -->'))
        self.assertEqual(tree_hashes(self.original), self.before)
        self.assertEqual((output / 'scripts/recalculate.py').read_bytes(), b'original implementation\n')
        for relative, mapping in result['mappings'].items():
            original_text = (self.original / relative).read_bytes().decode()
            rendered_text = (output / relative).read_bytes().decode()
            for span in mapping['spans']:
                if span['origin'] == 'original':
                    self.assertEqual(original_text[span['original_start']:span['original_end']],
                                     rendered_text[span['rendered_start']:span['rendered_end']])

    def test_reference_target_preserves_other_files(self):
        entry = self.entry('Additional instruction.', relative_path='references/notes.md')
        output = self.root / 'rendered'
        render_patch_set(self.original, [entry], output)
        self.assertEqual((output / 'SKILL.md').read_bytes(), (self.original / 'SKILL.md').read_bytes())
        self.assertIn('### How to interpret', (output / 'references/notes.md').read_text())

    def test_tampered_binding_or_duplicate_id_cannot_render(self):
        entry = self.entry()
        changed = copy.deepcopy(entry)
        changed['target']['insertion_offset'] += 1
        with self.assertRaisesRegex(ValueError, 'tampered'):
            render_patch_set(self.original, [changed], self.root / 'bad')
        self.assertFalse((self.root / 'bad').exists())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            render_patch_set(self.original, [entry, entry], self.root / 'bad')
        (self.original / 'scripts/recalculate.py').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'tampered'):
            render_patch_set(self.original, [entry], self.root / 'bad')

    def test_patch_reserved_markers_and_unsafe_outputs_are_rejected(self):
        entry = self.entry()
        for body in ('<!-- skillpatch:injected -->', '<!-- /skillpatch:injected -->', 'bad\x00text'):
            changed = {**entry, 'when_to_use': body}
            with self.subTest(body=body), self.assertRaises(ValueError):
                render_patch_set(self.original, [changed], self.root / 'bad')
        with self.assertRaisesRegex(ValueError, 'outside'):
            render_patch_set(self.original, [entry], self.original / 'copy')

    def test_deployed_quote_maps_to_original_or_patch_without_rewriting_source(self):
        entry = self.entry()
        output = self.root / 'rendered'
        result = render_patch_set(self.original, [entry], output)
        source = map_rendered_quote(output, result, 'SKILL.md', 'Cached values must be refreshed.')
        self.assertEqual(source['origin'], 'original')
        self.assertEqual(SKILL[source['original_start']:source['original_end']], source['original_quote'])
        self.assertEqual(self.bind(source['original_quote'])['target_id'], entry['target']['target_id'])
        supplement = map_rendered_quote(output, result, 'SKILL.md', entry['how_to_interpret'])
        self.assertEqual(supplement['origin'], 'patch')
        self.assertEqual(supplement['entry_id'], entry['entry_id'])
        self.assertEqual(supplement['target'], entry['target'])
        untouched = map_rendered_quote(output, result, 'references/notes.md', 'Additional instruction.')
        self.assertEqual(untouched['origin'], 'original')
        self.assertEqual(tree_hashes(self.original), self.before)

    def test_crossing_or_stale_rendered_provenance_rejected(self):
        entry = self.entry()
        output = self.root / 'rendered'
        result = render_patch_set(self.original, [entry], output)
        quote = '<!-- /skillpatch:P-original-r0 -->\n\n5. Recalculate'
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            map_rendered_quote(output, result, 'SKILL.md', quote)
        (output / 'scripts/recalculate.py').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'differs'):
            map_rendered_quote(output, result, 'SKILL.md', entry['how_to_interpret'])

    def test_single_whole_selection_metadata_maps_to_its_supplement(self):
        entry = self.entry(scope='whole_skill')
        output = self.root / 'rendered'
        result = render_patch_set(self.original, [entry], output)
        mapped = map_rendered_quote(output, result, 'SKILL.md', 'Workbook operations When to use:')
        self.assertEqual(mapped['origin'], 'selection_metadata')
        self.assertEqual(mapped['target']['scope'], 'whole_skill')

    def test_combined_selection_metadata_cannot_guess_which_supplement(self):
        one = self.entry(scope='whole_skill', entry_id='P-one')
        two = self.entry(scope='whole_skill', entry_id='P-two')
        two['when_to_use'] = 'A different selection condition.'
        output = self.root / 'rendered'
        result = render_patch_set(self.original, [one, two], output)
        with self.assertRaisesRegex(ValueError, 'composed selection'):
            map_rendered_quote(output, result, 'SKILL.md', 'Workbook operations When to use:')


if __name__ == '__main__':
    unittest.main()
