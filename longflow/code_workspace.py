"""Server-owned, copy-only code candidates; never an execution sandbox.

Only trusted server configuration may select roots/exclusions. The caller must keep
both trees inaccessible to concurrent untrusted local writers (portable Python
cannot defeat hostile directory-replacement races). No shell, candidate execution,
formal apply, or data restore is provided. Approval authentication belongs to the
API/governance layer; a matching hash alone is NOT an authorization credential.
Candidate identities are process-local and unknown identities fail closed.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from collections.abc import Mapping


class CodeWorkspaceError(ValueError):
    """A safe, machine-readable service error."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


_EXCLUDED = frozenset({
    '.git', '.hg', '.svn', 'data', 'knowledge', 'uploads', 'artifacts',
    '.lfwork', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache',
})
_SECRET_SUFFIXES = ('.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm',
                    '.sqlite-wal', '.sqlite-shm', '.pem', '.key', '.pyc')
_RESERVED = re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)', re.I)


def _fail(code: str, message: str):
    raise CodeWorkspaceError(code, message)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _digest(manifest: dict) -> str:
    return _sha(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value:
        _fail('unsafe_path', 'Paths must be nonempty relative POSIX paths')
    parts = value.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) or _RESERVED.match(p)
           or any(ord(c) < 32 or c in ':*?"<>|' for c in p) for p in parts):
        _fail('unsafe_path', 'Noncanonical or unsafe relative path')
    return value


def _checked(path: Path):
    """Check every ancestor, including Windows reparse points and hardlinks."""
    for node in (*reversed(path.parents), path):
        info = node.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            _fail('unsafe_path', 'Symlinks and reparse points are forbidden')
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            _fail('unsafe_path', 'Only regular files and directories are supported')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            _fail('unsafe_path', 'Hardlinked files are forbidden')
    return info


