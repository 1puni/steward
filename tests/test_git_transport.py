"""Controller Git transport never consults agent-owned repository metadata."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.git_transport import (
    ControllerGitTransport,
    GitTransportError,
    GitTransportInterrupted,
)
from steward_harness.runtime.execution import UntrustedExecutionBroker


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _remote_repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git("init", "-q", "-b", "main", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-q", "-b", "main", str(source), cwd=tmp_path)
    (source / "file").write_text("base\n")
    _git("add", "file", cwd=source)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "base", cwd=source,
    )
    base = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=source)
    return remote, source, base


def test_transport_fetches_into_controller_owned_store(tmp_path: Path) -> None:
    remote, _source, expected = _remote_repository(tmp_path)

    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )

    assert transport.fetch() == expected
    assert _git(
        f"--git-dir={transport.git_dir}", "rev-parse", transport.remote_ref, cwd=tmp_path
    ) == expected


def test_store_creation_ignores_permissive_daemon_umask(tmp_path: Path) -> None:
    prior = os.umask(0o002)
    try:
        transport = ControllerGitTransport(
            tmp_path / "state.db",
            "app",
            "https://example.invalid/repository.git",
            "main",
        )
    finally:
        os.umask(prior)

    assert transport.git_dir.parent.stat().st_mode & 0o077 == 0
    assert all(
        path.lstat().st_mode & 0o022 == 0
        for path in (transport.git_dir, *transport.git_dir.rglob("*"))
    )


def test_store_rejects_poisoned_existing_metadata(tmp_path: Path) -> None:
    transport = ControllerGitTransport(
        tmp_path / "state.db",
        "app",
        "https://example.invalid/repository.git",
        "main",
    )
    (transport.git_dir / "config").chmod(0o660)

    with pytest.raises(GitTransportError, match="not controller-owned"):
        ControllerGitTransport(
            tmp_path / "state.db",
            "app",
            "https://example.invalid/repository.git",
            "main",
        )


def test_store_identity_is_bound_to_remote_and_branch(tmp_path: Path) -> None:
    transport = ControllerGitTransport(
        tmp_path / "state.db",
        "app",
        "https://example.invalid/one.git",
        "main",
    )
    manifest = json.loads((transport.git_dir / "steward-transport.json").read_text())
    assert manifest["remote_url"] == "https://example.invalid/one.git"

    with pytest.raises(GitTransportError, match="identity does not match"):
        ControllerGitTransport(
            tmp_path / "state.db",
            "app",
            "https://example.invalid/two.git",
            "main",
        )


@pytest.mark.parametrize("branch", ["-main", "refs/heads/main", "main..next"])
def test_transport_rejects_invalid_branch(branch: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid Git branch"):
        ControllerGitTransport(
            tmp_path / "state.db",
            "app",
            "https://example.invalid/repository.git",
            branch,
        )


def test_transport_rejects_local_remote_by_default(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local Git remote"):
        ControllerGitTransport(
            tmp_path / "state.db", "app", str(tmp_path / "remote.git"), "main"
        )


def test_remote_objects_cross_without_trusting_agent_origin(tmp_path: Path) -> None:
    remote, _source, expected = _remote_repository(tmp_path)
    agent = tmp_path / "agent"
    _git("init", "-q", "-b", "main", str(agent), cwd=tmp_path)
    _git("remote", "add", "origin", "/definitely/not/the/trusted/remote", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )

    observed = transport.sync_remote_to_agent(
        agent, UntrustedExecutionBroker(UntrustedExecutionConfig())
    )

    assert observed == expected
    assert _git("rev-parse", transport.remote_ref, cwd=agent) == expected


def test_deploy_resolution_accepts_only_exact_remote_history(tmp_path: Path) -> None:
    remote, agent, base = _remote_repository(tmp_path)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )

    assert transport.resolve_remote_commit() == base
    assert transport.resolve_remote_commit(base) == base
    # A branch name means "whatever this points at next", which is the opposite
    # of a pinned commit, so no amount of operator convenience admits one.
    for named in ("main", "HEAD", "HEAD@{1}", "v1.0", "main^{commit}"):
        with pytest.raises(ValueError, match="7 to 40 lowercase hex"):
            transport.resolve_remote_commit(named)

    (agent / "local-only").write_text("not remote\n")
    _git("add", "local-only", cwd=agent)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "local only", cwd=agent,
    )
    local_only = _git("rev-parse", "HEAD", cwd=agent)
    with pytest.raises(GitTransportError, match="not contained"):
        transport.resolve_remote_commit(local_only)


def _write_object(git_dir: Path, kind: str, body: bytes) -> str:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "hash-object", "-t", kind, "-w", "--stdin"],
        input=body,
        check=True,
        capture_output=True,
    ).stdout.decode().strip()


def _ambiguous_prefix(git_dir: Path, tree: str) -> str:
    """Mine two commits sharing a seven-character prefix and write both here.

    Mined rather than asserted about: an abbreviation is only ambiguous if the
    store genuinely holds two objects under it, and a fabricated one would
    prove nothing about which side is doing the deciding.
    """
    seen: dict[str, bytes] = {}
    stamp = 0
    while True:
        body = (
            f"tree {tree}\n"
            f"author T <t@example.invalid> {stamp} +0000\n"
            f"committer T <t@example.invalid> {stamp} +0000\n"
            f"\nambiguity {stamp}\n"
        ).encode()
        digest = hashlib.sha1(b"commit %d\x00" % len(body) + body).hexdigest()
        twin = seen.get(digest[:7])
        if twin is not None:
            _write_object(git_dir, "commit", twin)
            _write_object(git_dir, "commit", body)
            return digest[:7]
        seen[digest[:7]] = body
        stamp += 1


def test_deploy_resolution_expands_an_operator_typed_prefix(tmp_path: Path) -> None:
    """An operator reads a short SHA off a log; the store says which one it is.

    Expansion is Git's job, not a scan of our own, which is what makes the two
    refusals below refusals rather than a guess between candidates. Everything
    the caller gets back is still forty characters.
    """
    remote, _agent, base = _remote_repository(tmp_path)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    transport.fetch()

    assert transport.resolve_remote_commit(base[:7]) == base
    assert transport.resolve_remote_commit(base[:12]) == base
    with pytest.raises(ValueError, match="7 to 40 lowercase hex"):
        transport.resolve_remote_commit(base[:6])

    # A tree is an object the operator could have copied from `cat-file`, and
    # it is not a release. `^{commit}` is what refuses it.
    tree = _git(f"--git-dir={transport.git_dir}", "rev-parse", f"{base}^{{tree}}", cwd=tmp_path)
    with pytest.raises(GitTransportError, match="unknown or ambiguous"):
        transport.resolve_remote_commit(tree[:10])

    blob = _write_object(transport.git_dir, "blob", b"not a release\n")
    with pytest.raises(GitTransportError, match="unknown or ambiguous"):
        transport.resolve_remote_commit(blob[:10])

    with pytest.raises(GitTransportError, match="unknown or ambiguous"):
        transport.resolve_remote_commit("deadbee")

    ambiguous = _ambiguous_prefix(transport.git_dir, tree)
    with pytest.raises(GitTransportError, match="unknown or ambiguous"):
        transport.resolve_remote_commit(ambiguous)


def test_candidate_crosses_and_is_retained_after_ancestry_proof(tmp_path: Path) -> None:
    remote, agent, base = _remote_repository(tmp_path)
    (agent / "file").write_text("candidate\n")
    _git("add", "file", cwd=agent)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "candidate", cwd=agent,
    )
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    transport.fetch()

    imported = transport.import_candidate(
        agent,
        candidate,
        base,
        UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )

    assert imported == candidate
    assert _git(
        f"--git-dir={transport.git_dir}",
        "rev-parse",
        transport.candidate_ref(candidate),
        cwd=tmp_path,
    ) == candidate


def test_retained_candidates_cannot_overwrite_each_others_recovery_root(
    tmp_path: Path,
) -> None:
    remote, agent, base = _remote_repository(tmp_path)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    transport.fetch()
    candidates: list[str] = []
    for name in ("first", "second"):
        _git("checkout", "-q", "--detach", base, cwd=agent)
        (agent / name).write_text(f"{name}\n")
        _git("add", name, cwd=agent)
        _git(
            "-c", "user.name=T", "-c", "user.email=t@example.invalid",
            "commit", "-q", "-m", name, cwd=agent,
        )
        candidate = _git("rev-parse", "HEAD", cwd=agent)
        transport.import_candidate(
            agent,
            candidate,
            base,
            UntrustedExecutionBroker(UntrustedExecutionConfig()),
        )
        candidates.append(candidate)

    for candidate in candidates:
        assert _git(
            f"--git-dir={transport.git_dir}",
            "rev-parse",
            transport.candidate_ref(candidate),
            cwd=tmp_path,
        ) == candidate


def test_controller_publishes_its_imported_candidate_under_an_exact_base_lease(
    tmp_path: Path,
) -> None:
    remote, agent, base = _remote_repository(tmp_path)
    (agent / "file").write_text("candidate\n")
    _git("add", "file", cwd=agent)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "candidate", cwd=agent,
    )
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    transport.fetch()
    transport.import_candidate(
        agent,
        candidate,
        base,
        UntrustedExecutionBroker(UntrustedExecutionConfig()),
    )

    assert transport.push_candidate(candidate, base)
    assert transport.fetch() == candidate
    # Pushing the same revision again is a no-op, not a second landing.
    assert transport.push_candidate(candidate, base)
    assert transport.fetch() == candidate


def test_candidate_requires_the_current_controller_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = tmp_path / "agent"
    _git("init", "-q", "-b", "main", str(agent), cwd=tmp_path)
    (agent / "file").write_text("candidate\n")
    _git("add", "file", cwd=agent)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "candidate", cwd=agent,
    )
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db",
        "app",
        "https://example.invalid/repository.git",
        "main",
    )
    monkeypatch.setattr(
        transport,
        "fetch_from_agent",
        lambda *_args, **_kwargs: pytest.fail("agent fetch started before base proof"),
    )

    with pytest.raises(GitTransportError, match="base was not observed"):
        transport.import_candidate(
            agent,
            candidate,
            candidate,
            UntrustedExecutionBroker(UntrustedExecutionConfig()),
        )


def test_observer_fast_forward_does_not_invalidate_a_candidate_base(tmp_path):
    remote, agent, base = _remote_repository(tmp_path)
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True,
    )
    assert transport.fetch() == base
    (agent / "candidate").write_text("candidate\n")
    _git("add", "candidate", cwd=agent)
    _git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "candidate", cwd=agent)
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    _git("checkout", "-q", "--detach", base, cwd=agent)
    (agent / "peer").write_text("peer\n")
    _git("add", "peer", cwd=agent)
    _git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "peer", cwd=agent)
    peer = _git("rev-parse", "HEAD", cwd=agent)
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=agent)
    assert transport.fetch() == peer

    assert transport.import_candidate(agent, candidate, base, broker) == candidate
    assert not transport.push_candidate(candidate, base)
    assert transport.fetch() == peer
    # Retaining agent objects never makes them a trusted remote base.
    with pytest.raises(GitTransportError, match="base was not observed"):
        transport.import_candidate(agent, candidate, candidate, broker)


def test_concurrent_remote_syncs_do_not_overwrite_a_newer_agent_observation(tmp_path, monkeypatch):
    remote, agent, base = _remote_repository(tmp_path)
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True,
    )
    copying, release, started = threading.Event(), threading.Event(), threading.Event()
    second_copy = threading.Event()
    copied = []
    failures = []
    original = transport.push_to_agent

    def hold_first(*args, **kwargs):
        if not copying.is_set():
            copying.set()
            assert release.wait(5)
        else:
            second_copy.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(transport, "push_to_agent", hold_first)

    def sync():
        try:
            started.set()
            copied.append(transport.sync_remote_to_agent(agent, broker))
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(target=sync)
    second = threading.Thread(target=sync)
    first.start()
    try:
        assert copying.wait(3)
        (agent / "peer").write_text("peer\n")
        _git("add", "peer", cwd=agent)
        _git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
             "commit", "-qm", "peer", cwd=agent)
        peer = _git("rev-parse", "HEAD", cwd=agent)
        _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=agent)
        started.clear()
        second.start()
        assert started.wait(2)
        assert not second_copy.wait(0.1), "another sync passed an unfinished observation"
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert second_copy.is_set()
    assert copied == [base, peer]
    assert _git("rev-parse", transport.remote_ref, cwd=agent) == peer


def test_rejected_candidate_leaves_no_ref(tmp_path: Path) -> None:
    remote, agent, base = _remote_repository(tmp_path)
    _git("checkout", "-q", "--orphan", "unrelated", cwd=agent)
    _git("rm", "-q", "-rf", ".", cwd=agent)
    (agent / "other").write_text("unrelated\n")
    _git("add", "other", cwd=agent)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "unrelated", cwd=agent,
    )
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    assert transport.fetch() == base

    with pytest.raises(GitTransportError, match="does not descend"):
        transport.import_candidate(
            agent,
            candidate,
            base,
            UntrustedExecutionBroker(UntrustedExecutionConfig()),
        )

    candidate_ref = subprocess.run(
        [
            "git",
            f"--git-dir={transport.git_dir}",
            "show-ref",
            "--verify",
            "--quiet",
            transport.candidate_ref(candidate),
        ],
        check=False,
    )
    assert candidate_ref.returncode == 1


def test_observing_an_unchanged_remote_transfers_no_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll that discovers nothing moved must cost nothing.

    The observation loop calls this for every repository on every poll. It sent
    the tip's whole object closure each time to a clone that already held all
    of it — on gg that was ~2.85 of 8 cores, permanently, to learn that five
    repositories were unchanged.
    """
    remote, source, base = _remote_repository(tmp_path)
    agent = tmp_path / "agent"
    _git("init", "-q", "-b", "main", str(agent), cwd=tmp_path)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())

    def objects() -> int:
        counted = dict(line.split(": ") for line in _git("count-objects", "-v", cwd=agent).splitlines())
        return int(counted["count"]) + int(counted["in-pack"])

    assert transport.sync_remote_to_agent(agent, broker) == base
    delivered = objects()
    assert delivered > 0, "the first sync has to deliver the closure"

    # Three more polls with nothing moving on the remote.
    for _ in range(3):
        assert transport.sync_remote_to_agent(agent, broker) == base
    assert objects() == delivered, "an unchanged remote must transfer nothing at all"

    # And when it does move, only the difference goes: one commit, one tree, one blob.
    (source / "file").write_text("moved\n")
    _git("add", "file", cwd=source)
    _git(
        "-c", "user.name=T", "-c", "user.email=t@example.invalid",
        "commit", "-q", "-m", "moved", cwd=source,
    )
    moved = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=source)

    assert transport.sync_remote_to_agent(agent, broker) == moved
    assert objects() == delivered + 3


