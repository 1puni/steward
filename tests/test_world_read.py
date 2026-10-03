import os
import subprocess
from datetime import UTC, datetime

import pytest

from steward_harness import world_read as wr


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    """An owner repository with a nested package, and a clone whose checkout falls behind."""
    owner, clone = tmp_path / "owner", tmp_path / "clone"
    owner.mkdir()
    _git(owner, "init", "-q", "-b", "main")
    _git(owner, "config", "user.email", "t@example.com")
    _git(owner, "config", "user.name", "t")
    files = {
        "README.md": "# Company\nSee `docs/papers/README.md`.\n",
        "docs/papers/README.md": "# The paper package\nRead [context](context.md) and `thesis/README.md`.\n",
        "docs/papers/context.md": "# Context\nfirst draft\n",
        "docs/papers/thesis/README.md": "# A separate thesis\n",
        "docs/loose.md": "no package\n",
        ".hidden/secret.md": "never listed\n",
    }
    for path, text in files.items():
        (owner / path).parent.mkdir(parents=True, exist_ok=True)
        (owner / path).write_text(text)
    os.symlink("/etc/passwd", owner / "docs/escape.md")
    _git(owner, "add", "-A")
    _git(owner, "commit", "-qm", "found")
    subprocess.run(["git", "clone", "-q", str(owner), str(clone)], check=True, capture_output=True)
    (owner / "docs/papers/context.md").write_text("# Context\nedited after review\n")
    subprocess.run(["git", "-C", str(owner), "commit", "-qam", "editorial pass"], check=True, capture_output=True,
                   env={**os.environ, "GIT_COMMITTER_DATE": "2030-01-01T00:00:00Z", "GIT_AUTHOR_DATE": "2030-01-01T00:00:00Z"})
    return owner, clone


def test_a_pin_is_one_exact_commit(world, tmp_path):
    _owner, clone = world
    with pytest.raises(ValueError):
        wr.pin_sha(clone, "main")
    assert wr.pin_ref(clone, "origin/nonexistent") is None
    pin = wr.pin_ref(clone, "origin/main")
    assert len(pin.sha) == 40 and pin.repository == clone


def test_reads_come_from_the_pin_even_when_the_ref_moves(world):
    owner, clone = world
    before = wr.pin_ref(clone, "origin/main")
    assert wr.fetch(clone, "origin").error is None
    after = wr.pin_ref(clone, "origin/main")
    assert after.sha != before.sha
    assert "first draft" in wr.read(before, "docs/papers/context.md")
    assert "edited after review" in wr.read(after, "docs/papers/context.md")
    assert wr.changes(before)["docs/papers/context.md"]["subject"] == "found"
    assert wr.changes(after)["docs/papers/context.md"]["subject"] == "editorial pass"
    assert (clone / "docs/papers/context.md").read_text() == "# Context\nfirst draft\n"  # checkout untouched


def test_a_failed_fetch_is_not_fresh_evidence(world, tmp_path):
    _owner, clone = world
    good = wr.fetch(clone, "origin")
    assert good.error is None and good.observed_at == good.attempted_at
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    failed = wr.fetch(clone, "origin", good)
    assert failed.error and failed.observed_at == good.observed_at and failed.attempted_at > good.attempted_at
    recovered = failed.after(failed.attempted_at, None)
    assert recovered.error is None and recovered.observed_at == failed.attempted_at


def test_tree_lists_only_regular_visible_files(world):
    owner, _clone = world
    pin = wr.pin_ref(owner, "main")
    paths = wr.tree(pin)
    assert "docs/escape.md" not in paths and ".hidden/secret.md" not in paths
    assert "docs/loose.md" in paths
    assert wr.tree(pin, (".json",)) == {}
    texts = wr.read_many(pin, ["README.md", "docs/missing.md"])
    assert list(texts) == ["README.md"]


def test_history_carries_diffs_and_counts(world):
    owner, _clone = world
    pin = wr.pin_ref(owner, "main")
    [newest, first] = wr.history(pin, "docs/papers/context.md")
    assert newest["subject"] == "editorial pass" and (newest["added"], newest["removed"]) == (1, 1)
    assert "+edited after review" in newest["diff"] and first["subject"] == "found"
    assert wr.changes(pin)["docs/papers/context.md"]["added"] == 1


