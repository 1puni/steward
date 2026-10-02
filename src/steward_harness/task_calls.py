"""Live conversation calls; accepted task Git commits own mutation receipts."""

from __future__ import annotations

import hashlib
import json
import re
import os
import socket
import struct
import sys
import tempfile
import shutil
from pathlib import Path
from socketserver import UnixStreamServer
import sqlite3
import uuid
from http.server import BaseHTTPRequestHandler
from threading import Thread

from steward_harness.state import ConversationId, TaskAction, TaskId, TaskSpec, TaskOriginKind
from steward_harness.task_store import PREFIX


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class TaskCalls:
    """Authority is captured by the controller, never supplied by the caller."""

    def __init__(self, state, turn_id, *, cancel=lambda _execution: False):
        self.state = state
        self.cancel = cancel
        self.turn_id = str(turn_id)
        with state.connect() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id=? AND state='running'",
                                     (str(turn_id),)).fetchone()
        if row is None:
            raise ValueError("task calls require a running conversation")
        self.owner = ConversationId(row["conversation_id"])
        if self.owner.kind == "rhythm":
            raise ValueError("rhythms cannot operate on tasks")
        self.operator_id = row["operator_id"]
        self.generation = state.lineage(self.owner).generation

    def _owned(self, task_id, operator_id=None):
        operator_id = self.operator_id if operator_id is None else operator_id
        try:
            task = self.state.tasks.get(TaskId(task_id))
        except (LookupError, PermissionError):
            raise ValueError("this conversation does not own that task") from None
        if task.owner != str(self.owner) and not (task.owner is None and not operator_id.startswith("harness:")):
            raise ValueError("this conversation does not own that task")
        return task

    @staticmethod
    def _summary(task):
        return dict(task_id=str(task.task_id), repository=task.repository,
                    title=task.title, status=task.status.value, revision=task.revision,
                    reason=task.reason)

    def __call__(self, request):
        if not isinstance(request, dict):
            raise ValueError("request must be an object")
        operation = request.get("operation")
        fields = {
            "submit": {"operation", "key", "repository", "title", "brief"},
            "list": {"operation"}, "show": {"operation", "task_id"},
            **{kind: {"operation", "key", "task_id", "text"}
               for kind in ("answer", "retry", "note", "cancel")},
        }
        if not isinstance(operation, str) or operation not in fields or set(request) - {"source_id"} != fields[operation]:
            raise ValueError("unknown operation or incorrect fields")
        if not all(isinstance(value, str) for value in request.values()):
            raise ValueError("fields must be strings")
        tasks = self.state.tasks
        with self.state.connect(write=True) as connection, tasks.lease:
            context_id = request.get("source_id", self.turn_id)
            context = connection.execute("SELECT * FROM turns WHERE turn_id=? AND conversation_id=?",
                                         (context_id, str(self.owner))).fetchone()
            if context is None or (context["execution_turn_id"] is not None
                                   and context["input_disposition"] != "accepted"):
                raise ValueError("source must be a controller-accepted input to this conversation")
            operator_id = context["operator_id"]

            root = connection.execute("SELECT * FROM turns WHERE turn_id=?", (self.turn_id,)).fetchone()
            lineage = self.state._lineage_in(connection, str(self.owner))
            if (root is None or root["state"] != "running" or lineage is None
                    or (root["generation"] is None and lineage.generation != self.generation)
                    or (root["generation"] is not None and
                        (root["generation"] != lineage.generation or root["provider"] != lineage.provider
                         or root["provider_session_id"] != lineage.provider_session_id))):
                raise ValueError("task capability no longer belongs to the active execution")

            def require_current_source():
                if context_id != self.turn_id and context["execution_turn_id"] != self.turn_id:
                    raise ValueError("source is not an input to this execution; only exact accepted retries may use old sources")
                if "source_id" not in request and connection.execute(
                        "SELECT 1 FROM turns WHERE execution_turn_id=? LIMIT 1", (self.turn_id,)).fetchone():
                    raise ValueError("mixed-input execution requires an explicit source_id")

            if operation in {"list", "show"}:
                require_current_source()
            if operation == "list":
                found = []
                for ref in tasks.refs():
                    try:
                        task = self._owned(ref.removeprefix(PREFIX), operator_id)
                    except (ValueError, PermissionError):
                        continue
                    found.append(task)
                newest = sorted(found, key=lambda task: task.updated_at, reverse=True)[:200]
                return {"operation": operation, "tasks": [self._summary(task) for task in newest],
                        "truncated": len(found) > 200}
            if operation == "show":
                task = self._owned(request["task_id"], operator_id)
                return {"operation": operation, **self._summary(task),
                        "brief": tasks.read(task.task_id)[2], "findings": task.findings}
            key = request["key"]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
                raise ValueError("key must be 1-128 ASCII letters, digits, or _ . : -")
            # Stable across executions of the same conversation, distinct per intent.
            scope = "submit" if operation == "submit" else request["task_id"]
            identity = "controller:task-call:" + _digest(str(self.owner) + ":" + scope) + ":" + _digest(key) + ":"
            task_id = (TaskId("task-" + uuid.uuid5(uuid.NAMESPACE_URL, identity).hex)
                       if operation == "submit" else TaskId(request["task_id"]))
            fingerprint = identity + _digest(json.dumps(request, sort_keys=True, ensure_ascii=True))
            source = fingerprint + ":" + context_id
            if operation == "submit":
                if request["repository"] not in (tasks.repositories or ()):
                    raise ValueError("repository work is not authorized")
            else:
                self._owned(request["task_id"], operator_id)
                if operator_id == "harness:desk-watch" and operation != "note":
                    raise ValueError("a desk observation may only note owned work")
            # Search only accepted task first-parent histories: native work cannot
            # forge a controller receipt. No receipt database or second registry.
            try:
                revision, definition, _ = tasks.read(task_id)
            except LookupError:
                history = ""
            else:
                self._owned(str(task_id), operator_id)
                history = tasks.git("log", "--first-parent", "--fixed-strings",
                                    "--grep=Steward-Source: " + identity,
                                    "--format=%H %(trailers:key=Steward-Source,valueonly)",
                                    *tasks._history(revision, definition))
            for line in history.splitlines():
                revision, _, prior = line.partition(" ")
                if prior.startswith(identity):
                    if prior.rsplit(":", 1)[0] != fingerprint:
                        raise ValueError("operation key already used with a different request")
                    if operation == "submit":
                        lineage = self.state._lineage_in(connection, str(self.owner))
                        self.state._open_in(connection, str(ConversationId.for_task(task_id)),
                                            provider=lineage.provider, profile=lineage.profile)
                    if operation == "cancel" and tasks.cancelled(task_id):
                        self.cancel(str(ConversationId.for_task(task_id)))
                    return dict(operation=operation, key=key, task_id=str(task_id),
                                revision=revision, accepted=True, replayed=True)
            require_current_source()
            if operation == "submit":
                lineage = self.state._lineage_in(connection, str(self.owner))
                self.state._insert_task(connection, TaskSpec(repository=request["repository"],
                    title=request["title"], brief=request["brief"]), task_id=task_id,
                    source=source, kind=TaskOriginKind.CONVERSATION, origin_ref=None,
                    conversation_id=self.owner, provider=lineage.provider, profile=lineage.profile)
            else:
                task_id = TaskId(request["task_id"])
                _, rejection = self.state._apply_task_action(
                    self.owner, operator_id,
                    TaskAction(task_id, operation, request["text"]), source=source)
                if rejection:
                    raise ValueError(rejection)
            if operation == "cancel":
                self.cancel(str(ConversationId.for_task(task_id)))
            return dict(operation=operation, key=key, task_id=str(task_id),
                        revision=tasks.read(task_id)[0], accepted=True, replayed=False)


