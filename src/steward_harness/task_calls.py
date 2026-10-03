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
import time
from http.server import BaseHTTPRequestHandler
from threading import Thread

from steward_harness.state import ConversationId, TaskAction, TaskId, TaskSpec, TaskOriginKind
from steward_harness.task_store import PREFIX


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


NOTIFY_DIRECTIVE = """Final replies from automatic runs are recorded only. To notify this run's
owner, call the native steward_tasks task tool with operation="notify", key and
text. Use a distinct key per message; retry an identical request with the same
key after a lost receipt. A receipt confirms durable queuing, not transport
completion. You may call during execution. Without a working tool, keep findings
in the final reply; they will not be sent. Most runs should notify no one."""


def queue_notification(state, *, owner, source, key, text, require_current=lambda: None):
    """The existing delivery receipt is the durable send intent and replay identity.

    Callers serialize acceptance and supply controller-resolved owner/source.
    Transport may still duplicate a send after losing its delivery receipt.
    """
    if not owner:
        raise ValueError("this run has no notification owner")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
        raise ValueError("key must be 1-128 ASCII letters, digits, or _ . : -")
    if not isinstance(text, str) or not text.strip() or len(text) > 12000:
        raise ValueError("notification text must contain 1-12000 characters")
    identity = "notify:" + _digest(json.dumps([str(owner), source, key]))
    receipt = state.result_receipt(identity)
    replayed = bool(receipt)
    if receipt:
        if receipt["result_text"] != text:
            raise ValueError("notification key already used with different text")
    else:
        require_current()
        state.save_result_receipt({
            "owner": str(owner), "task_id": None, "source_key": identity,
            "notification_source": source, "key": key,
            "result_text": text, "reply": text, "done": False,
            "recorded_at": time.time(),
        })
    return {"operation": "notify", "accepted": True, "receipt": identity,
            "owner": str(owner), "source_id": source, "replayed": replayed}


class TaskCalls:
    """Authority is captured by the controller, never supplied by the caller."""

    def __init__(self, state, turn_id, *, cancel=lambda _execution: False, notify_owner=None):
        self.state = state
        self.cancel = cancel
        self.turn_id = str(turn_id)
        with state.connect() as connection:
            row = connection.execute("SELECT * FROM turns WHERE turn_id=? AND state='running'",
                                     (str(turn_id),)).fetchone()
        if row is None:
            raise ValueError("task calls require a running conversation")
        self.owner = ConversationId(row["conversation_id"])
        self.notify_owner = notify_owner if self.owner.kind == "rhythm" else str(self.owner)
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
            "notify": {"operation", "key", "text"},
            "submit": {"operation", "key", "repository", "title", "brief"},
            "list": {"operation"}, "show": {"operation", "task_id"},
            **{kind: {"operation", "key", "task_id", "text"}
               for kind in ("answer", "retry", "note", "cancel")},
        }
        if not isinstance(operation, str) or operation not in fields or set(request) - {"source_id"} != fields[operation]:
            raise ValueError("unknown operation or incorrect fields")
        if not all(isinstance(value, str) for value in request.values()):
            raise ValueError("fields must be strings")
        if self.owner.kind == "rhythm" and operation != "notify":
            raise ValueError("rhythms may only notify their owner")
        tasks = self.state.tasks
        # Observations validate against a SQLite snapshot; slow Git reads must
        # not reserve the writer needed by unrelated provider session binds.
        with self.state.connect(write=operation not in {"list", "show"}) as connection, tasks.lease:
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

            if operation == "notify":
                return queue_notification(self.state, owner=self.notify_owner,
                                          source=context_id, key=request["key"], text=request["text"],
                                          require_current=require_current_source)
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
- notify: operation, key, text (durably queue a message to this run's owner)
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


class TaskExecutionCalls:
    """Task-scoped calls; the native invocation supplies no owner or authority."""

    def __init__(self, state, task_id, repositories):
        from steward_harness.task_query import OwnershipCalls
        self.state = state
        self.task_id = task_id
        self.owner = state.tasks.get(task_id).owner
        self.query = OwnershipCalls(state.tasks, task_id, repositories)
        self.start_writer()

    def start_writer(self):
        """A replacement native invocation cannot inherit pending closure intent."""
        self.execution = uuid.uuid4().hex
        self.intent = None
        self.conflicted = False
        self.revision = None

    def closure(self, findings):
        """Resolve only after the native writer and its descendants are torn down.

        The caller commits this decision onto the settled tree. A lost execution
        has no accepted closure; its intent cannot be replayed into a new writer.
        """
        from dataclasses import replace
        if self.intent is None or self.conflicted:
            raise ValueError("missing or conflicting close operation")
        if self.state.tasks.read(self.task_id)[0] != self.revision:
            raise ValueError("task changed after close intent; work retained for continuation")
        return replace(self.intent[1], findings=findings or "")

    def _close(self, request):
        from steward_harness.landing.checkpoint import TickClosure
        if self.conflicted:
            raise ValueError("close intent is conflicted; work will be retained without publication")
        if self.intent is not None and self.intent[0] != request:
            self.conflicted = True
            raise ValueError("conflicting close intents; no disposition will be accepted")
        required = {"operation", "key", "subject", "disposition"}
        if (not required <= set(request) or set(request) - required - {"question"}
                or not all(isinstance(value, str) for value in request.values())):
            raise ValueError("close requires key, subject, disposition and an optional question")
        subject, disposition, question = (request["subject"], request["disposition"], request.get("question"))
        if (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request["key"])
                or not subject.strip() or len(subject) > 120 or any(ord(c) < 32 or ord(c) == 127 for c in subject)
                or disposition not in {"continue", "idle", "ask"}
                or (disposition == "ask" and (not question or not question.strip() or len(question) > 1000))
                or (disposition != "ask" and question is not None)):
            raise ValueError("invalid close intent: bounded subject and explicit disposition; only ask takes a question")
        replayed = self.intent is not None
        if not replayed:
            self.revision = self.state.tasks.read(self.task_id)[0]
            self.intent = (dict(request), TickClosure(subject, disposition, question))
        return {"operation": "close", "pending": True, "accepted": False,
                "task_id": str(self.task_id), "execution": self.execution,
                "task_revision": self.revision, "key": request["key"], "replayed": replayed,
                "message": "Intent recorded for this execution only. Acceptance requires writer teardown and a settled checkpoint."}

    def __call__(self, request):
        if isinstance(request, dict) and request.get("operation") == "close":
            with self.state.tasks.lease:
                if self.state.tasks.cancelled(self.task_id):
                    raise ValueError("task was cancelled")
                return self._close(request)
        if not isinstance(request, dict) or request.get("operation") != "notify":
            return self.query(request)
        if set(request) != {"operation", "key", "text"}:
            raise ValueError("notify requires operation, key, text")
        with self.state.connect(write=True), self.state.tasks.lease:
            if self.state.tasks.cancelled(self.task_id):
                raise ValueError("task was cancelled")
            return queue_notification(self.state, owner=self.owner,
                                      source=str(self.task_id), key=request["key"], text=request["text"])