def test_packages_are_discovered_from_readmes_newest_first(world):
    owner, _clone = world
    pin = wr.pin_ref(owner, "main")
    found = wr.packages(pin)
    assert [p["folder"] for p in found] == ["docs/papers", "docs/papers/thesis"]
    papers = found[0]
    assert papers["title"] == "The paper package"
    assert papers["documents"] == ["docs/papers/README.md", "docs/papers/context.md"]  # thesis is its own package
    assert papers["updated_at"] == wr.changes(pin)["docs/papers/context.md"]["at"]
    assert [p["folder"] for p in wr.packages(pin, skip=("docs/papers/thesis",))] == ["docs/papers"]


def test_configured_includes_select_paths():
    paths = ["README.md", "docs/a.md", "docs/deep/b.md", "notes/c.md"]
    assert wr.select(paths, ["*.md", "docs/*.md"]) == ["README.md", "docs/a.md"]
    assert wr.select(paths, ["docs/**"]) == ["docs/a.md", "docs/deep/b.md"]
    assert wr.select(paths, None) == sorted(paths)


def test_links_resolve_as_written_and_stay_inside_the_world(world):
    owner, _clone = world
    pin = wr.pin_ref(owner, "main")
    paths = wr.tree(pin)
    resolve = wr.resolver(list(paths) + ["evidence/sleep/voice-staging.md"])
    assert resolve("context.md", "docs/papers/README.md") == "docs/papers/context.md"
    assert resolve("artifacts/sleep/voice-staging.md", "README.md") == "evidence/sleep/voice-staging.md"
    assert resolve("voice-staging.md#top", "docs/loose.md") == "evidence/sleep/voice-staging.md"
    assert resolve("README.md", "docs/loose.md") == "README.md"  # from the root before guessing
    assert resolve("../../etc/passwd", "docs/loose.md") is None
    assert resolve("https://example.com/a.md", "README.md") is None
    edges = wr.link_edges(paths, wr.read_many(pin, [p for p in paths if p.endswith(".md")]))
    assert ("README.md", "docs/papers/README.md") in edges
    assert ("docs/papers/README.md", "docs/papers/context.md") in edges
    assert ("docs/papers/README.md", "docs/papers/thesis/README.md") in edges


def test_owner_links_are_credential_free_github_only(world):
    owner, _clone = world
    assert wr.owner_url("git@github.com:1puni/org-world.git") == "https://github.com/1puni/org-world"
    assert wr.owner_url("ssh://git@github.com/1puni/org-world") == "https://github.com/1puni/org-world"
    assert wr.owner_url("https://user:tok@github.com/1puni/org-world") is None
    assert wr.owner_url("/srv/world") is None
    pin = wr.pin_ref(owner, "main")
    assert wr.blob_url("https://github.com/o/r", pin, "a.md") == f"https://github.com/o/r/blob/{pin.sha}/a.md"
    assert wr.blob_url(None, pin, "a.md") is None


def test_direct_reads_refuse_nonregular_hidden_and_escaping_paths(world):
    owner, _ = world
    pin = wr.pin_ref(owner, 'main')
    for path in ('docs/escape.md', '.hidden/secret.md', 'docs', '../README.md', '/README.md'):
        with pytest.raises(ValueError):
            wr.read(pin, path)
        with pytest.raises(ValueError):
            wr.read_many(pin, [path])


def test_batch_reads_ignore_ambient_object_routing(world, monkeypatch):
    owner, _ = world
    pin = wr.pin_ref(owner, 'main')
    monkeypatch.setenv('GIT_OBJECT_DIRECTORY', '/does/not/exist')
    assert wr.read_many(pin, ['README.md'])['README.md'].startswith('# Company')


def test_change_windows_are_independent_and_results_not_shared(world):
    owner, _ = world
    pin = wr.pin_ref(owner, 'main')
    assert wr.changes(pin, window=0)['docs/papers/context.md']['added'] is None
    assert wr.changes(pin, window=200)['docs/papers/context.md']['added'] == 1
    wr.changes(pin)['docs/papers/context.md']['subject'] = 'corrupted by caller'
    assert wr.changes(pin)['docs/papers/context.md']['subject'] == 'editorial pass'