class TaskCallServer:
    """A revocable execution capability for the native MCP stdio bridge."""

    def __init__(self, calls):
        self._invocation = None
        def authorized(connection):
            if self._invocation is None:
                return False
            # Linux's kernel supplies the connecting PID, never the caller JSON.
            if hasattr(socket, "SO_PEERCRED"):
                pid, _uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            elif sys.platform == "darwin":
                # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2. Python does
                # not expose these constants on every supported interpreter.
                pid = struct.unpack("i", connection.getsockopt(0, 2, 4))[0]
            else:
                return False
            session, unit = self._invocation
            try:
                if unit is not None:
                    return f"0::/system.slice/{unit}" in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
                return os.getsid(pid) == session
            except (OSError, ProcessLookupError):
                return False

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # No request logging from native task calls.

            def do_POST(self):
                if self.path != "/task" or not authorized(self.connection):
                    self.reply(403, {"accepted": False, "error": "task tool does not belong to this native invocation"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 128_000:
                        raise ValueError("request must contain 1-128000 bytes")
                    request = json.loads(self.rfile.read(size))
                    result = calls(request)
                    status = 200
                except (ValueError, LookupError, PermissionError) as error:
                    result, status = {"accepted": False, "error": str(error)}, 400
                except (RuntimeError, OSError, sqlite3.Error):
                    # The commit may already exist. Never label an ambiguous
                    # failure as a rejection; exact retry finds the Git receipt.
                    result, status = {"accepted": None, "error": "operation outcome unknown; retry the exact request"}, 503
                self.reply(status, result)

            def reply(self, status, result):
                body = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Accepted Git survives a lost reply.

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

        self.directory = tempfile.mkdtemp(prefix="steward-task-", dir="/tmp")
        os.chmod(self.directory, 0o755)
        self.path = str(Path(self.directory) / "call.sock")
        self.server = UnixStreamServer(self.path, Handler)
        os.chmod(self.path, 0o666)  # Peer invocation checks, not shared-UID secrecy.
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def bind(self, pid: int, unit: str | None):
        """Only a controller process-launch callback selects this invocation."""
        self._invocation = (os.getsid(pid), unit)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        shutil.rmtree(self.directory)

    @property
    def prompt(self):
        return '''\n## Live task operations
Use the native steward_tasks task tool during this execution. Arguments:
- submit: operation, key, repository, title, brief
- show: operation, task_id
- list: operation
- answer, retry, note, cancel: operation, key, task_id, text
All fields are strings. Include source_id naming the controller-provided input
that authorizes the operation: the initial Turn id or a live incoming source_id.
The controller resolves its speaker and requires accepted delivery; sender claims
are not authority. Omission means the initial input only in an unmixed execution.
With multiple inputs, source_id is required. A new call cannot use an old source;
only an exact already-accepted retry may do so. Use only configured repositories
and authorized work.
Use a distinct descriptive key for each independent intent (1-128 letters, digits,
_ . : -). Keep the SAME key and EXACT request on retries, including after a lost
receipt or execution restart. Submission keys belong to this conversation across executions; steering keys are scoped to the target task within this conversation.
Call submit repeatedly for independent work without waiting for completion or a
new operator message. Each accepted response names its durable task_id and Git
revision immediately. A failed/unknown call is not evidence of admission: retry
the same request. Show/list inspect your tasks; answer resumes waiting work,
retry resumes blocked/cancelled work, note supplies context, cancel withdraws work.
Accepted operations survive this conversation's failure, cancellation and world
acceptance failure. Parent cancellation does not cancel admitted tasks. Inspect
or cancel tasks explicitly. Results return to this conversation through the normal
result delivery path. The tool capability expires when execution ends. Use the newly supplied tool after restart.
Final TASK_PROPOSAL/TASK_ACTION lines do not execute operations.\n'''
