"""DSH 0.2.0-rc.2 native headless JSON sessions over the execution broker."""
from __future__ import annotations

import json
import signal
import math
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from steward_harness.config.schema import ProviderConfig
from steward_harness.runtime.contracts import (
    Availability, ProviderCapabilities, RuntimeExecutionError, RuntimeRequest,
    RuntimeResult, TokenMeter, validated_uuid,
)
from steward_harness.runtime.native_evidence import record_mappings
from steward_harness.runtime.native_workspace import native_workspace
from steward_harness.runtime.process import ProcessController
from steward_harness.runtime.task_call_mcp import server_config


class DeepSeekEvents:
    """Only a matching completed turn, lossless final and clean exit can succeed."""

    def __init__(self, request: RuntimeRequest):
        self.request = request
        self.session_id: str | None = None
        self.turn: int | None = None
        self.completed = False
        self.ended = False
        self.failure: str | None = None
        self.output: str | None = None
        self.meter = TokenMeter(request.token_budget)

    def fail(self, message: str):
        raise RuntimeExecutionError(message, session_id=self.session_id)

    def consume(self, line: str):
        try:
            event = json.loads(line)
        except ValueError:
            self.fail("DSH emitted invalid JSON")
        if not isinstance(event, dict) or not isinstance(event.get('type'), str):
            self.fail("DSH emitted an invalid event")
        kind = event['type']
        if self.output is not None:
            self.fail("DSH emitted events after final")
        if kind == 'error':
            # Do not reflect arbitrary provider data or credentials into task prose.
            self.failure = "DSH reported a native execution error; inspect its private session"
            return
        if event.get('truncated') and kind in {'session', 'status', 'final'}:
            self.fail("DSH truncated lifecycle evidence")
        if kind == 'session':
            identity = event.get('sessionId')
            if (self.session_id is not None or not isinstance(identity, str)
                    or not identity.startswith('session-')
                    or validated_uuid(identity[8:]) is None
                    or (self.request.provider_session_id and identity != self.request.provider_session_id)
                    or event.get('cwd') != str(self.request.cwd)):
                self.fail("DSH session identity mismatch")
            self.session_id = identity
            self.request.on_session_started(identity)
            return
        if self.session_id is None:
            self.fail("DSH omitted its opening session")
        if kind == 'status':
            phase = event.get('phase')
            turn = event.get('turn')
            if phase == 'turn_start':
                if self.turn is not None or type(turn) is not int or turn < 0:
                    self.fail("DSH turn identity mismatch")
                self.turn = turn
            elif phase in {'step_start', 'step_end', 'turn_end'}:
                if self.turn is None or type(turn) is not int or turn != self.turn or self.ended:
                    self.fail("DSH turn identity mismatch")
                if phase in {'step_start', 'step_end'} and (type(event.get('step')) is not int or event['step'] < 0):
                    self.fail('DSH step identity mismatch')
                if phase == 'step_end':
                    usage = event.get('usage', {})
                    if isinstance(usage, dict):
                        self.meter.report(event.get('step'), usage.get('outputTokens'))
                if phase == 'turn_end':
                    self.ended = True
                    self.completed = event.get('reason') == {'kind': 'completed'}
                    if not self.completed:
                        self.failure = 'DSH turn did not complete successfully; inspect its private session'
            else:
                self.fail("DSH emitted an unknown lifecycle phase")
        elif kind == 'final':
            if not self.ended or not isinstance(event.get('text'), str):
                self.fail("DSH final arrived without successful turn completion")
            if len(event['text']) > 64_000:
                self.fail("DSH answer exceeded safe limit")
            if not event['text'].strip() and not self.request.allow_empty_output and not self.failure:
                self.fail("DSH completed without output")
            self.output = event['text']
        elif kind in {'text', 'thinking', 'tool_call', 'tool_result'}:
            if self.turn is None or self.ended:
                self.fail("DSH content outside its active turn")
            if kind == 'tool_call':
                try:
                    self.request.on_progress('DSH tool: ' + str(event.get('tool', 'unknown'))[:100])
                except Exception:
                    pass  # Narration has no lifecycle authority.
        else:
            self.fail("DSH emitted an unknown event type")


# The credential plugin reads this original directly; no credential file is
# copied or linked into owner homes, records, overlays or candidate trees.
_PREPARE_PATCH = r'''
import json, os, pathlib, stat, sys
home, credential, patch = json.load(sys.stdin)
credential = pathlib.Path(credential)
meta = credential.stat()
assert stat.S_ISREG(meta.st_mode) and meta.st_uid == os.geteuid() and stat.S_IMODE(meta.st_mode) == 0o600, 'DSH credential must be an owned private regular file'
path = pathlib.Path(home) / '.steward-run.patch.json'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'w') as target:
    json.dump(patch, target)
print(str(path))
'''


