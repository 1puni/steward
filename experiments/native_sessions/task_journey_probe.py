"""Disposable native task → gate → running release → owning-session result.

Invoked by codex_probe.py --task-only/--live-sources or glm_probe.py --live-sources.
Uses model quota and loopback HTTP;
all Git publication and release mutation stay beneath the disposable fixture.
The service controller is local subprocess management, not systemd/split UID.
"""
from __future__ import annotations

import shutil
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import socket
import subprocess
import sys
from pathlib import Path

from steward_harness.cognition import Cognition
from steward_harness.config.schema import CommandSpec, DeployConfig, RepositoryConfig, UntrustedExecutionConfig
from steward_harness.conversations import ConversationService
from steward_harness.delivery import ResultDeliveryDrain
from steward_harness.deploy.controller import DeploymentController
from steward_harness.deploy.engine import DeployEngine
from steward_harness.deployment import DeploymentConvergence
from steward_harness.git_transport import ControllerGitTransport
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.providers.claude import ClaudeRuntime
from steward_harness.runtime.providers.codex_app_server import CodexAppServerRuntime
from steward_harness.state import ConversationBusy, StateDatabase
from steward_harness.task_runner import TaskRunner
from steward_harness.world.git_world import GitWorld
from steward_harness.lease import Lease
from steward_harness.world.turn_checkpoint import WorldTurnCheckpoint

_APP = '''import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
root = Path(__file__).resolve().parent
sha = root.name
healthy = (root / "result.txt").read_text().strip() == "completed"
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if healthy else 503)
        self.end_headers()
        self.wfile.write(json.dumps({"sha": sha}).encode())
    def log_message(self, *args): pass
HTTPServer.allow_reuse_address = True
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
'''


