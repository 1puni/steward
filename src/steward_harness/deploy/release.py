"""Immutable release directories with atomic current-symlink activation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tarfile
from pathlib import Path

from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.artifact import construct_artifact
from steward_harness.git import validate_object_id
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.git_transport import ControllerGitTransport, GitTransportError


class ReleaseError(RuntimeError):
    """Raised when staging or activating a release fails."""


class ReleaseManager:
    """Stages immutable per-SHA release directories and swaps the current symlink atomically."""

    def __init__(
        self,
        repo_name: str,
        transport: ControllerGitTransport,
        config: SystemdReleaseConfig,
        broker: UntrustedExecutionBroker | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.transport = transport
        self.config = config
        self.broker = broker
        self.releases_root = Path(config.release_root) / repo_name
        self.releases_root.mkdir(parents=True, exist_ok=True)
        # Each release is opened to 0755 so a separate service identity can run
        # it; the directory holding them must be traversable too, or that
        # identity dies at chdir. A supervised worker's umask would make it 0700.
        self.releases_root.chmod(0o755)
        self.current_symlink = Path(config.current_symlink)

    def _identity(self, sha: str) -> tuple[str, str]:
        try:
            validate_object_id(sha)
        except ValueError:
            raise ReleaseError(f"Release SHA must be a full object ID, got {sha!r}")
        try:
            return self.transport.release_identity(sha)
        except (GitTransportError, ValueError) as exc:
            raise ReleaseError(f"Release SHA {sha!r} is unavailable") from exc

    @staticmethod
    def _manifest(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            stat = path.lstat()
            # Interpreter caches are disposable runtime output. Keep inspecting
            # other content in cache directories, and never exempt symlinks.
            if not path.is_symlink() and (
                (path.is_dir() and path.name == "__pycache__")
                or (path.is_file() and path.parent.name == "__pycache__" and path.suffix == ".pyc")
            ):
                continue
            if path.is_symlink():
                record = ("symlink", relative, os.readlink(path))
            elif path.is_dir():
                record = ("directory", relative)
            elif path.is_file():
                record = ("file", relative, bool(stat.st_mode & 0o111), stat.st_size)
            else:
                raise ReleaseError(f"Release contains unsupported file type: {relative}")
            digest.update(json.dumps(record, separators=(",", ":")).encode())
            digest.update(b"\0")
            if path.is_file() and not path.is_symlink():
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")
        return digest.hexdigest()

    def _receipt_path(self, sha: str) -> Path:
        return self.releases_root / f".{sha}.release.json"

    def _receipt(self, sha: str, tree: str, manifest: str) -> dict:
        identity = {"sha": sha, "tree": tree, "manifest": manifest}
        if self.config.artifact_build is not None:
            identity["artifact_build"] = self.config.artifact_build.model_dump(mode="json")
        return identity

    def _write_receipt(self, sha: str, tree: str, manifest: str) -> None:
        receipt = self._receipt_path(sha)
        staged = receipt.with_name(f"{receipt.name}.{os.getpid()}.tmp")
        staged.write_text(
            json.dumps(self._receipt(sha, tree, manifest), sort_keys=True) + "\n"
        )
        os.replace(staged, receipt)

    def _verify_release(
        self, release_dir: Path, sha: str, tree: str, *, check_policy: bool = True,
    ) -> None:
        receipt = self._receipt_path(sha)
        try:
            identity = json.loads(receipt.read_text())
        except FileNotFoundError:
            if self.config.artifact_build is not None:
                raise ReleaseError(f"Artifact release {sha!r} has no staging receipt") from None
            temporary = Path(
                tempfile.mkdtemp(prefix=f".{sha}.verification-", dir=self.releases_root)
            )
            try:
                self._materialize(sha, temporary)
                manifest = self._manifest(temporary)
                if self._manifest(release_dir) != manifest:
                    raise ReleaseError(
                        f"Existing release {sha!r} does not match the committed tree"
                    )
                self._write_receipt(sha, tree, manifest)
                return
            finally:
                shutil.rmtree(temporary, ignore_errors=True)
        except (OSError, ValueError) as exc:
            raise ReleaseError(f"Existing release {sha!r} has no valid staging receipt") from exc
        expected = self._receipt(sha, tree, self._manifest(release_dir))
        if not check_policy and isinstance(identity, dict):
            expected.pop("artifact_build", None)
            if "artifact_build" in identity:
                expected["artifact_build"] = identity["artifact_build"]
        if identity != expected:
            raise ReleaseError(f"Existing release {sha!r} does not match its staging receipt")

    def _materialize(self, sha: str, destination: Path) -> None:
        descriptor, archive_name = tempfile.mkstemp(
            prefix=f".{sha}.archive-", suffix=".tar", dir=self.releases_root
        )
        os.close(descriptor)
        archive_path = Path(archive_name)
        try:
            try:
                self.transport.export_release_archive(sha, archive_path)
            except (GitTransportError, ValueError) as exc:
                raise ReleaseError(f"git archive {sha} failed") from exc
            extract = subprocess.run(
                # The exporter fixes safe modes; preserve them even when an
                # unprivileged staging process has a private umask of 077.
                ["tar", "-x", "-p", "-f", str(archive_path), "-C", str(destination)],
                check=False,
                capture_output=True,
                timeout=300,
            )
            if extract.returncode != 0:
                raise ReleaseError(
                    f"tar extraction failed: {extract.stderr.decode().strip()}"
                )
        finally:
            archive_path.unlink(missing_ok=True)

    def stage(self, sha: str) -> Path:
        """Materialize the committed tree of ``sha`` into an immutable release directory."""
        sha, tree = self._identity(sha)
        release_dir = self.releases_root / sha
        if release_dir.exists():
            self._verify_release(release_dir, sha, tree)
            return release_dir

        temporary = Path(tempfile.mkdtemp(prefix=f".{sha}.staging-", dir=self.releases_root))
        try:
            self._materialize(sha, temporary)
            if self.config.artifact_build is not None:
                if self.broker is None:
                    raise ReleaseError("artifact construction requires the execution broker")
                built = Path(tempfile.mkdtemp(prefix=f".{sha}.artifact-", dir=self.releases_root))
                try:
                    construct_artifact(temporary, built, self.config.artifact_build, self.broker)
                    shutil.rmtree(temporary)
                    built.rename(temporary)
                except (ValueError, OSError, tarfile.TarError) as exc:
                    raise ReleaseError(f"artifact construction failed: {exc}") from exc
                finally:
                    shutil.rmtree(built, ignore_errors=True)
            # mkdtemp's private root must not survive as the published release:
            # the separate service/agent identity needs to traverse its code.
            temporary.chmod(0o755)
            manifest = self._manifest(temporary)
            self._write_receipt(sha, tree, manifest)
            try:
                temporary.rename(release_dir)
            except OSError as exc:
                if not release_dir.exists():
                    raise ReleaseError(f"Could not publish staged release {sha!r}: {exc}") from exc
                self._verify_release(release_dir, sha, tree)
                return release_dir
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return release_dir

    def verify(self, sha: str, *, check_policy: bool = True) -> Path:
        """Verify that an existing release is complete and still immutable."""
        sha, tree = self._identity(sha)
        release_dir = self.releases_root / sha
        if not release_dir.is_dir():
            raise ReleaseError(f"Release {sha!r} is not staged")
        self._verify_release(release_dir, sha, tree, check_policy=check_policy)
        return release_dir

    def activate(self, sha: str, *, check_policy: bool = True) -> Path:
        """Point the current symlink at a release with an atomic rename."""
        release_dir = self.verify(sha, check_policy=check_policy)
        self.current_symlink.parent.mkdir(parents=True, exist_ok=True)
        staged_link = self.current_symlink.parent / f".{self.current_symlink.name}.staging-{os.getpid()}"
        if staged_link.is_symlink() or staged_link.exists():
            staged_link.unlink()
        staged_link.symlink_to(release_dir, target_is_directory=True)
        try:
            os.replace(staged_link, self.current_symlink)
        except OSError as exc:
            staged_link.unlink(missing_ok=True)
            raise ReleaseError(f"Atomic symlink swap failed: {exc}") from exc
        return release_dir

    def deactivate(self) -> None:
        """Atomically restore the explicit state in which no release is active."""
        if not os.path.lexists(self.current_symlink):
            return
        # Validate the pointer before moving it.  The deployment lease held by
        # DeployEngine prevents another harness mutation between these steps.
        self.current_release()
        removed = self.current_symlink.parent / (
            f".{self.current_symlink.name}.removed-{os.getpid()}"
        )
        if os.path.lexists(removed):
            removed.unlink()
        try:
            os.replace(self.current_symlink, removed)
        except OSError as exc:
            raise ReleaseError(f"Atomic release deactivation failed: {exc}") from exc
        removed.unlink(missing_ok=True)

    def current_release(self) -> str | None:
        """SHA of the managed active release, or None when no pointer exists."""
        if not os.path.lexists(self.current_symlink):
            return None
        if not self.current_symlink.is_symlink():
            raise ReleaseError(
                f"Current release pointer {self.current_symlink} is not a symlink"
            )
        try:
            target = self.current_symlink.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ReleaseError(
                f"Current release pointer {self.current_symlink} is invalid"
            ) from exc
        try:
            validate_object_id(target.name)
        except ValueError:
            valid_name = False
        else:
            valid_name = True
        if (
            target.parent != self.releases_root.resolve()
            or not valid_name
            or not target.is_dir()
        ):
            raise ReleaseError(
                f"Current release pointer {self.current_symlink} is outside managed releases"
            )
        return target.name

    def failed(self, sha: str) -> bool:
        """Whether this release already broke: one directory entry, no column."""
        return (self.releases_root / f".{sha}.failed").exists()

    def mark_failed(self, sha: str) -> None:
        """Record a broken release, written before its pointer moves back."""
        (self.releases_root / f".{sha}.failed").touch()

    def clear_failed(self, sha: str) -> None:
        """Retire a condemnation the same release has since disproved."""
        (self.releases_root / f".{sha}.failed").unlink(missing_ok=True)

    def prune(self, keep: int = 5, *, retain: str | None = None) -> list[str]:
        """Delete oldest releases beyond ``keep``, never the active or deployed one.

        ``retain`` is the release the deployed ref names, which rollback needs
        to still have a directory even after the pointer has moved past it.
        Failure markers outlive artifacts: retention cannot authorize a retry.
        """
        current = self.current_release()
        staged = sorted(
            (p.stat().st_mtime, p.name)
            for p in self.releases_root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
        candidates = staged[:-keep] if keep > 0 else staged
        removed: list[str] = []
        for _mtime, name in candidates:
            if name in {current, retain}:
                continue
            shutil.rmtree(self.releases_root / name, ignore_errors=True)
            self._receipt_path(name).unlink(missing_ok=True)
            removed.append(name)
        return removed
