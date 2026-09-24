# The follow-through environment

A green `uv run pytest` says nothing about Telegram follow-through: the suite
mocks the transport inside the process, on the desk's own Git, as the user
already running it. The question the environment answers is coarser and
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
  instant empty answer would turn the poll loop into the hot-spin shape that
  burned CPU on the live gg instance), and carries a control plane under
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
| **B** | a cancelled turn leaves no phantom episode and no edit — the accepted-world doctrine (main's 126bfc2) exercised end-to-end |
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

## September 12 verification: one real failure

Full A–D run against `8b782ec`: **31 passed, 1 failed**. The failure is
`operator received the completed task result`. The waiting receipt arrives;
the answer lands and deploys; the replacement daemon handles the final
conversation. The final done receipt never arrives. The assertion remained red
at that checkpoint; the warm continuation below exercises its correction.

The cause is visible in `StateDatabase.pending_task_result_conversations()`
and `pending_task_result_for()`: both exclude a task once any turn has source
key `task_result:<task_id>`. `ConversationService.deliver_task_result()` uses
that same key for the waiting result and every later result. Delivering the
question therefore suppresses completion for the lifetime of the task. The
correction must distinguish successive results of the same task while retaining
replay safety; the original environment change did not alter that production
behaviour.

### Warm continuation: receipt correction

Harness correction: `0e93f3b` (`Deliver successive task results to their owning
conversation`). The isolated acceptance revisions below are based on the
original scenario's Git history, so they have different commit identities.

Reused `orgself` on port 18099 without resetting its state, Git history or
journal. The controller now selects the existing task row revision as part of
the result's source key and carries that selected key into the owning turn.
An old task-only receipt suppresses its historical outcome, not a later row
revision. Two questions and repeated identical failures after retry therefore
remain distinct; changing priority does not stamp a new outcome. An idle slice
whose branch still owes publication is not delivered as a finished result.

The isolated fake origin advanced from `5d4287e6fa75650d691c5d0865efb7010fc66f20`
to the receipt correction `9782d289eadc678eed386669ffe11c4914955381`. Existing
deployment replaced the daemon and health reported that exact revision. The
previously missing done receipt then arrived for the original task, naming its
original landed SHA `5d4287e6fa75650d691c5d0865efb7010fc66f20`.

A direct service restart exposed a fixture defect: PID 1's `os.wait()` raised
`ChildProcessError` during the childless gap before the replacement was adopted.
`services.py` now tolerates that gap. Restarted the same container with its
data intact, then repeated the direct service restart successfully. A new
`ORG-AFTER-DEPLOY RECEIPT-FIX-RESTART` conversation received its reply; the journal
still contained exactly one done receipt. No real Telegram message or external
repository push was involved.

That direct restart also revealed that services inherited the `docker exec`
caller's environment rather than PID 1's declared environment. HTTP health was
green while Git fetches failed because the fixture's HTTPS trust configuration
was absent. The service fixture now reads its manager's environment for both
daemon and detached-worker starts. After that correction, repository observation
and deployment converged to `a0b33e197d8e69bbb054ba908cd4ba13c7559cff`, including
the priority regression fix. Health reported that exact revision and the done
receipt count remained one. The retained fixture used port 18099 at the time of that observation.

Focused receipt/conversation/control tests passed in the warm Linux container
(24 tests). The local related state, conversation, world, task and deployment
checks passed (66 tests). These are targeted continuation results, not a claim
that a fresh full A–D run was repeated. The original 31/1 run remains evidence
of the failure that led to this fix. That fixture run did not deploy any production instance.

## What it has already caught

- **A held offset multiplied one update into a thousand messages** — main's
  poller re-queued every redelivery of an update still owed a reply, and each
  copy re-sent the reply. A downstream instance had found this (2bf4183); the
  environment reproduced it hermetically before the fix was ported, which is
  what earned the port.
- **A task's trailers misparsed under Git 2.39** — `for-each-ref`'s
  `contents:trailers:key=` filter is silently ignored by Git at least as new
  as 2.39, so no task could be seen as `idle` and publication stopped while
  the projection looked alive. The desk runs Git 2.52 (green suite); the
  pinned image — and any Debian-bookworm host like gg's — runs 2.39. Found
  here first, not on the desk.

The environment's value is exactly this shape: an assertion of *did happen*
against the whole daemon, under Linux, under the UID boundary, under the Git
the deployment actually runs.


## September 23, 2026 fixture cleanup incident

An acceptance attempt used the runner's old default project-name cleanup and
removed the pre-existing `selfimprove` containers and its state, journal and
Git-remote volumes. The retained test state was not recovered. This was an
unintended destructive action, not a successful acceptance result. The runner
now generates unique project names, refuses names with existing resources,
and limits cleanup to projects created by its own invocation. A stubbed
Docker check verified that an existing project is refused before any create
or remove command. Historical verification above remains historical evidence.

The subsequent uniquely named real-systemd run passed all 12 assertions in
A–C: accepted conversational edits, cancellation without accepted edits or
phantom episodes, and recovery from a dead poll. D reached actual initial
self-deployment and exposed group-writable Git archive modes: the replacement
correctly refused its insufficiently protected ownership launcher. That run
was stopped and its own fixture resources cleaned; it is not a passing D result.

After correcting archive permissions and updating the scripted CLI to read
the task document explicitly named by current prompts, the final D rerun
passed **22 assertions, 0 failed**. It used the final controller-pidfd guardian
implementation and a fresh current-source snapshot. The proof includes initial
unowned target delivery, question/answer retention, gated publication,
exact-revision health, daemon PID replacement, and a conversation accepted by
the replacement. Alongside A–C's 12 assertions, the distinct scenario coverage
is **34 passing assertions**; A–C ran before the final guardian refinement,
while D ran after it. The uniquely named final fixture cleaned up successfully.
Logs for this session are `/private/tmp/steward-followthrough-heroic.log`
(A–C and first D diagnosis) and `/private/tmp/steward-followthrough-D3.log`
(final D). These are local run artifacts, not repository fixtures.
