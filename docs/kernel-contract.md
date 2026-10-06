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
                                      owned result → delivery + assessment ────┘
                                      observed refs → named target drivers
```

A world update may admit a task, steer an existing task, or do neither. A repository
task may produce code, findings or a question. Its checkpoint records what the session
concluded; an idle checkpoint becomes eligible for publication. Product files need not
change for the task account to be useful.

The controller is one daemon with a bounded background executor. Native providers own
their tools and their local reasoning loops. The harness owns exactly the authority
those loops cannot grant themselves, and nothing else.

Mutable work has one writer, and only verified, immutable work crosses an authority
boundary: a native session writes its checkout under the task lock; the controller
publishes one fixed candidate SHA that no gate may change. The representation should
make a second writer or a mutable publication input impossible, not merely detected.

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
- no second executor: an external watch alerts while the controller is down, and
  repairs arrive as ordinary admitted tasks;
- no configuration field that does not select implemented behavior.

That last one matters most. A field the loader accepts and quietly ignores is a field
the operator believes is protecting them. Unknown fields fail validation for the same
reason.

Read this list as a constraint on responsibilities, not a boast about line count.
Moving code between modules, compressing syntax, or handing the same complexity to
another harness does not satisfy any of it. See the
[engineering doctrine](engineering-doctrine.md).

## Configuration

YAML is trusted policy. Each block owns one kind of decision:

| Block | Owns |
| --- | --- |
| `identity`, `provider` | Steward identity, provider order, model profiles, private native homes, workdir and state database |
| `execution` | Untrusted execution identity and environment boundary |
| `controller` | Polling, health listener and one background-worker budget |
| `tasks` | Optional private remote for accepted `tasks/*` refs |
| `repositories` | Trusted remotes, default branches, gates and publication requirements; configuring one is the authority to work in it |
| `pipelines`, `incident_policy` | Probes, failure confirmation and repair allowance |
| `world` | The Git world for durable knowledge; optional named reconciliation procedure |
| `procedures` | Accepted instructions, model and access settings |
| `rhythms` | Non-overlapping interval triggers for procedures; `input: world` keeps one obligation per captured interval |
| `targets` | Desired refs, installed drivers and required evidence |
| `telegram`, `desk` | Optional transports: each feeds the shared inbox and receives its own replies and results |

`controller.poll_seconds` bounds only background rechecks — admission, convergence and
probes. Conversation ingress and running task turns do not wait on it, so raising it
trades background latency for controller CPU and nothing else.

Each selected built-in provider needs an explicit private `native_homes` entry; see
[native runtime setup](native-provider-runtime.md). The
[example configuration](../config/steward.example.yaml) is a provisioning template:
its accounts, directories, provider logins and remote URLs must already exist on the
target host.

## Authority

| Owner | Authority |
| --- | --- |
| Controller | Trusted configuration, state, private Git custody, remote credentials, publication, release activation, service control and Telegram token |
| Execution environment | Native provider sessions, granted files, local Git, model-authored commands, gates, probes and product adapters |
| Operator | Repository and deployment grants, task confirmation where configured, steering and withdrawal |
| Incident policy | Failure confirmation and repair allowance for its configured pipeline |

All model-controlled commands cross the execution broker. The controller does not run
repository code under its privileged identity. Native credentials stay private to the
selected provider; the landing credential and release mutation stay controller-side.
Filesystem scope and ownership of accepted work are distinct: a model may inspect
granted repositories, but only the owning acceptance path can publish their changes. See
[execution identities](execution-boundary.md).

Effective permission is the configured grant intersected with what the external system
actually grants; neither widens the other. Repository content is evidence of how an
organisation works, never permission. Learning that someone leads a repository is a
durable fact for the world; giving them controller capabilities or registering a
remote is a separate, explicit grant. Delegate only effects the delegate can perform:
a task returns findings and proposals, and the owning conversation or the controller
applies anything controller-only.

Organisation-specific commands belong in `telegram.adapter_commands` as one declared
argv and argument shape. They run without a shell through the same untrusted broker. A
model turn can reach anything that broker runs, so an act only the operator may cause —
minting a phone pairing link, say — declares `authority: controller` instead: the
controller runs it as itself, with an empty environment, from an executable the
boundary audit proves the agent cannot replace.

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

Conversation task `list` and `show` calls validate source and execution authority
against a read-only SQLite snapshot while holding the task Git lease. Their Git
reads do not reserve SQLite's writer, so independent provider sessions can bind
while those observations run. Mutating task calls retain their writer transaction
and accepted-receipt replay checks.

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

Only the owning conversation, a rhythm or incident policy admits harness tasks.
An opted-in world rhythm (`drive_tasks: true`) admits tasks to its configured
result owner and may answer, retry or note that owner's existing work. It cannot
steer unowned/other-owner work, cancel tasks, or resume cancelled work. Admission
retains the rhythm source and idempotency key; results return to the owner's normal
transport and assessment loop. Default rhythms remain observational. A task
session may use its provider's native subagents freely, but it cannot admit further
tasks; native work becomes a harness task only when it needs its own scope, admission
or deliverable. A native subtask ID is correlation, not a task ID.

Admission creates the accepted task document. A slice holds the task lock, resumes its
native session, then derives completion from successful native execution after writer
teardown and commits findings and disposition on the `tasks/<id>` branch. No model
close call is required; explicit waits and continuation use the existing optional
decision primitive until their native mappings exist.

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

Accepting understanding, ending native execution and authorizing publication are three
boundaries, and none implies another. A clock or a poll may observe work but never
creates a semantic stopping point. Silence means the last accepted account is old, not
that the task failed.

Notes are pending context. A checkpoint records only inputs actually consumed;
unacknowledged and later inputs remain in accepted Git for a later slice. Answers
and retries reuse the task's work. There is no SQL task-input ledger.

### Task results

A finished task is evidence for its owner to reassess against the broader outcome, not
closure of that outcome. The owner may close on sufficient evidence without routine
operator sign-off; it involves the operator when ambiguity would materially change the
work, or when the operator's judgment is part of what completion means.

Delivery eligibility is derived from configured transport routes before dispatch.
An unavailable route retains its pending receipt and a diagnostic visible in
`/status`; restoring the route makes the same result eligible again. A target
outcome (live, or persistently failing; never progress) without a task owner uses
a deterministic operator report in the same
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

Delivery and assessment have separate decisions and completion receipts. Delivery
freezes the selected outcome and existing owner/source in the private result receipt,
sends it, then marks it delivered. A busy or cleared owning conversation cannot
block that send. A receipt already selected for transport remains owed even if the
task advances; it describes the retained outcome. Before later assessment the
controller rechecks current task outcome and owner, skipping obsolete task questions
or reassigned work. Assessment uses the ordinary world-turn path and current
authority. The [live task tool](git-native-tasks.md#live-conversation-task-calls)
can answer, retry, note or cancel authorized work; prose cannot resume it.

Automatic runs remain quiet unless they explicitly request notification; null owners
retain evidence only. A requested message is delivered as retained. Assessment cannot
replace or silence it, and assessment final prose does not send a second notification.
New operator-facing judgment requires a separate explicit notification decision
through the same result receipt transport. Full evidence remains in Git and the receipt.

**Latency expectation:** with a running controller, healthy local storage and an
available route, a newly reportable outcome is queued on the next controller pass
(default 5 seconds). One dedicated transport worker drains these jobs independently
of all cognition slots and conversation execution. With no transport backlog the
send starts within that polling interval plus discovery overhead. With a backlog,
add the service time of jobs ahead; each owner has at most one queued attempt, and
failed attempts yield before retrying on a later pass. This is a capacity guarantee,
not a remote-service availability SLA. Telegram result attempts send at most 12,000
characters of text, use the API's 30-second I/O timeout per piece, and never sleep
through rate-limit waits or execute attachment/action markers embedded in evidence.
A failing route remains pending with diagnostics; unavailable transport cannot have
a promised successful-delivery deadline. Host stalls and actual transport latency
require deployed measurement.

Transport retries replay saved replies, including historical silence, without
repeating cognition. Existing accepted assessment alone is not proof of delivery.
Optional assessment follows delivery through the shared worker budget. Its accepted
world receipt survives restart; provider failure is recorded as an assessment error
and cannot retract the delivered evidence. Checkpoints have no separate broadcast
channel. Telegram reuses per-piece receipts across retry and restart. Preserve both
adjacent receipt directories during upgrades. A crash between remote acceptance and
the local receipt write can duplicate that piece: delivery is at-least-once,
not exactly-once. A reply Telegram refuses on its content (a deleted topic,
unparseable markup) is never retried: an inbound message is parked, a result
receipt is settled with `rejected`, and `/status` names refused results for a day.
Nothing queued behind it waits.

Notification calls use the existing private result receipts. Stable owner/source
and key identify one intent; exact replay returns its receipt and changed-payload
replay is rejected. Accepted queuing survives provider failure and world
acceptance failure. Zero calls send nothing; several calls remain distinct.
A provider without a working tool channel records evidence only. Neither final
reply parsing nor assessment is a fallback send mechanism.


### Inbound messages

Every inbound message is a file in an inbox, and one drain answers them all. The
suffix is ownership: `.json` queued, `.json.claimed` being answered, `.json.failed`
and `.rejected` parked for the operator. A restart returns claims to the queue. The
drain answers messages in name order within a conversation and different
conversations concurrently; a deferral (a busy owner, a storage refusal, pending or
conflicting world work, a transient transport error) returns the message to the front
of its conversation and retries it a second later. A message is removed only after
its reply is delivered. Each source replies through its own transport.

The directory names who is speaking, never the record. The desk inbox
(`desk.inbox_dir`) accepts `{"kind": "message", "id", "text", "topic", "profile"?,
"context"?}` from external clients, whose permission to write it is their boundary;
replies are `reply` lines in `desk.events_file` carrying the message `id`. The Telegram
ingress admits a sender first and then writes the same record shape, with its chat,
sender, message and image metadata, into a private inbox under
`<state_db>.telegram-inbox/<chat>/`. A desk client therefore cannot speak as a
Telegram operator.

### Telegram ingress

Each admitted update is written into the Telegram inbox, claimed by the ingress,
before the persisted offset (`offset` in that directory) moves past it; only then
is it routed. The offset is the acknowledgement: Telegram redelivers nothing below
it, an update below it is never retained again, and a redelivery in the window
between the two writes is recognized by name. Delivery is at-least-once — an
uncertain send can duplicate. Never run two pollers for one bot token; when moving a
bot, copy the inbox and its offset only after the old ingress has stopped. A start with
no persisted offset first adopts the previous release's per-update receipts (see
[upgrading](upgrading.md#one-inbox-for-telegram-and-the-desk)); if that fails, Telegram
ingress stays off rather than polling from zero.

Inputs are deduplicated by source identity, never by content: the same words under a
different update ID are a new input and get a new reply. A saved reply is resent,
never recomputed, and each confirmed piece is recorded in the message file so a
retry or restart sends only what is missing.

Control commands are answered on the polling thread so they overtake a busy topic.
Text for a topic whose native execution is running becomes live input to it, unless
queued messages in that topic would be overtaken; a command never does. Everything
else waits for the drain. Admission is asked again when a message is answered,
because a retained update is not a grant; an unreadable administrator list is a
deferral, not a refusal.

In a forum, Telegram sends no thread ID for General, so topic `0` is a valid route in
both directions. Ingress logs whether the thread field was present, its value and the
selected route, and never infers a destination from message content. `passive_topics`
drops declared feed topics at the trust boundary, before commands and replay;
undeclared topics are still admitted. Group-administrator admission is read at most
once a minute and fails closed without caching the failure. Unknown and retired
commands fail honestly; they never become model prompts, and the menu advertises only
implemented verbs.

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
final revision, and completes the source in SQL. World success can be rendered
only after that acceptance; replay reads its receipt. Live task calls use their own
[Git acceptance boundary](git-native-tasks.md#live-conversation-task-calls), so admitted
tasks survive a later failure of this world boundary.

Cognition happens outside the world lease in an owning checkout. World conflict
resolution uses the configured Git reconciler outside the lease, then rechecks the world
before application. A conflict retains its candidate and defers acceptance. An unrelated
operator edit is never grounds for a destructive reset.

Startup recovers prepared updates before admitting new world writers. An unprepared
interrupted turn retains unfinished evidence; a saved reply alone cannot recover missing
files. See [world durability](world-turn-durability.md).

## Concurrent dispatch

`controller.workers` is one shared budget — default **8**, range **1–32** — and it is
the budget for background cognition and work. Tasks, repository convergence, probes,
rhythms and task-result assessment all compete for the same slots. The executor's own
queue is the assignment: first asked, first served, without per-lane reservation or
fairness ordering among cognition jobs. Retained result transport has one separate worker as
described in [task results](#task-results); it cannot run cognition.

In-flight keys are not the exclusion. They exist only so one pass does not enqueue a
second copy of a job already queued, which would grow the backlog by an entry per poll;
with them the backlog holds at most one entry per owner, so nobody waits behind a
duplicate of themselves. The task lock and the repository lease are the exclusion, and
unlike an in-memory key they are durable, so they hold across the crash that would
strand a claim.

Each exclusion answers a different question: the daemon lease says which controller is
alive, the task lock who runs a task, the repository lease who publishes, the world
lease who applies a world update.

Repositories run independently. Each task has its own lock. One rhythm dispatch owner
resumes incomplete scheduled work before choosing another due definition. Result
assessment is keyed by the owning conversation. No separate repository-observation pool
exists.

A days-long task turn holds one slot for all of those days; the remedy is operator
policy, not reservation or preemption. A controller restart interrupts every running
task turn. Continuity comes from the accepted account, retained work and the native
session, not from any process surviving.

There is no recovery pass either. A task interrupted by a crash never left the queue and
its lock died with its worker, so the next pass yields it like any other and the runner
resumes the turn it finds open.

Pause is a filter on that derivation, not a branch around it. It stops the steward
taking on work: new task slices, probes and rhythms. Already submitted jobs
finish. Repository convergence and result assessment stay active, because a repository
that owes a publication does not stop owing it. A crash-interrupted task stays queued,
so pause also delays its next slice. Pause is not a quiescence barrier; do not treat it
as one before an upgrade.

Telegram polling and the inbox drain run outside this background budget, and pause
does not apply to them. The operator talking to their steward is answered by the drain,
so the budget can be full and they still get a reply. Authenticated control commands can
be handled while cognition is active; commands that run external work wait in the
inbox like any message. One native conversation can receive supported live inputs without
acquiring a second candidate writer. Unsupported inputs wait. Spare background capacity
and a responsive conversation are separate claims, and neither establishes the other.

Desk messages run through the same native conversation and repository authorization
as any operator message. Keep both inboxes, with their `.claimed` and `.failed` files
and the Telegram offset, across upgrades, and inspect a parked `.failed` message's
native evidence before retrying it.

The harness reports its own stalls without a model: a rhythm becoming held and a
storage-reserve refusal go straight to the operator topic, once per event and
quiet period. A watcher that hands findings to cognition fails exactly when
cognition does.

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
acceptance and independent durable task admission, clean exact-revision gates, remote-safe
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
| Live conversations stay responsive with a full worker budget | `daemon.py`, `inbox.py` | [responsiveness](../tests/test_scheduler_responsiveness.py), [inbox](../tests/test_inbox.py) |
| The broker enforces a separate service identity | `runtime/execution.py` | [Linux boundary acceptance](../tests/test_boundary_acceptance.py) |
| An invocation cannot leave escaped writers or kill a peer | `runtime/ownership.py`, `runtime/process.py` | [host ownership](../tests/test_host_ownership.py), [process input and stop](../tests/test_process_input.py) |
| Configuration rejects unknown fields and colliding resources | `config/` | [configuration](../tests/test_config.py) |

The [follow-through environment](follow-through-environment.md) runs the whole
message-to-result journey against a fake Bot API and a scripted provider. The
[native probes](../experiments/native_sessions/README.md) drive real installed
providers. Scripted cognition cannot establish provider behaviour, and a successful
isolated probe cannot establish the health of a deployed steward.

### Telegram cosmetics and delivery

Every run has one voice to its owner. An operator conversation's final reply is
delivered, and an ordinary task's result is its report, so neither is offered
`notify`. Automatic result assessment, world rhythms and rhythm tasks record their
finals instead, and speak only through explicit notifications.

The native task tool supports `operation: telegram` with string fields `action`,
`key`, and `text`. `info` requires empty text and returns only the configured
group's title, description, photo identity, and the bot's cosmetic rights.
`set_description` takes at most 255 characters; `set_photo` takes an absolute
PNG/JPEG path below `telegram.delivery_roots`, bounded to 5 MB. Every path
component is opened without following symlinks. Enable each mutation explicitly
in `telegram.agent_actions`; the bot also needs Telegram's Change Group Info
right. The capability cannot select another chat, read invite links, change
membership, or grant permissions. Procedure tasks and world rhythms cannot
invoke it. Use it only for operator-authorized cosmetics.

The controller retains an intent before mutation and a verified read-back after
mutation. Identical completed requests replay their receipt. An ambiguous
mutation is retained for reconciliation, never blindly repeated. Stable keys
are scoped to the conversation or task; changing content under a key is refused.
