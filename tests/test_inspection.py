"""Read-only inspection and provenance tests; no task or model execution."""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from skillpatch.inspection import LIMITS, LiveInspector, snapshot_file_state, snapshot_initial_files


class LocalSandbox:
    """Run the fixed inspection program against temporary fixture files only."""

    def __init__(self):
        self.calls = []

    async def exec(self, command, *, user, timeout_sec):
        self.calls.append((command, user, timeout_sec))
        process = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        return SimpleNamespace(return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())


def tiny_pdf(pages=1):
    page_ids = [4 + 2 * number for number in range(pages)]
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [' + b' '.join(str(index).encode() + b' 0 R' for index in page_ids)
        + b'] /Count ' + str(pages).encode() + b' >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    for number, index in enumerate(page_ids, 1):
        stream = f'BT /F1 12 Tf 72 720 Td (A table with duplicate rows on page {number}.) Tj ET'.encode()
        objects.extend([
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents '
            + str(index + 1).encode() + b' 0 R >>',
            b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream',
        ])
    data, offsets = b'%PDF-1.4\n', [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    start = len(data)
    size = str(len(objects) + 1).encode()
    data += b'xref\n0 ' + size + b'\n0000000000 65535 f \n'
    data += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    return data + b'trailer\n<< /Size ' + size + b' /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n'


class LiveInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.workspace, self.skills = self.base / 'workspace', self.base / 'skills'
        self.workspace.mkdir()
        self.skills.mkdir()
        self.source = self.workspace / 'table.txt'
        self.source.write_text('row 1\nrow 2\nrow 2\nlast row\n', encoding='utf-8')
        (self.skills / 'SKILL.md').write_text('Read the table before editing.', encoding='utf-8')
        self.output = self.base / 'observed'
        self.sandbox = LocalSandbox()

    def inspector(self, hashes=None, **kwargs):
        return LiveInspector(self.sandbox, str(self.workspace), str(self.skills), self.output,
                             hashes or {}, **kwargs)

    async def resource(self, inspector, suffix):
        catalog = await inspector.prepare()
        return next(item['resource_id'] for item in catalog['resources'] if item['relative_path'].endswith(suffix))

    async def test_initial_hash_and_paginated_read_preserve_source(self):
        hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills), user='test-agent')
        self.assertIn(str(self.source), hashes)
        inspector = self.inspector(hashes, user='test-agent')
        identifier = await self.resource(inspector, '/table.txt')
        result = await inspector.call('read_text', {'resource_id': identifier, 'start_line': 2, 'max_lines': 1})
        self.assertNotIn('error', result)
        item = result['evidence'][0]
        self.assertEqual(item.evidence_id, 'A000001')
        self.assertEqual(item.kind, 'artifact')
        self.assertEqual(item.availability, 'initial')
        self.assertEqual(item.text, '2: row 2')
        self.assertTrue(result['truncated'])
        self.assertIn(item.evidence_id, result['text'])
        self.assertNotIn(item.text, result['text'])
        self.assertEqual(Path(item.source_path).read_bytes(), self.source.read_bytes())
        self.assertEqual(item.version, hashes[str(self.source)])
        saved = json.loads((self.output / 'observations/A000001.json').read_text())
        self.assertEqual(saved['observation']['arguments']['start_line'], 2)
        self.assertIsNotNone(datetime.fromisoformat(saved['observation']['observed_at']).tzinfo)
        self.assertTrue(all(user == 'test-agent' for _, user, _ in self.sandbox.calls))
        self.assertTrue(all('python3 -I -c ' in cmd for cmd, _, _ in self.sandbox.calls))
        self.source.unlink()
        self.assertIn(b'row 2', Path(item.source_path).read_bytes())

    async def test_changed_and_unhashed_files_are_not_initial(self):
        hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills))
        self.source.write_text('changed contents', encoding='utf-8')
        inspector = self.inspector(hashes)
        identifier = await self.resource(inspector, '/table.txt')
        changed = await inspector.call('read_text', {'resource_id': identifier})
        self.assertEqual(changed['evidence'][0].availability, 'post_execution')
        inspector.initial_hashes = {}
        unknown = await inspector.call('read_text', {'resource_id': identifier})
        self.assertEqual(unknown['evidence'][0].availability, 'unknown')
        self.assertEqual(unknown['evidence'][0].evidence_id, 'A000002')

    async def test_catalog_distinguishes_recorded_hash_from_pending_availability(self):
        hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills))
        self.source.write_text('changed after baseline', encoding='utf-8')
        generated = self.workspace / 'generated.txt'
        generated.write_text('created later', encoding='utf-8')
        inspector = self.inspector(hashes)
        catalog = await inspector.prepare()
        entries = {entry['relative_path']: entry for entry in catalog['resources']}
        for name, recorded in [('workspace/table.txt', True), ('skills/SKILL.md', True),
                               ('workspace/generated.txt', False)]:
            with self.subTest(name=name):
                entry = entries[name]
                self.assertEqual(entry['initial_hash_recorded'], recorded)
                self.assertEqual(entry['availability_check'], 'pending_observation')
                self.assertNotIn('availability', entry)
        self.assertEqual(entries['workspace/.']['availability_check'], 'not_applicable')
        self.assertNotIn('availability', entries['workspace/.'])
        self.assertFalse((self.output / 'artifacts').exists())
        for name, expected in [('workspace/table.txt', 'post_execution'), ('skills/SKILL.md', 'initial'),
                               ('workspace/generated.txt', 'unknown')]:
            with self.subTest(name=name):
                result = await inspector.call('read_text', {'resource_id': entries[name]['resource_id']})
                self.assertEqual(result['evidence'][0].availability, expected)
        self.assertTrue(any('not that the baseline is missing' in note for note in catalog['limitations']))

    async def test_workspace_inspection_without_task_skills(self):
        private = self.workspace / '.ssh'
        private.mkdir()
        (private / 'id_rsa').write_text('private key', encoding='utf-8')
        for skills_dir in (None, ''):
            with self.subTest(skills_dir=skills_dir):
                state = await snapshot_file_state(self.sandbox, str(self.workspace), skills_dir)
                self.assertIn(str(self.source), state['hashes'])
                self.assertNotIn(str(private / 'id_rsa'), state['hashes'])
                self.assertNotIn(str(self.skills / 'SKILL.md'), state['hashes'])
                self.assertEqual(state['limitations'], [])
                inspector = LiveInspector(self.sandbox, str(self.workspace), skills_dir,
                                          self.output / ('none' if skills_dir is None else 'empty'), state['hashes'])
                catalog = await inspector.prepare()
                self.assertTrue(all(item['source_role'] == 'workspace' for item in catalog['resources']))
                identifier = await self.resource(inspector, '/table.txt')
                result = await inspector.call('read_text', {'resource_id': identifier})
                self.assertNotIn('error', result)
                self.assertIn('row 1', result['evidence'][0].text)
                self.assertEqual(result['evidence'][0].availability, 'initial')

    async def test_literal_search_and_list_pagination(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        result = await inspector.call('search_text', {'resource_id': identifier, 'query': 'row 2', 'max_matches': 1})
        self.assertEqual(result['evidence'][0].text, '2: row 2')
        self.assertTrue(result['truncated'])
        none = await inspector.call('search_text', {'resource_id': identifier, 'query': 'row .*'})
        self.assertIn('No matching lines', none['evidence'][0].text)
        listing = await inspector.call('inspect_list', {'limit': 1})
        self.assertTrue(listing['truncated'])
        self.assertEqual(listing['evidence'], [])
        root_id = await self.resource(inspector, 'workspace/.')
        children = await inspector.call('inspect_list', {'resource_id': root_id})
        entries = json.loads(children['text'])['resources']
        self.assertEqual([item['resource_id'] for item in entries], [identifier])

    async def test_ordinary_project_tests_visible_private_and_oracle_paths_excluded(self):
        for folder in ('tests', 'verifier', 'oracle', '.ssh', '.config'):
            directory = self.workspace / folder
            directory.mkdir()
            (directory / 'sample.txt').write_text('content', encoding='utf-8')
        (self.workspace / '.env').write_text('secret=value', encoding='utf-8')
        (self.workspace / 'private.key').write_text('key', encoding='utf-8')
        (self.workspace / 'credentials.json').write_text('{}', encoding='utf-8')
        inspector = self.inspector()
        paths = [item['relative_path'] for item in (await inspector.prepare())['resources']]
        self.assertIn('workspace/tests/sample.txt', paths)
        self.assertFalse(any(any(part in path for part in ('verifier', 'oracle', '.ssh', '.config', '.env', '.key', 'credentials')) for path in paths))

    async def test_benchmark_exclusions_apply_to_snapshots_catalog_and_reads(self):
        hidden = self.workspace / 'custom_hidden'
        public = self.workspace / 'custom_hidden_public'
        hidden.mkdir()
        public.mkdir()
        (hidden / 'answers.txt').write_text('hidden answers', encoding='utf-8')
        (public / 'data.txt').write_text('public data', encoding='utf-8')
        excluded = (str(hidden) + '/',)
        state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills), excluded_paths=excluded)
        self.assertNotIn(str(hidden / 'answers.txt'), state['hashes'])
        self.assertIn(str(public / 'data.txt'), state['hashes'])
        self.assertEqual(state['excluded_paths'], [str(hidden)])
        hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills), excluded_paths=excluded)
        self.assertEqual(hashes, state['hashes'])
        inspector = self.inspector(excluded_paths=excluded)
        paths = [item['relative_path'] for item in (await inspector.prepare())['resources']]
        self.assertNotIn('workspace/custom_hidden/answers.txt', paths)
        self.assertIn('workspace/custom_hidden_public/data.txt', paths)
        # Defense in depth: the remote program rechecks exclusions even if a
        # stale/inconsistent host catalog exposes an excluded resource ID.
        inspector.resources['R999999'] = {
            'path': str(hidden / 'answers.txt'), 'root': str(self.workspace),
            'source_role': 'workspace', 'kind': 'file', 'relative_path': 'workspace/custom_hidden/answers.txt',
        }
        result = await inspector.call('read_text', {'resource_id': 'R999999'})
        self.assertIn('outside the inspection boundary', result['error'])
        self.assertEqual(result['evidence'], [])

    async def test_benchmark_exclusions_reject_relative_and_traversal_paths(self):
        for excluded in [('relative',), ('/tmp/../elsewhere',), ('/bad\x00path',), '/absolute/string', ('/',)]:
            with self.subTest(excluded=excluded):
                with self.assertRaises(ValueError):
                    self.inspector(excluded_paths=excluded)
                with self.assertRaises(ValueError):
                    await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills), excluded_paths=excluded)

    async def test_catalog_and_reads_refuse_symlinks_and_post_catalog_swap(self):
        outside = self.base / 'outside.txt'
        outside.write_text('should remain outside', encoding='utf-8')
        (self.workspace / 'linked.txt').symlink_to(outside)
        (self.workspace / 'outside-dir').symlink_to(self.base, target_is_directory=True)
        inspector = self.inspector()
        catalog = await inspector.prepare()
        self.assertFalse(any('linked.txt' in item['relative_path'] or 'outside-dir' in item['relative_path'] for item in catalog['resources']))
        identifier = await self.resource(inspector, '/table.txt')
        self.source.unlink()
        self.source.symlink_to(outside)
        result = await inspector.call('read_text', {'resource_id': identifier})
        self.assertIn('error', result)
        self.assertEqual(result['evidence'], [])
        self.assertFalse((self.output / 'artifacts').exists())

    async def test_parent_directory_swap_to_symlink_is_refused(self):
        inner = self.workspace / 'inner'
        inner.mkdir()
        (inner / 'note.txt').write_text('safe', encoding='utf-8')
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/inner/note.txt')
        outside = self.base / 'external'
        outside.mkdir()
        (outside / 'note.txt').write_text('outside', encoding='utf-8')
        shutil.rmtree(inner)
        inner.symlink_to(outside, target_is_directory=True)
        result = await inspector.call('read_text', {'resource_id': identifier})
        self.assertIn('error', result)

    async def test_arguments_cannot_supply_paths_commands_or_unbounded_requests(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        before = len(self.sandbox.calls)
        cases = [
            ('exec', {'command': 'touch hacked'}),
            ('read_text', {'resource_id': str(self.source)}),
            ('read_text', {'resource_id': identifier, 'command': 'touch hacked'}),
            ('read_text', {'resource_id': identifier, 'max_lines': 501}),
            ('read_text', {'resource_id': identifier, 'start_line': True}),
            ('search_text', {'resource_id': identifier, 'query': ''}),
            ('read_pdf', {'resource_id': identifier, 'max_pages': 21}),
            ('inspect_list', {'offset': -1}),
        ]
        for name, args in cases:
            with self.subTest(name=name, args=args):
                self.assertIn('error', await inspector.call(name, args))
        self.assertEqual(before, len(self.sandbox.calls))

    async def test_shell_metacharacters_in_filename_and_query_are_data(self):
        unusual = self.workspace / "name'; $(touch HACKED).txt"
        unusual.write_text('literal $(touch HACKED)', encoding='utf-8')
        inspector = self.inspector()
        identifier = await self.resource(inspector, unusual.name)
        result = await inspector.call('search_text', {'resource_id': identifier, 'query': '$(touch HACKED)'})
        self.assertNotIn('error', result)
        self.assertIn('literal', result['evidence'][0].text)
        self.assertFalse((self.workspace / 'HACKED').exists())
        self.assertFalse((Path.cwd() / 'HACKED').exists())

    async def test_known_secret_observation_is_rejected_without_source_copy(self):
        secret = 'known-private-credential-987'
        self.source.write_text('line 1\n' + secret + '\n', encoding='utf-8')
        inspector = self.inspector(secrets=(secret,))
        identifier = await self.resource(inspector, '/table.txt')
        # The requested page excludes the secret; the complete source still
        # cannot be preserved unredacted.
        result = await inspector.call('read_text', {'resource_id': identifier, 'max_lines': 1})
        self.assertIn('error', result)
        self.assertNotIn(secret, result['text'])
        self.assertFalse((self.output / 'artifacts').exists())
        self.assertFalse((self.output / 'observations').exists())

    async def test_scan_and_output_bounds_are_explicit(self):
        inspector = self.inspector()
        with patch.dict(LIMITS, {'max_files': 1}):
            catalog = await inspector.prepare()
        self.assertTrue(any('file scan limit' in note for note in catalog['limitations']))
        identifier = await self.resource(inspector, '/table.txt')
        self.source.write_text('x' * 100, encoding='utf-8')
        with patch.dict(LIMITS, {'max_text_chars': 16}):
            result = await inspector.call('read_text', {'resource_id': identifier})
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['evidence'][0].text), 16)
        with patch.dict(LIMITS, {'max_file_bytes': 10}):
            result = await inspector.call('read_text', {'resource_id': identifier})
        self.assertIn('byte limit', result['error'])

    async def test_baseline_reports_scan_hash_and_transport_limits(self):
        with patch.dict(LIMITS, {'max_snapshot_bytes': 1}):
            state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(state['hashes'], {})
        self.assertTrue(any('snapshot byte limit' in note for note in state['limitations']))
        with patch.dict(LIMITS, {'max_files': 1}):
            state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(len(state['hashes']), 1)
        self.assertTrue(any('file scan limit' in note for note in state['limitations']))
        with patch.dict(LIMITS, {'max_file_bytes': 1}):
            state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(state['hashes'], {})
        self.assertTrue(any('could not be hashed' in note for note in state['limitations']))
        self.sandbox.exec = AsyncMock(side_effect=TimeoutError())
        state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(state['hashes'], {})
        self.assertTrue(any('timed out' in note for note in state['limitations']))
        with self.assertWarnsRegex(RuntimeWarning, 'incomplete'):
            hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(hashes, {})

    async def test_baseline_reports_unavailable_roots(self):
        missing = self.base / 'missing'
        state = await snapshot_file_state(self.sandbox, str(missing), str(self.skills))
        self.assertTrue(any('workspace root is unavailable' in note for note in state['limitations']))
        self.assertIn(str(self.skills / 'SKILL.md'), state['hashes'])

    async def test_malformed_scan_is_reported(self):
        self.sandbox.exec = AsyncMock(return_value=SimpleNamespace(
            return_code=0, stdout=json.dumps({'resources': [None], 'limitations': []})))
        state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertEqual(state['hashes'], {})
        self.assertTrue(any('invalid response' in note for note in state['limitations']))

    async def test_scan_depth_limit_is_explicit(self):
        nested = self.workspace / 'subdirectory'
        nested.mkdir()
        (nested / 'file.txt').write_text('not cataloged', encoding='utf-8')
        with patch.dict(LIMITS, {'max_depth': 0}):
            state = await snapshot_file_state(self.sandbox, str(self.workspace), str(self.skills))
        self.assertTrue(any('depth limit' in note for note in state['limitations']))
        self.assertNotIn(str(nested / 'file.txt'), state['hashes'])

    async def test_transport_failures_are_explicit_and_cancellation_propagates(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        self.sandbox.exec = AsyncMock(side_effect=TimeoutError())
        result = await inspector.call('read_text', {'resource_id': identifier})
        self.assertIn('timed out', result['error'])
        self.assertEqual(result['evidence'], [])
        self.sandbox.exec = AsyncMock(return_value=SimpleNamespace(return_code=0, stdout='invalid', stderr=''))
        self.assertIn('invalid response', (await inspector.call('read_text', {'resource_id': identifier}))['error'])
        self.sandbox.exec = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await inspector.call('read_text', {'resource_id': identifier})

    async def test_image_result_preserves_png_and_provenance(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        source = self.source.read_bytes()
        png = b'\x89PNG\r\n\x1a\nfixture-image'
        response = {'source_base64': base64.b64encode(source).decode(),
                    'image_base64': base64.b64encode(png).decode(), 'text': 'Image preview; visual observation only.',
                    'sha256': 'sha256:' + hashlib.sha256(source).hexdigest(), 'truncated': False}
        with patch('skillpatch.inspection._remote', AsyncMock(return_value=response)):
            result = await inspector.call('view_image', {'resource_id': identifier})
        self.assertEqual(result['images'][0].read_bytes(), png)
        self.assertEqual(result['evidence'][0].observation['images'][0]['sha256'], 'sha256:' + hashlib.sha256(png).hexdigest())
        self.assertIn('A000001', result['text'])

    async def test_missing_pdf_support_is_explained(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        with patch('skillpatch.inspection._remote', AsyncMock(return_value={'error': 'PDF support unavailable: required command is not installed'})):
            result = await inspector.call('pdf_info', {'resource_id': identifier})
        self.assertIn('not installed', result['error'])
        self.assertEqual(result['evidence'], [])

    async def test_invalid_observations_do_not_save_artifacts(self):
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/table.txt')
        source = self.source.read_bytes()
        valid = {'source_base64': base64.b64encode(source).decode(), 'text': 'numbered text',
                 'sha256': 'sha256:' + hashlib.sha256(source).hexdigest(), 'truncated': False}
        cases = [{}, {**valid, 'source_base64': '@@'}, {**valid, 'sha256': 'sha256:wrong'},
                 {**valid, 'text': []}, {**valid, 'truncated': 1},
                 {**valid, 'image_base64': base64.b64encode(b'not an image').decode()}]
        for response in cases:
            with self.subTest(response=response):
                with patch('skillpatch.inspection._remote', AsyncMock(return_value=response)):
                    result = await inspector.call('read_text', {'resource_id': identifier})
                self.assertIn('error', result)
                self.assertFalse((self.output / 'artifacts').exists())
                self.assertFalse((self.output / 'observations').exists())

    @unittest.skipUnless(shutil.which('pdfinfo') and shutil.which('pdftotext') and shutil.which('pdftoppm'), 'Poppler unavailable')
    async def test_pdf_info_text_search_and_render_on_fixture(self):
        pdf = self.workspace / 'document.pdf'
        pdf.write_bytes(tiny_pdf())
        hashes = await snapshot_initial_files(self.sandbox, str(self.workspace), str(self.skills))
        inspector = self.inspector(hashes)
        identifier = await self.resource(inspector, '/document.pdf')
        info = await inspector.call('pdf_info', {'resource_id': identifier})
        self.assertNotIn('error', info)
        self.assertIn('page count: 1', info['evidence'][0].text)
        text = await inspector.call('read_pdf', {'resource_id': identifier})
        self.assertIn('duplicate rows', text['evidence'][0].text)
        search = await inspector.call('search_pdf', {'resource_id': identifier, 'query': 'duplicate'})
        self.assertIn('Page 1', search['evidence'][0].text)
        image = await inspector.call('view_image', {'resource_id': identifier})
        self.assertNotIn('error', image)
        self.assertTrue(image['images'][0].read_bytes().startswith(b'\x89PNG'))
        self.assertEqual(image['evidence'][0].availability, 'initial')

    @unittest.skipUnless(shutil.which('pdfinfo') and shutil.which('pdftotext'), 'Poppler unavailable')
    async def test_pdf_page_and_match_limits_are_recorded(self):
        pdf = self.workspace / 'document.pdf'
        pdf.write_bytes(tiny_pdf(pages=3))
        inspector = self.inspector()
        identifier = await self.resource(inspector, '/document.pdf')
        result = await inspector.call('read_pdf', {'resource_id': identifier, 'start_page': 2, 'max_pages': 1})
        item = result['evidence'][0]
        self.assertIn('page 2', item.text)
        self.assertNotIn('page 1', item.text)
        self.assertEqual(item.observation['total_pages'], 3)
        self.assertEqual(item.observation['start_page'], 2)
        self.assertEqual(item.observation['end_page'], 2)
        self.assertTrue(result['truncated'])
        search = await inspector.call('search_pdf', {'resource_id': identifier, 'query': 'duplicate', 'max_matches': 1})
        self.assertEqual(search['evidence'][0].observation['matches_in_scanned_pages'], 3)
        self.assertTrue(search['truncated'])
        nonexistent = await inspector.call('read_pdf', {'resource_id': identifier, 'start_page': 4})
        self.assertIn('exceeds PDF page count', nonexistent['error'])

    def test_roots_reject_traversal_and_normalize_multiple_slashes(self):
        for root in ('relative', '/', '/tmp/../etc', '/tmp/\x00evil'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                LiveInspector(self.sandbox, root, str(self.skills), self.output, {})
        inspector = LiveInspector(self.sandbox, '//' + str(self.workspace).lstrip('/'), str(self.skills), self.output, {})
        self.assertEqual(inspector.roots[0], str(self.workspace))

    def test_definitions_expose_only_constrained_named_tools(self):
        definitions = self.inspector().definitions()
        self.assertEqual(len(definitions), 7)
        for definition in definitions:
            self.assertEqual(definition['type'], 'function')
            self.assertFalse(definition['function']['parameters']['additionalProperties'])
            self.assertNotIn('command', definition['function']['parameters']['properties'])


if __name__ == '__main__':
    unittest.main()
