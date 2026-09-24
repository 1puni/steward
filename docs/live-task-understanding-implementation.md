# Live task understanding: implementation

2026-09-23. Implementation record for the [initial brief](live-task-understanding.md),
which remains the unchanged acceptance baseline. This page states what
was built and what the evidence establishes; it does not restate the requirements.

## One offer, one acceptance path

The prompt carries the canonical accepted account from task Git. A running native
parent consolidates a revised account and **offers** its body directly:

```text
blob = git hash-object -w --stdin   # "Steward-Base: <accepted revision>\n\n<account body>"
git update-ref refs/steward/understanding/<task-id> <blob>
```

- **Representation.** The offer is an immutable Git blob in the task's own untrusted
  repository. The blob holds exactly two things: the accepted revision the account
  builds on, and the Markdown body. Its object ID is the offer's identity. There
  are no other fields. Frontmatter is rejected, so task authority cannot be offered.
  The single task-local ref is only routing. Re-pointing it is how the parent makes
  a new offer, and the ref confers nothing.
- **Observation.** Each running non-procedure turn has one offer watcher, which stays
  idle until the turn's live input channel exists. It is a thread started and joined by the slice itself, so it lives
  exactly as long as the turn.
  - Every `controller.poll_seconds` it resolves that ref through the untrusted
    execution broker (`run_agent_git rev-parse`).
  - When the value is new it reads the object with one bounded broker read (at most
    256 KiB + 1 bytes).
  - The controller then proves the bytes are the Git blob the offer ID names, by
    SHA-1 of `blob <n>\0<bytes>`. Git does not re-hash objects on read, and the
    object database is agent-writable.
  - It then parses exactly the UTF-8 header and a nonblank body.

  Nothing in the offer selects a path, helper or command. The ref name comes from
  the task ID the controller already owns.

  The watcher runs off the daemon loop. A slow or hostile repository can therefore
  delay only its own task's offers, and each broker call has a 30 s bound. Closure,
  including the shutdown drain, joins the watcher. A hostile ref, such as a FIFO, can
  therefore delay that task's closure by up to about a minute: two bounded broker
  calls plus a store-lease wait.

  The cost is honest polling: one broker `rev-parse` per running task per poll.
  Under Linux host ownership, that is one transient invocation unit each, or about
  17,000 per task per day at the default 5 s. Raising `controller.poll_seconds`
  trades acknowledgement latency for fewer launches.
- **Acceptance.** `GitTaskStore.accept_understanding` runs under the existing store
  lease and uses the same `_commit` compare-and-swap as every task decision:
  - if an earlier commit on the accepted decision line carries `Steward-Offer: <blob>`,
    it returns that revision and writes nothing (this is replay). A procedure task's
    first commit has its product input as first parent, so every reader of accepted
    history stops there: the replay lookup, `inputs`, `has_source`,
    `outcome_revision` and `created_at`. Product commits cannot supply accepted
    offers, inputs or sources. The last four readers had the same pre-existing
    defect;
  - otherwise it refuses if the task holds `proposed`, `blocked` or `cancelled`;
  - otherwise it refuses unless the base is the current accepted tip, which is
    exact-base compare-and-swap;
  - otherwise it commits the offered body with the Definition copied unchanged.

  The acceptance commit's subject is `accept understanding` and its only trailer is
  `Steward-Offer`. It has no `Steward-Input` or `Steward-Consumed` trailer and no
  work parent.
- **Acknowledgement.** Every evaluated offer gets a controller-origin `RuntimeInput`
  over the turn's existing live-input channel:
  - **accepted:** the accepted revision, plus the current revision to use as the next
    base, which differs on replay;
  - **refused:** the reason. For a stale offer the reply also carries the current
    revision and the accepted text the parent has not seen. That is either the
    sections appended since its base, or the whole current account when it was
    rewritten, for example by an operator edit that arrived through the task remote;
  - **not evaluable:** the error type, for example a task Git failure. This reply is
    sent once. The same offer is then evaluated again each poll, and its decision is
    replied to when it exists, so the parent need not re-offer.

  A reply counts as delivered only when the provider reports that input as
  `accepted`: a Claude `command_lifecycle` event, or a Codex `turn/steer` response. A
  failed send, or a `rejected` or `unresolved` result, returns the reply to the
  outbox, and it is sent again without deciding again. Each reply has its own
  source ID. Refusals and
  failures are also logged. Acknowledgement source IDs are excluded from the inputs a
  checkpoint consumes. Writing the ref or delivering a message proves nothing. The
  accepted revision in task Git is the only acknowledgement recovery relies on.

