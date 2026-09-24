"""Persistent Claude CLI stream runtime; GLM is the same CLI routed to z.ai."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, cast
from uuid import uuid4

from steward_harness.runtime.contracts import (
    SESSION_WORKSPACE_CAPABILITIES,
    Availability,
    MissingProviderSession,
    ProviderFamily,
    RuntimeExecutionError,
    RuntimeInput,
    RuntimeInputResult,
    RuntimeRequest,
    RuntimeResult,
    RuntimeUnavailable,
    validated_uuid,
)
from steward_harness.runtime.native_workspace import native_workspace
from steward_harness.runtime.process import ProcessController, ProcessInput

_MAX_RESPONSE_CHARS = 64_000
_SANDBOX_SETTINGS = json.dumps(
    {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            # Claude Code 2.1.218-220's socket-filter helper fails while
            # creating its nested userns on Ubuntu 26.04. Keep bwrap and the
            # network proxy, but bypass that helper until upstream #81799 is fixed.
            "network": {"allowAllUnixSockets": True},
        }
    },
    separators=(",", ":"),
)
_READ_ONLY_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "WebSearch",
    "WebFetch",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(rg:*)",
    "Bash(grep:*)",
    "Bash(sed:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(find:*)",
)
_WORKSPACE_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "Grep",
    "Glob",
    "WebSearch",
    "WebFetch",
    "Bash",
)
# Explicit routing replaces every ambient Anthropic endpoint identity and any
# cloud-provider selector; the configured pair is the only one the child sees.
_ANTHROPIC_SELECTORS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def _declines_turn(message: str) -> bool:
    """Whether Claude is declining this turn rather than failing at it.

    An expired or revoked sign-in says nothing about the work: the same prompt
    succeeds on any other configured provider now, and on this one as soon as
    somebody logs in. It arrives as an ordinary turn error, so wording is the
    only thing separating it from a real failure — and that reading belongs
    here, in the adapter that owns the wire format, not in the lifecycle,
    which must never learn a provider's name.

    Kept deliberately narrow: these must not match a model discussing
    authentication in its own reply text, which is one of the two paths that
    reaches here. If one slips through anyway the turn is retried elsewhere,
    and if nobody can take it the lifecycle still reports every provider's own
    words rather than one provider's raw message.
    """
    text = message.casefold()
    return any(
        marker in text
        for marker in (
            # "Failed to authenticate: OAuth session expired and could not be
            # refreshed" — the text a downstream instance lost every turn to.
            "failed to authenticate",
            "oauth session expired",
            "invalid_refresh_token",
            "token_expired",
            "log out and sign in",
        )
    )


class _ClaudeLifecycle:
    """Validates the strict Claude stream-json lifecycle for one persistent turn."""

    def __init__(
        self,
        expected_session_id: str | None,
        provider: str,
        *,
        sensitive_event_value: str | None = None,
        allow_empty_output: bool = False,
    ) -> None:
        self._expected_session_id = expected_session_id
        self._provider = provider
        self._sensitive_event_value = sensitive_event_value
        self._allow_empty_output = allow_empty_output
        self.failure: RuntimeExecutionError | RuntimeUnavailable | None = None
        self.session_id: str | None = None
        self.effective_model: str | None = None
        self._output: str | None = None
        self._completed = False
        self._acted = False

    def decode(self, line: str) -> dict[str, Any]:
        if (
            self._sensitive_event_value is not None
            and self._sensitive_event_value in line
        ):
            raise RuntimeExecutionError(
                f"{self._provider} emitted unsafe credential content",
                session_id=self.session_id,
            )
        try:
            raw_event: object = json.loads(line)
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeExecutionError(
                f"{self._provider} emitted a malformed event stream"
            ) from error

        if not isinstance(raw_event, dict):
            raise RuntimeExecutionError(f"{self._provider} emitted a malformed event stream")
        event = cast(dict[str, Any], raw_event)
        event_type = event.get("type")
        if not isinstance(event_type, str):
            raise RuntimeExecutionError(f"{self._provider} emitted a malformed event stream")
        return event

    def consume_event(self, event: dict[str, Any]) -> str | None:
        event_type = event["type"]
        if event_type == "system" and event.get("subtype") in ("task_notification", "dev_intent"):
            # Background-agent and development-intent notices are informational,
            # not session initialization or turn completion. Claude can emit
            # them before init or during a turn without a session identity.
            return None
        if self.session_id is None:
            return self._accept_init(event)

        session_id = self._event_session_id(event)
        if session_id != self.session_id:
            raise RuntimeExecutionError(
                f"{self._provider} emitted a different persistent session identity",
                session_id=self.session_id,
            )
        if event_type == "error":
            error = event.get("error") or {}
            raise self._error(error.get("message", "reported an error turn"))
        if event_type == "assistant":
            content = event.get("message", {}).get("content")
            self._acted = self._acted or any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in (content if isinstance(content, list) else ())
            )
        if event_type == "assistant" and event.get("parent_tool_use_id") is None:
            code = event.get("error")
            message = "\n".join(
                block["text"] for block in event.get("message", {}).get("content", [])
                if block.get("type") == "text"
            )
            self.failure = self._error(message) if code else None
        if event_type != "result":
            return None
        if event.get("subtype") != "success" or event.get("is_error") is True:
            raise self.failure or self._error(
                "\n".join(event.get("errors") or []) or event.get("result")
                or "reported a failed turn",
            )
        self.failure = None
        if event.get("terminal_reason") != "completed":
            raise RuntimeExecutionError(
                f"{self._provider} did not finish its native command",
                session_id=self.session_id,
            )
        result = event.get("result")
        if not isinstance(result, str) or (not result.strip() and not self._allow_empty_output):
            raise RuntimeExecutionError(
                f"{self._provider} completed without an agent response",
                session_id=self.session_id,
            )
        self._output = result.strip()[-_MAX_RESPONSE_CHARS:]
        self._completed = True
        return None

    def finish(self) -> tuple[str, str, str]:
        if self.failure is not None:
            raise self.failure
        if (
            self.session_id is None
            or self.effective_model is None
            or self._output is None
            or not self._completed
        ):
            raise RuntimeExecutionError(
                f"{self._provider} stream ended before a successful terminal result",
                session_id=self.session_id,
            )
        return self._output, self.session_id, self.effective_model

    def _error(self, message: str) -> RuntimeExecutionError | RuntimeUnavailable:
        """Build the turn-ending error, or the refusal that tries someone else.

        Every message the provider reports about its own turn funnels through
        here, so this is the one place that can tell "this cannot be done" from
        "not by me". A dead sign-in is the latter: the prompt is fine, the next
        configured provider can take it now, and a login fixes this one later.
        Once a tool has run, the turn has effects in its checkout: it resumes
        here or fails, and is never replayed from scratch by someone else.
        """
        text = f"{self._provider}: {message}"[:_MAX_RESPONSE_CHARS]
        if _declines_turn(message) and not self._acted:
            return RuntimeUnavailable(text)
        return RuntimeExecutionError(text, session_id=self.session_id)

    def _accept_init(self, event: Mapping[str, Any]) -> str:
        if event.get("type") != "system" or event.get("subtype") != "init":
            raise RuntimeExecutionError(f"{self._provider} stream did not start with system init")
        session_id = self._event_session_id(event)
        self.session_id = session_id
        model = event.get("model")
        if not isinstance(model, str) or not model:
            raise RuntimeExecutionError(
                f"{self._provider} init omitted its effective model",
                session_id=session_id,
            )
        if self._expected_session_id is not None and session_id != self._expected_session_id:
            raise RuntimeExecutionError(
                f"{self._provider} resumed a different persistent session identity",
                session_id=session_id,
            )
        self.effective_model = model
        return session_id

    def _event_session_id(self, event: Mapping[str, Any]) -> str:
        candidate = event.get("session_id")
        validated = validated_uuid(candidate) if isinstance(candidate, str) else None
        if validated is None:
            raise RuntimeExecutionError(
                f"{self._provider} emitted an invalid persistent session identity"
            )
        return validated


class ClaudeInputStream:
    """One candidate writer, closed after every offered command and its result."""

    def __init__(self, request: RuntimeRequest, lifecycle: _ClaudeLifecycle):
        self.request = request
        self.lifecycle = lifecycle
        self.writer: ProcessInput | None = None
        self.commands: dict[str, str | None] = {}
        self.sources: set[str] = set()
        self.acknowledged: set[str] = set()
        self.started: set[str] = set()
        self.completed: set[str] = set()
        self.turn_root: str | None = None
        self.expected_results: set[str] = set()
        self.results: set[str] = set()
        self.session_id: str | None = None
        self.closed = False
        self.interrupt_id: str | None = None
        self.lock = RLock()

    def connect(self, writer: ProcessInput) -> None:
        self.writer = writer
        self._write(self.request.prompt, None)

    def stop(self) -> None:
        """Ask the native stream to interrupt; acknowledgement is not completion."""
        with self.lock:
            if self.closed or self.interrupt_id is not None:
                return
            self.interrupt_id = str(uuid4())
            self.writer.write(json.dumps({
                "type": "control_request", "request_id": self.interrupt_id,
                "request": {"subtype": "interrupt"},
            }) + "\n")

    def _write(self, text: str, source: str | None) -> None:
        command = str(uuid4())
        self.writer.write(json.dumps({
            "type": "user", "uuid": command, "session_id": "",
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": text},
        }) + "\n")
        self.commands[command] = source

    def send(self, source: RuntimeInput) -> None:
        with self.lock:
            if source.source_id in self.sources:
                raise RuntimeExecutionError("native input source was already offered", session_id=self.session_id)
            # A stopped run finished its input buffer, so the write below
            # raises on its own. There is nothing to ask a predicate about.
            if self.closed or self.interrupt_id is not None:
                self.request.on_input_result(RuntimeInputResult(source.source_id, "rejected"))
                raise RuntimeExecutionError("native command queue is no longer accepting input", session_id=self.session_id)
            try:
                self._write(source.attributed_text, source.source_id)
            except RuntimeExecutionError:
                # ProcessInput.write is atomic: failure queued no bytes.
                self.request.on_input_result(RuntimeInputResult(source.source_id, "rejected"))
                raise
            self.sources.add(source.source_id)

    def consume(self, line: str) -> None:
        # The common parser checks credential content, JSON shape and session
        # identity. Native queue events can precede system/init, so hold their
        # identity separately until the ordinary lifecycle establishes it.
        event = self.lifecycle.decode(line)
        with self.lock:
            if event.get("type") == "control_response":
                response = event.get("response", {})
                if (not isinstance(response, dict) or self.interrupt_id is None
                        or response.get("request_id") != self.interrupt_id
                        or response.get("subtype") != "success"):
                    raise RuntimeExecutionError("native interrupt was not acknowledged", session_id=self.session_id)
                return
            if event.get("type") == "command_lifecycle":
                self._command_event(event)
            else:
                if event.get("type") == "result":
                    # user_message_uuid is optional timing metadata (it can be
                    # absent after native agent work). The serial lifecycle's
                    # current fresh command owns the result before completing.
                    command = event.get("user_message_uuid", self.turn_root)
                    if (not isinstance(command, str) or command not in self.expected_results
                            or command in self.results):
                        raise RuntimeExecutionError("native result has no offered command identity", session_id=self.session_id)
                    if self.started - self.completed - {command}:
                        raise RuntimeExecutionError("native returned before active commands completed", session_id=self.session_id)
                    self.results.add(command)
                if self.interrupt_id is not None and event.get("type") == "result":
                    # Interrupted results are terminal evidence, never a usable
                    # reply. Drain command completion before closing transport.
                    self._session(event.get("session_id"))
                    started = None
                else:
                    started = self.lifecycle.consume_event(event)
                if started is not None:
                    self._session(started)
                    self.request.on_session_started(started)
                    if self.request.on_input_ready is not None:
                        self.request.on_input_ready(self.send)
            # A folded command completes before its consuming turn's result;
            # the command that began that turn completes after the result.
            # Both must be present. A queued correction may own a later result.
            if (self.completed.issuperset(self.commands)
                    and (self.interrupt_id is not None
                         or (self.expected_results and self.results == self.expected_results))):
                self.closed = True
                self.writer.close()

    def _session(self, identity: object) -> None:
        if (not isinstance(identity, str) or validated_uuid(identity) is None
                or (self.session_id is not None and identity != self.session_id)
                or (self.request.provider_session_id is not None and identity != self.request.provider_session_id)):
            raise RuntimeExecutionError("native command stream changed session identity", session_id=self.session_id)
        self.session_id = identity

    def _command_event(self, event: dict[str, Any]) -> None:
        self._session(event.get("session_id"))
        command, state = event.get("command_uuid"), event.get("state")
        if not isinstance(command, str) or not isinstance(state, str):
            raise RuntimeExecutionError("native command stream reported malformed identity/state", session_id=self.session_id)
        if command not in self.commands:
            if state not in {"queued", "started"} or validated_uuid(command) is None:
                raise RuntimeExecutionError("native command stream reported unowned work", session_id=self.session_id)
            # Native notifications/agents can enqueue their own work. Track its
            # quiescence in this session, without inventing a controller source.
            self.commands[command] = None
        if state not in {"queued", "started", "completed", "cancelled", "discarded"}:
            raise RuntimeExecutionError("native command stream reported an invalid state", session_id=self.session_id)
        if command in self.completed or (state == "completed" and command not in self.started):
            raise RuntimeExecutionError("native command stream reported an invalid transition", session_id=self.session_id)
        if ((state == "queued" and command in self.acknowledged)
                or (state == "started" and command in self.started)):
            raise RuntimeExecutionError("native command stream repeated a resolved transition", session_id=self.session_id)
        if state == "completed" and command in self.expected_results and command not in self.results:
            raise RuntimeExecutionError("native root completed before its result", session_id=self.session_id)
        source = self.commands[command]
        if state in {"queued", "started", "completed"} and command not in self.acknowledged:
            self.acknowledged.add(command)
            if source is not None:
                self.request.on_input_result(RuntimeInputResult(source, "accepted"))
        if state == "completed":
            self.completed.add(command)
        if state == "started":
            self.started.add(command)
            if self.turn_root is None or self.turn_root in self.completed:
                self.turn_root = command
                self.expected_results.add(command)
        if state in {"cancelled", "discarded"}:
            if source is not None and command not in self.acknowledged:
                self.acknowledged.add(command)
                self.request.on_input_result(RuntimeInputResult(source, "rejected"))
            if self.interrupt_id is not None:
                self.completed.add(command)
            else:
                raise RuntimeExecutionError("native command did not complete", session_id=self.session_id)

    def disconnect(self) -> None:
        with self.lock:
            self.closed = True
            for command, source in self.commands.items():
                if source is not None and command not in self.acknowledged:
                    self.acknowledged.add(command)
                    self.request.on_input_result(RuntimeInputResult(source, "unresolved"))

    def finish(self) -> None:
        if (not self.expected_results or self.results != self.expected_results
                or not self.completed.issuperset(self.commands)):
            raise RuntimeExecutionError("native stream ended before correlated command completion", session_id=self.session_id)


class ClaudeRuntime:
    """Run or resume a persistent turn via Claude CLI stream-json.

    One adapter instance serves one provider family: Anthropic's own endpoint
    when unrouted, or any Anthropic-compatible endpoint (z.ai for GLM) when a
    base URL and credential file are configured.
    """

    capabilities = replace(SESSION_WORKSPACE_CAPABILITIES, ongoing_input=True)

    def __init__(
        self,
        executable: Path = Path("/usr/local/bin/claude"),
        *,
        controller: ProcessController,
        native_home: Path,
        base_url: str | None = None,
        credential_path: Path | None = None,
        family: ProviderFamily = "claude",
    ) -> None:
        if (base_url is None) != (credential_path is None):
            raise ValueError(
                "Explicit Anthropic-compatible routing needs both base URL and credential path"
            )
        if not native_home.is_absolute():
            raise ValueError(f"{family} native home must be absolute")
        self.executable = executable
        self._controller = controller
        self.native_home = native_home
        self.base_url = base_url
        self.credential_path = credential_path
        self.family: ProviderFamily = family
        self.display_name = "Claude" if family == "claude" else family.upper()

    def available(self) -> Availability:
        if not self._controller.broker.can_execute(self.executable):
            return Availability(False, f"{self.display_name} CLI is not executable at {self.executable}")
        if self.credential_path is not None:
            try:
                self._read_credential(self.credential_path)
            except (OSError, ValueError):
                return Availability(
                    False,
                    f"{self.display_name} credential is unavailable at {self.credential_path}",
                )
        return Availability(True)

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        if not self._controller.broker.is_directory(self.native_home):
            raise RuntimeExecutionError(
                f"Provision the steward's {self.display_name} native home before execution"
            )
        session_id = None
        if request.provider_session_id is not None:
            session_id = validated_uuid(request.provider_session_id)
            if session_id is None:
                raise ValueError(f"Invalid persisted {self.display_name} session ID")
        with native_workspace(
            self._controller.broker, request, self.native_home,
            mappings={"projects": f"artefacts/{self.family}/projects"},
            resume_pattern=f"artefacts/{self.family}/projects/*/{{session}}.jsonl",
        ) as workspace:
            request = workspace.remaining_request(request)
            environment = self.environment()
            environment["CLAUDE_CONFIG_DIR"] = str(workspace.home)
            return self._execute(request, session_id, environment, resume=workspace.resume)

    def _execute(self, request, session_id, environment, *, resume=None):
        command = self._command(request, resume or session_id)
        if request.sandbox_mode == "workspace-write":
            environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "0"
        # Any configured router token must never survive into recorded events.
        lifecycle = _ClaudeLifecycle(
            session_id,
            self.display_name,
            sensitive_event_value=environment.get("ANTHROPIC_AUTH_TOKEN"),
            allow_empty_output=request.allow_empty_output,
        )
        stream = ClaudeInputStream(request, lifecycle)
        try:
            output = self._controller.run(
                command,
                cwd=request.cwd,
                env=environment,
                timeout_seconds=request.timeout_seconds,
                on_stdout_line=stream.consume,
                on_input_ready=stream.connect,
                on_started=request.on_started,
                on_stop=stream.stop,
            )
        except RuntimeExecutionError as error:
            if error.session_id is None:
                error.session_id = lifecycle.session_id
            raise
        finally:
            stream.disconnect()

        if output.returncode != 0:
            if lifecycle.failure is not None:
                raise lifecycle.failure
            if (
                session_id is not None
                and lifecycle.session_id is None
                and _missing_saved_conversation(output.stderr, session_id)
            ):
                raise MissingProviderSession(
                    f"Saved {self.display_name} session is unavailable"
                )
            raise RuntimeExecutionError(
                f"{self.display_name} exited {output.returncode} without a usable reply",
                session_id=lifecycle.session_id,
            )
        stream.finish()
        reply, confirmed_session_id, effective_model = lifecycle.finish()
        return RuntimeResult(
            output=reply,
            resolved=request.resolved,
            effective_model=effective_model,
            provider_session_id=confirmed_session_id,
        )

    def environment(self, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
        environment = dict(os.environ if inherited is None else inherited)
        for name in ("OPENAI_API_KEY", "ZAI_AUTH_TOKEN", "CODEX_HOME"):
            environment.pop(name, None)
        environment["CLAUDE_CONFIG_DIR"] = str(self.native_home)
        if self.base_url is not None:
            try:
                token = self._read_credential(self.credential_path)
            except (OSError, ValueError) as error:
                raise RuntimeUnavailable(
                    f"{self.display_name} credential is unavailable"
                ) from error
            for name in tuple(environment):
                if name.startswith("ANTHROPIC_") or name in _ANTHROPIC_SELECTORS:
                    environment.pop(name)
            environment["ANTHROPIC_BASE_URL"] = self.base_url
            environment["ANTHROPIC_AUTH_TOKEN"] = token
        environment["IS_SANDBOX"] = "1"
        # Only a writable request with explicit native configuration enables
        # memory; its command maps the files into that request's Git worktree.
        environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        return environment

    def _command(self, request: RuntimeRequest, session_id: str | None) -> list[str]:
        settings = json.loads(_SANDBOX_SETTINGS)
        # A dropped OS identity is the boundary. Asking the provider to police
        # itself on top of an enforced boundary buys nothing and costs reasoning.
        unrestricted = self._controller.broker.enabled and request.sandbox_mode == "workspace-write"
        if unrestricted:
            settings["sandbox"] = {"enabled": False}
        settings.update(
            autoMemoryEnabled=request.sandbox_mode == "workspace-write",
            autoMemoryDirectory=str(request.cwd.resolve() / "memories" / self.family),
        )
        command = [
            str(self.executable),
            "--output-format",
            "stream-json",
            "--verbose",
            "--settings",
            json.dumps(settings, separators=(",", ":")),
            "--permission-mode",
            "bypassPermissions" if unrestricted else (
                "acceptEdits" if request.sandbox_mode == "workspace-write" else "dontAsk"),
            *([] if unrestricted else ["--allowed-tools",
                *(_WORKSPACE_TOOLS if request.sandbox_mode == "workspace-write" else _READ_ONLY_TOOLS),
                *(["Agent"] if request.sandbox_mode == "workspace-write" else [])]),
            "--model",
            request.resolved.model,
            # No effort resolved means the provider picks its own. This family
            # also serves `glm` against a third-party gateway, which is not
            # obliged to accept Claude's effort vocabulary.
            *(
                ("--effort", request.resolved.reasoning_effort)
                if request.resolved.reasoning_effort is not None
                else ()
            ),
        ]
        for root in request.writable_roots:
            command.extend(("--add-dir", str(root)))
        if session_id is not None:
            command.extend(("--resume", session_id))
        command.extend(("-p", "--input-format", "stream-json", "--replay-user-messages"))
        return command

    @staticmethod
    def _read_credential(path: Path) -> str:
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"configured credential is empty at {path}")
        return token


def _missing_saved_conversation(stderr: str, requested_session_id: str) -> bool:
    expected = f"No conversation found with session ID: {requested_session_id}"
    return stderr in (expected, f"{expected}\n", f"{expected}\r\n")
