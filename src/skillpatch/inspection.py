"""Bounded, read-only inspection of a live execution sandbox.

The sandbox runs only the fixed isolated Python program below. Model arguments
are data, never commands. Artifact observations are not execution events.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shlex
import warnings
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from skillpatch.environment import redact
from skillpatch.models import EvidenceItem
from skillpatch.storage import now, write_json


LIMITS = {
    "max_files": 1000, "max_entries": 3000, "max_depth": 8,
    "max_file_bytes": 16 * 1024 * 1024, "max_snapshot_bytes": 128 * 1024 * 1024,
    "max_text_chars": 24000, "max_lines": 500, "max_matches": 100,
    "max_pdf_pages": 20, "max_image_dimension": 1600, "timeout_seconds": 30,
}

# -I excludes the task directory and PYTHONPATH from Python imports. All paths
# are checked again inside the sandbox, including after catalog preparation.
_REMOTE = r'''
import base64, hashlib, io, json, os, stat, subprocess, sys
from pathlib import PurePosixPath

p = json.loads(base64.b64decode(sys.argv[1]))
limits = p['limits']
roots = p['roots']
blocked_roots = ('/verifier', '/tests', '/oracle', '/testbed_verify', '/logs/verifier', '/proc', '/sys', '/dev') + tuple(p.get('excluded_paths', []))
blocked_parts = {'verifier', 'oracle', 'testbed_verify', '.git', '.ssh', '.aws', '.azure', '.config', '.codex', '.claude', '.openhands', '.cache', 'node_modules', '.venv', '__pycache__'}

def allowed(path, root):
    if not (path == root or path.startswith(root.rstrip('/') + '/')):
        return False
    if any(path == b or path.startswith(b + '/') for b in blocked_roots):
        return False
    # An explicitly supplied skills root can live in an agent config directory;
    # its siblings remain unreachable. Other private directories are excluded.
    is_skills_root = len(roots) > 1 and root == roots[1]
    parts = PurePosixPath(os.path.relpath(path, root)).parts if is_skills_root else PurePosixPath(path).parts
    for part in parts:
        low = part.lower()
        if low in blocked_parts or low in {'.netrc', '.npmrc', '.pypirc', 'auth.json', 'credentials.json', 'credentials', 'secrets.json'}:
            return False
        if low == '.env' or low.startswith('.env.') or low.startswith('id_rsa') or low.startswith('id_ed25519') or low.endswith(('.pem', '.key')):
            return False
    return True

def open_directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in PurePosixPath(path).parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise

def read_file(path, root):
    if not allowed(path, root):
        raise ValueError('resource is outside the inspection boundary')
    # Open every component without following symlinks, not just the final file.
    fd = open_directory(str(PurePosixPath(path).parent))
    try:
        stream = os.open(PurePosixPath(path).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(stream, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('resource is not a regular file')
            if info.st_size > limits['max_file_bytes']:
                raise ValueError('file exceeds the inspection byte limit')
            data = handle.read(limits['max_file_bytes'] + 1)
            if len(data) > limits['max_file_bytes']:
                raise ValueError('file exceeds the inspection byte limit')
            return data
    finally:
        os.close(fd)

def scan():
    resources, notes, visited = [], [], set()
    entries = files = hash_bytes = 0
    stop = False
    for role, root in zip(('workspace', 'skills'), roots):
        if not allowed(root, root) or not os.path.isdir(root) or os.path.islink(root):
            notes.append(role + ' root is unavailable or excluded')
            continue
        stack = [(root, 0)]
        while stack and not stop:
            folder, depth = stack.pop()
            if folder in visited:
                continue
            visited.add(folder)
            resources.append({'path': folder, 'root': root, 'relative_path': role + '/' + os.path.relpath(folder, root), 'source_role': role, 'kind': 'directory'})
            try:
                folder_fd = open_directory(folder)
                try:
                    with os.scandir(folder_fd) as iterator:
                        children = []
                        for entry in iterator:
                            entries += 1
                            if entries > limits['max_entries']:
                                notes.append('entry scan limit reached; catalog is incomplete')
                                stop = True
                                break
                            info = entry.stat(follow_symlinks=False)
                            children.append((entry.name, info.st_mode, info.st_size))
                finally:
                    os.close(folder_fd)
                for entry_name, mode, size in sorted(children):
                    entry_path = os.path.join(folder, entry_name)
                    if stat.S_ISLNK(mode) or not allowed(entry_path, root):
                        continue
                    if stat.S_ISDIR(mode):
                        if depth >= limits['max_depth']:
                            notes.append('depth limit reached; catalog is incomplete')
                        else:
                            stack.append((entry_path, depth + 1))
                    elif stat.S_ISREG(mode):
                        files += 1
                        if files > limits['max_files']:
                            notes.append('file scan limit reached; catalog is incomplete')
                            stop = True
                            break
                        item = {'path': entry_path, 'root': root, 'relative_path': role + '/' + os.path.relpath(entry_path, root), 'source_role': role, 'kind': 'file', 'size': size}
                        if p.get('hash_files'):
                            if hash_bytes + item['size'] > limits['max_snapshot_bytes']:
                                notes.append('snapshot byte limit reached; some initial hashes are unavailable')
                            else:
                                try:
                                    data = read_file(entry_path, root)
                                    hash_bytes += len(data)
                                    item['sha256'] = 'sha256:' + hashlib.sha256(data).hexdigest()
                                except (OSError, ValueError):
                                    notes.append('some files could not be hashed')
                        resources.append(item)
            except OSError:
                notes.append('some directories could not be listed')
        if stop:
            break
    return {'resources': resources, 'limitations': sorted(set(notes))}

def poppler(command, data):
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    except FileNotFoundError:
        raise ValueError('PDF support unavailable: neither PyMuPDF nor the required Poppler command is installed')
    if result.returncode:
        raise ValueError('Poppler could not process this document')
    return result.stdout

def pdf_document(data):
    try:
        import fitz
    except ImportError:
        return None, None
    return fitz, fitz.open(stream=data, filetype='pdf')

def pdf_count(data, document):
    if document is not None:
        return len(document)
    raw = poppler(['pdfinfo', '-'], data).decode('utf-8', 'replace')
    for line in raw.splitlines():
        if line.startswith('Pages:'):
            return int(line.split(':', 1)[1].strip())
    raise ValueError('PDF page count is unavailable')

def pdf_text(data, document, page):
    if document is not None:
        return document[page - 1].get_text()
    return poppler(['pdftotext', '-f', str(page), '-l', str(page), '-layout', '-', '-'], data).decode('utf-8', 'replace')

def observe():
    data = read_file(p['path'], p['root'])
    action, args = p['action'], p['args']
    result = {'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(), 'source_base64': base64.b64encode(data).decode(), 'truncated': False}
    text = ''
    if action in ('read_text', 'search_text'):
        if b'\x00' in data:
            raise ValueError('resource is binary; use the PDF or image tools where applicable')
        try:
            decoded = data.decode('utf-8-sig')
        except UnicodeError:
            raise ValueError('resource is not UTF-8 text')
        lines = decoded.splitlines()
        if action == 'read_text':
            start = args['start_line']
            selected = list(enumerate(lines[start - 1:start - 1 + args['max_lines']], start))
            result.update(total_lines=len(lines), start_line=start, truncated=start - 1 + len(selected) < len(lines))
        else:
            matches = [(n, line) for n, line in enumerate(lines, 1) if args['query'] in line]
            selected = matches[:args['max_matches']]
            result.update(total_matches=len(matches), truncated=len(matches) > len(selected))
        text = '\n'.join(str(n) + ': ' + line for n, line in selected)
        if not text:
            text = '(No matching lines.)' if action == 'search_text' else '(No text in the requested line range.)'
    elif action in ('pdf_info', 'read_pdf', 'search_pdf') or (action == 'view_image' and data.startswith(b'%PDF-')):
        fitz, document = pdf_document(data)
        try:
            count = pdf_count(data, document)
            result['total_pages'] = count
            if action == 'pdf_info':
                text = 'PDF page count: ' + str(count)
                if document is not None:
                    text += '\nMetadata: ' + json.dumps(document.metadata, ensure_ascii=False)
            elif action == 'view_image':
                page = args['page']
                if page > count:
                    raise ValueError('requested page exceeds PDF page count')
                if document is not None:
                    obj = document[page - 1]
                    scale = min(2, limits['max_image_dimension'] / max(obj.rect.width, obj.rect.height))
                    png = obj.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
                else:
                    png = poppler(['pdftoppm', '-f', str(page), '-l', str(page), '-singlefile', '-scale-to', str(limits['max_image_dimension']), '-png', '-'], data)
                result['image_base64'] = base64.b64encode(png).decode()
                text = 'Rendered PDF page ' + str(page) + ' of ' + str(count) + '; visual observation only.'
            else:
                start = args['start_page']
                if start > count:
                    raise ValueError('requested page exceeds PDF page count')
                end = min(count, start + args['max_pages'] - 1)
                result.update(start_page=start, end_page=end, truncated=end < count)
                chunks, matches = [], 0
                for page in range(start, end + 1):
                    page_text = pdf_text(data, document, page)
                    if action == 'read_pdf':
                        chunks.append('[Page ' + str(page) + ']\n' + (page_text or '(No extractable text; use visual inspection for this page.)'))
                    else:
                        for number, line in enumerate(page_text.splitlines(), 1):
                            if args['query'] in line:
                                matches += 1
                                if matches <= args['max_matches']:
                                    chunks.append('[Page ' + str(page) + ', line ' + str(number) + '] ' + line)
                text = '\n'.join(chunks)
                if action == 'search_pdf':
                    result['matches_in_scanned_pages'] = matches
                    result['truncated'] = result['truncated'] or matches > args['max_matches']
                    text = text or '(No matches in the inspected PDF pages; scanned images may have no extractable text.)'
                else:
                    text = text or '(No extractable text in the inspected PDF pages.)'
        finally:
            if document is not None:
                document.close()
    elif action == 'view_image':
        try:
            import fitz
        except ImportError:
            raise ValueError('Image preview requires PyMuPDF, which is not installed')
        suffix = PurePosixPath(p['path']).suffix.lower().lstrip('.')
        if suffix not in {'png', 'jpg', 'jpeg', 'bmp', 'tif', 'tiff', 'gif', 'webp'}:
            raise ValueError('unsupported image format')
        if args['page'] != 1:
            raise ValueError('image preview supports page 1 only')
        with fitz.open(stream=data, filetype=suffix) as document:
            page = document[0]
            scale = min(1, limits['max_image_dimension'] / max(page.rect.width, page.rect.height))
            png = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
        result['image_base64'] = base64.b64encode(png).decode()
        text = 'Image preview; visual observation only.'
    else:
        raise ValueError('unsupported inspection action')
    if len(text) > limits['max_text_chars']:
        text = text[:limits['max_text_chars']]
        result['truncated'] = True
    result['text'] = text
    return result

try:
    response = scan() if p['action'] == 'scan' else observe()
except Exception as exc:
    # Do not expose parser messages that may echo document bytes or paths.
    message = str(exc) if isinstance(exc, ValueError) and exc.__class__ is ValueError else type(exc).__name__ + ': inspection could not complete'
    response = {'error': message}
print(json.dumps(response, ensure_ascii=True, allow_nan=False))
'''


def _roots(workspace: str, skills_dir: str | None) -> list[str]:
    roots = []
    values = (workspace,) if skills_dir in (None, '') else (workspace, skills_dir)
    for value in values:
        if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
            raise ValueError("inspection roots must be absolute sandbox paths")
        path = PurePosixPath('/' + value.lstrip('/'))
        if '..' in path.parts or str(path) == '/':
            raise ValueError("inspection roots must be bounded directories")
        roots.append(str(path))
    return roots


def _excluded_paths(values: tuple[str, ...]) -> list[str]:
    if not isinstance(values, (tuple, list)):
        raise ValueError('Excluded paths must be a list or tuple of absolute sandbox paths.')
    paths = []
    for value in values:
        if not isinstance(value, str) or not value.startswith('/') or '\x00' in value:
            raise ValueError('Excluded paths must be absolute sandbox paths.')
        path = PurePosixPath('/' + value.lstrip('/'))
        if '..' in path.parts or str(path) == '/':
            raise ValueError('Excluded paths must be bounded directories or files without traversal.')
        paths.append(str(path))
    return sorted(set(paths))


async def _remote(sandbox: Any, payload: dict, user: str) -> dict:
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=True, allow_nan=False).encode()).decode()
    command = 'python3 -I -c ' + shlex.quote(_REMOTE) + ' ' + shlex.quote(encoded)
    try:
        result = await asyncio.wait_for(
            sandbox.exec(command, user=user, timeout_sec=LIMITS['timeout_seconds']),
            timeout=LIMITS['timeout_seconds'] + 5,
        )
    except TimeoutError:
        return {'error': 'Inspection timed out; no observation was recorded.'}
    except Exception:
        return {'error': 'Sandbox inspection command failed; no observation was recorded.'}
    get = result.get if isinstance(result, dict) else lambda key, default=None: getattr(result, key, default)
    if get('return_code', get('exit_code', 0)) != 0:
        return {'error': 'Sandbox Python inspection process exited unsuccessfully.'}
    stdout = get('stdout', '')
    if not isinstance(stdout, str) or len(stdout) > 2 * LIMITS['max_file_bytes'] + 8 * 1024 * 1024:
        return {'error': 'Inspection response is absent or exceeds the transport limit.'}
    try:
        value = json.loads(stdout)
        if not isinstance(value, dict):
            raise ValueError
        if 'error' in value and not isinstance(value['error'], str):
            raise ValueError
        if payload['action'] == 'scan' and 'error' not in value:
            if (not isinstance(value.get('resources'), list)
                    or not isinstance(value.get('limitations'), list)
                    or not all(isinstance(note, str) for note in value['limitations'])):
                raise ValueError
            for item in value['resources']:
                if (not isinstance(item, dict)
                        or not all(isinstance(item.get(key), str) for key in ('path', 'root', 'relative_path', 'source_role', 'kind'))
                        or item['root'] not in payload['roots']
                        or item['kind'] not in ('directory', 'file')
                        or item['source_role'] not in ('workspace', 'skills')
                        or ('sha256' in item and not isinstance(item['sha256'], str))):
                    raise ValueError
        return value
    except (ValueError, TypeError):
        return {'error': 'Sandbox inspection returned an invalid response.'}


async def snapshot_file_state(sandbox: Any, workspace: str, skills_dir: str | None,
                              user: str = 'agent', *, excluded_paths: tuple[str, ...] = ()) -> dict:
    """Capture bounded hashes plus any gaps, before execution or after verification.

    Callers decide when this snapshot is taken. Only a snapshot taken before
    execution can establish initial file availability. Missing hashes never do.
    """
    response = await _remote(sandbox, {'action': 'scan', 'roots': _roots(workspace, skills_dir),
                                      'limits': LIMITS, 'hash_files': True,
                                      'excluded_paths': _excluded_paths(excluded_paths)}, user)
    limitations = list(response.get('limitations', []))
    if response.get('error'):
        limitations.append(response['error'])
    hashes = {item['path']: item['sha256'] for item in response.get('resources', []) if 'sha256' in item}
    return {'hashes': hashes, 'limitations': limitations, 'limits': dict(LIMITS),
            'excluded_paths': _excluded_paths(excluded_paths)}


async def snapshot_initial_files(sandbox: Any, workspace: str, skills_dir: str | None,
                                 user: str = 'agent', *, excluded_paths: tuple[str, ...] = ()) -> dict[str, str]:
    """Compatibility helper; prefer snapshot_file_state to persist limitations."""
    state = await snapshot_file_state(sandbox, workspace, skills_dir, user, excluded_paths=excluded_paths)
    if state['limitations']:
        warnings.warn('Initial file snapshot is incomplete: ' + '; '.join(state['limitations']),
                      RuntimeWarning, stacklevel=2)
    return state['hashes']


_PARAMS = {
    'inspect_list': {'resource_id': ('string', None), 'offset': ('integer', 0), 'limit': ('integer', 100)},
    'read_text': {'resource_id': ('string', ...), 'start_line': ('integer', 1), 'max_lines': ('integer', 200)},
    'search_text': {'resource_id': ('string', ...), 'query': ('string', ...), 'max_matches': ('integer', 50)},
    'pdf_info': {'resource_id': ('string', ...)},
    'read_pdf': {'resource_id': ('string', ...), 'start_page': ('integer', 1), 'max_pages': ('integer', 3)},
    'search_pdf': {'resource_id': ('string', ...), 'query': ('string', ...), 'start_page': ('integer', 1), 'max_pages': ('integer', 20), 'max_matches': ('integer', 50)},
    'view_image': {'resource_id': ('string', ...), 'page': ('integer', 1)},
}


class LiveInspector:
    def __init__(self, sandbox: Any, workspace: str, skills_dir: str | None, output: Path,
                 initial_hashes: dict[str, str], user: str = 'agent', secrets: tuple = (),
                 *, excluded_paths: tuple[str, ...] = ()):
        self.sandbox, self.roots, self.output = sandbox, _roots(workspace, skills_dir), Path(output)
        self.excluded_paths = _excluded_paths(excluded_paths)
        self.initial_hashes, self.user, self.secrets = dict(initial_hashes), user, tuple(secrets)
        self.resources: dict[str, dict] = {}
        self.catalog: dict | None = None
        self._number = 0
        self._lock = asyncio.Lock()

    async def prepare(self) -> dict:
        if self.catalog is not None:
            return self.catalog
        response = await _remote(self.sandbox, {'action': 'scan', 'roots': self.roots, 'limits': LIMITS,
                                               'excluded_paths': self.excluded_paths}, self.user)
        visible = []
        for index, item in enumerate(response.get('resources', []), 1):
            identifier = f'R{index:06d}'
            self.resources[identifier] = item
            entry = {'resource_id': identifier,
                     **{key: item[key] for key in ('relative_path', 'source_role', 'kind', 'size') if key in item}}
            if item['kind'] == 'file':
                entry.update(initial_hash_recorded=bool(self.initial_hashes.get(item['path'])),
                             availability_check='pending_observation')
            else:
                entry['availability_check'] = 'not_applicable'
            visible.append(entry)
        limitations = response.get('limitations', []) + [
            'Catalog excludes symlinks and known oracle/private paths. Oversized files may be listed but cannot be read; absence from this bounded catalog does not prove nonexistence.',
            'Catalog files have not yet been observed. initial_hash_recorded reports whether a pre-execution hash exists; pending_observation means a read and hash comparison is still needed, not that the baseline is missing or the file cannot be inspected.',
            'Initial availability requires a matching pre-execution hash and never proves the execution Agent read the file.',
        ]
        if response.get('error'):
            limitations.append(response['error'])
        if self.excluded_paths:
            limitations.append('Benchmark-specific excluded paths are omitted from this catalog and all observations.')
        self.catalog = redact({'resources': visible, 'limits': LIMITS, 'limitations': limitations}, self.secrets)
        write_json(self.output / 'catalog.json', self.catalog)
        return self.catalog

    def definitions(self) -> list[dict]:
        descriptions = {
            'inspect_list': 'List a page of the bounded catalog, optionally direct children of a directory resource. Never accepts a path.',
            'read_text': 'Read numbered UTF-8 text lines from one file and record an artifact observation.',
            'search_text': 'Case-sensitive literal search in one UTF-8 file; records the complete source snapshot and bounded matches.',
            'pdf_info': 'Read PDF page count and available metadata; requires PyMuPDF or Poppler.',
            'read_pdf': 'Extract a bounded range of PDF pages; extraction is not visual inspection or OCR.',
            'search_pdf': 'Case-sensitive literal search over a bounded PDF page range; scanned images may have no text.',
            'view_image': 'Render one PDF page or preview an image as PNG; records source and image provenance.',
        }
        return [{'type': 'function', 'function': {'name': name, 'description': descriptions[name],
                 'parameters': {'type': 'object', 'properties': {key: {'type': kind} for key, (kind, _) in fields.items()},
                                'required': [key for key, (_, default) in fields.items() if default is ...], 'additionalProperties': False}}}
                for name, fields in _PARAMS.items()]

    def _args(self, name: str, args: dict) -> dict:
        if name not in _PARAMS or not isinstance(args, dict) or set(args) - _PARAMS[name].keys():
            raise ValueError('Unknown inspection tool or unsupported arguments.')
        result = {}
        maxima = {'limit': 200, 'max_lines': LIMITS['max_lines'], 'max_matches': LIMITS['max_matches'], 'max_pages': LIMITS['max_pdf_pages']}
        for key, (kind, default) in _PARAMS[name].items():
            value = args.get(key, default)
            if value is ...:
                raise ValueError('Required inspection argument is missing.')
            if value is None and key == 'resource_id' and name == 'inspect_list':
                continue
            if kind == 'string' and (not isinstance(value, str) or not value or len(value) > 1000):
                raise ValueError('Inspection string argument is invalid.')
            if kind == 'integer' and (type(value) is not int or value < (0 if key == 'offset' else 1) or value > maxima.get(key, 1000000)):
                raise ValueError('Inspection numeric argument is outside the supported range.')
            result[key] = value
        return result

    async def call(self, name: str, args: dict) -> dict:
        async with self._lock:
            try:
                parsed = self._args(name, args)
                if self.catalog is None:
                    await self.prepare()
                if name == 'inspect_list':
                    return self._list(parsed)
                resource = self.resources.get(parsed['resource_id'])
                if resource is None or resource['kind'] != 'file':
                    raise ValueError('Inspection requires a catalog file resource ID.')
                response = await _remote(self.sandbox, {'action': name, 'roots': self.roots, 'limits': LIMITS,
                                                       'path': resource['path'], 'root': resource['root'], 'args': parsed,
                                                       'excluded_paths': self.excluded_paths}, self.user)
                if response.get('error'):
                    return self._error(response['error'])
                return self._record(name, parsed, resource, response)
            except (ValueError, OSError) as exc:
                # OSError may contain a host path; return only a safe diagnosis.
                return self._error(str(exc) if type(exc) is ValueError else 'Could not preserve the inspection artifact.')

    def _error(self, message: str) -> dict:
        cleaned = redact(message, self.secrets)
        return {'text': cleaned, 'error': cleaned, 'evidence': [], 'images': [], 'truncated': False}

    def _list(self, args: dict) -> dict:
        entries = list(self.catalog['resources'])
        if 'resource_id' in args:
            parent = self.resources.get(args['resource_id'])
            if parent is None or parent['kind'] != 'directory':
                raise ValueError('Listing requires a catalog directory resource ID.')
            entries = [entry for entry in entries if str(PurePosixPath(self.resources[entry['resource_id']]['path']).parent) == parent['path']]
        selected = entries[args['offset']:args['offset'] + args['limit']]
        truncated = args['offset'] + len(selected) < len(entries)
        return {'text': json.dumps({'resources': selected, 'total_in_catalog_scope': len(entries), 'truncated': truncated,
                                    'limitations': self.catalog['limitations']}, ensure_ascii=False),
                'evidence': [], 'images': [], 'truncated': truncated}

    def _record(self, name: str, args: dict, resource: dict, response: dict) -> dict:
        if (not isinstance(response.get('source_base64'), str)
                or not isinstance(response.get('sha256'), str)
                or not isinstance(response.get('text'), str)
                or type(response.get('truncated')) is not bool
                or ('image_base64' in response and not isinstance(response['image_base64'], str))):
            raise ValueError('Inspection observation has an invalid response shape.')
        data = base64.b64decode(response['source_base64'], validate=True)
        if len(data) > LIMITS['max_file_bytes'] or len(response['text']) > LIMITS['max_text_chars']:
            raise ValueError('Inspection observation exceeds the supported limits.')
        source_hash = 'sha256:' + hashlib.sha256(data).hexdigest()
        if source_hash != response['sha256']:
            raise ValueError('Inspection source hash mismatch; observation was not recorded.')
        text = response['text']
        # Reject rather than preserve an unredacted original whose contents are
        # already known to contain a credential. Do not rewrite original bytes.
        if redact(text, self.secrets) != text or any(secret and secret.encode() in data for secret in self.secrets):
            raise ValueError('Inspection content contains a known secret; observation was not recorded.')
        initial = self.initial_hashes.get(resource['path'])
        if initial and not initial.startswith('sha256:'):
            initial = 'sha256:' + initial
        availability = 'initial' if initial == source_hash else ('post_execution' if initial else 'unknown')
        image_data = None
        if 'image_base64' in response:
            image_data = base64.b64decode(response['image_base64'], validate=True)
            if not image_data.startswith(b'\x89PNG\r\n\x1a\n') or len(image_data) > 8 * 1024 * 1024:
                raise ValueError('Image preview is invalid or exceeds the byte limit.')
        self._number += 1
        identifier = f'A{self._number:06d}'
        suffix = PurePosixPath(resource['path']).suffix
        if len(suffix) > 12 or not suffix.lstrip('.').isalnum():
            suffix = '.bin'
        local_source = self.output / 'artifacts' / (source_hash.split(':')[1] + suffix)
        local_source.parent.mkdir(parents=True, exist_ok=True)
        local_source.write_bytes(data)
        images = []
        if image_data is not None:
            image_path = self.output / 'artifacts' / f'{identifier}.png'
            image_path.write_bytes(image_data)
            images.append(image_path)
        provenance = {key: value for key, value in response.items() if key not in {'source_base64', 'image_base64', 'text'}}
        provenance.update(tool=name, arguments=args, original_path=resource['path'], local_source=str(local_source),
                          initial_sha256=initial, source_sha256=source_hash, availability=availability,
                          observed_at=now(),
                          images=[{'path': str(path), 'sha256': 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()} for path in images])
        provenance = redact(provenance, self.secrets)
        evidence = EvidenceItem(identifier, 'artifact', text, str(local_source),
                                relative_path=redact(resource['relative_path'], self.secrets), version=source_hash,
                                availability=availability, source_role=resource['source_role'], observation=provenance)
        write_json(self.output / 'observations' / f'{identifier}.json', asdict(evidence))
        # The caller serializes canonical evidence[] into model messages. Keep
        # this summary short so full observations are not sent twice.
        rendered = (f"{name} observed {args['resource_id']} as {identifier}; "
                    f"truncated={bool(response['truncated'])}. Content and provenance are in evidence.")
        return {'text': rendered, 'evidence': [evidence], 'images': images, 'truncated': bool(response['truncated'])}