def test_binary_history_counts_are_unknown(world):
    owner, _ = world
    (owner / 'binary.md').write_bytes(b'\0binary')
    _git(owner, 'add', '.')
    _git(owner, 'commit', '-qm', 'binary')
    entry = wr.history(wr.pin_ref(owner, 'main'), 'binary.md')[0]
    assert entry['added'] is None and entry['removed'] is None


def test_exact_pin_rejects_tree_objects_and_preserves_provenance(world):
    owner, _ = world
    tree_sha = subprocess.check_output(['git', '-C', str(owner), 'rev-parse', 'HEAD^{tree}'], text=True).strip()
    with pytest.raises(ValueError, match='commit object'):
        wr.pin_sha(owner, tree_sha)
    fetched = wr.pin_ref(owner, 'main')
    assert fetched.provenance == 'fetched-ref' and fetched.ref == 'main'
    accepted = wr.pin_sha(owner, fetched.sha, provenance='controller-accepted')
    assert accepted.sha == fetched.sha and accepted.provenance == 'controller-accepted'
    assert wr.pin_sha(owner, fetched.sha).provenance == 'exact'


def test_linked_worktree_and_bare_repository(world, tmp_path):
    owner, _ = world
    linked = tmp_path / 'linked'
    _git(owner, 'worktree', 'add', '--detach', str(linked))
    pin = wr.pin_ref(linked, 'HEAD')
    assert wr.read(pin, 'README.md').startswith('# Company')
    bare = tmp_path / 'bare.git'
    subprocess.run(['git', 'clone', '--bare', '-q', str(owner), str(bare)], check=True)
    assert wr.read(wr.pin_ref(bare, 'main'), 'README.md') == wr.read(pin, 'README.md')


def test_unusual_paths_are_literal_in_batch_changes_and_history(world):
    owner, _ = world
    paths = ['docs/tab\tfile.md', 'docs/new\nline.md', 'docs/colon:name.md', 'docs/[*].md']
    for path in paths:
        (owner / path).write_text('# Literal\n')
    _git(owner, 'add', '.')
    _git(owner, 'commit', '-qm', 'odd paths')
    pin = wr.pin_ref(owner, 'main')
    assert wr.read_many(pin, paths) == dict.fromkeys(paths, '# Literal\n')
    for path in paths:
        assert wr.changes(pin)[path]['added'] == 1
        assert wr.history(pin, path)[0]['added'] == 1


def test_merge_changes_are_dated_and_counted_when_integrated(world):
    owner, _ = world
    _git(owner, 'checkout', '-qb', 'side')
    (owner / 'docs/papers/context.md').write_text('# Context\nfrom side\n')
    _git(owner, 'commit', '-qam', 'side edit')
    _git(owner, 'checkout', '-q', 'main')
    (owner / 'README.md').write_text('# Main\n')
    _git(owner, 'commit', '-qam', 'main edit')
    _git(owner, 'merge', '--no-ff', '-m', 'integrated paper', 'side')
    pin = wr.pin_ref(owner, 'main')
    entry = wr.history(pin, 'docs/papers/context.md')[0]
    assert entry['sha'] == pin.sha and entry['subject'] == 'integrated paper'
    assert (entry['added'], entry['removed']) == (1, 1)
    assert '+from side' in entry['diff']
    change = wr.changes(pin)['docs/papers/context.md']
    assert change['sha'] == pin.sha and change['at'] == entry['at']
    assert all(h['subject'] != 'side edit' for h in wr.history(pin, 'docs/papers/context.md'))


def test_discovery_uses_configured_roots_and_excludes_skipped_descendants(world):
    owner, _ = world
    before = wr.pin_ref(owner, 'main')
    for path in ('docs/new/README.md', 'private/README.md', 'docs/papers/ignored/README.md'):
        (owner / path).parent.mkdir(parents=True, exist_ok=True)
        (owner / path).write_text('# Newly discovered\n')
    _git(owner, 'add', '.')
    _git(owner, 'commit', '-qm', 'new package')
    pin = wr.pin_ref(owner, 'main')
    assert 'docs/new' not in {p['folder'] for p in wr.packages(before)}
    packages = wr.packages(pin, include=['docs/**'], skip=('docs/papers/ignored',))
    assert 'docs/new' in {p['folder'] for p in packages}
    assert 'private' not in {p['folder'] for p in packages}
    papers = next(p for p in packages if p['folder'] == 'docs/papers')
    assert not any('ignored' in p for p in papers['documents'])