def test_sync_materializes_missing_branch_commit_without_authorizing_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote, source, base = _remote_repository(tmp_path)
    agent = tmp_path / "agent"
    _git("clone", "-q", "--single-branch", str(remote), str(agent), cwd=tmp_path)
    # Worker metadata cannot select the credentialed transport destination.
    _git("remote", "set-url", "origin", "https://invalid.example/worker.git", cwd=agent)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    assert transport.sync_remote_to_agent(agent, broker) == base

    branch = "codex/social-starter-kit-20260912"
    _git("checkout", "-qb", branch, cwd=source)
    (source / "social.md").write_text("authorized source, not a published post\n")
    _git("add", "social.md", cwd=source)
    _git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "-qm", "social kit", cwd=source)
    requested = _git("rev-parse", "HEAD", cwd=source)
    # The requested commit can be an ancestor of the branch tip.
    _git("-c", "user.name=T", "-c", "user.email=t@example.invalid",
         "commit", "--allow-empty", "-qm", "follow-up", cwd=source)
    tip = _git("rev-parse", "HEAD", cwd=source)
    _git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}", cwd=source)
    for store in (agent, transport.git_dir):
        assert subprocess.run(
            ["git", "cat-file", "-e", requested], cwd=store, capture_output=True,
            check=False,
        ).returncode != 0

    # Even when main has not moved, the observation poll delivers new source.
    assert transport.sync_remote_to_agent(agent, broker) == base
    assert _git("show", f"{requested}:social.md", cwd=agent).startswith("authorized source")
    ref = f"refs/steward/remote/{branch}"
    assert _git("rev-parse", ref, cwd=agent) == tip
    assert _git("rev-parse", "HEAD", cwd=agent) == base
    assert _git("status", "--porcelain", cwd=agent) == ""
    with pytest.raises(GitTransportError, match="not contained"):
        transport.resolve_remote_commit(requested)

    assert transport.sync_remote_to_agent(agent, broker) == base
    # Deleted remote heads cease to be controller truth. Worker objects remain
    # usable until normal Git collection and never become release authority.
    _git("push", "-q", str(remote), f":refs/heads/{branch}", cwd=source)
    assert transport.sync_remote_to_agent(agent, broker) == base
    assert _git("for-each-ref", "--format=%(refname)", ref,
                cwd=transport.git_dir) == ""
    assert _git("show", f"{requested}:social.md", cwd=agent).startswith("authorized source")
    assert _git("for-each-ref", "--format=%(refname)", ref, cwd=agent) == ""
    _git("push", "-q", str(remote), f"HEAD:refs/heads/{branch}/replacement", cwd=source)
    assert transport.sync_remote_to_agent(agent, broker) == base
    assert _git("rev-parse", f"{ref}/replacement", cwd=agent) == tip


