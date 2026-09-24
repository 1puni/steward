"""Native Codex App Server turns over broker-owned stdio."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from threading import RLock

from steward_harness.runtime.contracts import (
    Availability,
    MissingProviderSession,
    ProviderCapabilities,
    ProviderFamily,
    RuntimeExecutionError,
    RuntimeInput,
    RuntimeInputResult,
    RuntimeRequest,
    RuntimeResult,
    RuntimeUnavailable,
    validated_uuid,
)
from steward_harness.runtime.process import ProcessController, ProcessInput
from steward_harness.runtime.native_workspace import native_workspace


def _declines_turn(message: str) -> bool:
    """Whether Codex is declining this turn rather than failing at it.

    Two different facts arrive by the same route. A usage limit is not a defect
    in the work: the same prompt succeeds when the window resets, and succeeds
    *now* on any other provider. Nor is a dead sign-in — an expired or revoked
    refresh token says nothing about the turn, and the operator's next login
    fixes it without the prompt changing. Both mean "not by me", so both must
    reach the next configured provider instead of ending the turn.

    Codex reports each as an ordinary turn error, so its wording is the only
    thing separating "this cannot be done" from "not by me" — which is why the
    reading belongs here, in the adapter that owns the wire format, and not in
    the lifecycle, which must never learn a provider's name.

    The credential markers are the ones `c2bdd38` established against the CLI
    provider this adapter replaced, kept deliberately narrow: a bare "refresh
    token" would also match a model discussing OAuth in its own reply.
    """
    text = message.casefold()
    if "usage limit" in text:
        return True
    return any(
        marker in text
        for marker in (
            "invalid_refresh_token",
            "token_expired",
            # "Your access token could not be refreshed because your refresh
            # token was revoked / was already used. Please log out and sign in."
            "access token could not be refreshed",
            "log out and sign in",
        )
    )


class CodexAppServerRuntime:
    """Map native lifecycle and steering; the provider owns tools and children."""

    family: ProviderFamily = "codex"
    capabilities = ProviderCapabilities(images=True, ongoing_input=True)

    def __init__(
        self, executable: Path = Path("/usr/bin/codex"), *,
        controller: ProcessController, native_home: Path,
    ):
        self.executable = executable
        self._controller = controller
        if not native_home.is_absolute():
            raise ValueError("Codex native home must be absolute")
        self.native_home = native_home

    def available(self) -> Availability:
        if not self._controller.broker.can_execute(self.executable):
            return Availability(False, f"Codex CLI is not executable at {self.executable}")
        return Availability(True)

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        if (
            request.provider_session_id is not None
            and validated_uuid(request.provider_session_id) is None
        ):
            raise ValueError("Invalid persisted Codex session ID")
        if not self._controller.broker.is_directory(self.native_home):
            raise RuntimeExecutionError(
                "Provision the steward's Codex native home before execution"
            )
        with native_workspace(
            self._controller.broker, request, self.native_home,
            mappings={
                "sessions": "artefacts/codex/sessions",
                "archived_sessions": "artefacts/codex/archived_sessions",
                "memories": "memories/codex",
            },
            resume_pattern="artefacts/codex/sessions/**/rollout-*{session}.jsonl",
        ) as workspace:
            request = workspace.remaining_request(request)
            turn = _AppServerTurn(request, resume_path=workspace.resume,
                unrestricted=self._controller.broker.enabled
                and request.sandbox_mode == "workspace-write")
            environment = self.environment()
            environment["CODEX_HOME"] = str(workspace.home)
            try:
                output = self._controller.run(
                    [str(self.executable), "app-server", "--stdio", "-c",
                     "sqlite_home=" + json.dumps(str(workspace.home))],
                    cwd=request.cwd,
                    env=environment,
                    timeout_seconds=request.timeout_seconds,
                    on_stdout_line=turn.consume,
                    on_input_ready=turn.connect,
                    on_started=request.on_started,
                    on_stop=turn.stop,
                )
                if output.returncode != 0:
                    raise turn.failure or RuntimeExecutionError(
                        "Codex App Server exited without a usable reply",
                        session_id=turn.thread_id,
                    )
                return turn.finish()
            except RuntimeExecutionError as error:
                if error.session_id is None:
                    error.session_id = turn.thread_id
                raise
            finally:
                turn.disconnect()

    def environment(self, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if inherited is None else inherited)
        for name in tuple(environment):
            if name.startswith("ANTHROPIC_") or name in {"ZAI_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"}:
                environment.pop(name)
        environment["CODEX_HOME"] = str(self.native_home)
        return environment


class _AppServerTurn:
    """One correlated native turn and its outstanding input acknowledgements."""

    def __init__(self, request: RuntimeRequest, *, resume_path: str | None = None,
                 unrestricted: bool = False):
        self.unrestricted = unrestricted
        self.request = request
        self.resume_path = resume_path
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self.writer: ProcessInput | None = None
        self.counter = 0
        self.pending: dict[int, tuple[str, str | None]] = {}
        self.sources: set[str] = set()
        self.children: set[str] = set()
        self.child_threads: set[str] = set()
        self.output: str | None = None
        self.model = request.resolved.model
        self.completed = False
        self.acted = False
        self.stopping = False
        self.failure: RuntimeExecutionError | RuntimeUnavailable | None = None
        self.closed = False
        self.lock = RLock()

    def connect(self, writer: ProcessInput) -> None:
        self.writer = writer
        self.send(
            "initialize",
            {
                "clientInfo": {"name": "steward_harness", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )

    def stop(self) -> None:
        """Request native interruption; terminal handling drains known terminals."""
        with self.lock:
            self.stopping = True
            if self.closed or self.completed or self.failure is not None:
                return
            if self.turn_id is not None:
                self.send("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
            elif not any(method == "turn/start" for method, _ in self.pending.values()):
                # No native turn was launched. Don't advance a pending handshake.
                self.closed = True
                self.writer.close()

    def send(self, method: str, params: dict, source: str | None = None) -> None:
        with self.lock:
            self.counter += 1
            self.writer.write(
                json.dumps({"id": self.counter, "method": method, "params": params})
                + "\n"
            )
            self.pending[self.counter] = (method, source)

    def steer(self, message: RuntimeInput) -> None:
        with self.lock:
            if message.source_id in self.sources:
                raise RuntimeExecutionError(
                    "native input source was already offered", session_id=self.thread_id
                )
            if self.closed or self.completed or self.stopping or self.failure is not None or self.turn_id is None:
                self.request.on_input_result(RuntimeInputResult(message.source_id, "rejected"))
                raise RuntimeExecutionError(
                    "native turn is no longer accepting input",
                    session_id=self.thread_id,
                )
            self.send(
                "turn/steer",
                {
                    "threadId": self.thread_id,
                    "expectedTurnId": self.turn_id,
                    "clientUserMessageId": message.source_id,
                    "input": [{"type": "text", "text": message.attributed_text}],
                },
                message.source_id,
            )
            self.sources.add(message.source_id)

    def consume(self, line: str) -> None:
        try:
            event = json.loads(line)
        except (ValueError, TypeError) as error:
            raise RuntimeExecutionError(
                "Codex emitted malformed JSON", session_id=self.thread_id
            ) from error
        if not isinstance(event, dict):
            raise RuntimeExecutionError(
                "Codex emitted an invalid event", session_id=self.thread_id
            )
        with self.lock:
            if self.closed:
                if "id" in event and self.pending.get(event["id"], (None,))[0] == "turn/steer":
                    self.response(event)
                return
            if "id" in event and "method" in event:
                # Approval grants stay outside cognition. Never approve a
                # provider request just to keep its transport moving.
                self.writer.write(
                    json.dumps(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "No additional authority granted by this execution",
                            },
                        }
                    )
                    + "\n"
                )
                return
            if "id" in event:
                self.response(event)
                return
            params = event.get("params", {})
            if params.get("threadId") != self.thread_id:
                return
            method = event.get("method")
            item = params.get("item", {})
            if item.get("type") == "subAgentActivity":
                child = item["agentThreadId"]
                if item["kind"] == "started":
                    self.children.add(child)
                    self.child_threads.add(child)
                elif item["kind"] == "completed":
                    self.children.discard(child)
            if method in ("item/started", "item/completed") and item.get("type") not in (
                None, "userMessage", "agentMessage", "reasoning",
            ):
                self.acted = True
            if method == "turn/completed" and params["turn"]["id"] == self.turn_id:
                terminal = params["turn"]
                if terminal["status"] != "completed":
                    error = terminal.get("error") or {}
                    message = (
                        error.get("message") or "Codex native turn did not complete"
                    )
                    # After a command or edit the checkout holds this turn's
                    # effects: it resumes here or fails, never replays elsewhere.
                    self.failure = (
                        RuntimeUnavailable(message)
                        if _declines_turn(message) and not self.acted
                        else RuntimeExecutionError(message, session_id=self.thread_id)
                    )
                elif self.children:
                    self.failure = RuntimeExecutionError(
                        "Codex returned with unfinished native children",
                        session_id=self.thread_id,
                    )
                else:
                    self.completed = True
                # A terminal turn can leave native shell sessions running.
                # Ask their native owner to stop them before closing transport.
                for thread in sorted({self.thread_id, *self.child_threads}):
                    self.send("thread/backgroundTerminals/clean", {"threadId": thread})
            if (
                method == "item/completed"
                and params.get("turnId") == self.turn_id
                and item.get("type") == "agentMessage"
                and item.get("phase") in (None, "final_answer")
            ):
                if not isinstance(item.get("text"), str):
                    raise RuntimeExecutionError(
                        "Codex emitted invalid agent message text", session_id=self.thread_id,
                    )
                self.output = item["text"].strip()[-64_000:]

    def response(self, event: dict) -> None:
        pending = self.pending.get(event["id"])
        if pending is None:
            raise RuntimeExecutionError(
                "Codex returned an uncorrelated response", session_id=self.thread_id
            )
        method, source = pending
        if method == "turn/steer":
            if "error" not in event and (
                not isinstance(event.get("result"), dict)
                or event["result"].get("turnId") != self.turn_id
            ):
                raise RuntimeExecutionError(
                    "Codex returned an invalid steering acknowledgement",
                    session_id=self.thread_id,
                )
            del self.pending[event["id"]]
            self.request.on_input_result(
                RuntimeInputResult(
                    source, "rejected" if "error" in event else "accepted"
                )
            )
            return
        del self.pending[event["id"]]
        if "error" in event:
            message = str(event["error"].get("message", ""))
            if (
                method == "thread/resume"
                and "no rollout found for thread id" in message
            ):
                raise MissingProviderSession("Saved Codex session is unavailable")
            if _declines_turn(message):
                raise RuntimeUnavailable(f"Codex {method} refused: {message}")
            raise RuntimeExecutionError(
                f"Codex {method} failed: {message}", session_id=self.thread_id
            )
        result = event["result"]
        if method == "initialize":
            self.writer.write(
                json.dumps({"method": "initialized", "params": {}}) + "\n"
            )
            params = {
                "cwd": str(self.request.cwd),
                "model": self.request.resolved.model,
                # Let Codex review MCP and sandbox approval requests itself.
                # Any request still delegated to this client remains denied in
                # consume(), so the harness never expands provider authority.
                "approvalPolicy": "never" if self.unrestricted else "on-request",
                "approvalsReviewer": "auto_review",
                "sandbox": "danger-full-access" if self.unrestricted else self.request.sandbox_mode,
            }
            # No effort resolved means Codex keeps whatever its own
            # configuration says, so the override is not sent at all.
            if self.request.resolved.reasoning_effort is not None:
                params["config"] = {
                    "model_reasoning_effort": self.request.resolved.reasoning_effort
                }
            if self.request.provider_session_id is not None:
                params["threadId"] = self.request.provider_session_id
                if self.resume_path and self.resume_path != self.request.provider_session_id:
                    params["path"] = self.resume_path
            self.send(
                "thread/resume" if self.request.provider_session_id else "thread/start",
                params,
            )
        elif method in {"thread/start", "thread/resume"}:
            thread_id = result["thread"]["id"]
            if validated_uuid(thread_id) is None or (
                self.request.provider_session_id is not None
                and thread_id != self.request.provider_session_id
            ):
                raise RuntimeExecutionError(
                    "Codex returned a different or invalid thread identity"
                )
            self.thread_id = thread_id
            self.model = result.get("model", self.request.resolved.model)
            self.request.on_session_started(thread_id)
            sandbox = {"type": "readOnly"}
            if self.unrestricted:
                sandbox = {"type": "dangerFullAccess"}
            elif self.request.sandbox_mode == "workspace-write":
                sandbox = {
                    "type": "workspaceWrite",
                    "networkAccess": True,
                    "writableRoots": [
                        str(self.request.cwd),
                        *map(str, self.request.writable_roots),
                    ],
                }
            self.send(
                "turn/start",
                {
                    "threadId": thread_id,
                    "sandboxPolicy": sandbox,
                    "input": [
                        {"type": "text", "text": self.request.prompt},
                        *[
                            {"type": "localImage", "path": str(path)}
                            for path in self.request.images
                        ],
                    ],
                },
            )
        elif method == "turn/start":
            self.turn_id = result["turn"]["id"]
            if self.stopping:
                self.stop()
            elif self.request.on_input_ready is not None:
                self.request.on_input_ready(self.steer)
        elif method == "thread/backgroundTerminals/clean":
            if result != {}:
                raise RuntimeExecutionError(
                    "Codex returned an invalid terminal cleanup acknowledgement",
                    session_id=self.thread_id,
                )
            if not any(method == "thread/backgroundTerminals/clean" for method, _ in self.pending.values()):
                self.closed = True
                self.writer.close()

    def disconnect(self) -> None:
        with self.lock:
            self.closed = True
            unresolved = [
                source
                for method, source in self.pending.values()
                if method == "turn/steer"
            ]
            self.pending.clear()
        for source in unresolved:
            self.request.on_input_result(RuntimeInputResult(source, "unresolved"))

    def finish(self) -> RuntimeResult:
        if self.failure is not None:
            raise self.failure
        if (self.stopping or not self.closed or not self.completed or self.thread_id is None
                or (not self.output and not self.request.allow_empty_output)):
            raise RuntimeExecutionError(
                "Codex stream ended before the requested turn completed",
                session_id=self.thread_id,
            )
        return RuntimeResult(
            output=self.output or "",
            resolved=self.request.resolved,
            effective_model=self.model,
            provider_session_id=self.thread_id,
        )