class CodeWorkspaceService:
    """Construct with existing absolute source and separate server-owned base.

    ``changes`` maps relative POSIX paths to UTF-8 text, or None for deletion.
    Entire included source trees (not only changed files) participate in conflict
    checks. Data is excluded, not versioned or restored. Config has its own hash.
    The copy root must be provisioned with private server ownership/ACLs externally.
    """

    def __init__(self, source_root: str | Path, *, server_owned_base: str | Path,
                 excluded_paths=(), max_total_bytes: int = 16 * 1024 * 1024,
                 max_files: int = 2000):
        self.source_root = self._root(source_root)
        self.base = self._root(server_owned_base)
        if (self.source_root == self.base or self.source_root in self.base.parents
                or self.base in self.source_root.parents):
            _fail('unsafe_path', 'Source and candidate base must not overlap')
        self.excluded_paths = tuple(_relative(p).casefold() for p in excluded_paths)
        if max_total_bytes <= 0 or max_files <= 0:
            _fail('invalid_limit', 'Resource limits must be positive')
        self.max_total_bytes = max_total_bytes
        self.max_files = max_files
        self._candidates: dict[str, dict] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _root(value):
        if not value:
            _fail('sandbox_unavailable', 'An explicit existing absolute root is required')
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts:
            _fail('unsafe_path', 'Root must be absolute without traversal')
        try:
            info = _checked(path)
            if not stat.S_ISDIR(info.st_mode):
                _fail('sandbox_unavailable', 'Root must be a directory')
        except OSError as exc:
            raise CodeWorkspaceError('sandbox_unavailable', 'Root unavailable') from exc
        return path

    def _excluded(self, name):
        parts = name.casefold().split('/')
        return (any(p in _EXCLUDED or p.startswith('.env') for p in parts)
                or name.casefold().endswith(_SECRET_SUFFIXES)
                or any(name.casefold() == p or name.casefold().startswith(p + '/')
                       for p in self.excluded_paths))

    def _scan(self, root: Path, *, candidate=False) -> dict[str, bytes]:
        result = {}
        total = 0
        seen = set()

        def visit(directory):
            nonlocal total
            _checked(directory)
            for child in sorted(directory.iterdir()):
                name = _relative(child.relative_to(root).as_posix())
                info = _checked(child)
                if self._excluded(name):
                    if candidate:
                        _fail('unsafe_path', 'Excluded content found in candidate')
                    continue
                folded = name.casefold()
                if folded in seen:
                    _fail('unsafe_path', 'Case-insensitive path collision')
                seen.add(folded)
                if len(seen) > self.max_files * 4:
                    _fail('resource_limit', 'Too many directory entries')
                if stat.S_ISDIR(info.st_mode):
                    visit(child)
                else:
                    if len(result) >= self.max_files or total + info.st_size > self.max_total_bytes:
                        _fail('resource_limit', 'Candidate size limit exceeded')
                    with child.open('rb') as stream:
                        content = stream.read(self.max_total_bytes - total + 1)
                        after = os.fstat(stream.fileno())
                    _checked(child)
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        _fail('source_conflict', 'File changed while being read')
                    total += len(content)
                    if total > self.max_total_bytes:
                        _fail('resource_limit', 'Candidate size limit exceeded')
                    result[name] = content
        visit(root)
        return result

    @staticmethod
    def _manifest(files):
        return {name: {'sha256': _sha(content), 'size': len(content),
                       'kind': 'config' if name.split('/')[0].casefold() == 'config'
                       or name.endswith(('.yaml', '.yml', '.toml', '.ini', '.cfg')) else 'code'}
                for name, content in sorted(files.items())}

    def _changes(self, files, changes):
        result = dict(files)
        if not isinstance(changes, Mapping):
            _fail('invalid_changes', 'Changes must be a mapping')
        for name, content in changes.items():
            name = _relative(name)
            if self._excluded(name):
                _fail('unsafe_path', 'Excluded paths cannot be edited')
            if content is None:
                result.pop(name, None)
            elif isinstance(content, str):
                result[name] = content.encode('utf-8')
            else:
                _fail('invalid_changes', 'Content must be text or null')
        names = set()
        spellings = {}
        for name in result:
            folded = name.casefold()
            if folded in names:
                _fail('unsafe_path', 'Case-insensitive path collision')
            names.add(folded)
            parts = name.split('/')
            for i in range(1, len(parts) + 1):
                prefix = '/'.join(parts[:i])
                previous = spellings.setdefault(prefix.casefold(), prefix)
                if previous != prefix:
                    _fail('unsafe_path', 'Case-insensitive directory collision')
        if any('/'.join(n.split('/')[:i]) in names
               for n in names for i in range(1, len(n.split('/')))):
            _fail('unsafe_path', 'File/directory path collision')
        if len(result) > self.max_files or sum(map(len, result.values())) > self.max_total_bytes:
            _fail('resource_limit', 'Candidate size limit exceeded')
        return result

    def _new_copy(self, files):
        # No in-place fallback, including if mkdir or an individual write fails.
        _checked(self.base)
        root = Path(tempfile.mkdtemp(prefix='candidate-', dir=self.base))
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _checked(target.parent)
            with target.open('xb') as stream:
                stream.write(content)
        return root

    def _record(self, candidate_id):
        if not isinstance(candidate_id, str) or candidate_id not in self._candidates:
            _fail('not_found', 'Unknown candidate')
        return self._candidates[candidate_id]

    def create_candidate(self, changes=None) -> dict:
        with self._lock:
            try:
                source = self._scan(self.source_root)
                candidate = self._changes(source, {} if changes is None else changes)
                root = self._new_copy(candidate)
                if self._scan(self.source_root) != source:
                    _fail('source_conflict', 'Source changed during candidate creation')
                candidate_id = root.name
                self._candidates[candidate_id] = {'root': root, 'source': source}
                return self.preview(candidate_id)
            except OSError as exc:
                raise CodeWorkspaceError('sandbox_unavailable', 'Candidate creation failed; source untouched') from exc

    def update_candidate(self, candidate_id, changes, *, expected_candidate_sha) -> dict:
        with self._lock:
            record = self._record(candidate_id)
            try:
                current = self._scan(record['root'], candidate=True)
                if _digest(self._manifest(current)) != expected_candidate_sha:
                    _fail('candidate_conflict', 'Candidate version changed')
                new_files = self._changes(current, changes)
                # Build a new complete revision before swapping the server-owned pointer.
                record['root'] = self._new_copy(new_files)
                return self.preview(candidate_id)
            except OSError as exc:
                raise CodeWorkspaceError('sandbox_unavailable', 'Candidate update failed; source untouched') from exc

    def preview(self, candidate_id) -> dict:
        with self._lock:
            record = self._record(candidate_id)
            try:
                files = self._scan(record['root'], candidate=True)
            except OSError as exc:
                raise CodeWorkspaceError('sandbox_unavailable', 'Candidate unavailable') from exc
            original = record['source']
            base_manifest = self._manifest(original)
            manifest = self._manifest(files)
            diffs = []
            for name in sorted(original.keys() | files.keys()):
                before, after = original.get(name, b''), files.get(name, b'')
                if before == after and (name in original) == (name in files):
                    continue
                old_label = 'a/' + name if name in original else '/dev/null'
                new_label = 'b/' + name if name in files else '/dev/null'
                try:
                    old, new = before.decode('utf-8'), after.decode('utf-8')
                    if '\x00' in old + new:
                        raise UnicodeError()
                    patch = list(difflib.unified_diff(old.splitlines(keepends=True),
                                 new.splitlines(keepends=True), old_label, new_label))
                    if not patch:  # Empty file creation/deletion still has an explicit diff.
                        patch = ['--- ' + old_label + '\n', '+++ ' + new_label + '\n']
                    diffs.extend(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
                                 for line in patch)
                except UnicodeError:
                    diffs.append(f'Binary files {old_label} and {new_label} differ\n')
            versions = {}
            for kind in ('code', 'config'):
                versions[kind] = {
                    'base_sha': _digest({k: v for k, v in base_manifest.items() if v['kind'] == kind}),
                    'candidate_sha': _digest({k: v for k, v in manifest.items() if v['kind'] == kind})}
            versions['data'] = {'status': 'excluded', 'restore_supported': False}
            return {'candidate_id': candidate_id, 'mode': 'copy', 'copy_path': str(record['root']),
                    'base_manifest': base_manifest, 'candidate_manifest': manifest,
                    'base_sha': _digest(base_manifest), 'candidate_sha': _digest(manifest),
                    'unified_diff': ''.join(diffs), 'versions': versions,
                    'execution_supported': False, 'apply_supported': False,
                    'capability': 'candidate_preview_only'}

    def preflight_apply(self, candidate_id, *, expected_source_sha, approved_candidate_sha) -> dict:
        """Check version binding only; never apply or mint approval.

        API must supply approved_candidate_sha from an authenticated, approved,
        candidate-specific governance record, never from an untrusted boolean.
        A passing preflight is not executable authorization or a race-free commit.
        """
        with self._lock:
            preview = self.preview(candidate_id)
            if not approved_candidate_sha or approved_candidate_sha != preview['candidate_sha']:
                _fail('approval_mismatch', 'Approval must bind the current candidate hash')
            if expected_source_sha != preview['base_sha']:
                _fail('source_conflict', 'Expected source version does not match the base')
            try:
                current_sha = _digest(self._manifest(self._scan(self.source_root)))
            except OSError as exc:
                raise CodeWorkspaceError('source_conflict', 'Source unavailable') from exc
            if current_sha != expected_source_sha:
                _fail('source_conflict', 'Source changed since candidate creation')
            return {'candidate_id': candidate_id, 'preflight_passed': True,
                    'source_sha': current_sha, 'candidate_sha': preview['candidate_sha'],
                    'apply_supported': False, 'execution_supported': False,
                    'capability': 'candidate_preview_only', 'reason': 'no_reliable_apply_executor',
                    'data_restore_supported': False}
