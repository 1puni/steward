#!/usr/bin/env python3
"""A fake Telegram Bot API server for the follow-through environment.

Implements exactly the surface `steward_harness.telegram.api` calls, plus a
test control plane under `/__test/` for injecting operator messages, reading
a journal of everything the harness sent, and arming one-shot faults.

Two properties matter more than fidelity:

- Long polls hold. `getUpdates` blocks until an update at or after `offset`
  is available or the client's own `timeout` elapses. Answering instantly
  with an empty list would turn the harness's poll loop into a hot spin —
  the exact CPU-burn shape that once bit a live deployment.
- Faults are one-shot and named after real failures: a hanging poll, a
  failing send, a 429 with a retry hint. These are the cases a live Telegram
  cannot produce on demand and the reason this server exists.

Stdlib only, so both containers run the same pinned python image.

The second listener (HTTPS, :8443) is the managed repository's origin: a
self-hosted git remote served through `git http-backend`. The production
boundary rejects local-path remotes for a split-identity instance — the
controller must only trust a URL whose transport it controls — so the
self-improvement scenario needs a real non-local remote, and this is one.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "acceptance-token"
JOURNAL_FILE = Path("/journal/journal.jsonl")
BIND = ("0.0.0.0", 8081)
GIT_BIND = ("0.0.0.0", 8443)
GIT_ROOT = Path("/git")
GIT_REPOSITORY = "steward-harness"
GIT_HTTP_BACKEND = "/usr/lib/git-core/git-http-backend"


class State:
    """All mutable state behind one condition, so polls wake on injection."""

    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.updates: deque[tuple[int, dict]] = deque()
        self.next_update_id = 1000
        self.next_message_id = 1
        self.journal: list[dict] = []
        self.faults: dict[str, int] = {}  # name -> remaining hits
        self.registered_commands: list | None = None

    def record(self, entry: dict) -> None:
        entry["ts"] = time.time()
        self.journal.append(entry)
        JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def inject(self, text: str, chat_id: int, from_id: int, thread_id: int | None) -> int:
        message: dict = {
            "message_id": self.next_update_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Acceptance"},
            "from": {"id": from_id, "is_bot": False, "first_name": "Operator"},
            "text": text,
        }
        if thread_id and thread_id > 0:
            message["message_thread_id"] = thread_id
        update_id = self.next_update_id
        self.next_update_id += 1
        with self.condition:
            self.updates.append((update_id, {"update_id": update_id, "message": message}))
            self.condition.notify_all()
        return update_id

    def take_fault(self, name: str) -> bool:
        remaining = self.faults.get(name, 0)
        if remaining <= 0:
            return False
        self.faults[name] = remaining - 1
        return True


STATE = State()


def _bot_reply(result) -> dict:
    return {"ok": True, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # keep the console readable
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        chunks, remaining = [], length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 16))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _respond_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        body = self._read_body()
        if self.path.startswith(f"/{GIT_REPOSITORY}.git/"):
            self._git_http(body)
            return
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond_json(400, {"ok": False, "description": "bad JSON"})
            return

        if self.path.startswith("/__test/"):
            self._control(self.path, payload)
            return

        parts = self.path.strip("/").split("/")
        # Telegram routes methods as /bot<TOKEN>/<method> — "bot" is a
        # prefix of the first segment, not a segment of its own.
        if len(parts) != 2 or not parts[0].startswith("bot") or parts[0][3:] != TOKEN:
            self._respond_json(401, {"ok": False, "description": "unknown bot token"})
            return
        self._bot_method(parts[1], payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith(f"/{GIT_REPOSITORY}.git/"):
            self._git_http(b"")
            return
        if self.path == "/__test/health":
            self._respond_json(200, {"ok": True})
            return
        parts = self.path.strip("/").split("/")
        # getMe is the one GET in the harness's API client.
        if len(parts) == 2 and parts[0] == f"bot{TOKEN}" and parts[1] == "getMe":
            self._respond_json(200, _bot_reply(
                {"id": 1, "is_bot": True, "first_name": "Acceptance Steward",
                 "username": "acceptance_steward_bot", "can_join_groups": True}
            ))
            return
        self._respond_json(404, {"ok": False, "description": "not found"})

    # -- control plane -----------------------------------------------------

    def _control(self, path: str, payload: dict) -> None:
        if path == "/__test/inject":
            update_id = STATE.inject(
                text=str(payload.get("text", "")),
                chat_id=int(payload.get("chat_id", -1009990001111)),
                from_id=int(payload.get("from_id", 70001)),
                thread_id=payload.get("thread_id"),
            )
            STATE.record({"method": "__inject", "update_id": update_id,
                          "text": payload.get("text", "")})
            self._respond_json(200, {"ok": True, "update_id": update_id})
        elif path == "/__test/fault":
            name = str(payload.get("name", ""))
            STATE.faults[name] = int(payload.get("times", 1))
            STATE.record({"method": "__fault", "name": name,
                          "times": STATE.faults[name]})
            self._respond_json(200, {"ok": True})
        elif path == "/__test/journal":
            with STATE.condition:
                snapshot = list(STATE.journal)
            self._respond_json(200, {"ok": True, "journal": snapshot})
        elif path == "/__test/reset":
            with STATE.condition:
                STATE.journal = []
                STATE.faults = {}
            self._respond_json(200, {"ok": True})
        else:
            self._respond_json(404, {"ok": False, "description": "unknown control"})

    # -- Bot API -----------------------------------------------------------

    def _bot_method(self, method: str, payload: dict) -> None:
        if method == "getUpdates":
            self._get_updates(payload)
            return

        if STATE.take_fault("send_error") and method in {
            "sendMessage", "sendChatAction", "pinChatMessage", "sendPhoto", "sendDocument",
        }:
            STATE.record({"method": method, "fault": "send_error"})
            self._respond_json(500, {"ok": False, "description": "injected send failure"})
            return
        if STATE.take_fault("send_rate_limit") and method == "sendMessage":
            STATE.record({"method": method, "fault": "send_rate_limit"})
            self._respond_json(429, {
                "ok": False, "error_code": 429,
                "description": "Too Many Requests: retry after 2",
                "parameters": {"retry_after": 2},
            })
            return

        if method == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Acceptance Steward",
                      "username": "acceptance_steward_bot", "can_join_groups": True}
        elif method == "setMyCommands":
            STATE.registered_commands = payload.get("commands")
            STATE.record({"method": "setMyCommands", "commands": payload.get("commands")})
            result = True
        elif method == "getChat":
            # Topics are configured, so the probe requires a forum chat.
            result = {"id": payload.get("chat_id"), "type": "supergroup",
                      "title": "Acceptance", "is_forum": True}
        elif method == "getChatMember":
            result = {"user": {"id": payload.get("user_id"), "is_bot": False},
                      "status": "member"}
        elif method == "getFile":
            result = {"file_id": payload.get("file_id"),
                      "file_path": f"uploads/{payload.get('file_id')}"}
        elif method in {"sendMessage", "sendChatAction", "pinChatMessage",
                        "sendPhoto", "sendDocument"}:
            STATE.next_message_id += 1
            STATE.record({
                "method": method,
                "chat_id": payload.get("chat_id"),
                "thread": payload.get("message_thread_id"),
                "text": payload.get("text") or payload.get("caption") or payload.get("action"),
            })
            result = {"message_id": STATE.next_message_id, "date": int(time.time())}
        else:
            self._respond_json(404, {"ok": False,
                                     "description": f"method not implemented: {method}"})
            return
        self._respond_json(200, _bot_reply(result))

    # -- git smart HTTP ----------------------------------------------------

    def _git_http(self, body: bytes) -> None:
        """Bridge one request to `git http-backend`, which speaks CGI.

        The controller sees a normal HTTPS git remote; nothing here knows
        anything about the harness's transport policy.
        """
        path, _, query = self.path.partition("?")
        environment = dict(os.environ)
        environment.update({
            "GIT_PROJECT_ROOT": str(GIT_ROOT),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "REQUEST_METHOD": self.command,
            "QUERY_STRING": query,
            "REMOTE_ADDR": self.client_address[0],
            "GATEWAY_INTERFACE": "CGI/1.1",
            "SERVER_PROTOCOL": "HTTP/1.1",
            # The origin is seeded steward-owned by the harness entrypoint;
            # this server runs as root and must still serve it.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        })
        if self.headers.get("Content-Type"):
            environment["CONTENT_TYPE"] = self.headers["Content-Type"]
        if body:
            environment["CONTENT_LENGTH"] = str(len(body))
        completed = subprocess.run(
            [GIT_HTTP_BACKEND], input=body, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if not completed.stdout:
            self._respond_json(500, {"ok": False, "description": "empty git CGI response"})
            return
        head, separator, payload = completed.stdout.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            name_text, value_text = name.decode("latin-1").strip(), value.decode("latin-1").strip()
            if name_text.lower() == "status":
                status = int(value_text.split()[0]) if value_text.split() else 200
            else:
                headers.append((name_text, value_text))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _get_updates(self, payload: dict) -> None:
        offset = int(payload.get("offset") or 0)
        timeout = min(float(payload.get("timeout") or 0), 50.0)

        if STATE.take_fault("getupdates_hang"):
            # Hold far past the client's own read timeout, so the harness sees
            # exactly a dead poll and must recover from it.
            STATE.record({"method": "getUpdates", "fault": "getupdates_hang",
                          "offset": offset})
            time.sleep(timeout + 60)
            self._respond_json(200, _bot_reply([]))
            return

        deadline = time.monotonic() + timeout
        while True:
            with STATE.condition:
                while STATE.updates and STATE.updates[0][0] < offset:
                    STATE.updates.popleft()
                if STATE.updates:
                    result = [update for _, update in STATE.updates]
                    STATE.record({"method": "getUpdates", "offset": offset,
                                  "returned": len(result)})
                    self._respond_json(200, _bot_reply(result))
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    STATE.record({"method": "getUpdates", "offset": offset, "returned": 0})
                    self._respond_json(200, _bot_reply([]))
                    return
                STATE.condition.wait(min(remaining, 1.0))


def _ensure_certificate() -> tuple[Path, Path]:
    """A self-signed cert for the git origin, generated once per volume."""
    cert = GIT_ROOT / "ssl" / "cert.pem"
    key = GIT_ROOT / "ssl" / "key.pem"
    if not cert.is_file():
        cert.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "2",
             "-subj", "/CN=fake-botapi"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return cert, key


def main() -> None:
    server = ThreadingHTTPServer(BIND, Handler)
    server.daemon_threads = True

    git_server = ThreadingHTTPServer(GIT_BIND, Handler)
    git_server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*_ensure_certificate())
    git_server.socket = context.wrap_socket(git_server.socket, server_side=True)
    threading.Thread(target=git_server.serve_forever, daemon=True,
                     name="fake-git-https").start()

    print(f"fake-botapi listening on {BIND} and {GIT_BIND} (git)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
