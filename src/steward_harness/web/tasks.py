"""Read-only task board, signed by the bot token and served beside /healthz.

The design is `docs/task-mini-app.md`; three of its decisions are load-bearing
here and are repeated where they bind.

**One snapshot, then Python.** A listing is one ``GitTaskStore.all()``, after
which every field is an attribute. Nothing in this module calls
``checkpoints`` for more than one task, because each is a ``git log``.

**The version is the digest of the body.** Not a hash of the row: status is
derived from the branch tip, its trailers, ancestry and a lock, and none of
those touch the row. A token that enumerates the facts it thinks matter falls
behind the renderer the moment they part company, which is exactly what
happened. A token computed from the bytes that were served cannot. The price is
that the body must hold nothing nondeterministic — hence ``sort_keys`` and no
clock anywhere below.

**Nothing here writes.** The actions live in ``/task`` and stay there; this
would be the first inbound authority in a harness whose boundary is UID and
file permissions, and the text an authenticated write would carry is read by a
provider.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from steward_harness.state import StateDatabase, TaskId, TaskStatus

#: How long one Telegram sign-in stays good for. Long enough to read a board,
#: short enough that a captured `initData` is not a standing grant.
AUTH_WINDOW_SECONDS = 3600

#: What a browse is for: the ones that need the operator come first, the ones
#: that need nobody come last. Within a rank the order is `GitTaskStore.all`'s —
#: priority, age, slug — which a stable sort preserves and which is total, so
#: two reads of an unchanged board serialize identically.
_RANK = {
    TaskStatus.WAITING: 0,
    TaskStatus.BLOCKED: 1,
    TaskStatus.PROPOSED: 2,
    TaskStatus.RUNNING: 3,
    TaskStatus.QUEUED: 4,
    TaskStatus.DONE: 5,
    TaskStatus.CANCELLED: 6,
}

_PREFIX = "/tasks"
_ASSETS = {
    "": ("index.html", "text/html; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
#: On every answer this module gives, including its refusals.
_SECURITY = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors https://web.telegram.org "
        "https://*.telegram.org"
    ),
}


def authenticate(
    init_data: str,
    token: str,
    allowed_users: tuple[int, ...],
    *,
    now: float | None = None,
) -> int:
    """Verify one Telegram `initData` blob and return the operator it names.

    Telegram signs the sorted pairs with the bot token, so possession of a
    valid blob is possession of a grant the bot issued — which is why this is
    required even on a loopback bind: the day an instance puts a proxy in
    front, the surface behind it is still closed.

    `strict_parsing` rather than tolerant parsing because a duplicated key
    would otherwise be signed as one value and read as another.
    """
    if not init_data or len(init_data) > 12000:
        raise PermissionError("Open this board from the Telegram bot to sign in.")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise PermissionError("Invalid Telegram sign-in") from None
    data = dict(pairs)
    if len(data) != len(pairs):
        raise PermissionError("Invalid Telegram sign-in")
    digest = data.pop("hash", "")
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise PermissionError("Invalid Telegram sign-in")
    try:
        age = (time.time() if now is None else now) - int(data["auth_date"])
        user_id = json.loads(data["user"])["id"]
    except (KeyError, TypeError, ValueError):
        raise PermissionError("Invalid Telegram sign-in") from None
    if age < -30 or age > AUTH_WINDOW_SECONDS:
        raise PermissionError("This sign-in expired. Reopen the board in Telegram.")
    if type(user_id) is not int or user_id not in allowed_users:
        raise PermissionError("This board is not available to this account.")
    return user_id


class TaskBoard:
    """Every task the store knows, resolved against one Git snapshot."""

    def __init__(self, state: StateDatabase) -> None:
        self.state = state

    def board(self) -> dict:
        """The whole board as one document; the browser does the rest.

        There is no page, no offset and no server-side filter. The SQL those
        were written in had a `status` column and no longer does, and the
        Python that would replace it buys an offset that goes stale between
        requests and a token covering one page of it. The board caps the
        read at 200 rows, which is the ceiling this relies on.
        """
        rows = [self._summary(task) for task in self.state.tasks.all()[:200]]
        rows.sort(key=lambda row: _RANK[TaskStatus(row["status"])])
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"paused": self.state.paused(), "counts": counts, "tasks": rows}

    def detail(self, task_id: TaskId) -> dict:
        """One task, from the same accessors `/task show` reads.

        Deliberately the same ones: two surfaces reporting the same task from
        different stores is how they come to disagree. `findings` and
        `checkpoints` are one `git log` each, which is affordable exactly once
        and never in `board()`.
        """
        task = self.state.tasks.get(task_id)
        return self._summary(task) | {
            "brief": task.brief,
            "findings": task.findings,
            "pending_inputs": len(task.pending),
            "checkpoints": [
                {
                    "disposition": item.disposition.value,
                    "question": item.question,
                    "at": item.created_at,
                }
                for item in self.state.tasks.checkpoints(task, 5)
            ],
        }

    @staticmethod
    def _summary(task) -> dict:
        status = task.status
        return {
            "task_id": str(task.task_id),
            "title": task.title,
            "repository": task.repository,
            "status": status.value,
            "reason": task.reason,
            "priority": task.priority,
            "origin": task.origin_kind.value,
            # No timestamp. `Task` carries none, and widening it so a view can
            # print an age would be the store growing a column for a renderer.
            # A withdrawal aimed at work running right now is a marker file,
            # because it outlives neither this machine nor that run.
            "withdrawn": task.definition.hold == "cancelled",
            # For a landed task this is the commit the remote took; publication
            # retips the branch at what it pushed.
            "tip": task.tip if status is TaskStatus.DONE else None,
        }


class TaskWeb:
    """Route `/tasks` over the health listener: a shell, and a signed read."""

    def __init__(
        self,
        board: TaskBoard,
        *,
        token_path: str,
        allowed_users: tuple[int, ...],
    ) -> None:
        self.board = board
        self.allowed_users = tuple(allowed_users)
        try:
            self.token = Path(token_path).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise RuntimeError(f"Could not read Telegram token from {token_path}: {error}")
        if not self.token:
            raise RuntimeError(f"Telegram token at {token_path} is empty")

    def get(self, path: str, headers) -> tuple[int, dict[str, str], bytes] | None:
        """Answer one GET, or None when the path is not this board's."""
        answer = self._route(path, headers)
        if answer is None:
            return None
        status, extra, body = answer
        return status, {**_SECURITY, **extra}, body

    def _route(self, path: str, headers) -> tuple[int, dict[str, str], bytes] | None:
        """The shell is unauthenticated because it carries no data; every byte
        of task content is behind the signature."""
        split = urlsplit(path)
        if split.path != _PREFIX and not split.path.startswith(_PREFIX + "/"):
            return None
        suffix = split.path[len(_PREFIX):]
        asset = _ASSETS.get(suffix)
        if asset is not None:
            name, mime = asset
            body = (Path(__file__).parent / "task_app" / name).read_bytes()
            return 200, {"Content-Type": mime}, body
        if suffix != "/api":
            return self._json(404, {"error": "Not found"})
        try:
            authenticate(
                headers.get("Authorization", "").removeprefix("tma "),
                self.token,
                self.allowed_users,
            )
            query = dict(parse_qsl(split.query))
            requested = query.get("task")
            document = (
                self.board.detail(TaskId(requested))
                if requested
                else self.board.board()
            )
        except PermissionError as error:
            return self._json(403, {"error": str(error)})
        except LookupError:
            return self._json(404, {"error": "Task not found"})
        except ValueError as error:
            return self._json(400, {"error": str(error)})
        body = json.dumps(document, sort_keys=True, ensure_ascii=False).encode()
        # The version *is* this digest. Nothing enumerates the facts that
        # matter, so nothing can fall behind them; see the module docstring.
        etag = f'"{hashlib.sha256(body).hexdigest()[:12]}"'
        if headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag}, b""
        return 200, {"Content-Type": "application/json; charset=utf-8", "ETag": etag}, body

    @staticmethod
    def _json(status: int, payload: dict) -> tuple[int, dict[str, str], bytes]:
        return (
            status,
            {"Content-Type": "application/json; charset=utf-8"},
            json.dumps(payload).encode(),
        )
