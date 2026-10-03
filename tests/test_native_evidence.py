"""Actual rsync snapshot restore/hash checks; no private provider records."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import os

import pytest

from steward_harness.runtime import native_evidence


def test_versioned_archive_restores_records_after_worktree_reset(tmp_path):
    home = tmp_path / 'native' / '.steward-launch-fixture'
    home.mkdir(parents=True, mode=0o700)
    records = tmp_path / 'worktree' / 'projects'
    records.mkdir(parents=True)
    (home / 'projects').symlink_to(records)
    with sqlite3.connect(home / 'queue.sqlite') as database:
        database.execute('CREATE TABLE queue (research TEXT)')
        database.execute("INSERT INTO queue VALUES ('unique queued research')")
    (records / 'session.jsonl').write_bytes(b'old session and tool events\n')
    child = records / 'session' / 'subagents'
    child.mkdir(parents=True)
    (child / 'child.jsonl').write_bytes(b'child result\n')
    first = native_evidence.snapshot(home, {'projects': records})
    assert json.loads((first / 'sources.json').read_text())['roots'] == {
        'home': str(home), 'records/projects': str(records)}
    (records / 'session.jsonl').write_bytes(b'new session version\n')
    second = native_evidence.snapshot(home, {'projects': records})
    assert first != second and first.exists()
    # Model a Git reset/disposal of the mapped candidate after archiving.
    shutil.rmtree(records)
    restored = tmp_path / 'restore'
    restored.mkdir()
    expected = json.loads((first / 'SHA256.json').read_text())
    for name, digest in expected.items():
        target = restored / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(first / name, target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest
    with sqlite3.connect(restored / 'home/queue.sqlite') as database:
        assert database.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert database.execute('SELECT research FROM queue').fetchall() == [('unique queued research',)]
    assert (restored / 'records/projects/session.jsonl').read_bytes() == b'old session and tool events\n'
    assert (restored / 'records/projects/session/subagents/child.jsonl').read_bytes() == b'child result\n'
    assert (first / 'SHA256.json').stat().st_mode & 0o777 == 0o600
    assert first.parent.stat().st_mode & 0o777 == 0o700


def test_low_storage_refuses_snapshot_without_deleting_any_source(tmp_path, monkeypatch):
    home = tmp_path / 'native'
    home.mkdir()
    source = home / 'memory.md'
    source.write_text('unique memory')
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(native_evidence.shutil, 'disk_usage', lambda _: type(usage)(usage.total, usage.total - 1, 1))
    with pytest.raises(RuntimeError, match='storage reserve reached'):
        native_evidence.snapshot(home, {})
    assert source.read_text() == 'unique memory'
    assert not list(tmp_path.rglob('*.snapshot'))


def test_corrupt_archive_is_not_promoted_or_used_to_delete_sources(tmp_path, monkeypatch):
    home = tmp_path / 'native'
    home.mkdir()
    (home / 'session').write_text('original')
    original = native_evidence.subprocess.run

    def corrupt(command, **kwargs):
        result = original(command, **kwargs)
        (Path(command[-1]) / 'session').write_bytes(b'broken snapshot')
        return result

    monkeypatch.setattr(native_evidence.subprocess, 'run', corrupt)
    with pytest.raises(RuntimeError, match='verification failed'):
        native_evidence.snapshot(home, {})
    assert (home / 'session').read_text() == 'original'
    assert not list(tmp_path.rglob('*.snapshot'))
    assert list(tmp_path.rglob('*.partial'))


def test_unchanged_backup_files_share_storage_but_never_source_inodes(tmp_path):
    home = tmp_path / ('.steward-owner-' + '1' * 32 + '-' + '2' * 16)
    home.mkdir()
    source = home / 'evidence'
    source.write_bytes(b'old')
    first = native_evidence.snapshot(home, {})
    original_time = source.stat().st_mtime_ns
    os.utime(source, ns=(original_time + 10**9, original_time + 10**9))
    second = native_evidence.snapshot(home, {})
    assert (first / 'home/evidence').stat().st_ino == (second / 'home/evidence').stat().st_ino
    assert source.stat().st_ino != (second / 'home/evidence').stat().st_ino
    # A same-length edit with the same mtime must not reuse the prior inode.
    before = source.stat()
    source.write_bytes(b'new')
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    third = native_evidence.snapshot(home, {})
    assert (first / 'home/evidence').read_bytes() == b'old'
    assert (third / 'home/evidence').read_bytes() == b'new'
    assert (third / 'home/evidence').stat().st_ino != (first / 'home/evidence').stat().st_ino
    # Source deletions never propagate into any previous version.
    source.unlink()
    fourth = native_evidence.snapshot(home, {})
    assert not (fourth / 'home/evidence').exists()
    assert (third / 'home/evidence').read_bytes() == b'new'


def test_same_owner_generation_reuses_backup_files_without_cross_owner_links(tmp_path):
    def home(owner, generation):
        path = tmp_path / ('.steward-owner-' + owner * 32 + '-' + generation * 16)
        path.mkdir()
        (path / 'cache').write_bytes(b'identical catalog')
        return path
    first = native_evidence.snapshot(home('1', '2'), {})
    second = native_evidence.snapshot(home('1', '3'), {})
    other = native_evidence.snapshot(home('4', '3'), {})
    assert (first / 'home/cache').stat().st_ino == (second / 'home/cache').stat().st_ino
    assert (other / 'home/cache').stat().st_ino != (second / 'home/cache').stat().st_ino
    first_source = json.loads((first / 'sources.json').read_text())['roots']['home']
    second_source = json.loads((second / 'sources.json').read_text())['roots']['home']
    assert first_source != second_source
    assert first_source.endswith('2' * 16) and second_source.endswith('3' * 16)


def test_seed_snapshot_excludes_managed_owners_and_its_own_store(tmp_path):
    home = tmp_path / 'seed'
    home.mkdir()
    (home / 'sessions').mkdir()
    (home / 'sessions/seed.jsonl').write_text('seed evidence')
    for name in ['.steward-owner-private', '.steward-launch-private', '.steward-read-scopes']:
        path = home / name
        path.mkdir()
        (path / 'private').write_text('another scope')
    first = native_evidence.snapshot(home, {}, seed=True)
    second = native_evidence.snapshot(home, {}, seed=True)
    assert (second / 'home/sessions/seed.jsonl').read_text() == 'seed evidence'
    assert not list((second / 'home').glob('.steward-*'))
    assert first.exists()
    assert (home / '.steward-owner-private/private').read_text() == 'another scope'