class DeepSeekRuntime:
    family = 'deepseek'
    capabilities = ProviderCapabilities()  # Headless has no live input or image transport.

    def __init__(self, executable: Path, *, controller: ProcessController,
                 native_home: Path, credential_home: Path | None = None):
        self.executable = executable
        self._controller = controller
        self.native_home = native_home
        self.credential_home = credential_home or native_home

    def available(self) -> Availability:
        broker = self._controller.broker
        if not broker.can_execute(self.executable):
            return Availability(False, 'DSH CLI is unavailable')
        result = broker.run([broker.python_executable, '-I', '-c',
            "import os,pathlib,stat,sys; p=pathlib.Path(sys.argv[1]); s=p.stat(); "
            "assert stat.S_ISREG(s.st_mode) and s.st_uid==os.geteuid() and stat.S_IMODE(s.st_mode)==0o600; "
            "assert os.access(p,os.R_OK)", str(self.credential_home / '.credentials.yaml')],
            cwd='/', timeout=5)
        return Availability(result.returncode == 0, None if result.returncode == 0 else 'DSH native credential is unavailable or not private')

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        if request.text_only or request.images or request.on_input_ready is not None:
            raise ValueError('DSH headless does not support text-only isolation, images or live input')
        effort = request.resolved.reasoning_effort
        if effort not in {None, 'low', 'high', 'max'}:
            raise ValueError('DSH reasoning effort must be low, high, max or omitted')
        # Version is a native protocol dependency, not a provider-model guess.
        deadline = None if request.timeout_seconds is None else time.monotonic() + request.timeout_seconds
        version = self._controller.broker.run([str(self.executable), '--version'], cwd='/',
            timeout=min(10, request.timeout_seconds or 10))
        if version.returncode or version.stdout.strip() != '0.2.0-rc.2':
            raise RuntimeExecutionError('DSH adapter requires verified CLI version 0.2.0-rc.2')
        if deadline is not None:
            remaining = math.ceil(deadline - time.monotonic())
            if remaining <= 0:
                raise RuntimeExecutionError("DSH setup exceeded execution deadline", session_id=request.provider_session_id)
            request = replace(request, timeout_seconds=remaining)
        with native_workspace(self._controller.broker, request, self.native_home,
                mappings=record_mappings(self.family),
                resume_pattern='artefacts/deepseek/sessions/*/{session}/session.v4.jsonl*',
                credential_home=self.credential_home) as workspace:
            credential = self.credential_home / '.credentials.yaml'
            selection = {'provider': 'deepseek-official', 'model': request.resolved.model}
            if effort is not None:
                selection['reasoningEffort'] = effort
            patch = [
                {'id': 'agent-default-model', 'config': selection},
                {'id': 'credentials', 'config': {'path': str(credential)}},
                {'id': 'sandbox-policy', 'config': {'mode': request.sandbox_mode, 'workspaceRoot': str(request.cwd)}},
                {'id': 'approval', 'config': {'policy': 'never'}},
                # Extra title inference is unrelated to the admitted operation.
                {'id': 'session-title-llm', 'disabled': True},
            ]
            if request.token_budget is not None:
                patch.append({'id': 'llm-deepseek', 'config': {'maxTokens': request.token_budget}})
            if request.task_call_socket:
                patch.append({'insert': [{'id': 'steward-tasks', 'name': '@deepseek-ai/dsh-mcp-client',
                    'config': {'serverName': 'steward_tasks', 'transport': 'stdio',
                        **server_config(request.task_call_socket),
                        'failOnStartupError': True, 'reconnect': {'enabled': False}}}]})
            prepared = self._controller.broker.run(
                [self._controller.broker.python_executable, '-I', '-c', _PREPARE_PATCH],
                cwd=request.cwd, timeout=min(30, workspace.remaining_request(request).timeout_seconds or 30),
                input_text=json.dumps([str(workspace.home), str(credential), patch]))
            if prepared.returncode:
                raise RuntimeExecutionError('DSH private launch configuration failed', session_id=request.provider_session_id)
            request = workspace.remaining_request(request)
            events = DeepSeekEvents(request)
            command = [str(self.executable), 'headless', '--patch', prepared.stdout.strip(), '--json']
            if request.provider_session_id:
                command += ['--session-id', request.provider_session_id]
            command.append('-')
            try:
                result = self._controller.run(command, cwd=request.cwd,
                    env={'DSH_HOME': str(workspace.home)}, timeout_seconds=request.timeout_seconds,
                    stdin_text=request.prompt, on_stdout_line=events.consume,
                    on_started=request.on_started, on_process_started=request.on_process_started,
                    interrupt_signal=signal.SIGINT, over_budget=events.meter.exhausted,
                    storage_paths=(workspace.home, request.cwd), retain_stdout=False)
                if events.failure:
                    events.fail(events.failure)
                if result.returncode or not events.completed or events.output is None:
                    events.fail('DSH exited without a successful terminal result; inspect its private session')
            except RuntimeExecutionError as error:
                if error.session_id is None:
                    error.session_id = events.session_id or request.provider_session_id
                raise
            # Headless emits no effective model metadata. This is the exact
            # native selection, not an independently attested backend identity.
            return RuntimeResult(events.output, request.resolved, request.resolved.model, events.session_id)


def build_deepseek(
    provider: ProviderConfig, controller: ProcessController, homes: Mapping[str, Path],
) -> DeepSeekRuntime:
    """DSH headless in its own native home."""
    return DeepSeekRuntime(
        Path(provider.deepseek_executable),
        controller=controller,
        native_home=homes["deepseek"],
        credential_home=Path(provider.native_credential_homes.get("deepseek", homes["deepseek"])),
    )