Every task decision is a compare-and-swap on the accepted ref. The body-only
acceptance path keeps authority fields unchanged and does not request another slice.

### Exact-base reconciliation

Anything accepted since the base makes an offer stale: an operator note, answer,
cancellation, body edit, or another accepted offer. The refusal includes the current
accepted account, unseen accepted operator inputs, and the new revision. Notes have
their own committed input history; they are not appended to a second product-side
prose file. Retrying against the new base requires the parent to reconcile that evidence.

Acceptance changes only the canonical account. Natural closure records findings and
disposition in the product checkpoint and consumes only acknowledged operator input.
It never rereads a product-side task file or overwrites an accepted account with one.
When the turn ends, closure stops and joins the offer watcher, waiting out any
acceptance in flight. After a crash, the next slice opens at the current accepted tip.

## What acceptance does not do

- It does not stage, commit or read product files. It does not move the native
  HEAD or index. It does not touch the worktree at all; the offer is read from the
  object database.
- It does not stop, signal or wait for the native parent or its children.
- It does not consume or reorder operator input.
- It does not change `hold`, `resume`, `work`, `repository`, `owner`
  or any other Definition field.
- It does not make publication eligible. Status comes from the Definition and the
  work disposition, and neither changes. The outcome revision and result keys ignore
  the new subject.
- It does not certify that the concurrently edited product tree is coherent.

Natural closure still produces `continue`/`ask`/`idle`. Publication still requires the
existing settled work, exact-input gates and authority checks.

## Ownership and attribution

The native parent owns consolidation by protocol: the prompt tells it to consolidate
subagent findings before offering. This is not an OS distinction. The parent and its
children share the execution identity, and any of them can write the ref. Every offer
is untrusted input regardless of which process wrote it. There is no child registry.

The ref namespace is also shared by every task worktree of the same repository.
Another task's agent, running under the same execution identity, could write this
task's offer ref. With a current base, which it could copy from a pending offer, it
could have a body accepted. Acceptance still grants no authority, and the body stays
in history, reviewable and revisable. This is the same exposure as that agent writing
this task's branch or working file today; the boundary does not attribute offers to a
task, and nothing here claims it does.

## Recovery

- **Crash after acceptance but before the acknowledgement.** The accepted body and
  its `Steward-Offer` trailer are in task Git. A fresh controller reads the account
  from Git alone. Its next slice opens at the accepted revision, and the prompt names
  that revision. If the ref still points at the same blob, the first pass replays it:
  the controller answers with the original revision and writes no second decision.
- **Execution loss.** Killed processes and unwritten child state are not
  reconstructed. The accepted account records what the parent had consolidated;
  native commits and session records keep their existing roles.
- **Leftover ref at a later slice.** The first watch of a turn evaluates whatever the
  ref holds. An accepted blob replays its original revision and names the current
  base to use, which is how a lost acknowledgement is recovered across a restart. An
  unaccepted, stale blob gets one refusal, carrying the text and revision needed to
  reconcile.

## Deadlines, cancellation and shutdown

Which executions keep deadlines:

- **Ordinary task cognition:** no routine deadline (`CognitionRequest.timeout_seconds=None`).
  It ends at natural closure, operator cancellation, controller shutdown or
  containment. There is no larger timer and no restart loop.
- **Read-only and procedure task runs:** they keep `provider.timeout_seconds`.
  They are finite reviews over captured inputs, and they have no offer watcher.
- **Conversation turns and world reconciliation:** they keep `provider.timeout_seconds`.
- **Gates, probes, deployments and target drivers:** they keep their own configured
  deadlines. They never used the provider deadline.
- **Native workspace preparation:** it keeps a finite setup bound.

