"""Loopback health endpoint reporting the running release identity."""

from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _loaded_release_sha() -> str:
    """Identify this loaded module inside a controller-staged release.

    Resolve the code location at import, before any current-symlink swap.
    The interpreter itself may be a symlink outside the release entirely.
    """
    for root in Path(__file__).resolve().parents:
        if re.fullmatch(r"[0-9a-f]{40}", root.name) is None:
            continue
        receipt = root.parent / f".{root.name}.release.json"
        try:
            identity = json.loads(receipt.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(identity, dict) and identity.get("sha") == root.name:
            return root.name
    return ""


_LOADED_RELEASE_SHA = _loaded_release_sha()


def release_sha() -> str:
    """Loaded release identity, or an explicit full-SHA packaging attestation."""
    if _LOADED_RELEASE_SHA:
        return _LOADED_RELEASE_SHA
    explicit = os.environ.get("STEWARD_RELEASE_SHA", "").strip()
    return explicit if re.fullmatch(r"[0-9a-f]{40}", explicit) else ""


class HealthServer:
    """Serves /healthz with the exact release SHA the deploy gate expects.

    It also carries the task board, when one is configured. A second listener
    would be a second port, a second bind to validate and a second thing to
    stop; this one is already loopback and already running, and the board is
    a `GET` like the other one.
    """

    def __init__(self, bind: str, board: object | None = None) -> None:
        host, _, port = bind.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError(f"health bind must be host:port, got {bind!r}")
        self._port = int(port)
        self._release_sha = release_sha()
        self._board = board

        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                answer = (
                    None if server._board is None
                    else server._board.get(self.path, self.headers)
                )
                if answer is not None:
                    self._write(*answer)
                elif self.path != "/healthz":
                    self._send(404, {"error": "not found"})
                else:
                    sha = server._release_sha
                    # A missing identity fails the gate: never report healthy
                    # for a release we cannot name.
                    self._send(200 if sha else 503, {"ok": bool(sha), "sha": sha})

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode("utf-8")
                self._write(
                    code,
                    {"Content-Type": "application/json", "Cache-Control": "no-store"},
                    body,
                )

            def _write(self, code: int, headers: dict, body: bytes) -> None:
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, fmt: str, *args: object) -> None:
                pass  # keep journald quiet

        self._httpd = ThreadingHTTPServer((host, self._port), Handler)
        self._httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        thread = threading.Thread(
            target=self._httpd.serve_forever, name="steward-health", daemon=True
        )
        thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
