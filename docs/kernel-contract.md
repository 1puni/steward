# Steward kernel contract

The harness turns authorized intent and observed Git work into accepted world updates,
published repository revisions and configured releases.

> The model proposes; the harness disposes.

This is the architecture contract: who owns which fact, who may do what, and what
must stay true when a process dies at the worst possible moment. The
[execution boundary](execution-boundary.md) owns the Linux identity and invocation
ownership details.

## Purpose

The machine has two acceptance paths:

```text
conversation → native world work → retained candidate → world acceptance
                                                         │          ▲
                                             accepted Git task      │
rhythm → procedure → accepted Git task                   │          │
                         │                     native task work     │
                         └───────────────────────────────┤          │
                                                         ▼          │
                              collapsed outcome → integrate → gates/reviews → push
                                                         │          │
                                      owned result → assessment ────┘
                                      observed refs → named target drivers
```

A world update may admit a task, steer an existing task, or do neither. A repository
task may produce code, findings or a question. Its checkpoint records what the session
concluded; an idle checkpoint becomes eligible for publication. Product files need not
change for the task account to be useful.

The controller is one daemon with a bounded background executor. Native providers own
their tools and their local reasoning loops. The harness owns exactly the authority
those loops cannot grant themselves, and nothing else.

## Deliberate limits

This is not a generic workflow engine, an event-sourcing framework, a multi-agent role
system, a federation bus or a semantic memory service. Those are all real things you can
go and build. None of them is what a steward needs in order to land a tested commit.

Until a concrete production requirement proves otherwise, the kernel has:

- one current database schema and no compatibility migrations;
- four tables, and no table for a fact Git already holds;
- one daemon lease and no second authorization protocol layered on top of it;
- one worktree per task, plus a temporary integration worktree per landing;
- one provider turn contract, shared by conversations, tasks, rhythms and reconciliation;
- one landing implementation and one executable target protocol;
- no persisted publication queue or second scheduling lifecycle;
- no provider capability that cannot honor its declared sandbox and workspace contract;
- no dynamically imported local-tool subsystem;
- no configuration field that does not select implemented behavior.

That last one matters most. A field the loader accepts and quietly ignores is a field
the operator believes is protecting them. Unknown fields fail validation for the same
reason.

Read this list as a constraint on responsibilities, not a boast about line count.
Moving code between modules, compressing syntax, or handing the same complexity to
another harness does not satisfy any of it. See the
[engineering doctrine](engineering-doctrine.md).

## Authority

| Owner | Authority |
| --- | --- |
| Controller | Trusted configuration, state, private Git custody, remote credentials, publication, release activation, service control and Telegram token |
| Execution environment | Native provider sessions, granted files, local Git, model-authored commands, gates, probes and product adapters |
| Operator | Repository and deployment grants, task confirmation where configured, steering and withdrawal |
| Incident policy | Failure confirmation and repair allowance for its configured pipeline |

All model-controlled commands cross the execution broker. The controller does not run
repository code under its privileged identity. Native credentials stay private to the
selected provider; push credentials and release mutation stay controller-side.
Filesystem scope and ownership of accepted work are distinct: a model may inspect
granted repositories, but only the owning acceptance path can publish their changes. See
[execution identities](execution-boundary.md).

Controller Git uses the configured remote URL and its private bare store. Candidate
objects cross by Git's own fetch and push, with the agent side run as the agent identity,
every object checked, and a candidate ref kept only after its ancestry check and
only for the publication pass that imported it.
Agent refs are working labels, not credential grants. A model-writable path never
authorizes a privileged arbitrary read or upload.

## Sources of truth

