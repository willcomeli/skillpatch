"""Standalone delivery retains original bytes and original-object provenance."""

import copy
import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skillpatch.models import SkillTarget
from skillpatch.patch_models import SkillPatch
from skillpatch.patch_set_rendering import map_rendered_quote, render_patch_set
from skillpatch.patch_sidecars import (SIDECAR_MANIFEST, SIDECAR_MARKDOWN,
                                      load_sidecars, write_sidecars)
from skillpatch.patch_targets import bind_target
from skillpatch.patches import render_patch, tree_hashes
from skillpatch.storage import sha256_file


SKILL = ('---\nname: sample\ndescription: |\n  A multiline description.\n---\n'
         '# Workflow\n1. Read the input.\n2. Preserve the required result.\n'
         '   Inspect its type.\n\n## Output format\nReturn this JSON:\n'
         '```json\n{"result": 1}\n```\n\n## Pitfalls\n'
         '| Symptom | Fix |\n| --- | --- |\n| Mixed reasoning | Extract the final answer. |\n')


class PatchSidecarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pack = self.root / 'pack'
        self.skill = self.pack / 'nested/sample'
        self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_bytes(SKILL.encode())
        (self.skill / 'helper.py').write_bytes(b'original script\n')
        (self.skill / 'notes.md').write_text('# Notes\nPreserve the schema.\n')
        self.before = tree_hashes(self.pack)

    def entry(self, quote='Preserve the required result.', scope='substep', identifier='P-one'):
        return {'entry_id': identifier,
                'target': bind_target(self.skill, 'publisher/sample', 'SKILL.md', scope, quote),
                'when_to_use': 'When the task requires a particular output type.',
                'how_to_interpret': 'Keep the requested type and verify its final representation.'}

    def patch(self, entry):
        target = entry['target']
        path = self.skill / target['relative_path']
        return SkillPatch(entry['entry_id'], 'run', 'sha256:source', 'C000001', 'under_use', 0,
                          SkillTarget(target['scope'], 'sample', 'S000001', target['source_quote'],
                                      'nested/sample/' + target['relative_path'], sha256_file(path), str(path)),
                          entry['when_to_use'], entry['how_to_interpret'])

    def test_default_single_entry_stage2_and_stage3_are_identical_and_originals_unchanged(self):
        for index, (scope, newline) in enumerate((('substep', '\n'), ('whole_skill', '\n'),
                                                 ('substep', '\r\n'), ('whole_skill', '\r\n'))):
            with self.subTest(scope=scope, newline=newline):
                (self.skill / 'SKILL.md').write_bytes(SKILL.replace('\n', newline).encode())
                before = tree_hashes(self.skill)
                entry = self.entry(scope=scope)
                a, b = self.root / f'stage2-{index}', self.root / f'stage3-{index}'
                stage2 = render_patch(self.pack, self.patch(entry), a)
                stage3 = render_patch_set(self.skill, [entry], b)
                self.assertEqual(tree_hashes(a / 'nested/sample'), tree_hashes(b))
                self.assertEqual(tree_hashes(self.skill), before)
                for name, digest in before.items():
                    self.assertEqual(tree_hashes(b)[name], digest)
                self.assertEqual(stage2['presentation_mode'], 'sidecar_v1')
                self.assertEqual(stage3['presentation_mode'], 'sidecar_v1')
                self.assertFalse(stage2['selection_description_updated'])
                self.assertFalse(stage3['selection_description_updated'])
                self.assertEqual(stage2['sidecar_paths'], ['nested/sample/' + SIDECAR_MARKDOWN,
                                                         'nested/sample/' + SIDECAR_MANIFEST])
                self.assertEqual(set(tree_hashes(b)) - set(before), {SIDECAR_MARKDOWN, SIDECAR_MANIFEST})
                self.assertEqual(load_sidecars(b)['entries'][0]['target']['relative_path'], 'SKILL.md')

    def test_restrictive_umask_keeps_sidecars_readable_and_original_permissions_unchanged(self):
        for name, mode in (('SKILL.md', 0o640), ('helper.py', 0o750), ('notes.md', 0o664)):
            (self.skill / name).chmod(mode)
        originals = {path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                     for path in self.skill.iterdir()}
        entry = self.entry()
        stage2, stage3 = self.root / 'private-stage2', self.root / 'private-stage3'
        previous = os.umask(0o077)
        try:
            render_patch(self.pack, self.patch(entry), stage2)
            render_patch_set(self.skill, [entry], stage3)
        finally:
            os.umask(previous)
        for deployed in (stage2 / 'nested/sample', stage3):
            with self.subTest(deployed=deployed):
                for name in (SIDECAR_MARKDOWN, SIDECAR_MANIFEST):
                    self.assertEqual(stat.S_IMODE((deployed / name).stat().st_mode), 0o644)
                self.assertIsNotNone(load_sidecars(deployed))
                for name, expected in originals.items():
                    for root in (self.skill, deployed):
                        path = root / name
                        self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), expected)
        self.assertEqual(tree_hashes(stage2 / 'nested/sample'), tree_hashes(stage3))

    def test_table_and_heading_plus_code_bind_without_insertion_location(self):
        quotes = ('| Mixed reasoning | Extract the final answer. |',
                  '## Output format\nReturn this JSON:\n```json\n{"result": 1}\n```',
                  'Return this JSON:\n```json\n{"result": 1}')
        for index, quote in enumerate(quotes):
            with self.subTest(quote=quote):
                entry = self.entry(quote)
                self.assertEqual(entry['target']['schema_version'], 2)
                self.assertNotIn('insertion_offset', entry['target'])
                rendered = self.root / f'layout-{index}'
                render_patch(self.pack, self.patch(entry), rendered)
                deployed = rendered / 'nested/sample'
                self.assertEqual((deployed / 'SKILL.md').read_bytes(), (self.skill / 'SKILL.md').read_bytes())
                self.assertEqual(load_sidecars(deployed)['entries'][0]['target']['quote'], quote)

    def test_stage2_delivers_unique_prose_without_requiring_a_stage3_object(self):
        patch = self.patch(self.entry())
        quote = 'Never start any operation earlier than the baseline.'
        for index, text in enumerate((
                '---\nname: sample\ndescription: Scheduling\n---\n' + quote + '\n',
                quote + '\n\n# Later section\nOther instructions.\n')):
            with self.subTest(text=text):
                path = self.skill / 'SKILL.md'
                path.write_bytes(text.replace('\n', '\r\n').encode())
                patch = replace(patch, target=replace(patch.target, quote=quote, version=sha256_file(path)))
                before = tree_hashes(self.pack)
                output = self.root / f'prose-{index}'
                render_patch(self.pack, patch, output)
                payload = load_sidecars(output / 'nested/sample')
                self.assertEqual(payload['entries'][0]['target']['quote'], quote)
                self.assertEqual(tree_hashes(self.pack), before)
                for name, digest in before.items():
                    self.assertEqual(tree_hashes(output)[name], digest)
                with self.assertRaisesRegex(ValueError, 'heading or numbered-step'):
                    bind_target(self.skill, 'publisher/sample', 'SKILL.md', 'substep', quote)

    def test_stage2_does_not_require_a_quote_to_fit_one_stage3_structural_unit(self):
        quote = 'Read the input.\n2. Preserve the required result.'
        patch = self.patch(self.entry())
        patch = replace(patch, target=replace(patch.target, quote=quote))
        output = self.root / 'crossing-units'
        render_patch(self.pack, patch, output)
        self.assertEqual(load_sidecars(output / 'nested/sample')['entries'][0]['target']['quote'], quote)
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            bind_target(self.skill, 'publisher/sample', 'SKILL.md', 'substep', quote)

    def test_stage2_still_rejects_missing_ambiguous_or_stale_prose_targets(self):
        patch = self.patch(self.entry())
        path = self.skill / 'SKILL.md'
        quote = 'Never start any operation earlier than the baseline.'
        for case, text, version in (
                ('missing', 'Other prose.\n', None),
                ('ambiguous', quote + '\n' + quote + '\n', None),
                ('stale', quote + '\n', 'sha256:stale')):
            with self.subTest(case=case):
                path.write_text(text)
                targeted = replace(patch, target=replace(patch.target, quote=quote,
                                                        version=version or sha256_file(path)))
                with self.assertRaisesRegex(ValueError, 'absent or ambiguous|version'):
                    render_patch(self.pack, targeted, self.root / case)

    def test_new_mode_preserves_old_target_identity_but_removes_location_dependency(self):
        quote = 'Preserve the required result.'
        sidecar = bind_target(self.skill, 'publisher/sample', 'SKILL.md', 'substep', quote)
        inline = bind_target(self.skill, 'publisher/sample', 'SKILL.md', 'substep', quote,
                             presentation_mode='inline_v1')
        self.assertEqual(sidecar['target_id'], inline['target_id'])
        self.assertNotIn('insertion_offset', sidecar)
        self.assertIn('insertion_offset', inline)
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            self.entry('Read the input.\n2. Preserve the required result.')
        with self.assertRaisesRegex(ValueError, 'frontmatter'):
            self.entry('A multiline description.')

    def test_reference_file_target_is_relative_to_single_skill_and_other_originals_are_untouched(self):
        entry = self.entry()
        entry['target'] = bind_target(self.skill, 'publisher/sample', 'notes.md', 'substep', 'Preserve the schema.')
        output = self.root / 'reference'
        render_patch(self.pack, self.patch(entry), output)
        self.assertEqual(load_sidecars(output / 'nested/sample')['entries'][0]['target']['relative_path'], 'notes.md')
        for name, digest in self.before.items():
            self.assertEqual(tree_hashes(output)[name], digest)

    def test_original_and_sidecar_quotes_have_distinct_provenance(self):
        first = self.entry(identifier='P-first')
        second = self.entry('Extract the final answer.', identifier='P-second')
        second['how_to_interpret'] = 'Separate final content from preliminary reasoning.'
        output = self.root / 'mapped'
        result = render_patch_set(self.skill, [first, second], output)
        source = map_rendered_quote(output, result, 'SKILL.md', 'Extract the final answer.')
        patch = map_rendered_quote(output, result, SIDECAR_MARKDOWN, second['how_to_interpret'])
        self.assertEqual(source['origin'], 'original')
        self.assertEqual(source['original_quote'], 'Extract the final answer.')
        self.assertEqual(patch['origin'], 'patch')
        self.assertEqual(patch['entry_id'], 'P-second')
        self.assertEqual(patch['target'], second['target'])
        text = (output / SIDECAR_MARKDOWN).read_text()
        self.assertLess(text.index('## Patch P-first'), text.index('## Patch P-second'))
        with self.assertRaisesRegex(ValueError, 'boundaries'):
            map_rendered_quote(output, result, SIDECAR_MARKDOWN,
                               first['how_to_interpret'] + '\n\n## Patch P-second')

    def test_load_rejects_stale_targets_body_changes_and_missing_pairs(self):
        output = self.root / 'checked'
        render_patch_set(self.skill, [self.entry()], output)
        self.assertIsNotNone(load_sidecars(output))
        body = output / SIDECAR_MARKDOWN
        original_body = body.read_bytes()
        body.write_bytes(original_body + b'Unbound instruction.\n')
        with self.assertRaisesRegex(ValueError, 'differs'):
            load_sidecars(output)
        body.write_bytes(original_body)
        target = output / 'SKILL.md'
        target.write_bytes(target.read_bytes() + b'Changed original.\n')
        with self.assertRaisesRegex(ValueError, 'version'):
            load_sidecars(output)
        target.write_bytes((self.skill / 'SKILL.md').read_bytes())
        body.unlink()
        with self.assertRaisesRegex(ValueError, 'paired'):
            load_sidecars(output)
        self.assertIsNone(load_sidecars(self.skill))

    def test_reserved_paths_tampered_bindings_and_duplicate_entries_are_rejected(self):
        entry = self.entry()
        tampered = copy.deepcopy(entry)
        tampered['target']['start'] += 1
        with self.assertRaisesRegex(ValueError, 'tampered'):
            render_patch_set(self.skill, [tampered], self.root / 'tampered')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            render_patch_set(self.skill, [entry, entry], self.root / 'duplicates')
        (self.skill / SIDECAR_MARKDOWN).write_text('Existing unrelated resource.\n')
        with self.assertRaisesRegex(ValueError, 'paired'):
            render_patch(self.pack, self.patch(entry), self.root / 'collision')
        self.assertFalse((self.root / 'collision').exists())

    def test_empty_set_retains_identical_original_and_has_no_instruction_file(self):
        output = self.root / 'empty'
        result = render_patch_set(self.skill, [], output)
        self.assertEqual(tree_hashes(output), tree_hashes(self.skill))
        self.assertEqual(result['sidecar_paths'], [])
        self.assertIsNone(load_sidecars(output))

    def test_stage2_appends_to_existing_verified_sidecars_and_matches_stage3_collection(self):
        first, second = self.entry(identifier='P-first'), self.entry(identifier='P-second')
        second['how_to_interpret'] = 'Inspect the final output representation before finishing.'
        first_pack = self.root / 'first-pack'
        render_patch(self.pack, self.patch(first), first_pack)
        before = tree_hashes(first_pack)
        output = self.root / 'second-pack'
        render_patch(first_pack, self.patch(second), output)
        expected = self.root / 'set'
        render_patch_set(self.skill, [first, second], expected)
        self.assertEqual(tree_hashes(expected), tree_hashes(output / 'nested/sample'))
        self.assertEqual(tree_hashes(first_pack), before)
        payload = load_sidecars(output / 'nested/sample')
        self.assertEqual([entry['entry_id'] for entry in payload['entries']], ['P-first', 'P-second'])
        for name, digest in self.before.items():
            self.assertEqual(tree_hashes(output)[name], digest)


if __name__ == '__main__':
    unittest.main()
