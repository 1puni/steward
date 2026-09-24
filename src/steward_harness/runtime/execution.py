"""Host-enforced execution boundary for model-controlled processes."""

from __future__ import annotations

import grp
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from steward_harness.config.schema import UntrustedExecutionConfig
from steward_harness.git import controller_git_dir, hardened_git_argv

if TYPE_CHECKING:
    from steward_harness.config.schema import StewardConfig

_PROVIDER_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "IS_SANDBOX",
        "OPENAI_API_KEY",
        "ZAI_AUTH_TOKEN",
    }
)


class ExecutionBoundaryUnavailable(RuntimeError):
    """The configured untrusted identity does not hold, so nothing may run.

    Named rather than a bare `RuntimeError` so a caller that legitimately
    absorbs it can say which condition it is absorbing. `task_status._read`
    is the one that does: a projection over a repository it may not read
    answers "Git has no opinion" instead of failing the turn. It used to
    catch `Exception` to do that, which meant a `TypeError` in the reader
    reported as the same healthy silence.
    """


@dataclass(frozen=True, slots=True)
class BoundaryStatus:
    enforced: bool
    reason: str
    uid: int | None = None
    gid: int | None = None


class UntrustedExecutionBroker:
    """Launch child code with a clean environment and optional dropped identity."""

    def __init__(
        self,
        config: UntrustedExecutionConfig,
        *,
        boundary_required: bool = False,
        private_files: Mapping[str, str | Path] | None = None,
        protected_paths: Mapping[str, str | Path] | None = None,
        shared_lock_files: Mapping[str, str | Path] | None = None,
        writable_paths: Mapping[str, str | Path] | None = None,
        readable_roots: Mapping[str, str | Path] | None = None,
    ) -> None:
        self.config = config
        self._boundary_required = boundary_required
        self._boundary_verified = False
        self._private_files = {
            label: Path(path) for label, path in (private_files or {}).items()
        }
        self._protected_paths = {
            label: Path(path) for label, path in (protected_paths or {}).items()
        }
        self._shared_lock_files = {
            label: Path(path) for label, path in (shared_lock_files or {}).items()
        }
        self._writable_paths = {
            label: Path(path) for label, path in (writable_paths or {}).items()
        }
        self._readable_roots = {
            label: Path(path) for label, path in (readable_roots or {}).items()
        }

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        """Configured agent directories, shared by OS checks and native runtimes."""
        return tuple(dict.fromkeys(path.resolve() for path in self._writable_paths.values()))

    @classmethod
    def for_steward(
        cls,
        config: StewardConfig,
        config_path: str | Path | None = None,
    ) -> UntrustedExecutionBroker:
        """Build the one broker contract for a complete steward instance."""
        state_path = Path(config.provider.state_db)
        private_files: dict[str, Path] = {}

        def add_database(label: str, path: Path) -> None:
            private_files[label] = path
            private_files[f"{label} WAL"] = path.with_name(f"{path.name}-wal")
            private_files[f"{label} shared memory"] = path.with_name(
                f"{path.name}-shm"
            )

        add_database("state database", state_path)
        protected_paths: dict[str, Path] = {
            "state directory": state_path.parent,
            "daemon lock": state_path.parent / f".{state_path.name}.kernel.lock",
        }
        shared_lock_files: dict[str, Path] = {}
        writable_paths: dict[str, Path] = {
            "provider work directory": Path(config.provider.workdir),
            **{
                f"provider {name!r} native home": Path(path)
                for name, path in config.provider.native_homes.items()
            },
            **{
                f"repository {name!r}": Path(repository.path)
                for name, repository in config.repositories.items()
            },
        }
        readable_roots: dict[str, Path] = {}
        if config_path is not None:
            protected_paths["configuration"] = Path(os.path.abspath(config_path))
        for name, procedure in config.procedures.items():
            protected_paths[f"procedure {name!r} instructions"] = Path(procedure.instructions)
            protected_paths[f"procedure {name!r} directory"] = Path(procedure.instructions).parent
        for name, target in config.targets.items():
            protected_paths[f"target {name!r} driver"] = Path(target.driver)
            protected_paths[f"target {name!r} driver directory"] = Path(target.driver).parent
        telegram = config.telegram
        if telegram is not None:
            token_path = Path(telegram.token_path)
            private_files["Telegram token"] = token_path
            protected_paths["Telegram token directory"] = token_path.parent
            for name, adapter in telegram.adapter_commands.items():
                if adapter.authority == "controller":
                    executable = Path(adapter.command.argv[0])
                    protected_paths[f"controller command {name!r}"] = executable
                    protected_paths[f"controller command {name!r} directory"] = executable.parent
            if telegram.inbound_media_dir is not None:
                media_root = Path(telegram.inbound_media_dir)
                protected_paths["Telegram inbound media"] = media_root
                readable_roots["Telegram inbound media"] = media_root
        world = config.world
        if world is not None:
            lock_path = world.lock_path(state_path)
            protected_paths["world lock directory"] = lock_path.parent
            if world.lock_dir:
                # An explicit lock directory coordinates controller writes with
                # external writers. Only the pre-created lock file is shared;
                # its stable controller-owned namespace remains protected.
                shared_lock_files["world lock"] = lock_path
            else:
                protected_paths["world lock"] = lock_path
            writable_paths["Git world"] = Path(world.root)
        if config.desk is not None:
            protected_paths["desk inbox"] = Path(config.desk.inbox_dir)
            protected_paths["desk events"] = Path(config.desk.events_file)
        for name, repository in config.repositories.items():
            protected_paths[f"repository {name!r} controller Git store"] = (
                controller_git_dir(state_path, name)
            )
        return cls(
            config.execution,
            # Every child obtained from the complete-steward factory enforces
            # the instance's authority boundary, including direct embeddings
            # that never enter the daemon startup loop.
            boundary_required=config.requires_execution_boundary,
            private_files=private_files,
            protected_paths=protected_paths,
            shared_lock_files=shared_lock_files,
            writable_paths=writable_paths,
            readable_roots=readable_roots,
        )

    @property
    def enabled(self) -> bool:
        return self.config.user is not None

    def _launch(self, command: Sequence[str], *, cwd: str | Path,
                env: Mapping[str, str], **kwargs: Any) -> subprocess.Popen:
        if self.config.user is not None and sys.platform == "linux":
            from steward_harness.runtime.ownership import popen
            _user, uid, gid, _home = self._resolved_identity()
            try:
                return popen(command, cwd, env, uid=uid, gid=gid, **kwargs)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ExecutionBoundaryUnavailable(f"host-UID ownership unavailable: {exc}") from exc
        return subprocess.Popen(list(command), cwd=cwd, env=env,
                                **self._identity_kwargs(), **kwargs)

    @staticmethod
    def signal_process(process: subprocess.Popen, sig: int) -> None:
        from steward_harness.runtime.ownership import OwnedProcess
        if isinstance(process, OwnedProcess):
            process.send_signal(sig)
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.send_signal(sig)

    @staticmethod
    def process_alive(process: subprocess.Popen) -> bool:
        from steward_harness.runtime.ownership import OwnedProcess
        if isinstance(process, OwnedProcess):
            return process.poll() is None
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @property
    def python_executable(self) -> str:
        return sys.executable

    def is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def path_exists(self, path: Path) -> bool:
        return path.exists()

    def is_symlink(self, path: Path) -> bool:
        return path.is_symlink()

    def resolve_path(self, path: str | Path) -> Path:
        return Path(path).resolve()

    def child_directories(self, path: Path) -> tuple[Path, ...]:
        return tuple(p for p in path.iterdir() if p.is_dir() and not p.is_symlink())

    def candidate_directory(self, root: Path, relative: str) -> Path:
        """Resolve a command directory in the filesystem that will execute it."""
        candidate = root.resolve()
        path = (candidate / relative).resolve()
        if not path.is_relative_to(candidate):
            raise ValueError(f"Working directory escapes candidate root: {path}")
        if not path.is_dir():
            raise ValueError(f"Working directory does not exist: {path}")
        return path

    def status(self) -> BoundaryStatus:
        """Prove that this process can launch a real non-root child identity."""
        if not self.enabled:
            return BoundaryStatus(False, "no untrusted execution user is configured")
        try:
            user, uid, gid, _home = self._resolved_identity()
        except (KeyError, ValueError) as exc:
            return BoundaryStatus(False, str(exc))
        if uid == 0 or gid == 0:
            return BoundaryStatus(False, "untrusted identity resolves to root", uid, gid)
        if os.geteuid() != 0:
            return BoundaryStatus(
                False,
                "controller must run as root to drop to the configured untrusted identity",
                uid,
                gid,
            )
        if uid == os.geteuid():
            return BoundaryStatus(
                False, "controller and untrusted identity are the same", uid, gid
            )

        executable = shutil.which("id") or "/usr/bin/id"
        probe_environment = self._command_environment({})
        identity_kwargs = self._identity_kwargs()
        try:
            result = subprocess.run(
                [executable, "-u"],
                cwd=self.home,
                env=probe_environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                **identity_kwargs,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return BoundaryStatus(
                False, f"identity probe could not start: {exc}", uid, gid
            )
        if result.returncode != 0 or result.stdout.strip() != str(uid):
            detail = result.stderr.strip() or f"reported uid {result.stdout.strip()!r}"
            return BoundaryStatus(False, f"identity probe failed: {detail}", uid, gid)
        test_executable = shutil.which("test") or "/usr/bin/test"
        for label, path in (("home", self.home), ("tmpdir", Path(self.config.tmpdir))):
            try:
                writable = subprocess.run(
                    [test_executable, "-w", str(path)],
                    cwd=self.home,
                    env=probe_environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    **identity_kwargs,
                )
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                return BoundaryStatus(
                    False, f"{label} access probe could not start: {exc}", uid, gid
                )
            if writable.returncode != 0:
                return BoundaryStatus(
                    False,
                    f"untrusted identity cannot write its configured {label}: {path}",
                    uid,
                    gid,
                )
        path_failure = self._path_failure(
            uid,
            gid,
            probe_environment=probe_environment,
            identity_kwargs=identity_kwargs,
        )
        if path_failure is not None:
            return BoundaryStatus(False, path_failure, uid, gid)
        self._boundary_verified = True
        return BoundaryStatus(True, f"commands run as {user} ({uid}:{gid})", uid, gid)

    def _path_failure(
        self,
        uid: int,
        gid: int,
        *,
        probe_environment: Mapping[str, str],
        identity_kwargs: Mapping[str, Any],
    ) -> str | None:
        for label, path in self._private_files.items():
            failure = self._unstable_namespace(
                label,
                path.parent,
                uid,
                probe_environment=probe_environment,
                identity_kwargs=identity_kwargs,
            )
            if failure is not None:
                return failure
            if not os.path.lexists(path):
                continue
            if path.is_symlink():
                return f"controller-private {label} must not be a symlink: {path}"
            if path.lstat().st_uid == uid:
                return f"untrusted identity owns controller-private {label}: {path}"
            if self._identity_can(
                "-r", path, probe_environment, identity_kwargs
            ):
                return f"untrusted identity can read controller-private {label}: {path}"
            if self._identity_can(
                "-w", path, probe_environment, identity_kwargs
            ):
                return (
                    f"untrusted identity can write controller-private {label}: {path}"
                )

        for label, path in self._protected_paths.items():
            failure = self._unstable_namespace(
                label,
                path.parent,
                uid,
                probe_environment=probe_environment,
                identity_kwargs=identity_kwargs,
            )
            if failure is not None:
                return failure
            if not os.path.lexists(path):
                continue
            if path.is_symlink():
                return f"controller-owned {label} must not be a symlink: {path}"
            if path.lstat().st_uid == uid:
                return f"untrusted identity owns controller-owned {label}: {path}"
            if self._identity_can(
                "-w", path, probe_environment, identity_kwargs
            ):
                return f"untrusted identity can write controller-owned {label}: {path}"

        for label, path in self._shared_lock_files.items():
            failure = self._unstable_namespace(
                label,
                path.parent,
                uid,
                probe_environment=probe_environment,
                identity_kwargs=identity_kwargs,
            )
            if failure is not None:
                return failure
            directory = path.parent
            directory_metadata = directory.lstat()
            if not stat.S_ISDIR(directory_metadata.st_mode):
                return f"controller-shared {label} directory is not a real directory: {directory}"
            if directory_metadata.st_uid != os.geteuid():
                return f"controller-shared {label} directory is not controller-owned: {directory}"
            if directory_metadata.st_gid != gid:
                return f"controller-shared {label} directory has the wrong execution group: {directory}"
            if stat.S_IMODE(directory_metadata.st_mode) != 0o750:
                return f"controller-shared {label} directory must have mode 0750: {directory}"
            if not os.path.lexists(path):
                return f"controller-shared {label} is unavailable: {path}"
            if path.is_symlink():
                return f"controller-shared {label} must not be a symlink: {path}"
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return f"controller-shared {label} must be a regular file: {path}"
            if metadata.st_uid != os.geteuid():
                return f"controller-shared {label} is not controller-owned: {path}"
            if metadata.st_gid != gid:
                return f"controller-shared {label} has the wrong execution group: {path}"
            if stat.S_IMODE(metadata.st_mode) != 0o660:
                return f"controller-shared {label} must have mode 0660: {path}"
            if metadata.st_nlink != 1:
                return f"controller-shared {label} must have one link: {path}"
            if not self._identity_can(
                "-w", path, probe_environment, identity_kwargs
            ):
                return f"untrusted identity cannot write controller-shared {label}: {path}"

        for label, path in self._writable_paths.items():
            if not path.is_dir():
                return f"model-writable {label} is not a directory: {path}"
            if not self._identity_can(
                "-w", path, probe_environment, identity_kwargs
            ):
                return f"untrusted identity cannot write {label}: {path}"
        return None

    def _unstable_namespace(
        self,
        label: str,
        parent: Path,
        uid: int,
        *,
        probe_environment: Mapping[str, str],
        identity_kwargs: Mapping[str, Any],
    ) -> str | None:
        current = Path(parent.anchor)
        for part in parent.parts[1:]:
            current /= part
            if not os.path.lexists(current):
                break
            if current.is_symlink():
                return f"controller-owned {label} has a symlinked ancestor: {current}"
            if current.lstat().st_uid == uid:
                return (
                    f"untrusted identity owns controller-owned {label} "
                    f"namespace: {current}"
                )
            if self._identity_can(
                "-w", current, probe_environment, identity_kwargs
            ):
                return (
                    f"untrusted identity can replace controller-owned {label}: {current}"
                )
        return None

    @staticmethod
    def _identity_can(
        mode: str,
        path: Path,
        environment: Mapping[str, str],
        identity_kwargs: Mapping[str, Any],
    ) -> bool:
        executable = shutil.which("test") or "/usr/bin/test"
        try:
            result = subprocess.run(
                [executable, mode, str(path)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                **identity_kwargs,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return True
        return result.returncode == 0

    def _require_boundary(self) -> None:
        if not self._boundary_required or self._boundary_verified:
            return
        status = self.status()
        if not status.enforced:
            raise ExecutionBoundaryUnavailable(
                f"untrusted execution boundary unavailable: {status.reason}"
            )

    def can_execute(self, path: str | Path) -> bool:
        """Whether the *configured identity* can run this program.

        `os.access(path, X_OK)` answers for whoever asks, and the controller is
        root in production. Root can execute a `0700` root-owned file that the
        agent cannot touch, so `steward check` reported a provider ready that
        could not launch a single turn — the failure then arriving at the first
        real turn, on someone else's host, as something else's error message.

        Asked through the same probe the boundary check uses, so there is one
        answer to "can the agent run this" rather than two that can disagree.
        With no configured identity the controller *is* the agent and
        `os.access` is the right question.
        """
        target = Path(path)
        if not target.is_file():
            return False
        if not self.enabled:
            return os.access(target, os.X_OK)
        return self._identity_can(
            "-x", target, self._command_environment({}), self._identity_kwargs()
        )

    def grant_read_access(self, path: str | Path) -> None:
        """Expose one controller-created path through a configured read-only root."""
        if not self.enabled:
            return
        requested = Path(os.path.abspath(path))
        if not any(
            requested == root or root in requested.parents
            for root in self._readable_roots.values()
        ):
            raise ValueError(f"read-only path is outside configured roots: {requested}")
        if requested.is_symlink():
            raise ValueError(f"read-only path must not be a symlink: {requested}")
        _user, _uid, gid, _home = self._resolved_identity()
        os.chown(requested, -1, gid)
        os.chmod(requested, 0o750 if requested.is_dir() else 0o640)
        environment = self._command_environment({})
        identity_kwargs = self._identity_kwargs()
        if not self._identity_can("-r", requested, environment, identity_kwargs):
            raise RuntimeError(
                f"untrusted identity cannot read shared path: {requested}"
            )
        if requested.is_dir() and not self._identity_can(
            "-x", requested, environment, identity_kwargs
        ):
            raise RuntimeError(
                f"untrusted identity cannot traverse shared path: {requested}"
            )
        if self._identity_can("-w", requested, environment, identity_kwargs):
            raise RuntimeError(f"untrusted identity can write shared path: {requested}")

    @property
    def home(self) -> Path:
        if not self.enabled:
            return Path(os.environ.get("HOME", "/tmp"))
        _user, _uid, _gid, account_home = self._resolved_identity()
        return Path(self.config.home or account_home)

    def _identity_environment(
        self,
        source: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Apply the configured identity's inherited-environment policy."""
        inherited = os.environ if source is None else source
        environment = {
            name: value
            for name, value in inherited.items()
            if name in self.config.inherited_environment
        }
        environment.update({"HOME": str(self.home), "TMPDIR": self.config.tmpdir})
        if self.enabled:
            user, _uid, _gid, _account_home = self._resolved_identity()
        else:
            user = pwd.getpwuid(os.geteuid()).pw_name
        # Native credential lookup can depend on the account name even when
        # this inert/local broker does not change UID (Claude's macOS keychain).
        # Derive it from the actual identity, never an inherited USER value.
        environment.update({"LOGNAME": user, "USER": user})
        environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        return environment

    def _provider_environment(self, source: Mapping[str, str]) -> dict[str, str]:
        environment = self._identity_environment(source)
        environment.update(
            (name, value)
            for name, value in source.items()
            if name in _PROVIDER_ENVIRONMENT
        )
        return environment

    def _command_environment(
        self,
        source: Mapping[str, str] | None = None,
        *,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        if extra is not None:
            forbidden = _PROVIDER_ENVIRONMENT.intersection(extra)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValueError(
                    f"command metadata cannot contain provider credentials: {names}"
                )
        environment = self._identity_environment(source)
        for name in _PROVIDER_ENVIRONMENT:
            environment.pop(name, None)
        if extra:
            environment.update(extra)
        return environment

    def popen(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        **kwargs: Any,
    ) -> subprocess.Popen[bytes]:
        self._require_boundary()
        return self._launch(
            list(command),
            cwd=cwd,
            env=self._provider_environment(env),
            **kwargs,
        )

    def popen_command(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start one credentialless local command, optionally with binary pipes."""
        self._require_boundary()
        return self._launch(
            list(command),
            cwd=cwd,
            env=self._command_environment(extra=extra_env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )

    def git_service(self, service: str) -> str:
        """The command `git fetch --upload-pack` or `git push --receive-pack` runs.

        That Git transport service, in an agent repository, as the agent
        identity with its environment, so the controller's Git speaks the
        ordinary pack protocol across the boundary instead of reading or
        writing an agent repository itself.
        """
        self._require_boundary()
        command = hardened_git_argv("-c", "uploadpack.allowAnySHA1InWant=true", service)
        if self.enabled:
            _user, uid, gid, _home = self._resolved_identity()
            environment = self._command_environment(extra={"GIT_NO_LAZY_FETCH": "1"})
            command = ["env", "-i", *(f"{name}={value}" for name, value in sorted(environment.items())),
                       "setpriv", f"--reuid={uid}", f"--regid={gid}", "--clear-groups", "--", *command]
        return "umask 077; " + shlex.join(command)

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path,
        timeout: float,
        env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        input_text: str | bytes | None = None,
        text: bool = True,
    ) -> subprocess.CompletedProcess[Any]:
        self._require_boundary()
        if self.config.user is not None and sys.platform == "linux":
            process = self._launch(command, cwd=cwd,
                env=self._command_environment(env, extra=extra_env),
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
                start_new_session=True)
            try:
                stdout, stderr = process.communicate(input_text, timeout=timeout)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except BaseException:
                process.terminate()
                process.communicate(timeout=15)
                raise
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=self._command_environment(env, extra=extra_env),
            capture_output=True,
            input=input_text,
            text=text,
            timeout=timeout,
            check=False,
            **self._identity_kwargs(),
        )

    def _resolved_identity(self) -> tuple[str, int, int, str]:
        configured_user = self.config.user
        if configured_user is None:
            raise ValueError("no untrusted execution user is configured")
        try:
            account = pwd.getpwnam(configured_user)
        except KeyError as exc:
            raise KeyError(
                f"untrusted execution user {configured_user!r} does not exist"
            ) from exc
        if self.config.group is None:
            gid = account.pw_gid
        else:
            try:
                gid = grp.getgrnam(self.config.group).gr_gid
            except KeyError as exc:
                raise KeyError(
                    f"untrusted execution group {self.config.group!r} does not exist"
                ) from exc
        return account.pw_name, account.pw_uid, gid, account.pw_dir

    def _identity_kwargs(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        _user, uid, gid, _home = self._resolved_identity()
        return {"user": uid, "group": gid, "extra_groups": (), "umask": 0o077}