| Fact | Authoritative representation |
| --- | --- |
| Repository scope, gates and deployment policy | Controller-loaded YAML |
| Task request, origin, owner, priority and decisions | Private accepted task Git document |
| Task work and slice history | `tasks/<slug>` branch, commit messages and trailers |
| Live task writer | Per-task `flock` |
| Task notes, answers and consumption | Accepted task commits and explicit consumed input commit IDs |
| Provider selection, generation and native token | `conversations` table in controller SQLite, written in the same transaction as turns |
| Conversation execution, source delivery, retained output and accepted receipt | One `turns` row per turn |
| Prepared world candidate | Controller Git objects, named by its `turns` row |
| Accepted world knowledge | World Git history |
| Procedure definitions, rhythms and target requirements | Controller configuration |
| Concrete procedure runs and evidence | Accepted task Git refs |
| Published work | Trusted remote observation and ancestry |
| Release state | Release directories, receipts, current symlink, failed markers and deployed ref |
| Incident confirmation and allowance | Current `incidents` row per pipeline |

The current database has four tables: `steward_schema`, `conversations`,
`turns` and `incidents`. Tasks and task inputs are not SQL records. The private task
Git store is enumerated directly, and a missing native lineage permits a fresh run.

Decisions need durable representation when they cannot be derived; that does not
require SQL. Git can record a withheld grant, cancellation, failed gate, answer or
retry. Controller permissions and accepted refs establish authority independently
of authored work. The [rewrite contract](git-native-tasks.md) specifies discovery,
concurrency, private metadata, editable understanding and publication crash boundaries.

An incompatible database is preserved and startup refuses it. There is no automatic
migration or reset, because a harness that rewrites the operator's state on startup is a
harness that can destroy it on startup. An upgrade includes the adjacent files and Git
stores, not just SQLite. Follow the [upgrade procedure](upgrading.md).

## Provider neutrality

Every model operation enters `Cognition`: conversation, task, rhythm and world conflict
resolution. Its request names the owner, prompt, profile, workspace, sandbox, provider
order, optional session and optional invocation deadline. Adapters own provider protocol
differences; lifecycle code does not branch on provider names.

A provider switch keeps the owning work and changes its native lineage. A token is never
offered to another family. Selection may skip an unavailable or incapable provider
before execution; a failure after execution starts does not authorize repeating side
effects on another provider. Missing-session recovery uses that provider's defined
recovery boundary.

One conversation can own several tasks. Each task has an independent session; topic
model controls cannot silently select a task. Generation fences protect session
callbacks. See [native runtime](native-provider-runtime.md) for storage, live input and
protocol evidence.

## Task lifecycle

Admission creates the accepted task document. A slice holds the task lock, resumes its
native session, then commits its findings and a validated closure on the `tasks/<id>`
branch.

The controller fetches that commit's object graph and accepts the decision with the work
retained as a parent. These are separate crash boundaries: before acceptance, the local native
checkout must be preserved; after acceptance, another controller can recover the
work from accepted Git alone.

Status follows accepted decisions, retained work, live task locks and whether the
observed remote has landed the current work. `continue` or unconsumed input queues another slice; `ask`
waits for an answer; `idle` workspace-write work awaits publication. Read-only
procedure completion retains evidence. Protected blocked, proposed and cancelled
decisions live in the accepted task document. None of these facts has a parallel
SQL task status row. See [execution lifecycle](execution-lifecycle.md).

Unfinished work survives on the task branch and, where necessary, its checkout. Ordinary
task turns have no routine deadline; procedure runs keep one. A procedure deadline or a
controller shutdown with a bound native session can autosave and resume. This is neither
completion nor an overall time/spending limit. Malformed output retains work and blocks
publication.

A running parent can offer an immutable account body bound to the accepted revision it
builds on. The controller accepts it by exact-base compare-and-swap into the task
document only, and acknowledges the accepted revision over live input. Acceptance
changes no Definition field, consumes no input, touches no product files and grants no
publication; closure reconciles against the last accepted offer. See
[live task understanding](live-task-understanding.md).

Notes are pending context. A checkpoint records only inputs actually consumed;
unacknowledged and later inputs remain in accepted Git for a later slice. Answers
and retries reuse the task's work. There is no SQL task-input ledger.

### Task results

