# Durable understanding during native work

A native task may work for days, with its own subagents and tools. Its
understanding should become durable *while* it works, not only when it stops.
Nobody should have to kill a perfectly healthy agent just to find out what it
has figured out.

So three things stay separate: accepting what the agent understands, ending its
native execution, and authorising publication. The controller does not invent
a stopping point to get the first one, and getting the first one grants neither
of the others.

A clock or a poll may observe work; it never creates a semantic stopping point.
Silence means the last accepted account is old, not that the task failed. And a
copied brief, a running shell or a fresh read of an old status file is not evidence
that work is progressing. The accepted account is.

## One offer, one acceptance path

The prompt carries the canonical accepted account from task Git. When the
running native parent has something worth keeping, it consolidates a revised
account and **offers** it:

```text
blob = git hash-object -w --stdin   # "Steward-Base: <accepted revision>\n\n<account body>"
git update-ref refs/steward/understanding/<task-id> <blob>
```

- **Representation.** The offer is an immutable Git blob in the task's own
  repository, holding exactly two things: the accepted revision it builds on, and
  the Markdown body. Its object ID is its identity. Frontmatter is rejected, so
  task authority cannot be offered. The ref is only routing: re-pointing it makes
  a new offer, and it confers nothing.
- **Observation.** Each running non-procedure turn has one offer watcher, a thread
  the slice starts and joins itself, so it lives exactly as long as the turn. It
  stays idle until the turn's live-input channel exists. Every `controller.poll_seconds` it
  resolves the ref through the untrusted execution broker. On a new value it
  reads the object with one bounded read (at most 256 KiB), proves the bytes hash
  to the object ID it was given (Git does not re-hash on read, and the object
  database is agent-writable), and parses the header and a nonblank body. Nothing
  in an offer selects a path, helper or command; the ref name comes from the task
  ID the controller already owns. The watcher runs off the daemon loop and each
  broker call has a 30-second bound, so a slow or hostile repository delays only
  its own task's offers. Because closure joins the watcher, a hostile ref (a FIFO,
  say) can delay that task's closure by about a minute: two bounded broker calls
  plus a store-lease wait.
- **Acceptance.** Under the existing store lease, with the same compare-and-swap
  as every task decision: an offer already accepted on the decision line replays
  its original revision and writes nothing; a task that is `proposed`, `blocked`
  or `cancelled` refuses; a base that is not the current accepted tip refuses;
  otherwise the body is committed with the task Definition copied unchanged. The
  commit's subject is `accept understanding` and its only trailer is
  `Steward-Offer`: no `Steward-Input`, no `Steward-Consumed`, no work parent, and
  no request for another slice. A procedure task's first commit has its product
  input as first parent, so every reader of accepted history (replay lookup,
  inputs, sources, outcome revision, creation time) stops there. Product commits
  cannot supply accepted offers, inputs or sources.
- **Acknowledgement.** Every evaluated offer is answered over the turn's existing
  live-input channel: *accepted* (with the accepted revision and the revision to
  build on next, which differ on replay), *refused* (with the reason, and for a
  stale offer the current revision plus the accepted text the parent has not seen:
  the sections appended since its base, or the whole account if it was rewritten),
  or *not evaluable* (sent once; the same offer is re-evaluated each poll and its
  decision is sent when it exists, so the parent need not offer again). A reply
  counts as delivered only when the provider reports the input accepted. A failed
  send, or a `rejected` or `unresolved` result, returns the reply to the outbox to
  be sent again without deciding again. Each reply has its own source ID, and
  those IDs are never counted among the inputs a checkpoint consumes. Refusals and
  failures are also logged.

Writing the ref proves nothing. Delivering a message proves nothing. The accepted
revision in task Git is the only acknowledgement recovery relies on.

The cost is honest polling: one broker `rev-parse` per running task per poll.
Under Linux host ownership each is a transient unit, roughly 17,000 per task per
day at the default five seconds. Raise `controller.poll_seconds` if you would
rather trade acknowledgement latency for fewer launches.

### Stale offers

Anything accepted since the offer's base makes it stale: an operator note, answer,
cancellation, body edit, or another accepted offer. The refusal carries the current
account, the operator input the parent has not seen, and the new revision. The
parent reconciles and offers again. An offer can never overwrite something it has
not read. Notes keep their own committed input history; they are never appended to
a second, product-side prose file.

Acceptance changes only the canonical account. Natural closure records findings and
disposition in the product checkpoint and consumes only acknowledged operator input.
It never rereads a product-side task file, and never overwrites an accepted account
with one. When the turn ends, closure stops and joins the watcher, waiting out any
acceptance in flight.

## What acceptance does not do

- It does not stage, commit or read product files, or move the native `HEAD` or
  index. The offer comes from the object database, not the worktree.
- It does not stop, signal or wait for the parent or its children.
- It does not consume or reorder operator input.
- It does not change any Definition field (`hold`, `resume`, `work`, `repository`,
  `owner` and friends).
