# Runtime readiness

The [kernel contract](kernel-contract.md) defines the architecture. The
[operator README](../README.md) defines configuration, commands and concurrency
limits. The [implementation and validation map](demolition.md#validation-map) links
current behavior to its executable checks.

Dated installation records, including any particular host's cutover and its open
defects, are instance evidence and are not restated here. What is reproduced,
inspected only, or unproven is in [usage after the refactor](usage-regressions.md).

## Execution and publication

Tasks execute independently in retained workspaces. One repository lock encloses
rebase, gates, non-force publication and deployment convergence. An unlanded
`Disposition: idle` tip carries the publication obligation. Remote ancestry
establishes landed work. Deployment uses the observed revision and release state;
see [automatic deployment](automatic-deployment.md).

Native providers use explicit private configuration homes and original session
records in their owning candidate workspace. Credentials remain private.
[Native runtime setup](native-provider-runtime.md) and
[working environments](working-environments.md) specify those boundaries.
World acceptance retains the exact candidate and records dependent effects;
[world durability](world-turn-durability.md) defines recovery and replay.

## Verification

Run local regression with `uv run pytest -q`. Run
`scripts/linux-boundary-acceptance.sh` for the real Linux identity transition,
startup refusal and split-identity deployment checks. Run it as root on a Linux
host with systemd, or pass `--docker` for the local Linux fixture. Both exercise
real systemd ownership; neither proves provider authentication.

The [native probe record](../experiments/native_sessions/README.md) records
reproducible installed-provider journeys and their limitations. The
[follow-through environment](follow-through-environment.md) validates the
integrated operator journey using a scripted provider.

Revision-specific evidence is retained in the
[validation map](demolition.md#observed-integration-evidence). It is evidence
for those revisions and environments, not the current checkout or a live service.
Record the source revision, execution identity, command and observed result for
any readiness claim. Process health alone does not establish a completed rhythm
or delivered task result.

## Downstream upgrade boundary

Use the [migration handoff](../migration-handoff.md) to inspect real state, take
consistent backups and rehearse conversion. Native originals and accepted world
candidates must survive the upgrade. An epoch integer in a fork does not establish
schema compatibility, and an empty private home does not establish login or
resumable history. There is no automatic database migration or reset.

Inspect the actual running revision and instance state before changing them.

## Telegram replay and workspace cleanup

Telegram intake deduplicates queued and executing update IDs. Replies retain
exact text and each confirmed text, attachment, pin and attachment-alert result
in atomic, fsynced JSON receipts at
`<database>.telegram-receipts/<chat>/<update>.json`. Replay resumes unfinished
pieces; completed updates are silent. Distinct update IDs receive distinct
replies even when their text matches.

The controller requires write access to that adjacent directory. Preserve it
across restarts. Completed receipts are removed only after a successful poll
acknowledges their IDs; an older pending update retains newer receipts. A crash
between Telegram accepting a send and the local receipt write, or a lost API
response, can duplicate that uncertain piece. This is not exactly-once network
delivery. Unsolicited notifications and product outboxes retain their own owners.
[Telegram tests](../tests/test_telegram.py) cover receipt replay and transport behavior.

Retry sleeps are bounded to 5 seconds on the polling thread and 300 seconds in
the executor. A reply needing a longer inline wait is retained for executor
delivery. These bounds are not whole-reply or HTTP deadlines.

Each live world acceptance removes its own integration checkout in `finally`.
Startup sweeps orphan integration checkouts before admitting new work. A live
conversation must not remove a peer's integration directory.
[World checkpoint tests](../tests/test_world_turn_checkpoint.py) cover that boundary.

## Readiness questions

The [post-refactor usage review](usage-regressions.md) records reproduced result
reselection failure and publication/deployment crash boundaries needing proof.
These qualify older recovery claims; a successful historical journey does not
establish every interruption path. New installations start with the
[fork and setup guide](getting-started.md).

The [backlog](backlog.md) owns unresolved product direction. Operational
acceptance needs actual Telegram delivery and observed scheduled outcomes,
including Sleep/REM where configured. Shutdown, forced host/process death and
escaped-writer behavior require evidence at those boundaries.

Verify operator-message responsiveness when repeated result assessments occupy
the same conversation. `deliver_task_result` uses the owning conversation lane;
background-worker concurrency alone does not establish fairness within that owner.

The private development repository retains historical candidate implementations;
those tags and their history are not part of the public export. Review subjects
include result-delivery transport failures, prompt-construction cleanup,
rhythm-owned task steering, environment retention, disk headroom and declarative
model effort. They are inspection questions, not verified defects in the current
checkout. The supported read-only task browser is documented in
[the task board](task-mini-app.md).
