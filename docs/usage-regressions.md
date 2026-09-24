# Usage after the refactor

Reviewed 2026-09-12 against committed `de6f167` and the shared working checkout. The
container migration was already in progress. This review changes documentation, not
runtime behavior. Findings below distinguish reproduced failures, code-inspected limits
and intentional changes; they are not a claim that all occurred live.

## Result return can stop after assessment starts

**Reproduced on `09c0b1f`; corrected after the September 12 cutover.**
The old selector suppressed an outcome as soon as assessment started, even when
no send had occurred. Results now retain their selected findings and source key
in private receipts beside controller state before starting assessment. Pending
receipts remain selected until the transport callback succeeds, even if an
operator answer or assessment action has already advanced the task.

Accepted assessment replays its world receipt. Interrupted assessment reports the
retained task findings with the interruption instead of repeating uncertain model
side effects. Telegram uses its existing per-piece transport receipts for task
results as well, so confirmed text/attachment pieces survive restart. A crash
between remote acceptance and local receipt writing can still duplicate an
uncertain send; this is not exactly-once delivery.

[Result regressions](../tests/test_task_result_delivery.py) separately fail the
provider, fail transport, interrupt after accepted assessment, advance the task
before delivery, reopen state, and assert one unchanged outcome without repeated
cognition. [Telegram regressions](../tests/test_telegram.py) exercise partial
multi-piece result delivery across restart. Historical assessments from releases
without these receipts cannot prove whether delivery happened and are not blindly
re-sent. Live rollout evidence belongs in the dated bugs/run records.

## Gate cancellation no longer interrupts the gate

**Code-inspected behavior change.** `/task cancel` records withdrawal and signals the
task's active native cognition. The gate uses a separate `ProcessController` without
task cancellation input in [gates.py](../src/steward_harness/landing/gates.py).
[Publication](../src/steward_harness/repository_reconciler.py) checks withdrawal after
preparation, before push.

The operator can stop publication, but a running gate may keep consuming its slot until
it finishes or reaches its own deadline. Earlier docs asserted immediate gate
process-group cancellation. Do not restore a cancellation subsystem solely for symmetry:
decide whether bounded waiting is acceptable, and preserve the pre-push withdrawal
requirement either way.

## Publication recovery retains the integrated candidate

**Corrected by the Git-native rewrite; deterministic crash regression rechecked
September 14 at `806db67`.** Publication retains the work, base and final candidate
in the accepted task before push. Completion observes that candidate in remote
ancestry; `_settle_landed` refreshes remote labels without retipping native work.

The [Git task regression](../tests/test_git_tasks.py),
`test_rebased_push_crash_before_retip_recovers_exact_candidate`, moves main after
task execution, pushes the integrated candidate, then raises before settlement.
It verifies that the original task work survives, status becomes `done`, and the
next publication pass does not push another commit. The test's historical name
does not imply that the current implementation still retips.

This proves the simulated post-push exception boundary, not a real process kill
or power loss. Result delivery has separate receipt tests above; this test does
not itself assert transport delivery.

## Deployment recovery needs more than a marker

**Deterministic driver recovery reproduced; live forced-crash acceptance remains
unproved.** The installed systemd driver's apply path now dispatches workers even
for condemned revisions. Under the pointer lease, those workers resume rollback
instead of attempting forward deployment. Tests interrupt before pointer
restoration and after restoration before service recovery, then invoke apply and
a fresh worker using the supervisor's retained arguments. They cover a prior
release, stopped absence, repeated recovery, immutable verification, service and
health failures, lock contention, and preservation of a newer active release.