def task_journey_probe(
    world: Path, native_home: Path, *, live_sources: bool = False,
    family: str = "codex", credential_path: Path | None = None,
) -> None:
    root = world.parent

    def git(*args, cwd=root):
        return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()

    if live_sources and family == "glm":
        (world / "inputs").mkdir()
        for index in range(5):
            (world / "inputs" / f"{index}.txt").write_text(f"Native file-tool fixture input {index}.\n")
        git("add", "inputs", cwd=world)
        git("commit", "-qm", "Add native live-input reading fixture", cwd=world)
    remote, repo = root / "origin.git", root / "repo"
    git("init", "-q", "--bare", "-b", "main", str(remote))
    git("clone", "-q", str(remote), str(repo))
    git("config", "user.name", "Native task probe", cwd=repo)
    git("config", "user.email", "probe@localhost", cwd=repo)
    (repo / "app.py").write_text(_APP)
    git("add", "app.py", cwd=repo)
    git("commit", "-qm", "Initialize runnable probe", cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    policy = DeployConfig(
        service="disposable-probe", release_root=str(root / "releases"),
        current_symlink=str(root / "current"), health_url=f"http://127.0.0.1:{port}",
        timeout_seconds=10,
    )
    gate = CommandSpec(argv=(sys.executable, "-c", "from pathlib import Path; assert Path('result.txt').read_text().strip() == 'completed'"))
    repository = RepositoryConfig(path=str(repo), remote_url=str(remote), gates=(gate,), deploy=policy)
    state = StateDatabase(root / "state.db")
    broker = UntrustedExecutionBroker(UntrustedExecutionConfig())
    transport = ControllerGitTransport(state.path, "app", str(remote), "main", allow_local=True)
    requests = []
    offered_inputs = []

    runtime_type = {"codex": CodexAppServerRuntime, "glm": ClaudeRuntime}[family]

    class ObservedRuntime(runtime_type):
        def execute(self, request):
            if "LIVE_SOURCE_FILE_FIXTURE" in request.prompt:
                request = replace(request, prompt=request.prompt +
                    f"\nThis execution's exact candidate directory is {request.cwd}. "
                    f"Use {request.cwd}/started.txt, {request.cwd}/marker.txt, "
                    f"{request.cwd}/observation.txt and {request.cwd}/inputs/ explicitly.")
            if request.on_input_ready is not None:
                ready = request.on_input_ready

                def on_ready(sender):
                    def offer(source):
                        offered_inputs.append(source)
                        sender(source)
                    ready(offer)

                request = replace(request, on_input_ready=on_ready)
            requests.append(request)
            return super().execute(request)

        def _execute(self, request, session_id, environment, *, resume=None):
            original_run = self._controller.run

            def observed_run(command, *, on_stdout_line, **kwargs):
                def observe(line):
                    event = json.loads(line)
                    if event.get("type") in {"result", "command_lifecycle"}:
                        print("native", event["type"], event.get("state", event.get("terminal_reason")), flush=True)
                    if event.get("type") == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "tool_use":
                                print("native tool", block.get("name"), flush=True)
                    on_stdout_line(line)
                return original_run(command, on_stdout_line=observe, **kwargs)

            self._controller.run = observed_run
            try:
                return super()._execute(request, session_id, environment, resume=resume)
            finally:
                self._controller.run = original_run

    class LocalService:
        process = None
        restarts = 0

        def close(self):
            if self.process is not None:
                self.process.terminate()
                self.process.wait(timeout=10)
                self.process = None

        def restart(self, service):
            assert service == "disposable-probe"
            self.close()
            self.restarts += 1
            self.process = subprocess.Popen(
                [sys.executable, "-B", str((root / "current/app.py").resolve()), str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    services = LocalService()

    class LocalDeployment(DeploymentController):
        def _engine(self, repo_slug, before_rollback=None):
            return DeployEngine(repo_slug, self.policies[repo_slug], self.transports[repo_slug], services, before_rollback)

    boundary = LocalDeployment({"app": policy}, {"app": transport}, None)
    convergence = DeploymentConvergence(state, boundary)
    runtime = ObservedRuntime(
        Path(shutil.which("codex" if family == "codex" else "claude")),
        controller=ProcessController(broker), native_home=native_home,
        **({"base_url": "https://api.z.ai/api/anthropic", "credential_path": credential_path, "family": "glm"} if family == "glm" else {}),
    )
    cognition = Cognition({family: runtime})
    checkpoint = WorldTurnCheckpoint(
        GitWorld(world, execution_broker=broker), Lease(root / "world-lock"),
        root / "world-turns", execution_broker=broker, state=state,
    )
    state.tasks.repositories = {"app"}
    conversations = ConversationService(
        state, cognition, provider_order=(family,), profile="fast", workspace=checkpoint,
        timeout_seconds=150,
    )
    intent = dict(
        transport="telegram", transport_key="1", source_event_key="disposable-intent", operator_id="probe",
        text=("In configured repository app, create result.txt containing completed followed by a newline. "
              "Leave app.py unchanged. This is the entire task. Its configured gate, publication and local "
              "deployment are authorized. Propose the repository task now; do not implement it in your "
              "conversation world or request further approval. No additional tasks are needed after it succeeds."),
    )
    try:
        admitted = conversations.run_turn(**intent)
        assert admitted.task_admission is not None, admitted.reply_text
        task_id = admitted.task_admission.task_id
        owner_session = state.find_conversation(intent["transport"], intent["transport_key"]).provider_session_id
        print("native owning-session admission", task_id, owner_session, flush=True)
        runner = TaskRunner(
            state=state, repositories={"app": repository}, transports={"app": transport},
            worktrees_root=root / "task-worktrees", broker=broker, cognition=cognition,
            provider_fallbacks=(), timeout_seconds=150,
        )
        from steward_harness.repository_reconciler import RepositoryReconciler
        from steward_harness.kernel import StewardKernel
        from steward_harness.state import TaskStatus
        publication = RepositoryReconciler(
            state=state, repositories={"app": repository}, transports={"app": transport},
            worktrees_root=root / "task-worktrees", broker=broker,
        )
        kernel = StewardKernel(state, publication, convergence.converge, runner)
        try:
            deadline = time.monotonic() + 300
            while state.tasks.get(task_id).status is not TaskStatus.DONE:
                kernel.tick()
                assert time.monotonic() < deadline, "task journey did not complete"
                time.sleep(0.02)
        finally:
            kernel.stop()
        from steward_harness.state import DeploymentId
        with state.connect() as connection:
            deployment_id = DeploymentId(connection.execute(
                "SELECT deployment_id FROM promotion_publications WHERE deployment_id IS NOT NULL"
            ).fetchone()[0])
        deployed = state.get_deployment(deployment_id)
        assert deployed.status.value == "succeeded", deployed
        sha = deployed.desired_sha
        assert boundary.inspect("app", sha).current_sha == sha
        assert (root / "current/result.txt").read_text().strip() == "completed"
        assert git(f"--git-dir={remote}", "rev-parse", "main") == sha
        with state.connect() as connection:
            assert connection.execute("SELECT tested_sha FROM promotion_publications").fetchone()[0] == sha
        print("gate, exact publication, running release health", sha, flush=True)
        received = []
        drain = ResultDeliveryDrain(state, {"telegram": received.append}, review=conversations.review_task_result)
        if live_sources:
            active = dict(
                transport="telegram", transport_key="1", source_event_key="active-world-work",
                operator_id="probe",
                text=("In your current conversation world, use a shell to write started.txt with working, "
                      "sleep 12 seconds, then write marker.txt containing ORIGINAL. A correction and a "
                      "controller task result will arrive while you work. Apply the correction and record "
                      "the controller's exact published SHA in observation.txt. No repository task is needed. "
                      "Keep your final response short; do not finish before the shell completes."),
            )
            if family == "glm":
                active["text"] = (
                    "LIVE_SOURCE_FILE_FIXTURE: in your current conversation candidate, use native Write "
                    "to create started.txt containing working. Then use native Read to inspect each of "
                    "the five inputs/*.txt files separately. Finally use Write to create marker.txt "
                    "containing ORIGINAL. A correction and controller task result will arrive while "
                    "you work: apply the correction and write the exact published SHA to observation.txt. "
                    "Do not use Bash or schedule future work in this bounded fixture. Finish with a short "
                    "reply after applying the incoming evidence; no repository task is needed."
                )
            correction = dict(
                transport="telegram", transport_key="1", source_event_key="live-correction",
                operator_id="probe",
                text="Correction: marker.txt must contain CORRECTED instead of ORIGINAL. Apply this in your current world.",
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                working = executor.submit(conversations.run_turn, **active)
                deadline = time.monotonic() + 120
                while not (len(requests) > 2 and (requests[-1].cwd / "started.txt").exists()):
                    if working.done():
                        raise AssertionError(f"native execution ended before live offer: {working.result()}")
                    if time.monotonic() > deadline:
                        raise TimeoutError(
                            "native execution did not reach its active tool; observed marker paths: "
                            + str([str(path.relative_to(root)) for path in root.rglob("started.txt")])
                        )
                    time.sleep(0.1)
                try:
                    conversations.run_turn(**correction)
                except ConversationBusy:
                    pass
                else:
                    raise AssertionError("live source should wait for world acceptance")
                drain.advance_one()
                assert not received, "native delivery is not world acceptance or transport delivery"
                accepted = working.result(timeout=150)
            assert (world / "marker.txt").read_text().strip() == "CORRECTED"
            assert sha in (world / "observation.txt").read_text()
            assert {source.origin for source in offered_inputs} == {"operator", "controller"}
            assert len(offered_inputs) == 2
            correction_replay = conversations.run_turn(**correction)
            assert correction_replay.reply_text == accepted.reply_text
            assert conversations.run_turn(**active).reply_text == accepted.reply_text
            print("active correction and attributed controller result shared one accepted native execution", flush=True)
        drain.advance_one()
        assert len(received) == 1 and sha in received[0].text
        assert state.find_conversation(intent["transport"], intent["transport_key"]).provider_session_id == owner_session
        assert len(requests) == 3, "admission, native task, owning-session execution; no separate live-result assessment"
        assert requests[-1].provider_session_id == owner_session
        assert list(world.glob(f"artefacts/{family}/**/*.jsonl"))
        assert list((root / "current").glob(f"artefacts/{family}/**/*.jsonl"))
        assert not list(world.rglob("auth.json"))
        replay = conversations.run_turn(**intent)
        assert replay.task_admission.task_id == task_id
        reopened = StateDatabase(state.path)
        assert DeploymentConvergence(reopened, boundary).converge(deployment_id).desired_sha == sha
        drain = ResultDeliveryDrain(reopened, {"telegram": received.append}, review=lambda _: (_ for _ in ()).throw(AssertionError("assessment repeated")))
        reopened.recover_result_deliveries()
        assert drain.advance_one() is None
        assert services.restarts == 1 and len(received) == 1 and len(requests) == 3
        print("same native owner received result; source/deployment/outbox replay did not repeat effects", flush=True)
    finally:
        services.close()
