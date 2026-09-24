# Demolition: doctrine and refactoring method

This is the core guide for a structural refactor of the harness. Read the
[engineering doctrine](engineering-doctrine.md), the
[refactoring mandate](independent-refactor.md) and its
[representation example](engineering-doctrine-example.md) with it. The
[kernel contract](kernel-contract.md) defines required behavior; implementation
structure must earn its place by serving that behavior.

## Start from the whole operator journey

**Do not begin by improving files one at a time.** First explain the system
without borrowing its existing class, module or table names:

source input → provider work in an owning checkout → controller acceptance →
repository gates and publication where applicable → deployment where configured
→ result returned to its owner.

Write the complete conceptual machine as explicit pseudocode before designing
the replacement. Name its authoritative inputs, actual state transitions,
owners, failure modes and externally observable outputs. List every elision:
identity enforcement, provider continuity, concurrency, replay, crash-safe
acceptance, rollback and delivery cannot disappear into an unexplained helper.
Use **observe → derive desired state → execute → verify → commit** as the
orientation, not as permission to omit required behavior.

Then inspect the implementation adversarially. Trace a responsibility through
all callers, stores, queues, recovery paths and tests. Identify where code
mirrors knowledge already held by Git, the filesystem, a process, the language
runtime or an existing transaction. Find the upstream representation that makes
several downstream compensations necessary. Correct it and remove those
compensations together.

Global simplicity dominates local elegance. A smaller explanation of the whole
system is the result; moving the same knowledge among cleaner modules is not.

## Three questions before retaining a mechanism

1. **Is the claimed invariant real?** Separate externally required behavior
   from a performance wish, tidiness preference or hypothetical fear.
2. **Does the representation already enforce it?** Find the exact primitive
   that makes the forbidden state impossible. If it does not, consider a
   representation that would, before adding another check or recovery path.
3. **If this concept is deleted, what concrete required behavior becomes
   impossible?** Name a reachable case and its consumer. If there is none,
   deletion is the default.

For example, withdrawn task work must not land. Stopping a gate immediately
may save CPU, but that is a separate requirement. The publication prohibition
belongs at the writer that can push. Cancellation of a live provider and
withdrawal of durable task authority are different facts.

Do not assume that an appealing representation proves the invariant. Deleting
a file does not revoke a writer that already holds a candidate unless the
actual publication path observes that deletion. A lock excludes live holders;
it cannot retain a receipt across a crash. Trace the real path to the effect.

## Delete first, then fill

Select a complete concept, not a table or a convenient file-sized slice.
Record the required behavior, authoritative input, replacement primitive,
expected deletions and validation before editing. Judge the design against
callers and constraints before paying for an implementation.

Delete the redundant concept throughout its scope: the store, state fields,
module, forwarding APIs, callers, synchronization and recovery machinery, and
tests that only preserve that machinery. Leave each necessary gap explicit,
with the primitive that will fill it named at the gap. A temporary `HOLE:`
marker identifies missing behavior; it is not a completed implementation.

**Do this everywhere within the concept before filling any of its holes.**
Do not leave half the workflow using one representation and half using another,
then build a synchronizer between them. Do not polish code slated for deletion.
A broad cut across dependencies is often the smallest coherent change.

Fill with the primitive and the minimum required composition. If the fill is
a new module, name the primitive or independent responsibility it embodies.
If it recreates the removed subsystem, reject the design and reconsider the
representation. Compare the whole replacement cost with the whole deletion,
including new parsers, locks, types, stores and explanations.

An intermediate cut can be incomplete in an isolated development checkout.
Finish the coherent behavior and its verification before presenting it as
working or publishing it. Preserve unrelated work in shared checkouts; inspect
and stage explicit paths or hunks. Substantial scope is not permission to erase
someone else's edits or deploy an unfinished cut.

## Measure semantic reduction

The measure is implementation complexity relative to irreducible behavior.
Count concepts, independent facts, state transitions, authority holders and
execution paths. Report source size as a diagnostic, not a target to satisfy
through dense syntax, trimmed explanations or omitted behavior. A pseudocode
sketch is a reasoning aid, not a fixed denominator or a proof of completeness.