Explicit `/task cancel` still commits the withdrawal and calls `Cognition.cancel`.
That sends a native interrupt, allows the ten-second cooperative grace, and then
applies process-group or Linux ownership containment. None of this depended on the
deadline.

Controller shutdown can no longer rely on a deadline to end task turns, so it asks
them to end. `TaskRunner.interrupt_running()` runs before the worker pool drains and
cancels each running task execution through the same cooperative path. This includes
procedure runs, which previously drained to their deadline. An execution that has not
yet reached its provider sees the stop flag instead. An interrupted slice is checkpointed and retained as a continuation
(`retain interrupted native continuation`). Even an empty interruption has its own
checkpoint, preventing the inherited main tip from being mistaken for task publication. During shutdown, any
execution error ends the same way, because a failure observed while the turn is
being interrupted cannot be told apart from the interruption. This bounds the drain by the interrupt grace plus containment, not by the
task. A forced stop by the service manager, and Linux invocation ownership on
controller death, remain the emergency containment. Neither is replaced by an
infinite wait.

Consequences to state plainly:

- A days-long task turn holds one `controller.workers` slot the whole time. The
  one-budget, no-scheduler contract is unchanged, so eight long tasks occupy eight
  slots. The fix for that is operator policy (more workers, fewer concurrent tasks),
  not a reservation or preemption.
- A controller restart interrupts every running task turn. Continuity across an
  upgrade comes from the accepted account, retained work and the native session. It
  does not come from the processes surviving.

## Adapter support

Both built-in adapters carry the acknowledgement over their existing live-input
channel. Claude's stream-json user message is acknowledged by `command_lifecycle`,
and Codex's App Server uses `turn/steer`. The offer itself is plain Git executed by
the native agent, so it is provider-neutral wherever the agent can write its
repository's object database and refs.

- **GLM** uses the Claude adapter path.
- **No live input.** A provider without `ongoing_input` gets no acknowledgement, and
  offers are not evaluated during its turn; it records understanding at closure only.
- **Late acknowledgement on Claude.** Input queued after a Claude result starts
  another native turn. The acknowledgement can therefore cost one extra turn that
  must again end with the closure lines.
- **Unverified sandbox write.** Without a dropped execution identity, upstream Codex
  sandboxing may protect `.git` inside writable roots. That would stop both the offer
  and ordinary native commits, and it is not verified here.

## Evidence

- `tests/test_live_understanding.py` covers real Git, a real child process, the per-turn
  watcher and an in-process adapter:
  - store body-only acceptance and replay;
  - a stale offer refused against an operator note, an operator body rewrite and a
    cancellation, with the unseen text returned;
  - the running-parent journey:
    - HEAD, index and dirty files are unchanged, and the child's heartbeat is still
      advancing;
    - the operator sees the accepted account while the task runs, through both
      `/task show` (`KernelCommands`) and the task board detail;
    - a duplicate is ignored;
    - a stale offer is refused with the note it had not seen;
    - two acceptances, then a late note;
    - natural closure happens without a reconciliation conflict, followed by ordinary
      publication to `done`;
  - malformed, blank, non-UTF-8, oversized, frontmatter/authority and tampered-object
    offers are refused while the turn closes normally;
  - an evaluation failure is reported;
  - cancellation during a live turn refuses later offers;
  - when the reply is lost after acceptance, a fresh controller recovers the original
    identity and the current base without a second decision.
- `tests/test_live_understanding_adapters.py`: the same handoff through the real
  Claude and Codex adapter classes over fake native executables that speak their
  wire protocols.
- `tests/test_unbounded_task_turns.py`:
  - task work past the former deadline with a child alive;
  - cancellation;
  - shutdown interruption retained as a continuation;
  - procedure deadlines retained.
- Linux host ownership acceptance (`tests/test_host_ownership.py`) requires root and a
  provisioned host. It skips honestly in the unprivileged development service; this
  change did not alter the ownership launcher.

## Development record

The implementation was developed and verified as an isolated, unprivileged
development run on a private host: nothing was deployed and no live steward was
restarted. The run's host, workspace and check-in route are instance evidence and
stay with the private records.
