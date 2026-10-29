# Durable understanding during native work

A task is an admitted intention with one account in task Git. A native agent
can revise that account while it keeps working. Saving understanding does not
end the work, change its authority, or authorise publication.

## Update through the existing task tool

The prompt contains the accepted account. When something meaningful changes,
the parent consolidates its understanding and calls `steward_tasks` with
`operation="understanding"`, a stable `key`, the observed `base` revision, and
the complete revised `body`. The body describes understanding, decisions, evidence, open questions and what
should happen next. It is Markdown prose, not task-definition frontmatter.

The existing task-call boundary supplies the task identity. The prompt and
subsequent tool results supply the observed revision. Under the task store's
existing lease, acceptance compares that revision with the current account and commits only the new body, keeping
the task Definition unchanged. The synchronous tool result reports acceptance
or the reason for refusal. No product-side account, special offer ref, polling
thread or asynchronous acknowledgement channel is involved.

A repeated key and identical body replay the original acceptance. Reusing a key
for different content is refused. Git owns the durable acceptance receipt, so
a controller restart cannot turn a retry into another accepted update.

## Concurrent changes

An operator note, answer or account edit may arrive after the agent read its
account. Such a change makes the attempted update stale. The tool refuses it
and returns the current account, relevant intervening inputs and its revision.
The agent reconciles that information and submits a new request with the
returned `base`. That token identifies the exact account being reconciled;
it need not appear in the agent's explanation to a person.

Every request names its original base, including retries after a restart.
Losing a refusal reply cannot silently rebind the old request to a newer account
and overwrite newer instructions.
An accepted retry also cannot treat later operator inputs as already seen.

## Ownership and recovery

The controller binds the tool to the running task. The parent consolidates its
children's findings; their text never grants authority. Procedure tasks cannot
use this operation to revise their assigned account.

Acceptance changes neither product files nor the task's owner, repository,
work, hold or publication obligation. It consumes no operator input. Successful
native completion still records findings; configured gates and publication
checks still decide whether the resulting work may land.

If a reply is lost after acceptance, the account and receipt already exist in
Git. A retry returns that decision. A fresh controller invocation receives the
current accepted account in its prompt. Provider fallback within an invocation reuses its original prompt and
therefore its original baseline; it must reconcile any later account changes.
Unwritten thoughts are not reconstructed; native records and retained work keep their ordinary roles.

## Deadlines, cancellation and shutdown

Ordinary task cognition has **no routine deadline**. It ends at natural closure,
operator cancellation, controller shutdown or containment. There is no bigger
timer and no restart loop. Everything else keeps its bound: read-only and procedure
task runs (finite reviews over captured inputs), conversation
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

Understanding updates use the same synchronous task-call bridge as other task
operations. They require neither a writable Git ref nor provider support for
live input. Codex and Claude expose the existing bridge through their native
tools; GLM uses the Claude path. Downstream providers must advertise this
operation through their existing task-tool listing as well.

## Evidence

[`test_live_understanding.py`](../tests/test_live_understanding.py) covers real
Git acceptance, replay, concurrent changes, authority limits and recovery.
[`test_live_understanding_adapters.py`](../tests/test_live_understanding_adapters.py)
exercises the provider bridge. These are protocol tests, not evidence of live
provider authentication or deployment.
[`test_unbounded_task_turns.py`](../tests/test_unbounded_task_turns.py) covers
long-running work, cancellation and shutdown. Linux host ownership tests need
root on a provisioned host and skip elsewhere.