- It does not make publication eligible. Status comes from the Definition and the
  work disposition, and neither changes; the outcome revision and result keys
  ignore the new subject.
- It does not certify that a concurrently edited product tree is coherent.

Natural closure still produces `continue`, `ask` or `idle`, and publication still
needs settled work, exact-input gates and the ordinary authority checks.

## Who owns the account

The native parent owns consolidation, by protocol: the prompt tells it to fold in
its subagents' findings before offering. That is not an OS distinction. Parent and
children share the execution identity and any of them can write the ref, so every
offer is untrusted input. There is no child registry.

Worktrees of the same repository share the ref namespace, so another task's agent
could in principle offer into this task, and with a current base (copied from a
pending offer, say) have a body accepted. Acceptance still grants no authority and
the body stays in history, reviewable and revisable. It is the same exposure as
that agent writing this task's branch today, and nothing here pretends otherwise.

## Recovery

- **Crash after acceptance, before the acknowledgement.** The body and its
  `Steward-Offer` trailer are in task Git. A fresh controller reads the account
  from Git alone, its next slice opens at the accepted revision, and the prompt
  names that revision. If the ref still points at the same blob, the first pass
  replays it with the original revision and writes no second decision.
- **A leftover ref at a later slice.** The first watch of a turn evaluates whatever
  the ref holds. An accepted blob replays its original revision and names the
  current base, which is how a lost acknowledgement survives a restart. An
  unaccepted, stale blob gets one refusal carrying what the parent needs to
  reconcile.
- **Execution loss.** Killed processes and unwritten child state are not
  reconstructed. The accepted account is what the parent had consolidated;
  native commits and session records keep their ordinary roles.

## Deadlines, cancellation and shutdown

Ordinary task cognition has **no routine deadline**. It ends at natural closure,
operator cancellation, controller shutdown or containment. There is no bigger
timer and no restart loop. Everything else keeps its bound: read-only and procedure
task runs (finite reviews over captured inputs, with no offer watcher), conversation
turns and world reconciliation use `provider.timeout_seconds`; gates, probes, deployments and
target drivers use their own; workspace preparation has a finite setup bound.

`/task cancel` commits the withdrawal and sends a native interrupt, allows a
ten-second cooperative grace, then applies process-group or Linux ownership
containment. Controller shutdown asks every running task execution, procedure runs
included, to end through the same path before the worker pool drains; an execution
that has not reached its provider yet sees the stop flag instead. An interrupted
slice is checkpointed as a continuation. Even an empty interruption gets its own
checkpoint, so the inherited main tip is never mistaken for task publication. During
shutdown any execution error ends the same way, because a failure observed while the
turn is being interrupted cannot be told apart from the interruption. The drain is
bounded by grace plus containment, not by the task. A forced stop by the service
manager, and Linux invocation ownership when the controller dies, remain the
emergency containment; neither is replaced by an infinite wait.

Two consequences, stated plainly:

- A days-long task holds one `controller.workers` slot the whole time. Eight long
  tasks occupy eight slots. That is the budget working. The answer is operator
  policy, not preemption.
- A controller restart interrupts every running task turn. Continuity across an
  upgrade comes from the accepted account, retained work and the native session,
  not from processes surviving.

## Provider support

Both built-in adapters acknowledge over their existing live-input channel: Claude
Code's stream-json input via `command_lifecycle`, Codex App Server via
`turn/steer`. GLM uses the Claude path. The offer itself is plain Git, so it works
wherever the agent can write its repository's objects and refs.

- A provider without live input gets no acknowledgement, and its offers are not
  evaluated during the turn; it records understanding at closure only.
- On Claude/GLM, input queued after a result can start another native turn.
  Understanding acknowledgements are marked as controller receipts: their native
  results are validated but do not replace the working turn's output or require
  another closure. Ordinary late notes and corrections can still replace that
  output. Codex steers within one native turn and cannot distinguish which input
  caused its final answer.
- Without a dropped execution identity, Codex sandboxing may protect `.git` inside
  writable roots, which would stop offers and native commits alike. Unverified.

## Evidence

[`test_live_understanding.py`](../tests/test_live_understanding.py) drives real Git,
a real child process and the per-turn watcher: acceptance and replay, stale offers
against notes, body rewrites and cancellation, malformed and tampered offers, and a
lost acknowledgement recovered by a fresh controller, all while the child's
heartbeat keeps advancing and `HEAD`, index and dirty files stay put. The operator
sees the accepted account while the task runs, through `/task show` and the task
board detail, and natural closure then publishes to `done` without a reconciliation
conflict.
[`test_live_understanding_adapters.py`](../tests/test_live_understanding_adapters.py)
runs the same handoff through the real Claude and Codex adapter classes against fake
native executables. [`test_unbounded_task_turns.py`](../tests/test_unbounded_task_turns.py)
covers task work that runs past the provider timeout with a child alive, cancellation and shutdown. Linux host
ownership (`tests/test_host_ownership.py`) needs root on a provisioned host and
skips everywhere else.
