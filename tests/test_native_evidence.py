"""Native records reach Git from their home through the index; no private copies."""
import shutil
import subprocess

import pytest

from steward_harness.runtime import native_evidence


def _git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'world'
    path.mkdir()
    _git(path, 'init', '-q')
    return path


def test_capture_stages_records_at_world_paths_without_materializing_them(tmp_path, world):
    home = tmp_path / 'home'
    (home / 'projects/steward/session').mkdir(parents=True)
    (home / 'projects/steward/session.jsonl').write_bytes(b'tool events\n')
    (home / 'projects/steward/session/child.jsonl').write_bytes(b'child result\n')
    (home / 'projects/link').symlink_to(tmp_path)
    (home / 'state.sqlite').write_bytes(b'not a mapped record')
    count = native_evidence.capture(home, {'projects': 'artefacts/claude/projects'}, world)
    assert count == 2
    assert _git(world, 'ls-files', '-v').splitlines() == [
        'S artefacts/claude/projects/steward/session.jsonl',
        'S artefacts/claude/projects/steward/session/child.jsonl',
    ]
    assert _git(world, 'show', ':artefacts/claude/projects/steward/session/child.jsonl') == 'child result\n'
    assert not (world / 'artefacts').exists()
    # A record the provider later deletes stays in Git; capture only adds.
    (home / 'projects/steward/session/child.jsonl').unlink()
    (home / 'projects/steward/session.jsonl').write_bytes(b'tool events\nmore\n')
    native_evidence.capture(home, {'projects': 'artefacts/claude/projects'}, world)
    assert _git(world, 'show', ':artefacts/claude/projects/steward/session.jsonl') == 'tool events\nmore\n'
    assert 'artefacts/claude/projects/steward/session/child.jsonl' in _git(world, 'ls-files')
    _git(world, 'add', '--all')
    assert _git(world, 'diff', '--cached', '--diff-filter=D', '--name-only') == ''


def test_capture_of_an_absent_mapping_is_a_no_op(tmp_path, world):
    assert native_evidence.capture(tmp_path / 'missing', {'sessions': 'artefacts/codex/sessions'}, world) == 0
    assert _git(world, 'ls-files') == ''


def test_failed_capture_raises_and_leaves_sources(tmp_path, world):
    home = tmp_path / 'home'
    (home / 'sessions').mkdir(parents=True)
    (home / 'sessions/rollout.jsonl').write_text('original')
    (world / '.git/index.lock').write_text('another writer')
    with pytest.raises(RuntimeError, match='native record capture'):
        native_evidence.capture(home, {'sessions': 'artefacts/codex/sessions'}, world)
    assert (home / 'sessions/rollout.jsonl').read_text() == 'original'


def test_low_storage_refuses_new_native_work(tmp_path, monkeypatch):
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(native_evidence.shutil, 'disk_usage', lambda _: type(usage)(usage.total, usage.total - 1, 1))
    with pytest.raises(RuntimeError, match='storage reserve reached'):
        native_evidence.check_headroom([tmp_path])


def test_release_leaves_clean_records_to_git_and_keeps_local_changes(tmp_path, world):
    for path, text in [('artefacts/codex/a.jsonl', 'a'), ('artefacts/codex/b.jsonl', 'b'),
                       ('artefacts/codex/c.jsonl', 'c'), ('docs/kept.md', 'kept')]:
        (world / path).parent.mkdir(parents=True, exist_ok=True)
        (world / path).write_text(text)
    _git(world, 'add', '--all')
    _git(world, '-c', 'user.name=F', '-c', 'user.email=f@x', 'commit', '-qm', 'records')
    (world / 'artefacts/codex/b.jsonl').write_text('b, not yet committed')
    (world / 'artefacts/codex/c.jsonl').unlink()
    (world / 'artefacts/codex/new.json').write_text('untracked')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'x.jsonl').write_text('not the checkout\'s')
    assert native_evidence.leave_to_git(world) == 1
    assert not (world / 'artefacts/codex/a.jsonl').exists()
    assert (world / 'artefacts/codex/b.jsonl').read_text() == 'b, not yet committed'
    assert (world / 'artefacts/codex/new.json').exists()
    assert (world / 'docs/kept.md').read_text() == 'kept'
    flags = dict(line.split(' ', 1)[::-1] for line in _git(world, 'ls-files', '-v').splitlines())
    assert flags == {'artefacts/codex/a.jsonl': 'S', 'artefacts/codex/b.jsonl': 'H',
                     'artefacts/codex/c.jsonl': 'S', 'docs/kept.md': 'H'}
    _git(world, 'add', '--all')
    assert _git(world, 'diff', '--cached', '--name-status').splitlines() == [
        'M\tartefacts/codex/b.jsonl', 'A\tartefacts/codex/new.json']
    # Run again after commit: everything committed leaves the checkout.
    _git(world, '-c', 'user.name=F', '-c', 'user.email=f@x', 'commit', '-qm', 'turn')
    native_evidence.leave_to_git(world)
    assert not (world / 'artefacts').exists()
    assert _git(world, 'status', '--porcelain') == ''


def test_capture_removes_an_older_checkout_copy_of_a_home_record(tmp_path, world):
    home = tmp_path / 'home'
    (home / 'sessions').mkdir(parents=True)
    (home / 'sessions/rollout.jsonl').write_text('old\nnew\n')
    stale = world / 'artefacts/codex/sessions/rollout.jsonl'
    stale.parent.mkdir(parents=True)
    stale.write_text('old\n')
    native_evidence.capture(home, {'sessions': 'artefacts/codex/sessions'}, world)
    assert not stale.exists()
    native_evidence.leave_to_git(world)
    _git(world, 'add', '--all')
    assert _git(world, 'show', ':artefacts/codex/sessions/rollout.jsonl') == 'old\nnew\n'