Delivery eligibility is derived from configured transport routes before dispatch.
An unavailable route retains its pending receipt and a diagnostic visible in
`/status`; restoring the route makes the same result eligible again. A target
transition without a task owner uses a deterministic operator report in the same
receipt store. It does not fabricate a task or replay cognition to deliver an
already retained reply. See [observation feedback](automatic-deployment.md#observation-feedback).

A reportable task outcome (`waiting`, `blocked`, `cancelled`, `done`) is offered to its
owning conversation with a source key derived from task ID, the latest
outcome-changing accepted commit and outcome kind. Retained Git supplies findings
and publication provenance. An idle workspace-write task awaiting publication is
not reported as done. Read-only results name evidence and reviewed-input commits,
without claiming product publication.

Telegram results return to the admitted owner's topic in the configured chat,
including topics absent from `telegram.topics`. That mapping names configured
destinations; it is not an allow-list for ordinary conversations or their results.

Assessment uses the ordinary world-turn path and current authority. A typed
`TASK_ACTION` can answer, retry or note an existing task; prose alone cannot resume it.
A turn proposes at most one action or one new task. A rhythm explicitly names its
configured result owner, or null for retained evidence only. Owned rhythm findings
use this same assessment path, including world knowledge and authorized follow-up
admission. An assessment cannot steer tasks owned by another conversation.

Accepted assessment and external delivery are separate facts. A private task-result
receipt retains the selected outcome before assessment and remains pending until
transport succeeds or accepted assessment chooses silence for a completed scheduled
read-only review. Such reviews send only their owner's material update; completion
without a final message records an empty reply and performs no transport call. Full evidence remains in the
task ref and receipt. Explicitly requested runs, questions, blocked/cancelled work,
and assessment failures retain their outcome reports. Transport retries replay the
saved reply, including silence, without repeating assessment. Checkpoints are local
Git/log evidence; they have no separate broadcast channel. Accepted world work is replayed without another model turn;
failed assessment still delivers the retained task findings with its interruption.
Telegram reuses per-piece receipts across retry and restart. Preserve both adjacent
receipt directories during upgrades. A crash between the transport accepting a send
and the local receipt write can still duplicate that piece: this is at-least-once
delivery with receipts, not exactly-once.

## Publication

The repository owner holds one lock through deterministic integration, gates and
push. Each external target has a separate convergence lock.

Accepted Git task documents own the work. An idle workspace-write task owes a
publication until the observed default branch holds a commit naming its current work
(`Steward-Work`), or the work itself. Preparation collapses the task's outcome before integration, rebases onto
the observed base, and fixes one single-parent candidate before every gate and
required review. Gates must leave that candidate unchanged. Read-only procedures
retain evidence without becoming product publication candidates.

A conflict or actionable red gate returns its exact inputs and diagnostics to the
owning task's native cognition. The publisher does not reason about edits. Missing
intent asks and waits; an unchanged product tree remaining red on the
same base stops automatic repair until an explicit decision.

The candidate names the work it lands in a `Steward-Work` trailer. The transport
checks that the candidate descends from the observed base, then uses an exact-base
remote lease. A moved or rewound remote
cannot accept a candidate prepared for a different base. Observation after push
settles an ambiguous response. Native exploration refs and history remain intact;
a fresh controller recovers status from the landed trailers in observed Git.

Unaccepted native branches grant no publication authority. Repository convergence
continues while admission is paused. Named targets independently follow configured
refs and require external exact-revision readiness. See
[automatic deployment](automatic-deployment.md) and the
[rewrite journeys](../tests/test_rewrite_convergence.py).

## Writable turn acceptance

Provider completion is not acceptance. The owner's checkout commits the candidate, with
`Steward-Turn` trailers, before the prepared world receipt is recorded. Under the world
lease the controller recognizes the turn in the world's history or fast-forwards to its
final revision, and completes the source and proposed task effect in SQL. Success can be rendered
only after acceptance; replay reads that receipt.

Cognition happens outside the world lease in an owning checkout. World conflict
resolution uses the configured Git reconciler outside the lease, then rechecks the world
before application. A conflict retains its candidate and defers acceptance. An unrelated
operator edit is never grounds for a destructive reset.

Startup recovers prepared updates before admitting new world writers. An unprepared
interrupted turn retains unfinished evidence; a saved reply alone cannot recover missing
files. See [world durability](world-turn-durability.md).

## Concurrent dispatch

`controller.workers` is one shared budget — default **8**, range **1–32** — and it is
the whole scheduling policy. Tasks, repository convergence, probes, desk messages,
rhythms and task-result assessment all compete for the same slots. The executor's own
queue is the assignment: first asked, first served. There is no per-lane reservation and
no fairness ordering, because both are a scheduler, and this does not have one. A
scheduler wants a fairness policy, a starvation story and a tuning knob; nothing here
has yet needed any of the three.

In-flight keys are not the exclusion. They exist only so one pass does not enqueue a
second copy of a job already queued, which would grow the backlog by an entry per poll;
with them the backlog holds at most one entry per owner, so nobody waits behind a
duplicate of themselves. The task lock and the repository lease are the exclusion, and
unlike an in-memory key they are durable, so they hold across the crash that would
strand a claim.

Repositories run independently. Each task has its own lock. One rhythm dispatch owner
resumes incomplete scheduled work before choosing another due definition. Desk messages
have message dispatch keys and retain their inbox/conversation ownership. Result
assessment is keyed by the owning conversation. No separate repository-observation pool
exists.

There is no recovery pass either. A task interrupted by a crash never left the queue and
its lock died with its worker, so the next pass yields it like any other and the runner
resumes the turn it finds open.

Pause is a filter on that derivation, not a branch around it. It stops the steward
taking on work: new task slices, probes, desk intake and rhythms. Already submitted jobs
finish. Repository convergence and result assessment stay active, because a repository
that owes a publication does not stop owing it. A crash-interrupted task stays queued,
so pause also delays its next slice. Pause is not a quiescence barrier; do not treat it
as one before an upgrade.

Telegram polling and conversation execution run outside this background budget. The
operator talking to their steward is answered on the ingress thread that received them,
so the budget can be full and they still get a reply. Authenticated control commands can
be handled while cognition is active; commands that run external work use the ordinary
executor path. One native conversation can receive supported live inputs without
acquiring a second candidate writer. Unsupported inputs wait. Spare background capacity
and a responsive conversation are separate claims, and neither establishes the other.

Configured desk intake accepts ordinary messages and retained watch observations.
Watch observations enter as `harness:desk-watch`, not operator grants, through the
same native conversation and repository authorization. They may propose work or
note an owned task; the controller rejects answers and retries from that source.
Their immutable inbox source and consumption marker prevent repeated samples from
repeating cognition. Accepted task refs and ordinary result receipts, not inbox
consumption, establish repair progress. The instance that produces
the samples owns their source identity and activation procedure.

Unexpected worker exceptions propagate to the daemon; shutdown cancels queued owners
and drains already-running writers while retaining the daemon lease. Queued work
remains derivable after restart. External service supervision owns restart. See [failure
boundaries](failure-boundaries.md) for the distinction between an operational failure
and a harness defect.

## Crash rules

| Boundary | Required interpretation |
| --- | --- |
| Native input acknowledged | Delivery evidence, not proof of consumed input or accepted work |
| Provider exits without prepared world work | Retain actual unfinished evidence; do not fabricate success |
| Prepared world candidate | Recover acceptance without repeating cognition or task admission |
| Task checkpoint | Git retains the work and disposition; SQL retains only its remaining decisions |
| Possible push | Observe remote truth; an already-landed effect cannot be cancelled retroactively |
| Release activation | The symlink alone does not prove a healthy running release |
| Failed deployment | Preserve the failed marker and prior-release evidence; report rollback failure honestly |
| Shutdown | Stop admission and drain owned work before releasing the daemon lease |

Stopping native cognition and withdrawing task authority are different actions, and the
difference is the whole design. Stopping a turn requests native interruption,
then invokes bounded containment. Its live cancellation route is ephemeral;
durable work and session identity are retained separately.
Withdrawing a *task* is durable, and it is checked by the only writer that can make the
work real: the push. Work already on the remote is reported as landed rather than
relabelled cancelled, because landed work cannot be un-landed.

Two honest exceptions. Gate interruption is not currently wired to task cancellation, so
a cancelled task can wait for a gate to finish before the publisher observes the
withdrawal. Forced host death and escaped subprocesses still need evidence at their real
execution boundary. Do not restore a cancellation subsystem for symmetry alone; decide
whether bounded waiting is acceptable, and keep the pre-push withdrawal check either way.

## Verification

Local regression, Linux identity tests, native-provider probes and deployed
acceptance establish different things. None substitutes for the others, and an old
green suite does not validate a changed checkout. A green macOS run says nothing
whatsoever about the UID boundary; only `scripts/linux-boundary-acceptance.sh` does.
Test totals are not evidence. A suite has to state what it establishes.

Required outcomes include isolated native work, retained interruption evidence, world
acceptance before dependent admission, clean exact-revision gates, remote-safe
publication, truthful withdrawal, artifact verification, rollback to a prior release or
absence, and an observable result at the owning transport. The open edges are listed
honestly in the [README](../README.md#where-it-actually-is).

### Validation map

Each link names an executable check, not a claim that any particular revision or
deployed instance has passed it. Implementation paths are relative to
`src/steward_harness/`.

| Behavior | Implementation | Validation |
| --- | --- | --- |
| Admission creates the accepted task ref and document | `task_store.py` | [admission](../tests/test_task_admission.py), [task files](../tests/test_task_charter.py) |
| Status derives from accepted decisions, trailers, ancestry and locks | `task_query.py`, `task_lock.py` | [task status](../tests/test_task_status.py), [operator transitions](../tests/test_state_control.py) |
| Publication holds the repository lock through integration, gates and exact-base push | `repository_reconciler.py`, `landing/` | [reconciliation](../tests/test_reconcile.py), [landing](../tests/test_landing.py), [rewrite journeys](../tests/test_rewrite_convergence.py) |
| Deployment converges on an exact revision, verifies health and rolls back | `deploy/` | [automatic deployment](../tests/test_automatic_deployment.py), [rollback truth](../tests/test_rollback_truth.py) |
| World acceptance applies the retained candidate under the world lease | `world/turn_checkpoint.py`, `state.py` | [world durability](../tests/test_world_durability.py), [checkpoint acceptance](../tests/test_world_turn_checkpoint.py) |
| Native sessions use private homes and candidate-owned records | `runtime/native_workspace.py`, `runtime/providers/` | [workspace](../tests/test_native_workspace.py), [Claude transport](../tests/test_claude_stream.py), [Codex transport](../tests/test_codex_app_server.py) |
| Provider errors separate "declined" from "failed" | `runtime/providers/` | [provider errors](../tests/test_provider_errors.py) |
| Task results survive restart and are not re-assessed | `conversations.py`, `receipts.py`, `telegram/` | [result delivery](../tests/test_task_result_delivery.py), [Telegram](../tests/test_telegram.py) |
| Live conversations stay responsive with a full worker budget | `daemon.py` | [responsiveness](../tests/test_scheduler_responsiveness.py) |
| The broker enforces a separate service identity | `runtime/execution.py` | [Linux boundary acceptance](../tests/test_boundary_acceptance.py) |
| An invocation cannot leave escaped writers or kill a peer | `runtime/ownership.py`, `runtime/process.py` | [host ownership](../tests/test_host_ownership.py), [process input and stop](../tests/test_process_input.py) |
| Configuration rejects unknown fields and colliding resources | `config/` | [configuration](../tests/test_config.py) |

The [follow-through environment](follow-through-environment.md) runs the whole
message-to-result journey against a fake Bot API and a scripted provider. The
[native probes](../experiments/native_sessions/README.md) drive real installed
providers. Scripted cognition cannot establish provider behaviour, and a successful
isolated probe cannot establish the health of a deployed steward.
