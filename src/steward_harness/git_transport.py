"""Controller-owned Git transport that never opens an agent repository."""

from __future__ import annotations

import json
import os
import signal
import shutil
import stat
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from steward_harness.git import (
    abbreviated_object_id,
    controller_git_dir,
    hardened_git_argv,
    redact_command_output,
    validate_git_branch,
    validate_git_remote_url,
    validate_object_id,
)

if TYPE_CHECKING:
    from steward_harness.config.schema import StewardConfig
    from steward_harness.runtime.execution import UntrustedExecutionBroker


class GitTransportError(RuntimeError):
    """The trusted remote transport could not establish repository truth."""


class GitTransportInterrupted(GitTransportError):
    """A controller-owned Git child was signalled, so nothing was established.

    A negative exit status is "killed by a signal", and the only thing that
    signals these children is the controller's own cgroup being stopped. Since
    the controller drains rather than dying instantly, every deployment now
    produces a handful of these mid-pass — which is a fact about the shutdown,
    not about the repository, and reporting it as the latter teaches whoever
    reads the error count to stop reading it.
    """


class ControllerGitTransport:
    """Fetch remote truth into one controller-owned bare object store."""

    def __init__(
        self,
        state_db: str | Path,
        repository: str,
        remote_url: str,
        branch: str,
        *,
        allow_local: bool = False,
        create: bool = True,
    ) -> None:
        validate_git_remote_url(remote_url, allow_local=allow_local)
        validate_git_branch(branch)
        self.repository = repository
        self.git_dir = controller_git_dir(state_db, repository)
        self.remote_url = remote_url
        self.branch = branch
        self._manifest = {
            "version": 1,
            "object_format": "sha1",
            "remote_url": remote_url,
            "branch": branch,
        }
        self._fetch_lock = threading.RLock()
        self._landed: tuple[str, dict[str, str | None]] | None = None
        self._open_store(create=create)

    @property
    def remote_ref(self) -> str:
        return f"refs/steward/remote/{self.branch}"

    def observed_tip(self) -> tuple[str, datetime] | None:
        """The mirrored remote tip and its committer date, or None if never seen.

        A local read. The pass has already fetched under the lease it still
        holds, so observing costs one `for-each-ref`; fetching again here would
        be the repeated traversal of the same remote that the one-pass rule
        exists to stop. Committer date is *when it hit the branch*, because
        landing rebases onto the observed default before pushing.
        """
        line = self._run(
            "for-each-ref",
            "--format=%(objectname) %(committerdate:unix)",
            self.remote_ref,
        )
        if not line:
            return None
        sha, _, stamp = line.partition(" ")
        validate_object_id(sha)
        return sha, datetime.fromtimestamp(int(stamp), UTC)

    def landing(self, work: str, tip: str) -> str | None:
        """The commit on ``tip`` that landed this task work, or None.

        A landing commit names its work (`Steward-Work: <work>`); work merged
        some other way lands as itself when the tip contains it. Both answers
        are fixed for a tip, so they are computed once per tip.
        """
        if self._landed is None or self._landed[0] != tip:
            values = self._run("log", "--format=%H%x00%(trailers:key=Steward-Work,valueonly)%x00",
                               tip).split("\0")
            named: dict[str, str | None] = {}
            for commit, works in zip(values[0::2], values[1::2]):
                for named_work in works.split():
                    named.setdefault(named_work, commit.strip())
            self._landed = (tip, named)
        answers = self._landed[1]
        if work not in answers:
            answers[work] = work if self._contains(work, tip) else None
        return answers[work]

    def fetch(self) -> str:
        """Fetch configured repository heads; return only the default-branch tip."""
        with self._fetch_lock:
            self._run(
                "fetch",
                "--no-tags",
                "--atomic",
                "--prune",
                "--no-write-fetch-head",
                "--force",
                "--",
                self.remote_url,
                "+refs/heads/*:refs/steward/remote/*",
            )
            sha = self._run("rev-parse", "--verify", f"{self.remote_ref}^{{commit}}")
            validate_object_id(sha)
            return sha

    def resolve_remote_commit(self, value: str | None = None) -> str:
        """Resolve only the remote tip or an exact commit contained by it.

        ``value`` is operator text, so it may be the short SHA they read off a
        log rather than all forty characters. This is the one place an
        abbreviation is allowed to exist: it is expanded against the store the
        fetch above just filled, and everything past this line — the ancestry
        check, ``release_identity``, ``stage``, the deployed ref — sees a full
        object ID exactly as it always did.
        """
        observed = self.fetch()
        if value is None:
            return observed
        prefix = abbreviated_object_id(value)
        if prefix is not None:
            value = self._expand_object_id(prefix)
        if not self._contains(value, observed):
            raise GitTransportError(
                "deploy commit is not contained by the remote default branch"
            )
        return value

    def _expand_object_id(self, prefix: str) -> str:
        """Expand one abbreviation against the store this transport fetched into.

        Git does the resolving rather than a scan of our own, and that is the
        whole point: ``--verify --quiet`` turns an ambiguous prefix into an
        exit status instead of an arbitrary choice between the objects it
        matches, and ``^{commit}`` refuses one that names a tree or a blob.
        A full ID never comes here — it is already the answer, and re-resolving
        it would replace "not contained by the remote default branch", which is
        what the operator needs to hear, with "unknown object".
        """
        result = self._run_result(
            "rev-parse", "--verify", "--quiet", f"{prefix}^{{commit}}"
        )
        if result.returncode != 0:
            raise GitTransportError(
                f"deploy commit {prefix} is unknown or ambiguous in this repository"
            )
        return validate_object_id(result.stdout.strip())

    def release_identity(self, sha: str) -> tuple[str, str]:
        """Return exact commit and tree identities from controller storage."""
        validate_object_id(sha)
        commit = self._run("rev-parse", "--verify", f"{sha}^{{commit}}")
        if commit != sha:
            raise GitTransportError("release identity resolved to an unexpected commit")
        tree = self._run("rev-parse", "--verify", f"{sha}^{{tree}}")
        validate_object_id(tree)
        return commit, tree

    def export_release_archive(self, sha: str, destination: str | Path) -> None:
        """Write one exact committed tree to a controller-chosen archive path."""
        self.release_identity(sha)
        archive = Path(destination)
        if not archive.is_absolute():
            raise ValueError("release archive destination must be absolute")
        result = self._run_result(
            # Git's default tar.umask=0002 leaves root-extracted releases
            # group-writable, which cannot host privileged launch helpers.
            "-c", "tar.umask=0022", "archive", f"--output={archive}", sha, timeout=300
        )
        if result.returncode != 0:
            raise GitTransportError("controller Git could not export release archive")

    def _contains(self, sha: str, descendant: str) -> bool:
        exists = self._run_result("cat-file", "-e", f"{sha}^{{commit}}")
        if exists.returncode != 0:
            return False
        return self._is_ancestor(sha, descendant)

    def sync_remote_to_agent(
        self, agent_repo: str | Path, broker: UntrustedExecutionBroker
    ) -> str:
        """Mirror the observed remote heads into an agent clone as labels.

        Source refs are convenience labels, never publication or deploy
        authority. The fetch lock covers the push so concurrent polls cannot
        regress refs; vanished heads are pruned.
        """
        with self._fetch_lock:
            sha = self.fetch()
            self.push_to_agent(agent_repo, broker, "+refs/steward/remote/*:refs/steward/remote/*",
                               prune=True)
            return sha

    def push_to_agent(self, agent_repo: str | Path, broker: UntrustedExecutionBroker,
                      *refspecs: str, prune: bool = False) -> None:
        """Push controller objects and refs into an agent repository."""
        self._run("push", "--quiet", *(("--prune",) if prune else ()),
                  f"--receive-pack={broker.git_service('receive-pack')}",
                  "--", str(self._agent_repo_path(agent_repo)), *refspecs)

    def import_candidate(
        self,
        agent_repo: str | Path,
        tested_sha: str,
        expected_base_sha: str,
        broker: UntrustedExecutionBroker,
    ) -> str:
        """Import one candidate closure and retain it only after ancestry proof."""
        validate_object_id(tested_sha)
        validate_object_id(expected_base_sha)
        repo = self._agent_repo_path(agent_repo)
        try:
            observed_base = self._run(
                "rev-parse", "--verify", f"{self.remote_ref}^{{commit}}"
            )
        except GitTransportError as exc:
            raise GitTransportError(
                "candidate base was not observed from the remote"
            ) from exc
        # Another observer may have fetched a fast-forward while gates ran.
        # Trust follows remote ancestry, not equality with that mutable tip.
        if not self._contains(expected_base_sha, observed_base):
            raise GitTransportError("candidate base was not observed from the remote")
        candidate_ref = self.candidate_ref(tested_sha)
        existing = self._run_result("rev-parse", "--verify", f"{candidate_ref}^{{commit}}")
        if existing.returncode == 0 and existing.stdout.strip() != tested_sha:
            raise GitTransportError("tested candidate retention changed unexpectedly")
        self.fetch_from_agent(repo, broker, f"{tested_sha}:{candidate_ref}")
        if not self._is_ancestor(expected_base_sha, tested_sha):
            self._run("update-ref", "-d", candidate_ref, tested_sha)
            raise GitTransportError("candidate does not descend from its tested base")
        return tested_sha

    def fetch_from_agent(self, agent_repo: str | Path, broker: UntrustedExecutionBroker,
                         *refspecs: str) -> None:
        """Fetch exact agent commits into this store, checking every object."""
        self._run("-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags",
                  "--no-write-fetch-head", f"--upload-pack={broker.git_service('upload-pack')}",
                  "--", str(self._agent_repo_path(agent_repo)), *refspecs)

    def push_candidate(self, tested_sha: str, expected_base_sha: str) -> bool:
        """Compare-and-swap the imported candidate onto exactly its tested base."""
        result = self._run_result(
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{self.branch}:{expected_base_sha}",
            "--",
            self.remote_url,
            f"{tested_sha}:refs/heads/{self.branch}",
        )
        return result.returncode == 0

    def candidate_ref(self, tested_sha: str) -> str:
        """The ref that keeps an imported candidate reachable until it is pushed."""
        validate_object_id(tested_sha)
        return f"refs/steward/candidates/{tested_sha}"

    def drop_candidate(self, tested_sha: str) -> None:
        self._run("update-ref", "-d", self.candidate_ref(tested_sha))

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run_result(
            "merge-base", "--is-ancestor", ancestor, descendant
        )
        if result.returncode not in {0, 1}:
            raise GitTransportError("controller Git ancestry check failed")
        return result.returncode == 0

    @staticmethod
    def _agent_repo_path(agent_repo: str | Path) -> Path:
        path = Path(agent_repo)
        if not path.is_absolute():
            raise ValueError("agent repository path must be absolute")
        return path

    def _open_store(self, *, create: bool = True) -> None:
        namespace = self.git_dir.parent
        if create:
            namespace.mkdir(mode=0o700, exist_ok=True)
        self._assert_private_directory(namespace)
        # Each initialization/import owns its temporary directory and cleanup.
        # A sibling may be active, even when this is a new transport instance.
        if os.path.lexists(self.git_dir):
            if not self.git_dir.is_dir() or self.git_dir.is_symlink():
                raise GitTransportError("controller Git store is not a private directory")
            self._assert_private_store()
            self._verify_manifest()
            return

        if not create:
            raise GitTransportError("controller Git store is missing")

        temporary = Path(
            tempfile.mkdtemp(prefix=f".new-{self.repository}-", dir=namespace)
        )
        try:
            try:
                result = subprocess.run(
                    hardened_git_argv(
                        "init", "--bare", "--object-format=sha1", str(temporary)
                    ),
                    cwd=namespace,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    umask=0o077,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise GitTransportError(
                    "could not initialize controller Git store"
                ) from exc
            if result.returncode != 0:
                raise GitTransportError("could not initialize controller Git store")
            manifest = temporary / "steward-transport.json"
            manifest.write_text(json.dumps(self._manifest, sort_keys=True) + "\n")
            manifest.chmod(0o600)
            self._assert_private_directory(temporary)
            self._assert_control_file(temporary / "config")
            self._assert_control_file(manifest)
            temporary.rename(self.git_dir)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def _verify_manifest(self) -> None:
        manifest = self.git_dir / "steward-transport.json"
        try:
            observed = json.loads(manifest.read_text())
        except (OSError, ValueError) as exc:
            raise GitTransportError("controller Git store has no valid identity") from exc
        if observed != self._manifest:
            raise GitTransportError("controller Git store identity does not match policy")

    @staticmethod
    def _assert_private_directory(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise GitTransportError("controller Git store cannot be inspected") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise GitTransportError("controller Git store is not controller-private")

    @staticmethod
    def _assert_control_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise GitTransportError("controller Git store cannot be inspected") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise GitTransportError("controller Git metadata is not controller-owned")

    def _assert_private_store(self) -> None:
        self._assert_private_directory(self.git_dir.parent)
        self._assert_private_directory(self.git_dir)
        self._assert_control_file(self.git_dir / "config")
        self._assert_control_file(self.git_dir / "steward-transport.json")

    def _run(self, *args: str) -> str:
        result = self._run_result(*args)
        if result.returncode != 0:
            operation = args[0] if args else "operation"
            if result.returncode < 0:
                raise GitTransportInterrupted(
                    f"controller Git {operation} was signalled "
                    f"({signal.Signals(-result.returncode).name})"
                )
            raise GitTransportError(
                f"controller Git {operation} failed with exit {result.returncode}: "
                f"{redact_command_output(result.stderr)}"
            )
        self._assert_private_store()
        return result.stdout.strip()

    def _run_result(
        self,
        *args: str,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[str]:
        self._assert_private_store()
        environment = dict(os.environ, GIT_NO_LAZY_FETCH="1")
        try:
            result = subprocess.run(
                hardened_git_argv(f"--git-dir={self.git_dir}", *args),
                cwd=self.git_dir.parent,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                umask=0o077,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitTransportError(
                f"controller Git {args[0] if args else 'operation'} could not run"
            ) from exc
        self._assert_private_store()
        return result


def controller_transport(
    config: StewardConfig, repository: str, *, create: bool = True,
) -> ControllerGitTransport:
    """Build one repository's transport from configuration.

    allow_local follows ``config.execution.user is None``: a local agent path is
    only acceptable when there is no separate untrusted execution identity. That
    rule belongs to the transport, not to whichever caller happens to need one,
    so no two callers can disagree about it.
    """
    managed = config.repositories[repository]
    return ControllerGitTransport(
        Path(config.provider.state_db).resolve(),
        repository,
        managed.remote_url,
        managed.default_branch,
        allow_local=config.execution.user is None,
        create=create,
    )