A fact moved from SQLite to JSON has not disappeared. SQLite already supplies
validation, atomicity and queries; replacement files may require more machinery.
A move to Git pays when Git's existing representation removes a second truth
and its consumers, not merely because a database table was deleted.

A target can fail review. Keep the required behavior and reject the deletion
when evidence shows it would lose session resumption, protocol correlation,
transition guards, private-file protection or another real constraint. Sunk
implementation effort and a passing suite do not rescue a bad design.
A correctness change may add code; state which required behavior it buys.

## Verify behavior and challenge the tests

Start from the operator journey and assert observable facts: input accepted,
work retained, candidate accepted, exact revision gated and published, release
healthy, result delivered. Use focused checks for actual failure and authority
boundaries, then an integration run for the resulting whole.

When tests fail, determine whether they protect required behavior or a deleted
implementation detail. Preserve the former; rewrite or remove the latter.
A failure test must reach the boundary it names and fail for that reason.
An unrelated `AttributeError` or `TypeError` does not validate a missing behavior,
even if an expected-failure marker makes the suite green. Where useful, remove
the protection temporarily and verify that its behavioral check catches the
specific failure; never use a reset that discards unrelated edits.

Validate under the relevant identity and environment. Local Git/SQLite fixtures
cannot establish authentication, systemd restart, a credentialed network push
or a complete live rhythm. Passing tests establish exercised behavior, not
that every represented concept deserves to exist.

## Convergence audit

During refactor work, every four commits use a separate read-only agent to
inspect the diff. It must not edit, commit or run the suite. Give it the
engineering doctrine, the refactoring mandate and this document; ask it to run
`scripts/converging.sh 4` and inspect `git log -4` and
`git diff HEAD~4 -- src`.

The auditor answers from the diff, not the implementer's summary:

- Which concept or responsibility disappeared? If none, say so.
- Which primitive owns the required behavior? Relocation is not removal.
- How did source size and the number of independent representations change?

Report a **DRIFTING** condition for each: source lines rise; a new module lacks
a named primitive and replaces no module; responsibilities only move; one
subsystem replaces another; or a green suite is the only offered evidence.
Two or more conditions produce **STOP**, naming the first change to reconsider.
Otherwise return **CONVERGING** or **DRIFTING** with concrete findings.
Source growth in a correctness change requires explanation; raw line count is
not a standalone termination target.

Update this document's current representations and evidence when the accepted
shape changes. Put revision-specific audit findings with the change record;
do not grow another chronological handoff inside the architecture guide.
Check required capabilities as well as concept removal.

## Completion criteria

The coherent workflow works; no unexplained holes remain. Each durable fact
has a named owner, each surviving mechanism serves a reachable requirement,
and the replacement does not maintain a second copy of the same truth.
Validation states its revision, environment and limits. A new reader can explain
the system without reconstructing the refactor's sessions.

Structural convergence and operational acceptance are separate requirements.
A simpler design that cannot complete the operator journey is unfinished.

## Representations

| Responsibility | Representation |
| --- | --- |
| Task identity and history | Named Git branch, task file, commit messages and trailers |
| Live task exclusion | `flock`, released when its holder exits |
| Repository publication | Repository lock enclosing rebase, gates and non-force push |
| Publication obligation | An unlanded branch tip carrying `Disposition: idle` |
| Gate result | Command exit status at the tested revision |
| Stop a live execution | Native interruption, then bounded broker containment; Linux host UID uses an invocation-owned systemd service |
| Withdraw task authority | Withdrawal marker checked by the writer before push; durable task decision |
| Rhythm due-ness | Comparison of prerequisites with `refs/steward/rhythm/<name>` |
| Manual rhythm execution | Deletion of the rhythm target ref |
| Deployment convergence | Observed revision, release state, healthy deployed ref and failed-release marker |
| Atomic attachment publication | Temporary file followed by `os.replace`; provider file identity in the pathname |
| Background work capacity | One bounded executor with in-flight owner keys |
| Service failure and restart | Process failure and external service supervision |

