"""Loopback healthz: release identity for the self-deploy gate."""

from __future__ import annotations

import json
import urllib.request

import pytest

from steward_harness.web.health import HealthServer, release_sha


def _get(port: int, path: str = "/healthz") -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz_reports_release_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEWARD_RELEASE_SHA", "a" * 40)
    server = HealthServer("127.0.0.1:0")
    server.start()
    try:
        assert release_sha() == "a" * 40
        monkeypatch.setenv("STEWARD_RELEASE_SHA", "b" * 40)
        status, body = _get(server.port)
        assert status == 200
        assert body == {"ok": True, "sha": "a" * 40}
        assert _get(server.port, "/other")[0] == 404
    finally:
        server.stop()


def test_healthz_fails_closed_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEWARD_RELEASE_SHA", raising=False)
    server = HealthServer("127.0.0.1:0")
    server.start()
    try:
        status, body = _get(server.port)
        assert status == 503
        assert body == {"ok": False, "sha": ""}
    finally:
        server.stop()


def test_health_bind_is_validated() -> None:
    with pytest.raises(ValueError):
        HealthServer("not-a-bind")
    with pytest.raises(ValueError):
        HealthServer("127.0.0.1:notaport")


@pytest.mark.parametrize("value", ["abc123", "main", "A" * 40, "a" * 39])
def test_invalid_explicit_identity_cannot_pass_health(monkeypatch, value):
    monkeypatch.setenv("STEWARD_RELEASE_SHA", value)
    assert release_sha() == ""
