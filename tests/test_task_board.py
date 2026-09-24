"""The board reads Git, its version is what it served, and it writes nothing."""

from __future__ import annotations

import subprocess

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from state_fixtures import admit_task
from steward_harness.config.schema import RepositoryConfig, UntrustedExecutionConfig
from steward_harness.runtime.execution import UntrustedExecutionBroker
from steward_harness.state import StateDatabase, TaskSpec, TaskId, CheckpointDisposition
from steward_harness.task_lock import task_lock
from steward_harness.task_store import GitTaskStore
from steward_harness.web.health import HealthServer
from steward_harness.web.tasks import TaskBoard, TaskWeb
# One way to write a slice, shared rather than copied: these commit exactly
# what `WorktreeCheckpointer.commit` commits, and a second copy of that would
# be a second definition of what a slice is.

TOKEN = "4242:test-bot-token"
OPERATOR = 7



def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    clone = tmp_path / "repo"
    clone.mkdir()
    _git("init", "-q", "-b", "main", cwd=clone)
    _git("config", "user.email", "test@example.invalid", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    (clone / "README.md").write_text("base\n")
    _git("add", "README.md", cwd=clone)
    _git("commit", "-qm", "base", cwd=clone)
    # What the observation loop delivers: the fetched default branch.
    _git("update-ref", "refs/steward/remote/main", "main", cwd=clone)
    return clone


def _slice(
    clone: Path,
    slug: str,
    disposition: str,
    *,
    base: str = "main",
    reason: str | None = None,
    findings: str | None = None,
) -> str:
    """One slice, written exactly as `WorktreeCheckpointer.commit` writes it."""
    if not _git("branch", "--list", f"tasks/{slug}", cwd=clone):
        _git("branch", "-f", f"tasks/{slug}", base, cwd=clone)
    worktree = clone.parent / f"wt-{slug}"
    _git("worktree", "add", "-q", str(worktree), f"tasks/{slug}", cwd=clone)
    (worktree / f"{slug}.txt").write_text(f"work {disposition}\n")
    _git("add", "--all", cwd=worktree)
    trailers = f"Disposition: {disposition}"
    if reason is not None:
        trailers += f"\nReason: {reason}"
    message = ["-m", f"slice for {slug}"]
    if findings is not None:
        message += ["-m", findings]
    _git("commit", "-q", *message, "-m", trailers, cwd=worktree)
    tip = _git("rev-parse", "HEAD", cwd=worktree)
    _git("worktree", "remove", "--force", str(worktree), cwd=clone)
    return tip

def _sign(
    fields: dict[str, str], *, token: str = TOKEN, extra: str = ""
) -> str:
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return f"{urlencode(fields)}&hash={digest}{extra}"


def _init_data(user_id: int = OPERATOR, *, age: int = 0, **kwargs) -> str:
    return _sign(
        {
            "auth_date": str(int(time.time()) - age),
            "user": json.dumps({"id": user_id}),
        },
        **kwargs,
    )


def _headers(init_data: str | None = None, **extra: str) -> dict[str, str]:
    signed = _init_data() if init_data is None else init_data
    return {"Authorization": f"tma {signed}", **extra}


class _Board:
    """One clone, one store and one index, wired exactly as the daemon wires them."""

    def __init__(self, tmp_path: Path) -> None:
        self.clone = _repository(tmp_path)
        self.state = StateDatabase(tmp_path / "state.db")
        self.locks = self.state.tasks.locks_root
        def landing(work, tip):
            log = _git("log", "--format=%H %(trailers:key=Steward-Work,valueonly)", tip, cwd=self.clone)
            named = {parts[1]: parts[0] for parts in map(str.split, log.splitlines()) if len(parts) == 2}
            return named.get(work)
        self.state.tasks.transports = {"app": SimpleNamespace(
            observed_tip=lambda: (_git("rev-parse", "refs/steward/remote/main", cwd=self.clone), None),
            landing=landing)}
        token = tmp_path / "telegram-token"
        token.write_text(TOKEN + "\n")
        self.model = TaskBoard(self.state)
        self.web = TaskWeb(
            self.model, token_path=str(token), allowed_users=(OPERATOR,)
        )

    def admit(self, title: str, brief: str = "Do the work."):
        return admit_task(
            self.state,
            TaskSpec("app", title, brief),

        )

    def read(self, query: str = "", **kwargs):
        return self.web.get(f"/tasks/api{query}", _headers(**kwargs))

    def row(self, task_id):
        return self.state.tasks.read(task_id)[0]

    def slice(self, slug, disposition, **kwargs):
        task_id = TaskId(slug)
        opening = self.state.tasks.read(task_id)[0]
        tip = _slice(self.clone, slug, disposition, **kwargs)
        self.state.tasks.git("fetch", "--no-write-fetch-head", str(self.clone), tip)
        self.state.tasks.finish_slice(task_id, disposition=CheckpointDisposition(disposition), opened_at=opening,
                                     work_sha=tip, detail=kwargs.get("reason"))
        return tip


@pytest.fixture
def board(tmp_path: Path) -> _Board:
    return _Board(tmp_path)


def test_the_version_follows_the_git_facts_the_status_follows(board: _Board) -> None:
    """The token must move whenever the branch does, and the row never moves.

    This is the defect the archived board shipped: its version hashed the
    `tasks` row and a max input sequence, and every fact that decides a task's
    status now lives outside that row. A tip moving, an `ask` trailer
    appearing and a `flock` being taken each change what the operator is
    looking at while writing nothing the old token could see.
    """
    task = board.admit("Rotate tokens")
    slug = str(task.task_id)

    status, headers, body = board.read()
    assert status == 200
    assert [item["status"] for item in json.loads(body)["tasks"]] == ["queued"]
    # A second read of an unchanged board is byte-identical, or the token is
    # noise: nothing in the document may depend on the clock or on dict order.
    assert board.read()[1]["ETag"] == headers["ETag"]
    assert board.read()[2] == body

    # The slice stops to ask. The row is not touched by any of it.
    before = board.row(task.task_id)
    board.slice(slug, "ask", reason="Which consumer is authoritative?")
    assert board.row(task.task_id) != before
    before = board.row(task.task_id)

    _status, asked, body = board.read()
    document = json.loads(body)
    assert document["tasks"][0]["status"] == "waiting"
    assert document["tasks"][0]["reason"] == "Which consumer is authoritative?"
    assert asked["ETag"] != headers["ETag"]

    # And `running` is a held lock, which writes nothing at all, anywhere.
    lock = task_lock(board.locks, task.task_id)
    assert lock.acquire()
    try:
        assert board.row(task.task_id) == before
        _status, locked, body = board.read()
        assert json.loads(body)["tasks"][0]["status"] == "running"
        assert locked["ETag"] not in {headers["ETag"], asked["ETag"]}
    finally:
        lock.release()


def test_an_unchanged_board_is_not_sent_twice(board: _Board) -> None:
    """The token earns its keep as a cache validator, which is all it is.

    It fences nothing, because nothing here writes.
    """
    board.admit("Rotate tokens")
    served = board.read()[1]["ETag"]
    status, _sent, body = board.web.get(
        "/tasks/api", _headers(**{"If-None-Match": served})
    )
    assert (status, body) == (304, b"")


def test_a_listing_costs_one_snapshot_however_many_tasks_exist(
    board: _Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filtering, counting and paging happen over a resolved snapshot.

    The way to break it is to ask the store something per row: `checkpoints`
    is a `git log` and belongs to the one-task read alone.
    """
    for ordinal in range(6):
        board.admit(f"Rotate token {ordinal}")
    calls: list[str] = []
    real = GitTaskStore.checkpoints
    monkeypatch.setattr(GitTaskStore, "checkpoints",
                        lambda self, *a, **k: calls.append("checkpoints") or real(self, *a, **k))
    document = board.model.board()

    assert len(document["tasks"]) == 6
    assert document["counts"] == {"queued": 6}
    assert calls == []


def test_the_one_task_read_carries_what_task_show_carries(board: _Board) -> None:
    task = board.admit("Rotate tokens", "Rotate the feed credentials.")
    slug = str(task.task_id)
    board.slice(slug, "continue", findings="Found the consumer.")
    board.slice(slug, "ask", reason="Which one?", findings="Two of them read it.")
    board.state.tasks.note(task.task_id, "The second one is authoritative.")

    status, _headers, body = board.read(f"?task={slug}")
    detail = json.loads(body)

    assert status == 200
    assert detail["brief"].startswith("Rotate the feed credentials.")
    assert detail["findings"] == "Two of them read it."
    assert detail["pending_inputs"] == 1
    assert [point["disposition"] for point in detail["checkpoints"]] == [
        "ask", "continue"
    ]
    assert board.read("?task=no-such-task")[0] == 404


@pytest.mark.parametrize(
    "init_data",
    [
        pytest.param("", id="unsigned"),
        pytest.param(_init_data(token="4242:another-bot"), id="wrong token"),
        pytest.param(_init_data(user_id=OPERATOR + 1), id="off the allowlist"),
        pytest.param(_init_data(age=7200), id="expired"),
        pytest.param(
            _init_data(extra="&auth_date=1"), id="duplicated key"
        ),
        pytest.param(_init_data()[:-1], id="mangled hash"),
    ],
)
def test_no_task_content_leaves_without_a_valid_signature(
    board: _Board, init_data: str
) -> None:
    """The signature is what protects the data, on loopback as anywhere else.

    A board that only checked its bind would be one nginx stanza away from
    public, which is the stanza this instance writes for every other service.
    """
    board.admit("Rotate tokens")
    status, _headers, body = board.web.get(
        "/tasks/api", {"Authorization": f"tma {init_data}"}
    )
    assert status == 403
    assert b"Rotate tokens" not in body
    assert b"rotate-tokens" not in body


def test_the_shell_carries_no_data_and_the_rest_is_not_this_boards(
    board: _Board,
) -> None:
    board.admit("Rotate tokens")
    for path in ("/tasks", "/tasks/", "/tasks/app.js"):
        status, headers, body = board.web.get(path, {})
        assert status == 200, path
        assert b"Rotate tokens" not in body
        assert "frame-ancestors" in headers["Content-Security-Policy"]
    assert board.web.get("/tasks/anything-else", {})[0] == 404
    # Not this board's path at all: the health endpoint still answers it.
    assert board.web.get("/healthz", {}) is None


def test_the_listener_serves_the_board_beside_health_and_admits_no_write(
    board: _Board,
) -> None:
    """One loopback listener, two GETs, and no method that changes anything."""
    server = HealthServer("127.0.0.1:0", board.web)
    server.start()
    try:
        root = f"http://127.0.0.1:{server.port}"
        # Health still answers; without a release identity it fails closed,
        # which is the point of it and not this board's business.
        with pytest.raises(urllib.error.HTTPError) as unnamed:
            urllib.request.urlopen(f"{root}/healthz", timeout=5)
        assert json.loads(unnamed.value.read()) == {"ok": False, "sha": ""}
        with urllib.request.urlopen(f"{root}/tasks", timeout=5) as response:
            assert b"<title>Tasks</title>" in response.read()
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(
                urllib.request.Request(
                    f"{root}/tasks/api", data=b"{}", headers=_headers()
                ),
                timeout=5,
            )
        assert refused.value.code == 501
    finally:
        server.stop()


def test_a_landed_task_reports_the_commit_the_remote_took(board: _Board) -> None:
    task = board.admit("Rotate tokens")
    work = board.slice(str(task.task_id), "idle")
    landed = _git("commit-tree", f"{work}^{{tree}}", "-p", "main",
                  "-m", f"steward: accept\n\nSteward-Work: {work}", cwd=board.clone)
    _git("update-ref", "refs/steward/remote/main", landed, cwd=board.clone)

    document = json.loads(board.read()[2])
    assert document["tasks"][0]["status"] == "done"
    assert document["tasks"][0]["tip"] == landed
