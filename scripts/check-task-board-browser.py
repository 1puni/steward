"""Exercise the signed board on mobile Chromium using real Git task fixtures.

uv run --extra dev --with playwright python scripts/check-task-board-browser.py
Uses installed Chrome; set CHROME_EXECUTABLE to choose another Chromium binary.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_task_board import _Board, _completed_backlog, _init_data
from steward_harness.task_lock import task_lock
from steward_harness.web.health import HealthServer


def main():
    output = ROOT / "artefacts" / "task-board"
    output.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="task-board-browser-") as directory:
        board = _Board(Path(directory))
        waiting = board.admit("Choose the deployment account")
        board.slice(str(waiting.task_id), "ask", reason="Which account should own this?")
        blocked = board.admit("Restore the dependency")
        board.state.tasks.hold(blocked.task_id, "blocked", "Dependency unavailable")
        running = board.admit("Process current work")
        board.admit("Next queued task")
        _completed_backlog(board)
        lock = task_lock(board.locks, running.task_id)
        assert lock.acquire()
        server = HealthServer("127.0.0.1:0", board.web)
        server.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path=os.environ.get("CHROME_EXECUTABLE", "/opt/google/chrome/chrome"),
                    args=["--no-sandbox"],
                )
                page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1, is_mobile=True, has_touch=True)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                # Only Telegram's host bridge is stubbed; the app and signed API are real.
                page.route("https://telegram.org/js/telegram-web-app.js", lambda route: route.fulfill(
                    content_type="text/javascript", body="window.Telegram={WebApp:{initData:" + json.dumps(_init_data()) + ",ready(){},expand(){}}};"))
                page.goto(f"http://127.0.0.1:{server.port}/tasks/")
                expect(page.locator(".task")).to_have_count(4, timeout=25000)
                assert "205 tasks (200 listed; all open tasks included) · 2 need your attention" in page.locator("#counts").inner_text()
                assert "Waiting" in page.locator(".task").first.inner_text()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(output / "mobile-board.png"))
                page.get_by_role("button", name="Needs you", exact=True).click()
                assert page.locator(".task").count() == 2
                page.screenshot(path=str(output / "mobile-needs-you.png"))
                page.get_by_role("button", name="All tasks", exact=True).click()
                assert page.locator(".task").count() == 200
                page.locator("#search").fill(waiting.task_id.short)
                assert page.locator(".task").count() == 1
                page.locator(".task").click()
                expect(page.locator("#content")).to_contain_text("Needs your attention", timeout=25000)
                assert "Which account should own this?" in page.locator("#content").inner_text()
                assert page.get_by_role("button", name=f"/task answer {waiting.task_id.short} <your answer>", exact=True).count() == 1
                page.screenshot(path=str(output / "mobile-question.png"))
                board.state.tasks.answer(waiting.task_id, "Use the shared account")
                # Let the production 15-second timer refresh the open detail and board.
                expect(page.locator("#content .meta")).to_contain_text("Queued", timeout=25000)
                assert page.locator(".command").filter(has_text="/task answer").count() == 0
                page.screenshot(path=str(output / "mobile-resumed.png"))
                resumed_lock = task_lock(board.locks, waiting.task_id)
                assert resumed_lock.acquire()
                try:
                    expect(page.locator("#content .meta")).to_contain_text("Running", timeout=25000)
                finally:
                    resumed_lock.release()
                page.locator("#back").click()
                page.locator("#search").fill("")
                page.get_by_role("button", name="Needs you", exact=True).click()
                assert page.locator(".task").count() == 1
                # Overflow the real 200-row budget with open work as well.
                older = board.admit("Older waiting question")
                board.slice(str(older.task_id), "ask", reason="Which account should own this?")
                for index in range(201):
                    board.admit(f"New queued task {index}")
                page.get_by_role("button", name="In progress", exact=True).click()
                expect(page.locator(".task")).to_have_count(206, timeout=25000)
                assert "all open tasks included" in page.locator("#counts").inner_text()
                page.screenshot(path=str(output / "mobile-open-overflow.png"))
                assert not errors, errors
                browser.close()
            evidence = {"revision": revision, "viewport": "390x844", "checks": ["signed API", "201 landed tasks", "default waiting visibility", "attention/all/search filters", "canonical pending question", "timer refresh waiting -> queued -> running", "answer command removed after resume", "206 open tasks survive budget", "no horizontal overflow", "no page errors"], "screenshots": sorted(path.name for path in output.glob("*.png"))}
            (output / "browser-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence, indent=2))
        finally:
            server.stop()
            lock.release()


if __name__ == "__main__":
    main()