def test_a_signalled_git_child_is_not_reported_as_a_repository_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A negative exit is the controller being stopped, not a broken remote."""
    remote, _source, _base = _remote_repository(tmp_path)
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", str(remote), "main", allow_local=True
    )
    monkeypatch.setattr(
        transport,
        "_run_result",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, -signal.SIGTERM, "", ""),
    )

    with pytest.raises(GitTransportInterrupted) as raised:
        transport.fetch()
    assert "SIGTERM" in str(raised.value)
    # Still a GitTransportError, so every existing handler keeps catching it.
    assert isinstance(raised.value, GitTransportError)


def test_publish_exact_base_lease_rejects_a_remote_rewind(tmp_path):
    remote, agent, initial = _remote_repository(tmp_path)
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    for name in ("base", "candidate"):
        (agent / name).write_text(name)
        _git("add", name, cwd=agent)
        _git("-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", name, cwd=agent)
        if name == "base":
            base = _git("rev-parse", "HEAD", cwd=agent)
            _git("push", "-q", str(remote), "HEAD:refs/heads/main", cwd=agent)
    candidate = _git("rev-parse", "HEAD", cwd=agent)
    transport = ControllerGitTransport(tmp_path / "state.db", "app", str(remote), "main", allow_local=True)
    assert transport.fetch() == base
    transport.import_candidate(agent, candidate, base, broker)
    # Simulate an independently authorized remote reset between observation and push.
    _git("update-ref", "refs/heads/main", initial, base, cwd=remote)
    assert not transport.push_candidate(candidate, base)
    assert _git("rev-parse", "main", cwd=remote) == initial


def test_failed_fetch_reports_bounded_redacted_stderr(tmp_path, monkeypatch):
    transport = ControllerGitTransport(
        tmp_path / "state.db", "app", "https://example.invalid/repo.git", "main"
    )
    stderr = "fatal: cannot fetch https://user:credential@example.invalid/repo.git\n" + "x" * 2000
    calls = []

    def failed_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 128, "", stderr)

    monkeypatch.setattr(transport, "_run_result", failed_run)
    with pytest.raises(GitTransportError) as raised:
        transport.fetch()
    assert calls[0][0] == "fetch"
    prefix = "controller Git fetch failed with exit 128: "
    message = str(raised.value)
    assert message.startswith(prefix + "fatal: cannot fetch [URL redacted]")
    assert "credential" not in message
    assert len(message) <= len(prefix) + 1000
