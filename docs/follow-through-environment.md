# The follow-through environment

A green `uv run pytest` says nothing about Telegram follow-through: the suite
mocks the transport inside the process, on your machine's Git, as whoever
happens to be running it. The question the environment answers is coarser and
harder: **when an operator sends a message, does the whole daemon — poll,
turn, world landing, episode, reply — actually happen?** And the second half:
**can one organisation converse, give its steward work on the harness,
answer its question, and keep conversing after the harness deploys itself?**

No hermetic official Telegram sandbox exists. The official `telegram-bot-api`
server is a real Bot API, not a simulator — it needs real accounts, tokens
and network egress. So the environment stands on two fakes that already had
seams in the harness:

- a **fake Bot API** (`testenv/fake_botapi.py`) — the stdlib HTTP server the
  `botapi_base` config seam points the transport at. It implements exactly
  the surface `telegram.api` calls, long-polls `getUpdates` for real (an
  instant empty answer would turn the poll loop into a hot spin, which is
  exactly how a live instance once burned its CPU), and carries a control plane under
  `/__test/` for injecting operator messages, reading a journal of everything
  sent, and arming one-shot faults — a hanging poll, a failing send, a 429 —
  the failures a live Telegram cannot produce on demand.
- a **scripted model CLI** (`testenv/claude_stub.py`) — speaks the native
  Claude-CLI stream-json contract over stdio: `system/init` with a session
  identity, per-command `command_lifecycle`, a terminal `result` naming the
  command. It runs as the dropped `steward` identity with the turn's worktree
  as CWD, so plan `files` land in the candidate exactly as a model's edits
  would, and it persists its session transcript under
  `artefacts/claude/projects/` the way the real CLI does — the harness resumes
  by globbing that path, handing `--resume <transcript path>`, not a bare id.

The second listener (HTTPS :8443) is a real git remote served through
`git http-backend`: the split-identity boundary rejects local-path remotes by
doctrine, so the self-improvement scenario needs a non-local origin, and this
is one. Its certificate is self-signed; the harness container's trust of it
is declared once in `harness-init.sh`, alongside the `safe.directory` env the
agent's isolated Git needs.

## Running it

```sh
FAKE_PORT=8099 sh scripts/follow-through-acceptance.sh          # everything
FAKE_PORT=8099 sh scripts/follow-through-acceptance.sh A        # one scenario
```

`FAKE_PORT` selects a specific local port; otherwise the runner chooses a free port. `KEEP=1` leaves the
containers up for inspection. `ACCEPT_PROJECT` and `SELFTEST_PROJECT` override
the generated per-run Compose project names. Existing containers, volumes or
networks under either name cause a refusal before mutation. Cleanup removes
only projects created by this invocation. Scenarios:

| | asserts |
|---|---|
| **A** | a plain message follows through: reply sent, edit on the accepted world `main`, one episode narrating the exchange |
| **B** | a cancelled turn leaves no phantom episode and no edit: world acceptance exercised end-to-end |
| **C** | the poll loop survives a dead `getUpdates`, follows through after it, and the journal shows both the hang and live polls after |
| **D** | one organisation: conversation → task → retained `ask` checkpoint → operator inspection and answer → `idle` checkpoint → gate → publication → self-deployment → another conversation |

Scenario D runs the `selfimprove` compose project (mode `selftest`). One
steward owns a world and one repository: the harness itself. A root-owned snapshot of the current
working tree is committed inside the fixture and cloned into the HTTPS origin; the agent works at
`/home/steward/steward-harness`. The gate runs the repository's task tests
against the candidate's `src/`, not the bootstrap pip installation.

The story first waits for the initial revision to deploy. A conversation
writes a world file and episode, then admits one task. The scripted worker
saves a draft and asks for wording. The driver observes `Disposition: ask`
in Git, checks the draft stayed off main, uses `/task show`, and answers with
unique wording. That answer must land in the task's published file. The
operator must receive the `done` result.

Deployment uses the installed systemd target driver: the detached release
worker stages the published SHA, moves `current`, replaces the daemon, and
checks its real `/healthz`. The assertion compares the observed Git tip,
`readlink(current)`, `/proc/<pid>/cwd`, the daemon's reported SHA, and the
healthy-deployment ref. The PID must change. A final message must receive a
reply and produce an accepted world edit and episode through the replacement.
No test reads SQLite or calls an internal harness API.

**The service boundary uses real Linux systemd.** Docker packages the local
Linux test host; it is not a production execution backend. The shared
`testenv/systemd.Dockerfile` boots systemd as PID 1. The controller is the
MainPID of `acceptance-steward.service`, and actual transient services own
model invocations and deployment workers. `testenv/services.py` only selects
and execs the current release; it does not emulate systemd. Bootstrap source
is copied into a root-owned namespace, and self-deployment includes current
uncommitted implementation changes without changing host Git history.
The model and Bot API remain scripted. The fixture does not prove real
Telegram behavior or model judgement. Native Linux process-boundary acceptance
also runs without Docker through `scripts/linux-boundary-acceptance.sh`.

Episode-count assertions in A–C are relative to scenario start, so those
scenarios may run standalone or together.

## Inspect the whole story

```sh
FAKE_PORT=8099 KEEP=1 sh scripts/follow-through-acceptance.sh D
# Task identity, questions, and completion are ordinary Git history.
docker compose -f testenv/compose.yaml -p selfimprove exec harness \
  git -c safe.directory='*' -C /home/steward/steward-harness log --all --format=full
# Release and running process are OS facts.
docker compose -f testenv/compose.yaml -p selfimprove exec harness \
  readlink /opt/steward-current
docker compose -f testenv/compose.yaml -p selfimprove exec harness \
  ps -eo pid,ppid,user,args
# The external conversation, including task receipts.
curl -sS -X POST http://127.0.0.1:8099/__test/journal
```

## What it has caught

Every one of these passed the unit suite first.

- **A result key that remembered too much.** Results were keyed by task alone,
  so delivering a task's *question* suppressed its *completion* for the rest of
  the task's life. The unit suite was green; scenario D waited for a `done`
  receipt that never came. Results are now keyed by the outcome-changing
  accepted revision, so successive outcomes of one task stay distinct and a
  replay still delivers once.
- **A held offset multiplied one update into a thousand messages.** The poller
  re-queued every redelivery of an update still owed a reply, and each copy
  re-sent the reply. The fake Bot API reproduced it hermetically before the fix.
- **Trailers silently misparsed on older Git.** `for-each-ref`'s
  `contents:trailers:key=` filter is ignored by Git 2.39, so no task ever looked
  `idle` and publication stopped while everything looked alive. A development
  laptop on a newer Git was green; the pinned Debian image was not. Test under
  the Git your deployment actually runs.
- **Services inheriting the wrong environment.** Started via `docker exec`, the
  service fixture inherited the caller's environment instead of PID 1's. Health
  was green while every Git fetch failed for lack of the fixture's TLS trust.
  Green health is not a working service.
- **Protection the replacement refused.** Self-deployment staged a release with
  group-writable archive modes, and the replacement daemon correctly refused its
  insufficiently protected ownership launcher. The boundary check worked; the
  release was wrong.

That is the environment's whole value: an assertion of *did it happen* against
the whole daemon, under Linux, under the UID boundary, under the Git the
deployment actually runs.

## Handle it with care

The runner once shared fixed project names with a long-lived fixture, and a
cleanup removed that fixture's containers and volumes along with its state. It
now generates unique project names per run, refuses names that already have
resources, and only removes projects its own invocation created. If you
override `ACCEPT_PROJECT` or `SELFTEST_PROJECT`, pick names you would not mind
losing.
