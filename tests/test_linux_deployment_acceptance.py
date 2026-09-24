"""Real Linux identities, artifact import, HTTP runtime, and durable rollback.

Run as root on a disposable checkout. All state and subprocesses belong to a
fresh temporary directory and transient invocation units; no installed service is touched.
"""

import grp
import json
import os
import pwd
import socket
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from steward_harness.config.schema import RepositoryConfig, UntrustedExecutionConfig
from steward_harness.deploy.engine import DeployEngine
from steward_harness.deploy.config import SystemdReleaseConfig
from steward_harness.deploy.transport import ReleaseTransport
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.execution import UntrustedExecutionBroker
from test_task_runner_kernel import _git, _repository


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="requires Linux root and existing nobody/daemon execution identities",
)


_SERVER = """
import json, os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import dependency
root = Path.cwd()
assert os.getuid() != 0 and os.getuid() != dependency.BUILDER_UID
assert not os.access(root / 'dependency.py', os.W_OK)
sha = root.name if Path('ready.txt').read_text() == 'ready' else 'wrong-sha'
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'sha': sha, 'uid': os.getuid(),
                                    'builder_uid': dependency.BUILDER_UID}).encode())
    def log_message(self, *args):
        pass
HTTPServer(('127.0.0.1', int(os.environ['PORT'])), Handler).serve_forever()
"""


@pytest.mark.parametrize("prior_release", [False, True])
def test_linux_split_identity_http_deployment_and_rollback(prior_release):
    builder = pwd.getpwnam("nobody")
    runtime = pwd.getpwnam("daemon")
    assert builder.pw_uid != runtime.pw_uid
    with tempfile.TemporaryDirectory(prefix="steward-linux-deploy-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        home = root / "build-home"
        home.mkdir(mode=0o700)
        os.chown(home, builder.pw_uid, builder.pw_gid)
        private = root / "controller-private"
        private.mkdir(mode=0o700)
        (private / "authority").write_text("synthetic controller-only sentinel")
        remote, clone = _repository(root)
        (clone / "server.py").write_text(_SERVER)
        (clone / "ready.txt").write_text("ready")
        _git("add", ".", cwd=clone)
        _git("commit", "-qm", "working HTTP release", cwd=clone)
        _git("push", "-q", "origin", "main", cwd=clone)
        previous = _git("rev-parse", "HEAD", cwd=clone)
        transport = ControllerGitTransport(
            private / "state.db", "app", str(remote), "main", allow_local=True,
        )
        transport.fetch()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        builder_uid = builder.pw_uid
        service_name = f"steward-acceptance-{uuid.uuid4().hex}.service"
        build = (
            "import os; from pathlib import Path; "
            f"assert os.getuid() == {builder_uid}; "
            f"assert not os.access({str(private / 'authority')!r}, os.R_OK); "
            "assert not os.getenv('GITHUB_TOKEN'); "
            "Path('dependency.py').write_text('BUILDER_UID = ' + str(os.getuid()) + '\\n')"
        )
        settings = SystemdReleaseConfig.model_validate(yaml.safe_load(yaml.safe_dump({
                "service": service_name,
                "release_root": str(root / "releases"),
                "current_symlink": str(root / "current"),
                "health_url": f"http://127.0.0.1:{port}/healthz",
                "timeout_seconds": 10,
                "artifact_build": {"argv": [sys.executable, "-c", build]},
        })))
        transport = ReleaseTransport(transport, "production", "main")
        broker = UntrustedExecutionBroker(UntrustedExecutionConfig(
            user=builder.pw_name, group=grp.getgrgid(builder.pw_gid).gr_name,
            home=str(home),
        ))

        class Service:
            process = None
            starts = 0
            stops = 0

            def stop(self, name):
                assert name == service_name
                self.stops += 1
                if self.process is not None:
                    self.process.terminate()
                    self.process.wait(timeout=10)
                    self.process = None

            def restart(self, name):
                self.stop(name)
                self.starts += 1
                self.process = subprocess.Popen(
                    [sys.executable, "-B", "server.py"],
                    cwd=(root / "current").resolve(strict=True),
                    env={"PATH": "/usr/bin:/bin", "PORT": str(port)},
                    user=runtime.pw_uid, group=runtime.pw_gid, extra_groups=[],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )

        service = Service()
        engine = DeployEngine("app", settings, transport,
                              services=service, broker=broker)
        # The only test of the real boundary — real UID split, real HTTP health,
        # real rollback. It drives the derivation the way a pass does: ask what
        # the observed ref makes due, then release exactly that. `settled` is a
        # clock far enough past every committer date here that the debounce
        # never hides a defect in what follows.
        settled = datetime.now(UTC) + timedelta(days=1)
        try:
            if prior_release:
                successful = engine.deploy(previous)
                assert successful.success, successful
                # Written only after the health gate passed, and by nothing else.
                assert transport.deployed_sha() == previous
                # Converged: the same observation now derives no work at all.
                response = httpx.get(settings.health_url).json()
                assert response == {"sha": previous, "uid": runtime.pw_uid,
                                    "builder_uid": builder_uid}
                published = (root / "current").resolve()
                assert all(path.stat().st_uid == 0 for path in published.rglob("*"))
                denied = broker.run(
                    [sys.executable, "-c", "from pathlib import Path; "
                     "Path('dependency.py').write_text('tampered')"],
                    cwd=published, timeout=10,
                )
                assert denied.returncode != 0 and "PermissionError" in denied.stderr

            (clone / "ready.txt").write_text("unhealthy")
            _git("add", ".", cwd=clone)
            _git("commit", "-qm", "unhealthy HTTP revision", cwd=clone)
            _git("push", "-q", "origin", "main", cwd=clone)
            desired = transport.fetch()
            failed = engine.deploy(desired)
            assert not failed.success, failed
            assert failed.rolled_back_to == (previous if prior_release else None)
            # A failed release is a directory entry beside releases/<sha>/ ...
            assert (engine.releases.releases_root / f".{desired}.failed").is_file()
            assert engine.releases.failed(desired)
            # ... and the deployed ref never named the release that broke.
            assert transport.deployed_sha() == (previous if prior_release else None)
            if prior_release:
                assert httpx.get(settings.health_url).json()["sha"] == previous
                assert engine.releases.current_release() == previous
            else:
                assert not (root / "current").exists()
                assert service.process is None
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
            starts, stops = service.starts, service.stops
            # The termination condition, at the real boundary: the ref has not
            # moved, so the next pass observes exactly what this one did and
            # derives nothing. Without the marker this restarts the live
            # process on every poll, for as long as the tip stays put.
            assert (service.starts, service.stops) == (starts, stops)
            assert transport.deployed_sha() == (previous if prior_release else None)
            print(json.dumps({"prior_release": prior_release,
                              "controller_uid": os.getuid(),
                              "builder_uid": builder_uid,
                              "runtime_uid": runtime.pw_uid,
                              "deployed_ref": transport.deployed_sha(),
                              "rollback_active_sha": failed.rolled_back_to,
                              "restart_replayed": False}))
        finally:
            service.stop(service_name)
