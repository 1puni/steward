# The task board

A read-only view of the tasks the harness already has. It is served on the
loopback health listener, opened as a Telegram Mini App and signed with the bot
token. It adds no table, no configuration field, no second listener and no write
path. Every action stays a `/task` command.

It is served when `controller.health_bind` and `telegram` are both configured,
under the path prefix `/tasks`, and not otherwise.

## 1. Where the displayed data comes from

One read: `GitTaskStore.all()` for the board, `GitTaskStore.get()` for one task.
Each returns `Task` values whose status is derived in one place from the accepted
task document, the retained slice commit's `Disposition`/`Reason` trailers and
message, the observed remote's ancestry, and the live task lock.

The board shows `task_id`, `title`, `repository`, `priority`, `origin`, `status`,
`reason`, `withdrawn` (the accepted `cancelled` hold) and, for a landed task, the
commit the remote took. The detail view adds the brief, the last slice's findings,
the pending input count and `checkpoints` (one `git log`). **paused** is the state
directory's marker file. No timestamp is shown.

The detail view uses the same accessors as `/task show`, so the two surfaces cannot
report different things about the same task.

## 2. The version token is the digest of the body

The `ETag` is the first 12 hex characters of the SHA-256 of the response body
itself. It is not also a field in the document, because a body that contained its
own digest would no longer be the thing the digest was taken over.

The obvious alternative is to hash the facts you think matter: a row, an input
counter, a cancellation time. It is wrong here, and silently so. Almost nothing
that moves a task between `queued`, `running`, `waiting` and `done` is written
anywhere you would think to hash: a slice commits and the tip moves; the tip
carries `Disposition: ask`; publication lands in the observed default branch; a
worker takes or releases a `flock` and writes nothing at all. A token that
enumerates facts falls behind the renderer the moment the two part company. A
token computed from the bytes that were served cannot, because it is not a second
description of anything.

The honest limitation is the right boundary: the token detects changes to
*displayed* facts only. For a read-only board, "is what I am looking at still what
the server would serve?" is exactly the question.

One constraint is load-bearing: **the body must contain nothing
nondeterministic.** No generation timestamp, no request id, no iteration order
that depends on insertion history. A body that changes on every request makes the
token useless while looking like it works. The tests assert that two reads of an
unchanged board return the same token, and that closing a slice changes it.

## 3. No paging, no filtering, no counting

The board is one snapshot, then Python: `all()` takes one observed tip per
repository and one lock scan, and every field after that is an attribute. It
serves at most 200 task summaries, about 60 KB, as one document, and the browser
scrolls it.

Server-side paging would buy a cursor, an offset that goes stale between requests
and a token that covers only a page. None of that is worth having for 60 KB.
`checkpoints` is never called in the listing path, because it is a `git log` per
task.

## 4. The trust boundary

**Bind.** The existing health server, on `controller.health_bind`. No second
listener, no second port, no new field. The bot token and the operator allowlist
come from the `telegram` block.

**TLS.** Nothing in this repository terminates TLS, and the harness has no opinion
about what sits in front of it. Telegram only opens a Mini App over HTTPS, so
reaching the board from a phone means the instance puts a TLS proxy in front and
registers the `web_app` URL itself. An ordinary reverse proxy to the loopback
listener is the usual shape.

**Auth.** Every data request carries `Authorization: tma <initData>` and is
verified before anything is read: HMAC-SHA256 over the sorted `initData` pairs,
keyed by `HMAC("WebAppData", bot token)`, compared in constant time; strict parsing,
so a duplicated key is rejected rather than quietly resolved; `auth_date` no older
than an hour; and `user.id` checked against `telegram.allowed_users`. The static
shell, one HTML file and one script with no task in it, is served without auth.

Authentication is required **even though the bind is loopback**, and that is the
deliberate part. A loopback service with no auth is a landmine: the first thing
anyone does with it is put a proxy in front. Requiring a signature means that step
exposes a surface that is still closed.

**What someone who reaches the port can do.** Fetch the static shell. Everything
else is `403` without an `initData` blob signed by the bot token, and there is
nothing to write. Read disclosure (titles, briefs, repository names, findings) is
what the signature protects, and it is the whole of the exposure.

## 5. Writes: none here

Every action stays where it already is: `/task
show|confirm|reject|answer|retry|note|cancel|priority|model|model_family`.
Three reasons, in order of weight.

**It would be the first inbound authority in the harness.** Telegram ingress is an
outbound long poll, and `/healthz` returns one SHA. Nothing accepts an instruction
from a socket someone else dialled. The [execution boundary](execution-boundary.md)
is made of UIDs and file permissions, and its controller side may run as root. An
internet-reachable write path is a different class of change from a view, and has
to be argued on its own merits, not arrive as part of a UI.

**`note` and `answer` text is fed to a provider.** An authenticated write endpoint
is an authenticated prompt-injection endpoint into the controller's own cognition.
The bar for that is a reviewed proxy, rate limiting and a certificate story, which
is instance infrastructure this repository does not ship.

**Two write surfaces drift.** A button that composes `/task cancel` would, in
practice, grow its own cancellation semantics. One write surface cannot have two.

The one thing a board is genuinely better at, cleaning up a pile of superseded
work, is met where the write already lives: `/task cancel a b c :: superseded by d`
cancels several tasks with one reason and reports per-task outcomes.

## 6. Deliberately not built

- **No table** and **no configuration field.** There is no write to make
  idempotent, and no public URL for the harness to pretend to own.
- **No server-side paging, filtering or search.** See §3.
- **No optimistic concurrency.** The token is a staleness *indicator*, not a fence.
  A fence guards a write.
- **No inline keyboards or callback queries.** The text commands already do those
  actions, and a menu would be a third way to express them.
- **No launch button, menu-button reconciliation or demo mode.** Each assumes a
  public HTTPS endpoint this repository cannot provide.

## Where it is

| Piece | File |
| --- | --- |
| Verifier, board, routing | `src/steward_harness/web/tasks.py` |
| Shell and script | `src/steward_harness/web/task_app/` |
| The listener it rides on | `src/steward_harness/web/health.py` |
| Bulk cancel | `KernelCommands` in `src/steward_harness/daemon.py` |
| Tests | `tests/test_task_board.py` |

About 260 lines of Python and a small browser script. An earlier attempt at the same
feature ran to 2,180 lines, queried ten tables and added two more. The difference is
not compression: it is one document instead of a paged API, no write path, no second
store, no configuration and no menus.
