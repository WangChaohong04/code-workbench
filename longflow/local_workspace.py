"""Server-selected local workspace and external, durable Git snapshots.

Caller authenticates approvals and protects/serializes both roots against hostile
local writers. Portable checks cannot defeat directory replacement races. Apply
is journaled, not multi-file atomic. Failures block applies until explicit recover.
No source Git operations, user commands, hooks or candidate execution occur.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .code_workspace import (CodeWorkspaceError, CodeWorkspaceService, _checked,
                             _digest, _fail, _relative)


class LocalWorkspace(CodeWorkspaceService):
    """Existing absolute source/storage roots; metadata binds storage to source.

    check_write() raises when writes are forbidden (True also means frozen).
    Every source mutation, including staging and recovery, invokes this callback.
    Matching hashes are version bindings, NOT approval authentication.
    """

    def __init__(self, source, storage, *, max_total_bytes=16 * 1024 * 1024,
                 max_files=2000, max_file_bytes=1024 * 1024):
        super().__init__(source, server_owned_base=storage,
                         max_total_bytes=max_total_bytes, max_files=max_files)
        if max_file_bytes <= 0:
            _fail('invalid_limit', 'File limit must be positive')
        self.max_file_bytes = max_file_bytes
        self.source, self.storage = self.source_root, self.base
        self._lock = threading.RLock()
        self.repo = self.storage / 'snapshots.git'
        self.metadata = self.storage / 'workspace.json'
        self.journal = self.storage / 'transaction.json'
        self.candidates = self.storage / 'candidates'
        limits = [max_total_bytes, max_files, max_file_bytes]
        if self.metadata.exists():
            record = self._load(self.metadata)
            if record.get('source') != str(self.source) or record.get('schema') != 1 or record.get('limits') != limits:
                _fail('workspace_mismatch', 'Storage association or limits differ')
            _checked(self.repo)
            _checked(self.candidates)
            if not self.versions():
                _fail('snapshot_failed', 'Baseline snapshot missing')
        else:
            if self.repo.exists() or self.candidates.exists():
                _fail('workspace_mismatch', 'Unassociated snapshot storage exists')
            files = self._scan(self.source)
            self.candidates.mkdir()
            self._git('init', '--bare', '--template=', str(self.repo), init=True)
            self._snapshot(files, 'Initial baseline')
            self._save(self.metadata, {'schema': 1, 'source': str(self.source),
                                      'limits': limits, 'created_at': self._now()})

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _excluded(self, name):
        return super()._excluded(name) or any(
            p in {'secrets', 'secret', '.ssh', '.aws', '.azure', '.gnupg',
                  'credentials', 'credentials.json', 'secrets.json', 'secrets.yaml'}
            or p.startswith('.local-workspace-txn-') for p in name.casefold().split('/'))

    def _text(self, content):
        if len(content) > self.max_file_bytes:
            _fail('resource_limit', 'Source file exceeds file limit')
        try:
            text = content.decode('utf-8')
        except UnicodeError as exc:
            raise CodeWorkspaceError('invalid_encoding', 'Only UTF-8 source files supported') from exc
        if '\x00' in text:
            _fail('invalid_encoding', 'Binary content is not supported')
        return text

    def _scan(self, root, *, candidate=False):
        files = super()._scan(root, candidate=candidate)
        for content in files.values():
            self._text(content)
        return files

    def _changes(self, files, changes):
        try:
            result = super()._changes(files, changes)
        except UnicodeError as exc:
            raise CodeWorkspaceError('invalid_encoding', 'Changes must be valid UTF-8') from exc
        for content in result.values():
            self._text(content)
        return result

    def _load(self, path):
        _checked(path)
        if path.stat().st_size > self.max_total_bytes * 14 + 1024 * 1024:
            _fail('resource_limit', 'Stored record exceeds limit')
        try:
            with path.open('r', encoding='utf-8') as stream:
                result = json.load(stream)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError) as exc:
            raise CodeWorkspaceError('storage_corrupt', 'Invalid workspace record') from exc

    def _save(self, path, record):
        _checked(path.parent)
        if path.exists() or path.is_symlink():
            _checked(path)
        temp = path.parent / ('.record-' + uuid.uuid4().hex)
        try:
            with temp.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(record, stream, sort_keys=True, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _git(self, *args, data=None, init=False):
        _checked(self.storage)
        if not init:
            _checked(self.repo)
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
        env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': os.devnull,
                    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
                    'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '0',
                    'GIT_AUTHOR_NAME': 'Local Workspace', 'GIT_AUTHOR_EMAIL': 'workspace@localhost',
                    'GIT_COMMITTER_NAME': 'Local Workspace', 'GIT_COMMITTER_EMAIL': 'workspace@localhost'})
        command = ['git', '-c', 'core.hooksPath=' + str(self.storage / 'disabled-hooks'),
                   '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=' + os.devnull,
                   '-c', 'commit.gpgSign=false', '-c', 'core.autocrlf=false',
                   '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
        if not init:
            command += ['--git-dir=' + str(self.repo)]
        try:
            result = subprocess.run(command + list(args), input=data, capture_output=True,
                                    env=env, cwd=self.storage, timeout=30, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeWorkspaceError('snapshot_failed', 'Git unavailable for snapshot operation') from exc
        if result.returncode:
            _fail('snapshot_failed', 'Git snapshot operation failed')
        return result.stdout

    @staticmethod
    def _oid(raw):
        value = raw.decode('ascii').strip()
        if not re.fullmatch(r'[0-9a-f]{40,64}', value):
            _fail('snapshot_failed', 'Invalid Git identity')
        return value

    def _snapshot(self, files, message):
        tree = {}
        for name, content in sorted(files.items()):
            node = tree
            parts = name.split('/')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = self._oid(self._git('hash-object', '-w', '--stdin', data=content))

        def write_tree(node):
            entries = []
            for name, value in sorted(node.items()):
                mode, kind, oid = ('040000', 'tree', write_tree(value)) if isinstance(value, dict) else ('100644', 'blob', value)
                entries.append(f'{mode} {kind} {oid}\t{name}'.encode('utf-8') + b'\x00')
            return self._oid(self._git('mktree', '-z', data=b''.join(entries)))

        args = ['commit-tree', write_tree(tree)]
        parents = self._git('rev-list', '--all').decode('ascii').splitlines()
        if parents:
            args += ['-p', parents[0]]
        commit = self._oid(self._git(*args, data=(message + '\n').encode('utf-8')))
        self._git('update-ref', 'refs/heads/snapshots', commit)
        return commit

    def info(self):
        with self._lock:
            return {'source': str(self.source), 'storage': str(self.storage),
                    'source_hash': _digest(self._manifest(self._scan(self.source))),
                    'recovery_required': self.journal.exists(), 'execution_supported': False,
                    'data_restore_supported': False}

    def list_files(self):
        with self._lock:
            return sorted(self._scan(self.source))

    def read_file(self, path):
        path = _relative(path)
        if self._excluded(path):
            _fail('unsafe_path', 'Excluded path')
        with self._lock:
            files = self._scan(self.source)
            if path not in files:
                _fail('not_found', 'Source file not found')
            return self._text(files[path])

    @staticmethod
    def _encode(files):
        return {k: v.decode('utf-8') for k, v in files.items()}

    def _decode(self, value):
        if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
            _fail('storage_corrupt', 'Invalid stored tree')
        return self._changes({}, value)

    def _record(self, candidate_id):
        if not isinstance(candidate_id, str) or not re.fullmatch(r'[0-9a-f]{32}', candidate_id):
            _fail('not_found', 'Unknown candidate')
        path = self.candidates / (candidate_id + '.json')
        if not path.exists():
            _fail('not_found', 'Unknown candidate')
        record = self._load(path)
        before, after = self._decode(record.get('before')), self._decode(record.get('after'))
        if record.get('source_hash') != _digest(self._manifest(before)) or record.get('candidate_hash') != _digest(self._manifest(after)):
            _fail('candidate_conflict', 'Stored candidate content changed')
        return record, before, after

    def _diff(self, before, after):
        changed, patches = [], []
        for name in sorted(before.keys() | after.keys()):
            if before.get(name) == after.get(name) and (name in before) == (name in after):
                continue
            changed.append(name)
            old_label = 'a/' + name if name in before else '/dev/null'
            new_label = 'b/' + name if name in after else '/dev/null'
            lines = list(difflib.unified_diff(self._text(before.get(name, b'')).splitlines(keepends=True),
                         self._text(after.get(name, b'')).splitlines(keepends=True), old_label, new_label))
            if not lines:
                lines = ['--- ' + old_label + '\n', '+++ ' + new_label + '\n']
            patches.extend(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines)
        return {'changed_files': changed, 'unified_diff': ''.join(patches)}

    def _create(self, before, after, restored_commit=None):
        candidate_id = uuid.uuid4().hex
        record = {'schema': 1, 'candidate_id': candidate_id, 'created_at': self._now(),
                  'before': self._encode(before), 'after': self._encode(after),
                  'source_hash': _digest(self._manifest(before)),
                  'candidate_hash': _digest(self._manifest(after)), 'restored_commit': restored_commit}
        self._save(self.candidates / (candidate_id + '.json'), record)
        return self.preview(candidate_id)

    def create_candidate(self, changes=None):
        with self._lock:
            before = self._scan(self.source)
            after = self._changes(before, {} if changes is None else changes)
            if self._scan(self.source) != before:
                _fail('source_conflict', 'Source changed during candidate creation')
            return self._create(before, after)

    def preview(self, candidate_id):
        with self._lock:
            record, before, after = self._record(candidate_id)
            return {'candidate_id': candidate_id, 'created_at': record['created_at'],
                    'source_hash': record['source_hash'], 'candidate_hash': record['candidate_hash'],
                    'base_sha': record['source_hash'], 'candidate_sha': record['candidate_hash'],
                    'before_manifest': self._manifest(before), 'after_manifest': self._manifest(after),
                    'restored_commit': record.get('restored_commit'), 'execution_supported': False,
                    **self._diff(before, after)}

    @staticmethod
    def _allow(check_write):
        if check_write() is True:
            _fail('frozen', 'Workspace writes frozen')

    def _target(self, name):
        target = self.source / _relative(name)
        _checked(self.source)
        for node in reversed(target.parents):
            if node == self.source or self.source in node.parents:
                if node.exists() or node.is_symlink():
                    _checked(node)
                    if not node.is_dir():
                        _fail('unsafe_path', 'Parent is not a directory')
        if target.exists() or target.is_symlink():
            _checked(target)
            if not target.is_file():
                _fail('unsafe_path', 'Target is not a regular file')
        return target

    def _write_source(self, name, content, journal, check_write):
        target = self._target(name)
        missing, parent = [], target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            journal['directories'].append(directory.relative_to(self.source).as_posix())
            self._save(self.journal, journal)
            _checked(directory.parent)
            self._allow(check_write)
            directory.mkdir()
        if content is None:
            if target.exists():
                self._target(name)
                self._allow(check_write)
                target.unlink()
            return
        temp = target.parent / ('.local-workspace-txn-' + uuid.uuid4().hex)
        journal['temps'].append(temp.relative_to(self.source).as_posix())
        self._save(self.journal, journal)
        _checked(temp.parent)
        self._allow(check_write)
        with temp.open('xb', buffering=0) as stream:
            self._allow(check_write)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        self._target(name)
        _checked(temp)
        self._allow(check_write)
        os.replace(temp, target)

    def apply(self, candidate_id, expected_source_hash, expected_candidate_hash, check_write=lambda: None):
        """Apply version-bound changes. Failures require explicit recovery."""
        with self._lock:
            if self.journal.exists():
                _fail('recovery_required', 'Unfinished transaction requires recovery')
            record, before, after = self._record(candidate_id)
            if not expected_candidate_hash or expected_candidate_hash != record['candidate_hash']:
                _fail('candidate_conflict', 'Candidate hash mismatch')
            if expected_source_hash != record['source_hash'] or self._scan(self.source) != before:
                _fail('source_conflict', 'Entire source must match candidate base')
            changed = self._diff(before, after)['changed_files']
            for name in changed:
                self._target(name)
            journal = {'schema': 1, 'candidate_id': candidate_id, 'before': record['before'],
                       'after': record['after'], 'temps': [], 'directories': [],
                       'created_at': self._now(), 'phase': 'writing'}
            self._save(self.journal, journal)
            try:
                for name in changed:
                    self._write_source(name, after.get(name), journal, check_write)
                if self._scan(self.source) != after:
                    _fail('source_conflict', 'Source changed during apply')
                commit = self._snapshot(after, 'Confirmed candidate ' + candidate_id)
                journal.update(phase='committed', commit=commit)
                self._save(self.journal, journal)
                self.journal.unlink()
                return {'candidate_id': candidate_id, 'commit': commit,
                        'source_hash': record['candidate_hash'], 'changed_files': changed, 'atomic': False}
            except Exception as exc:
                raise CodeWorkspaceError('recovery_required',
                    'Apply interrupted; source may be partially changed; explicit recovery required') from exc

    def recovery_status(self):
        with self._lock:
            if not self.journal.exists():
                return {'required': False}
            journal = self._load(self.journal)
            return {'required': True, 'candidate_id': journal['candidate_id'],
                    'phase': journal['phase'], 'commit': journal.get('commit')}

    def recover(self, check_write=lambda: None):
        """Explicit admin rollback, or finalize committed journal; never automatic.

        External drift outside the known before/after trees prevents recovery.
        Caller must authorize this operation independently, just like apply.
        """
        with self._lock:
            if not self.journal.exists():
                return {'recovered': False}
            journal = self._load(self.journal)
            before, after = self._decode(journal['before']), self._decode(journal['after'])
            current = self._scan(self.source)
            for name in before.keys() | after.keys() | current.keys():
                if (name in current, current.get(name)) not in ((name in before, before.get(name)), (name in after, after.get(name))):
                    _fail('source_conflict', 'External drift prevents safe recovery')
            if journal['phase'] == 'committed':
                if current != after:
                    _fail('source_conflict', 'Committed source changed')
                self.journal.unlink()
                return {'recovered': True, 'action': 'finalized', 'commit': journal['commit']}
            for name in self._diff(current, before)['changed_files']:
                self._write_source(name, before.get(name), journal, check_write)
            for name in journal['temps']:
                temp = self._target(name)
                if temp.exists():
                    self._allow(check_write)
                    temp.unlink()
            for name in reversed(journal['directories']):
                directory = self.source / _relative(name)
                if directory.exists():
                    _checked(directory)
                    if not any(directory.iterdir()):
                        self._allow(check_write)
                        directory.rmdir()
            if self._scan(self.source) != before:
                _fail('source_conflict', 'Source changed during recovery')
            self.journal.unlink()
            return {'recovered': True, 'action': 'rolled_back', 'source_hash': _digest(self._manifest(before))}

    def _version_files(self, commit):
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40,64}', commit):
            _fail('not_found', 'Unknown snapshot commit')
        if commit not in self._git('rev-list', 'refs/heads/snapshots').decode('ascii').splitlines():
            _fail('not_found', 'Unknown snapshot commit')
        files, total = {}, 0
        for entry in filter(None, self._git('ls-tree', '-rz', '--full-tree', commit).split(b'\x00')):
            header, raw_name = entry.split(b'\t', 1)
            mode, kind, oid = header.split(b' ')
            name = _relative(raw_name.decode('utf-8'))
            if mode != b'100644' or kind != b'blob' or self._excluded(name):
                _fail('unsafe_path', 'Unsafe snapshot entry')
            object_id = self._oid(oid)
            size = int(self._git('cat-file', '-s', object_id))
            if size > self.max_file_bytes or total + size > self.max_total_bytes or len(files) >= self.max_files:
                _fail('resource_limit', 'Snapshot exceeds limits')
            content = self._git('cat-file', 'blob', object_id)
            self._text(content)
            files[name] = content
            total += len(content)
        return self._changes({}, self._encode(files))

    def versions(self):
        with self._lock:
            result = []
            for commit in self._git('rev-list', '--all').decode('ascii').splitlines():
                timestamp, message = self._git('show', '-s', '--format=%cI%n%s', commit).decode('utf-8').split('\n', 1)
                files = self._version_files(commit)
                result.append({'commit': commit, 'timestamp': timestamp, 'message': message.strip(),
                               'source_hash': _digest(self._manifest(files))})
            return result

    def read_version(self, commit, path):
        path = _relative(path)
        if self._excluded(path):
            _fail('unsafe_path', 'Excluded snapshot path')
        with self._lock:
            files = self._version_files(commit)
            if path not in files:
                _fail('not_found', 'Snapshot file not found')
            return self._text(files[path])

    def diff_versions(self, before_commit, after_commit):
        with self._lock:
            before, after = self._version_files(before_commit), self._version_files(after_commit)
            return {'before_commit': before_commit, 'after_commit': after_commit,
                    'source_hash': _digest(self._manifest(before)),
                    'candidate_hash': _digest(self._manifest(after)), **self._diff(before, after)}

    def restore_candidate(self, commit):
        with self._lock:
            before = self._scan(self.source)
            after = self._version_files(commit)
            if self._scan(self.source) != before:
                _fail('source_conflict', 'Source changed during restore preview')
            return self._create(before, after, restored_commit=commit)
