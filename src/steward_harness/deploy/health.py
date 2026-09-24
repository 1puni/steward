"""Exact-SHA health gate over the deployed release's HTTP health document."""

from __future__ import annotations

import re
import time

import httpx

_GIT_OBJECT_NAME = re.compile(r"[0-9a-f]{40}")


class HealthGate:
    """Polls a health URL until it reports a target release SHA (or only a status code)."""

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    def wait_healthy(self, expected_sha: str | None = None, poll_interval: float = 2.0) -> tuple[bool, str]:
        """Block until healthy; verifies the reported SHA when the document provides one."""
        deadline = time.monotonic() + self.timeout_seconds
        last_details = "health check never ran"
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=5.0) as client:
                    response = client.get(self.url)
            except httpx.HTTPError as exc:
                last_details = f"health request failed: {exc}"
            else:
                if response.status_code != 200:
                    last_details = f"health status {response.status_code} (expected 200)"
                elif expected_sha is None:
                    return True, f"health status {response.status_code}"
                else:
                    reported = self._reported_sha(response)
                    if reported is None:
                        last_details = "health document reports no SHA"
                    elif reported == expected_sha:
                        return True, f"health reports {reported}"
                    else:
                        last_details = f"health reports {reported} (expected {expected_sha})"
            time.sleep(poll_interval)
        return False, last_details

    @staticmethod
    def _reported_sha(response: httpx.Response) -> str | None:
        try:
            document = response.json()
        except ValueError:
            return None
        if isinstance(document, dict):
            for key in ("sha", "release", "version", "commit"):
                value = document.get(key)
                # Only a Git object name answers "which commit is running".
                # A product version under the same key ("1.0.0") is not one,
                # and reading it as a SHA turns "reports no SHA" into a
                # mismatch that no correct deployment can ever clear.
                if isinstance(value, str) and _GIT_OBJECT_NAME.fullmatch(value):
                    return value
        return None
