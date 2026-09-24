# The task board

A read-only browsing surface over the tasks the harness already has, served on
the loopback health listener and signed by the Telegram bot token. It adds no
table, no configuration field, no second listener, and no write path.

This replaces the 2,180-line Mini App written on 2026-09-06 and never merged
(`archive/telegram-miniapp`, `archive/decisions-desk`,
`archive/miniapp-config-validation`). That work is kept for its intent and for
one competent piece of code — the `initData` verifier — and for nothing else.
It queried ten tables, nine of which no longer exist; it created two more; and
it read `tasks.status`, which is not a column any more.

## 1. Where the displayed data comes from

One read: `GitTaskStore.all()` for the board, `GitTaskStore.get()` for one task
(`src/steward_harness/task_store.py`). Each returns `Task` values whose status
is derived in one place from the accepted task document, the retained slice
commit's `Disposition`/`Reason` trailers and message, the observed remote's
ancestry, and the live task lock. The board shows `task_id`, `title`,
`repository`, `priority`, `origin`, `status`, `reason`, `withdrawn` (the
accepted `cancelled` hold) and, for a landed task, the commit the remote took.
The detail read adds the brief, the last slice's findings, the pending input
count and `checkpoints`, which is one `git log`. **paused** is the state
directory's marker file. No timestamp is shown.

## 2. The version token

The old token was
`sha256(tasks row + max(task_inputs.input_sequence) + task_cancellations.requested_at)`
(`task_board.py:52` in `archive/telegram-miniapp`). On today's code that is
silently wrong. Every fact that moves a task between `queued`, `running`,
`waiting` and `done` is outside the row:

- a slice commits and the tip moves — the row is untouched;
- the tip carries `Disposition: ask` — the row is untouched;
- publication lands the tip in the observed default branch — the row is
  untouched;
- a worker takes or releases the task's `flock` — nothing is written at all.

A mechanical port therefore yields a staleness check that cannot detect
staleness. No test would notice, because the row *is* stable and the hash *is*
stable and both are behaving exactly as written.

**The token is the SHA-256 digest of the response body itself**, first 12 hex
characters, returned as the `ETag` and nowhere else. It is not also a field in
the document, because a body that contained its own digest would no longer be
the thing the digest was taken over.

The justification is that this is the only definition that cannot fall behind
what the operator saw, because it is not a second description of the facts that
matter — it is the bytes that were served. Any change to any displayed field
changes the digest by construction. There is no enumeration to keep in sync with
the renderer, which is the precise thing that broke: the old token enumerated
`tasks.*` while the renderer displayed status, and the two drifted the moment
status stopped living in `tasks`.

The dual is the honest limitation, and it is the right boundary here: the token
detects changes to *displayed* facts only. A fact the board hides can change
without changing the token. For a read-only board that is exactly the contract
wanted — "is what I am looking at still what the server would serve?" — and it
is what the digest computes, rather than an approximation of it.

One constraint follows and is load-bearing: **the body must contain no
nondeterministic field.** No generation timestamp, no request id, no iteration
order that depends on a dict's insertion history. A body that changes on every
request makes the token useless while looking like it works — the same failure
class as the token it replaces. The regression test for this asserts that two
consecutive reads of an unchanged board return the same token, and that closing
a slice on a branch changes it.

## 3. Listing, filtering, counting, pagination

The board is one snapshot, then Python: `all()` takes one observed tip per
repository and one lock scan, and every field after that is an attribute.

**Filtering, counting and pagination are not done at all, on either side of the
wire.** The whole board is one document and the browser scrolls it. This is a
subtraction rather than a port: `WHERE status IN (...) LIMIT 8 OFFSET ?` plus
`COUNT(*) GROUP BY status` cannot be expressed in SQL now, and the natural
replacement — the same arithmetic in Python — buys a page cursor, an offset that
goes stale between requests, and a token that covers only the page. 200 task
summaries are about 60 KB; the board serves at most 200.

`checkpoints` is forbidden in the listing path and used only in the one-task
detail read, because it is one `git log` per task. The detail read uses the
same accessors `/task show` uses, so the two surfaces cannot report different
things about the same task.

## 4. The trust boundary

**Bind.** The board is served by the existing `HealthServer`
(`src/steward_harness/web/health.py:43`) on `controller.health_bind`
(`src/steward_harness/config/schema.py:523`), under the path prefix `/tasks`. No
second listener, no second port, no new configuration field. It is served when
`controller.health_bind` and `telegram` are both configured, and not otherwise —
the token and the operator allowlist come from `TelegramConfig`
(`src/steward_harness/config/schema.py:326`).

**TLS.** Nothing in this repository terminates TLS, and this design does not
pretend otherwise. The old one validated `task_app_url` as a public HTTPS URL,
which *required* a reverse proxy that exists in no script here — the one an
earlier rollout actually used was kept root-only on its host and was never in
the repository. That field is not
reintroduced. The harness binds loopback and has no opinion about what is in
front of it. Telegram will only open a Mini App over HTTPS, so launching this
from the bot requires an instance to put a TLS proxy in front and register the
`web_app` URL itself; an ordinary nginx `proxy_pass` to the loopback listener,
with WebSocket upgrade headers only where a service needs them, is the usual shape.