See [task representation](execution-lifecycle.md#task-representation),
[automatic deployment](automatic-deployment.md), [rhythms](rhythms-direction.md)
and [failure boundaries](failure-boundaries.md) for the precise contracts.

## Required boundaries

Configuration uses Pydantic plus cross-entity validation. Shared working paths,
publication targets and release resources require collision checks. Controller
secrets must remain outside model-writable roots. Those are relational and
security constraints; a different serialization does not remove them.
[Configuration tests](../tests/test_config.py) and
[boundary acceptance](../tests/test_boundary_acceptance.py) exercise these checks.

Provider adapters implement real protocols: request correlation, native session
resume, input acknowledgement and terminal completion. Availability selection
must not turn a failed, side-effecting execution into automatic replay on a
second provider. The [native runtime](native-provider-runtime.md) states the
contract and [provider-error tests](../tests/test_provider_errors.py) cover its
failure boundary.

Live Telegram conversations run outside the shared background-worker budget so
an operator can receive a reply when background capacity is full. Topic ordering
and transport retry belong to ingress. Moving that work into the background
dispatcher must preserve responsiveness and thread ownership.
[Responsiveness tests](../tests/test_scheduler_responsiveness.py) and
[Telegram tests](../tests/test_telegram.py) cover those behaviors.

SQLite retains incident memory and durable conversation/world-turn acceptance
facts. Task admission decisions now live in accepted Git documents. These are
representation choices: not derivable means record it, not necessarily in SQL. These include the owner of
a pending world candidate, retained unprepared work and source replay receipts.
An existence-only marker cannot express a reason or an atomic guarded state
transition. A process lock cannot retain acceptance evidence after a restart.
[State control](../tests/test_state_control.py),
[incident tests](../tests/test_incident_state.py) and
[world durability](../tests/test_world_durability.py) validate these boundaries.

## Sources of truth

| Fact | Owner | Validation |
| --- | --- | --- |
| Task identity and retained work | `tasks/<slug>` branch and `tasks/<slug>.md` | [task admission](../tests/test_task_admission.py) |
| Execution disposition, findings and checkpoint history | Git commit trailers and messages | [task status](../tests/test_task_status.py) |
| Live task execution | Per-task `flock` | [lock exclusion and dead-holder release](../tests/test_task_status.py) |
| Admission decisions and transition guards | Accepted task Git document and controller policy | [state control](../tests/test_state_control.py) |
| Provider continuity | Shared session lineage, with provider generation fencing | [lineage implementation](../src/steward_harness/state.py), [runtime lifecycle tests](../tests/test_runtime_lifecycle.py) |
| Accepted world content | World Git revision | [world acceptance](../tests/test_world_turn_checkpoint.py) |
| Prepared world work and replay | Controller candidate refs and durable acceptance receipts | [world durability](../tests/test_world_durability.py) |
| Published repository work | Observed remote ancestry | [reconciliation](../tests/test_reconcile.py) |
| Healthy deployed revision | Release state and deployed Git ref | [automatic deployment](../tests/test_automatic_deployment.py) |
| Probe failure counts and repair budget | Current incident record | [incident state](../tests/test_incident_state.py) |

A process lock cannot replace a receipt that must survive process exit. Git can record an operator's withheld grant or a gate failure in a decision
commit. SQL transactions were one implementation choice for those facts, not
an architectural necessity.
Replacing a table with files requires an actual reduction in responsibility;
file parsing, locking and atomic writes are additional behavior to account for.

## Validation map

Coverage links identify executable checks, not a claim that the current revision
or deployed instance has passed them.

| Behavior | Implementation | Validation |
| --- | --- | --- |
| Task admission creates a named Git branch and task file | `task_admission.py`, `task_file.py` | [admission](../tests/test_task_admission.py), [task files](../tests/test_task_charter.py) |
| Task execution state is derived from branch trailers, ancestry and locks, with explicit admission decisions in SQLite | `task_status.py`, `state.py` | [task status](../tests/test_task_status.py), [operator transitions](../tests/test_state_control.py) |
| Publication holds the repository lock through rebase, gates and non-force push | `repository_reconciler.py`, `landing/merger.py` | [reconciliation](../tests/test_reconcile.py), [landing](../tests/test_landing.py) |
| Deployment converges on an exact revision and verifies health, with rollback on failure | `deploy/engine.py` | [automatic deployment](../tests/test_automatic_deployment.py), [rollback truth](../tests/test_rollback_truth.py) |
| World acceptance applies the retained candidate and records dependent effects under the world lease | `world/turn_checkpoint.py`, `state.py` | [world durability](../tests/test_world_durability.py), [checkpoint acceptance](../tests/test_world_turn_checkpoint.py) |
| Native sessions use private configuration and candidate-owned records | `runtime/native_workspace.py`, `runtime/providers/` | [workspace](../tests/test_native_workspace.py), [Claude transport](../tests/test_claude_stream.py), [Codex transport](../tests/test_codex_app_server.py) |
| The execution broker enforces a separate service identity | `runtime/execution.py` | [Linux boundary acceptance](../tests/test_boundary_acceptance.py) |
| An invocation cannot leave escaped writers or kill a peer | `runtime/ownership.py`, `runtime/process.py` | [Linux ownership acceptance](../tests/test_host_ownership.py), [process input and stop](../tests/test_process_input.py) |

Implementation paths in the table are relative to `src/steward_harness/`.

## Validation scope

Run local regression checks with `uv run pytest -q`. For documentation changes,
run `python3 scripts/check-docs.py`; it checks local file/heading links without
network access. Validate configuration examples through the current loader too.
For the real Linux identity
boundary and split-identity deployment checks, run
`scripts/linux-boundary-acceptance.sh` with Docker available. That command uses
disposable state and a read-only source mount; it does not exercise systemd,
provider authentication or a credentialed network push.

The [native probe record](../experiments/native_sessions/README.md) supplies
reproducible authenticated journeys and their observed provider versions.
The [follow-through environment](follow-through-environment.md) checks the full
message-to-result path with a scripted provider. These establish different
boundaries: scripted cognition cannot establish native provider behavior, and a
successful isolated probe cannot establish the health of a deployed stewardship.

Deployment and migration evidence belongs in the [current handoff](handoff.md)
and the [downstream upgrade procedure](../migration-handoff.md). Record a
revision, command, observed result and material limitations when reporting a run.
Do not carry old suite totals forward as the validation of current source.

## Observed integration evidence

These observations apply only to the named revisions and fixtures:

| Source | Observed result | Limit |
| --- | --- | --- |
| Installed `843b22b` wheel, SHA-256 `8b03b23ce152faee62324d8dc446888bdda67d18adb6c33f82db8f822869c642` | Isolated downstream systemd service and artifact build ran as UID 978; asynchronous deployment survived the launching service's restart; HTTP 503 verified rollback to a prior release and to absence | Disposable resources were removed; this was not production cutover or rhythm acceptance |
| `1c0cea67`, CLI 2.1.259, GLM 5.3, broker UID 978 | Admission, interrupted-writer autosave, native continuation, explicit idle closure, real gate, exact-SHA fixture publication and owning-session assessment | Local receipt and isolated fixture; not actual Telegram delivery |
| `b7029cd284ea78ded038e7fcc7437d9ab055e84c`, CLI 2.1.259, GLM 5.3, broker UID 978 | Three task slices preserved native continuity; the real gate passed and `cee2c53986a37968c3b0d19c2fb21ad35f21c6ef` reached a disposable bare remote | Fixture worktree roots required correction before controller-only publication; owning assessment, transport delivery and deployment were not exercised |

These records establish observed outcomes without carrying their implementation
shapes or suite totals forward as current architecture. Reproduce the relevant
journey before attributing the result to another revision.
