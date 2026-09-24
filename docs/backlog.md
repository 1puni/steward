# Open decisions and their history

The original September 5 findings were an alignment review held in the private
records. This page keeps their stable IDs and current
disposition. Runtime guarantees live in the [kernel contract](kernel-contract.md);
concrete post-refactor failures and changed usage live in [Usage after the
refactor](usage-regressions.md).

The immediate work is result-return recovery, publication/deployment crash acceptance,
and the retained-environment question. Broader
organisation authority and spending limits remain product decisions. Historical
permission statements are not standing authority for a new rollout.

## B01 — Completed conversation can lose its world edits

Implemented through [world acceptance](world-turn-durability.md): retain candidate
objects before preparation, apply the exact revision, then accept dependent effects.
Recovery must preserve this ordering.

## B02 — Rhythm lock scope contradicts the documented concurrency boundary

Implemented: rhythm cognition runs outside the world lease. Only world mutation and
acceptance require it. See [rhythms](rhythms-direction.md).

## B03 — Slow scheduled work delays observation and result delivery

Independent work uses one shared worker budget; Telegram conversation handling is
separate. Per-conversation contention and fairness still need operator-journey evidence.
There are no separate per-lane pools or SQL scheduler claims. See
[dispatch](kernel-contract.md#concurrent-dispatch).

## B04 — No file changes forces task disposition to idle

Findings and explicit closure are supported. The representation changed: an
investigation appends its task account, so idle work can publish even without product
edits. See [the usage
change](usage-regressions.md#findings-only-tasks-now-publish-their-account).

## B05 — Cancellation and shutdown lack one explicit boundary matrix

Native cancellation, task withdrawal and orderly shutdown have separate owners. Gates
are no longer interrupted by task cancellation; forced process/host death and
interrupted rollback need their own acceptance. See [failure
boundaries](failure-boundaries.md) and [the usage review](usage-regressions.md).

## B06 — An incompatible schema discards live obligations

Resolved: incompatible state is preserved and startup refuses it. Actual instance
conversion follows the [upgrade procedure](../migration-handoff.md); no automatic reset
or migration framework.

## B07 — Workspace preparation failure can strand a running conversation

Workspace preparation and prompt construction share the conversation interruption
boundary. Preserve that behavior when changing execution environments. [Conversation
tests](../tests/test_conversations.py) cover turn cleanup.

## B08 — Runnable artifacts and observable deployment need a shared contract

Native artifact construction and receipt verification are implemented. The active
container migration must establish independent verification and actual host acceptance.
Automatic deployment recovery after activation/failure markers remains an [evidence
gap](usage-regressions.md#deployment-recovery-needs-more-than-a-marker).

## B09 — Running release identity must survive real path behavior

Implemented: running release identity is captured from loaded code and its receipt.
Moving a symlink does not relabel an old process. [Health
tests](../tests/test_health.py) cover the boundary.

## B10 — Error classification can hide programming and invariant faults

Keep declared operational failures distinct from invariant faults. Provider execution
failure is not permission to repeat work on another provider. [Failure
boundaries](failure-boundaries.md) owns current handling; changed wrappers must preserve
original defects and unfinished evidence.

## B11 — Steering a live task and reading its progress are unfinished

Task answer/retry/note operations and checkpoint inspection are implemented. Pending
inputs are turn rows, not sequence counters. Notes are deferred context. [Execution
lifecycle](execution-lifecycle.md) owns behavior.

## B12 — Scope, onboarding, and delegation mix implemented and future behavior

A [first-install guide](getting-started.md) and [onboarding
skill](../skills/org-onboarding/SKILL.md) now guide setup of explicitly configured
repositories. Authenticated cross-steward delegation and dynamic organisation discovery
are not kernel features.

## B13 — Documentation claims and verification evidence need reconciliation

The current docs separate contracts, operations, proposals and dated evidence. Future
changes must update the owning contract and relevant acceptance record, rather than
appending contradictory historical claims. The [documentation map](README.md) is the
entry point.

## B14 — Preserve outcome responsibility across bounded executions

The steward owns the broader outcome until evidence resolves it; completion of one task
is not outcome closure.
[D01–D04](design-discussion.md#d01--the-steward-owns-the-broader-outcome) record intent.
A separate outcome mechanism remains an unbuilt proposal. Exercise the
existing world, conversation and rhythm loop before adding another entity.

## B15 — Enforce the operating allowance across all execution

An overall operating allowance is agreed direction, not implemented enforcement.
`controller.workers` bounds concurrency and invocation deadlines bound calls; neither
bounds aggregate spend. Accounting units, concurrent reservations and behavior at
exhaustion remain to be selected.

## B16 — Onboard and operate an organisation under a YAML grant

The organisation proposal records broad grants and
narrower exceptions. Current YAML enumerates repositories; it has no organisation
presets, GitHub App provisioner, PR publisher or arbitrary deployment adapter. The
onboarding skill preserves each existing release system and reports unsupported bridges
explicitly.

## B17 — A longstanding conversation is permanently bound to its first task

Resolved: one conversation can admit several tasks, each with its own provider lineage
and explicit task controls. Starting a task does not take over the parent thread.

## B18 — A task receipt reaches the operator but bypasses the owning session

Result assessment returns to the owning conversation. Successive outcomes are keyed by
task revision after `0e93f3b`. However, failure after assessment starts can suppress
future selection before delivery. The [reproduced
regression](usage-regressions.md#result-return-can-stop-after-assessment-starts)
supersedes the former blanket claim of durable outbox recovery.

## B19 — Pinned model ids drift behind provider releases unnoticed

Backlog, not implemented. `provider.models` in `steward.yaml` pins an exact id per family
and profile, and nothing compares those pins with what providers actually serve. A newer
model can ship while a profile stays on its predecessor, and a retired id only surfaces
when turns start failing. On 2026-09-22 the `claude` `deep` profile was still
`claude-opus-5` after Opus 5.5 had shipped. On the same box, a downstream project's nightly memory
curation sat on `glm-5.1` and failed output validation five nights running while its
other tiers had already moved to `glm-5.3`.

Intended direction: a scheduled model watch reads each configured family's served model
list, diffs it against the pins, and reports to the operator topic only on change: a
newer model in a family, a pinned id no longer served or erroring, or a profile behind
its family's newest. It should also attribute accepted and rejected turns to the model
that ran them, so a model change can be judged on evidence. Whether the watch may
propose a pin change, or only report, is an open decision. It must not change pins
unattended.

## B20 — Finished task and idle world checkouts accumulate without retention

Implemented: hourly controller retention removes clean terminal task worktrees
only with accepted Git custody and the task/repository locks. Clean world owner
sessions retire after a configurable idle age, fenced against turn admission and
pending acceptance. Dirty, ignored and unaccepted work remains and is reported in
controller logs. See [task retention](execution-lifecycle.md#cancellation-and-retained-work),
[world retention](world-turn-durability.md#idle-session-retention) and the
[boundary tests](../tests/test_retention.py). This is repository implementation and
fixture evidence; no live cleanup or deployment is claimed.