Apply acceptance is not verified recovery, and successful rollback does not make
the condemned desired revision deployed. See the
[driver recovery contract](automatic-deployment.md#included-systemd-release-driver).
The local fixtures simulate the supervisor and external service; they do not
prove a live daemon self-deployment or real systemd crash recovery. No live fault
injection or restart was used for this repair. The interrupted rollback defect
predates the supervised worker's `-B` import change.

## Findings-only tasks now publish their account

**Intentional representation change with a visible effect.** Every valid slice appends
to `tasks/<slug>.md` and commits closure, even when product files are unchanged. An idle
investigation can run gates, publish its task account and trigger deployment. Earlier
wording promised no-publication completion at the original SHA;
[TaskRunner](../src/steward_harness/task_runner.py) no longer has that path. Keep this
explicit when onboarding repositories whose every main push starts an expensive
deployment.

## Recurring controls changed

**Current Git-native rewrite.** Trusted YAML defines `procedures` and `rhythms`.
Each rhythm binds a schedule, procedure, repository input ref and explicit result
owner (or `null`). Accepted runs are ordinary Git task records. `/rhythm list`
lists configured rhythms; `/rhythm run <name>` requests a new procedure task over
the resolved input. It does not delete a deployment target or a recurring-file
target ref. Schedules are edited in controller configuration, not through the
removed recurring-file commands. No default world definitions are recreated.

Global pause stops automatic rhythm admission and new task execution; an explicit
manual request can still be queued. See the current
[recurrence contract](git-native-rewrite.md#recurrence-and-evidence-boundaries).
The former `recurring.DEFAULT_BODIES` and `RhythmRunner` modules were removed;
their historical prompt disagreement is not a current implementation finding.

**Remaining observation gap.** The installed work observer derives rhythm entries
from existing tasks. Configured rhythms without task history remain invisible.
A passing task suite or documentation-link check does not establish missing-run
detection; the observer still needs configured expectations and schedule-aware
progress evidence, including pause and intentional inactivity.

## Owner environments remain an active migration

The published host path discarded accepted materializations and their ignored files.
That can erase a session's installed environment even when Git and native history
survive. The container execution backend that once retained owner environments was withdrawn;
production is native Linux execution ([identities](execution-boundary.md)). Do not claim
ignored-file persistence on an older release or erase existing warm workspaces while
testing the new one.

## Already corrected: successive result receipts

`0e93f3b` corrected a result key that hid later outcomes of the same task. [Current
tests](../tests/test_task_result_delivery.py) cover two questions followed by
publication, repeated failure after retry and legacy receipt suppression. The
[follow-through record](follow-through-environment.md) records the previously missing
receipt arriving in the warm fixture and remaining once after restart. That correction
does not establish recovery from the failure windows above.

## Busy world can strand an ordinary conversation before acceptance

Observed live after the September12 migration on controller388a62b: operator
message4259/update19352040 became turn_6d83fb1beaf84cc8a5e9c4c45400cf1d. It was
interrupted with `World is busy (thread mutex contention)` and no world receipt.
Telegram retried the transient exception, but the existing turn then caused
`has no durable acceptance receipt`; reply4261 reported the interruption instead
of answering the user. No automatic successful recovery was observed.

The preceding labelled continuity check4258→4260 passed. Do not conflate that
pass, this ordinary-conversation failure, and the separate task-result selection
gap above. Exact live observations belong in dated instance records, not here.

## An expired sign-in ended the turn instead of reaching the fallback

**Regression from the refactor; corrected 2026-09-21.**
The pre-refactor fork handled this. `c2bdd38` ("Fall back when Codex
authentication expires", 2026-09-04) taught the *CLI* provider —
`runtime/providers/codex.py` — to raise unavailability on
`invalid_refresh_token`, `token_expired` and "access token could not be
refreshed". The rewrite replaced that provider with `codex_app_server.py` and
did not carry the markers across. A pre-cutover review flagged exactly this
file as the thing to verify; the check was not made.

What survived was narrower than it looked. `_refuses_for_capacity` was
literally `return "usage limit" in message.casefold()`, so every other refusal
became a `RuntimeExecutionError` and ended the turn. `claude.py` was narrower
still: its only `RuntimeUnavailable` covered a credential *file* that would not
read, on the base-URL routing path, which is not how an OAuth session dies.

So a provider whose login had expired reported a fact about itself and the
harness recorded it as a defect in the work, leaving `fallback_families`
unasked — the same shape as the bug `491a822` fixed, because that fix named one
refusal by its wording rather than separating "declined" from "failed".

Observed over ten days to 2026-09-21: one instance lost every turn (all three
credentials dead at once, so the instance was simply down, and the operator's
own request died three times); a second lost two turns to "refresh token was
already used"; a third lost none. Re-authenticating clears the trigger, not the
cause, so it returns at the next expiry — silently, because the only evidence
is `status_reason` in the `turns` table.

Both adapters now classify a dead sign-in as declining the turn, next to the
usage limit that was already there. The marker lists are kept narrow on
purpose: one of the two Claude paths into that code carries the model's own
reply text, and a bare "refresh token" would match a model discussing OAuth.