**Auth.** Every data request carries `Authorization: tma <initData>` and is
verified before anything is read: HMAC-SHA256 over the sorted `initData` pairs
with `hmac.new(b"WebAppData", token, sha256)` as the key, compared with
`hmac.compare_digest`; `strict_parsing` so a duplicated key is rejected rather
than silently resolved; a bounded `auth_date` window; and the `user.id` checked
against `telegram.allowed_users`. This is the archived implementation's
mechanism, reused deliberately — it was the one piece of that work worth
keeping. The static shell — one HTML file and one script, neither of which
contains a task — is served unauthenticated for that reason.

Authentication is required **even though the bind is loopback**, and that is the
deliberate part. A loopback service with no auth is a landmine for exactly this
deployment: the instance already runs nginx with Let's Encrypt for other
services, and the first thing anyone will do is proxy this one. Requiring a
signature means that step exposes a surface that is still closed.

**What a reacher-of-the-port can do.** Fetch the static shell. Everything else
is `403` without an `initData` blob signed by the bot token, which they do not
have; and there is nothing to write, because there is no write path. Read
disclosure — task titles, briefs, repository names, findings — is what the
signature protects, and it is the whole of the exposure.

## 5. Writes: none here

The board writes nothing. Every action stays exactly where it already is:
`KernelCommands._task` (`src/steward_harness/daemon.py:258`), reached as
`/task show|confirm|reject|answer|retry|note|cancel|priority|model|model_family`
(`src/steward_harness/telegram/commands.py:46`), calling `state.confirm_task`,
`state.reject_task`, `state.answer_task`, `state.note_task`, `state.retry_task`,
`state.cancel_task` and `state.set_task_priority`.

Three reasons, in order of weight.

**It is the first inbound authority in the harness.** Telegram ingress is an
*outbound* long poll; `/healthz` returns one SHA. Nothing today accepts an
instruction from a socket someone else dialled. `docs/execution-boundary.md`
describes a trust boundary made of UID and file permissions, and the controller
side of it runs as root. Adding an internet-reachable write path to it is a
different class of change from adding a view, and it should be argued on its own
merits rather than arriving as part of a UI.

**`note` and `answer` text is fed to a provider.** `state.note_task` and
`state.answer_task` write operator input that the next slice reads
(`state.py:2490`). An authenticated write endpoint is therefore an
authenticated prompt-injection endpoint into a root-run controller's own
cognition. The bar for that is a reviewed proxy, rate limiting and a documented
certificate story — all of which are instance infrastructure this repository
does not ship.

**The write would be a string.** Routing through the same functions the text
commands call means, in practice, composing `/task cancel <id> <reason>` and
handing it to `_task`. The UI contribution is the *selection*, not the action —
and the operator's own complaint at
`docs/harness-feedback-2026-09-06_09.md:504` was that the two surfaces had
different cancellation semantics. One write surface cannot have two semantics.

That leaves the operator's second want — bulk-cleaning superseded work — unmet
by a read-only board, so it is met where the write already lives: **`/task
cancel` accepts several ids when a `::` separates them from the reason.**
`/task cancel a b c :: superseded by d` is one call to the same
`state.cancel_task` per id, with the same reason, reporting per-id outcomes.
Single-id syntax is untouched. This is the smallest form of "bulk clean" that
does not create a second way to cancel a task.

## 6. Deliberately not built

- **No table.** `task_ui_actions` and `task_ui_prompts` are not recreated. Both
  existed to make a write idempotent under replay; there is no write.
- **No configuration field.** `task_app_url` is gone with the public-HTTPS
  assumption it encoded. Nothing replaces it.
- **No server-side paging, filtering or search.** §3.
- **No optimistic-concurrency enforcement.** The token is a staleness
  *indicator*, not a fence. A fence guards a write.
- **No inline keyboards or callback queries.** The archived work also added
  `edit_message`, `answer_callback_query` and `callback_query` routing to the
  poller in order to build menus. The text commands already do those actions,
  and a menu is a third way to express them.
- **No attempts, landings or deployment panel.** Those tables are gone. `tip()`
  on a `done` task is the surviving fact.
- **No `supersede` action.** It was `cancel` with a prefixed reason. It is
  spelled `/task cancel <id> :: superseded by <other>`, which is what the CLI
  always did and what `harness-feedback-2026-09-06_09.md:504` asked for.
- **No launch button, menu-button reconciliation or demo mode.** Each assumes a
  public HTTPS endpoint this repository cannot provide.

## Where it is

| Piece | File |
| --- | --- |
| verifier, board, routing | `src/steward_harness/web/tasks.py` |
| shell and script | `src/steward_harness/web/task_app/` |
| the listener it rides on | `src/steward_harness/web/health.py` |
| construction | `StewardDaemon._board` in `src/steward_harness/daemon.py` |
| bulk cancel | `KernelCommands._cancel` in `src/steward_harness/daemon.py` |
| tests | `tests/test_task_board.py` |

356 lines of implementation and 92 of browser script, against the archived
2,180. The difference is not compression: it is one document instead of a
paged API, no write path, no second store, no configuration, and no menus.