def test_blob_and_batch_limits_refuse_oversized_reads(world, monkeypatch):
    owner, _ = world
    pin = wr.pin_ref(owner, 'main')
    monkeypatch.setattr(wr, 'MAX_BLOB', 3)
    with pytest.raises(ValueError, match='byte limit'):
        wr.read(pin, 'README.md')
    monkeypatch.setattr(wr, 'MAX_BLOB', 10000)
    monkeypatch.setattr(wr, 'MAX_OUTPUT', 3)
    with pytest.raises(ValueError, match='batch byte limit'):
        wr.read_many(pin, ['README.md'])


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_git_output_is_bounded_during_capture(tmp_path, monkeypatch, stream):
    import sys
    monkeypatch.setattr(wr, 'hardened_git_argv', lambda *a: [sys.executable, '-c',
        f'import sys; sys.{stream}.write("x" * 1000000)'])
    with pytest.raises(ValueError, match='output exceeds'):
        wr._git_bytes(tmp_path, max_output=1000)


def test_git_deadline_reaps_the_process(tmp_path, monkeypatch):
    import sys
    spawned = []
    original = subprocess.Popen
    def start(*a, **kw):
        process = original(*a, **kw)
        spawned.append(process)
        return process
    monkeypatch.setattr(wr, 'hardened_git_argv', lambda *a: [sys.executable, '-c', 'import time; time.sleep(30)'])
    monkeypatch.setattr(wr.subprocess, 'Popen', start)
    with pytest.raises(subprocess.TimeoutExpired):
        wr._git_bytes(tmp_path, timeout=0.05)
    assert spawned[0].poll() is not None


def test_links_cannot_escape_then_fall_back_to_an_in_world_suffix(world):
    owner, _ = world
    resolve = wr.resolver(['README.md', 'docs/a.md'])
    for ref in ('../../README.md', '//example.com/README.md', '/README.md', '%2e%2e/README.md'):
        assert resolve(ref, 'README.md') is None
    assert resolve('gurugee_data/docs/a.md', 'README.md') == 'docs/a.md'
    pin = wr.pin_ref(owner, 'main')
    assert wr.blob_url('https://u:secret@github.com/o/r', pin, 'a.md') is None
    assert wr.blob_url('https://github.com/o/r', pin, 'a #?.md').endswith('/a%20%23%3F.md')


def test_unavailable_repository_is_not_a_missing_ref(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        wr.pin_ref(tmp_path / 'missing-repository', 'main')


def test_package_dates_compare_instants_across_timezones(tmp_path):
    def git(*args, at=None):
        env = dict(os.environ)
        if at:
            env.update(GIT_COMMITTER_DATE=at, GIT_AUTHOR_DATE=at)
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True, env=env)
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Date fixture')
    git('config', 'user.email', 'date@example.invalid')
    for folder, at in [('earlier', '2031-10-03T14:00:00+03:00'), ('later', '2031-10-03T12:00:00+00:00')]:
        (tmp_path / folder).mkdir()
        (tmp_path / folder / 'README.md').write_text('# Package\n')
        git('add', '.')
        git('commit', '-qm', folder, at=at)
    assert [p['folder'] for p in wr.packages(wr.pin_ref(tmp_path, 'main'))] == ['later', 'earlier']
    (tmp_path / 'earlier' / 'details.md').write_text('later detail\n')
    git('add', '.')
    git('commit', '-qm', 'new detail', at='2031-10-03T12:30:00+00:00')
    entry = wr.packages(wr.pin_ref(tmp_path, 'main'))[0]
    assert entry['folder'] == 'earlier'
    assert datetime.fromisoformat(entry['updated_at']) == datetime(2031, 10, 3, 12, 30, tzinfo=UTC)
