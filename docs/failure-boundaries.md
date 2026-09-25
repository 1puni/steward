# Execution failures and invariant faults

A failed external operation and a defect in the harness are different events.
Operational failures retain the work needed for their next authorized step.
Unexpected exceptions must not become fictitious red gates, accepted receipts
or permission to repeat side effects.

## Current boundaries

| Boundary | Operational handling | What must not be inferred |
| --- | --- | --- |
| Provider selection | Skip unavailable or incapable adapters before execution | A post-start authentication error proves no tools ran |
| Native execution | Preserve known session provenance; stop the owned process on cancellation, shutdown or a non-task deadline | All detached descendants stopped, or a second provider can safely replay the work |
| Task slice | Autosave where possible; shutdown or a bound-session procedure deadline can continue; declared failures block; live account offers are accepted or refused visibly | A reply without validated closure grants publication |
| Gate | Launch errors, deadline, nonzero exit and changed input fail validation | A programming fault is an ordinary red command |
| Repository publication | Validation failures block a task or are logged for ambient work; declared transport/runtime failures are logged at the repository boundary | A lost push response proves nothing landed |
| Incident probe | Declared probe failure becomes an unhealthy sample under incident policy | A controller defect authorizes a repair task |
| World acceptance | Retain prepared candidates through contention, conflict and failed application | Provider completion or an episode proves accepted edits |
| Rhythm | Log declared Git transport failures in quiet-activity sampling and skip quiet rhythms until the next poll without changing their quiet state; interval rhythms remain eligible. Retain incomplete unprepared work for retry and prepared work for acceptance | Failure creates a new successful due boundary |
| Deployment | Report staging/activation failure; failed restart or health attempts rollback | A pointer flip or HTTP success alone proves artifact integrity |
| Desk ingress | Requeue messages on world contention, pending application or content conflict; log the deferral and retain candidates | One conflicted session requires stopping the controller or replaying accepted cognition |
| Telegram update reply | Persist confirmed delivery pieces and retry unfinished pieces | Exactly-once network delivery |
| Task-result assessment | Retain the selected outcome before assessment; replay accepted assessment and retry transport until acknowledged | Exactly-once network delivery, or permission to repeat uncertain model side effects |

Result receipts are selected before assessment starts and stay pending until the
transport confirms, so a crash mid-assessment cannot silently drop an outcome.
Invalid routes retain a diagnostic and do not enter the worker queue until the
configured route is usable. Unowned target transitions use a configured operator
route and deterministic text without invoking task cognition.

## Cancellation and shutdown

Live conversation/task cancellation reaches the active native execution through
`Cognition.cancel`. Durable task withdrawal is a marker checked by the task and
publisher. The publisher checks it before push; already-landed work remains a
remote fact. Gates use their own process deadline and currently receive no task
cancellation callback. Immediate gate termination is therefore not promised.

Shutdown discards queued work, interrupts running task turns through the cancellation
path (native interrupt, bounded grace, containment) and retains them as continuations,
then drains running writers while retaining the daemon lease. Task turns have no routine
deadline, so this interruption is what keeps the drain finite. Worker faults propagate through `Dispatch.reap`; external service
supervision restarts the process. This is not a bounded shutdown deadline or
proof under forced host death. Linux host-UID invocation ownership
is distinct from native cooperation; see [execution ownership](execution-boundary.md#execution-ownership).

Checkpoints remain in Git and local logs. The owning conversation assesses task
outcomes through the retained result receipt; no checkpoint broadcast runs inside
task execution. Delivery failure cannot turn a saved commit into a failed execution.

## Verification

[Failure tests](../tests/test_failure_boundaries.py),
[runtime lifecycle](../tests/test_runtime_lifecycle.py),
[task cancellation](../tests/test_task_cancellation.py),
[shutdown](../tests/test_shutdown.py) and
[rollback truth](../tests/test_rollback_truth.py) exercise distinct boundaries.
A test must reach the operation it claims to prove. A fixture failure before
that operation, or an old test total from a different revision, establishes no
current guarantee.
