# Upgrade an existing steward

An upgrade carries the steward's unfinished obligations, not just its code. An
empty database that boots is not continuity. It is amnesia with a green light.

Startup opens the current schema or creates a fresh database. Anything
incompatible is preserved and refused. There are no automatic historical
migrations and no reset, because a harness that rewrites your state on startup
is a harness that can destroy it on startup. Whoever upgrades, human or agent,
inspects the actual instance and performs a bounded, rehearsed conversion.

New installation? You want [getting started](getting-started.md), not this.

## Identify what is running

Record the running release SHA, the loaded config, the service identity and the
launch path. A checkout's `HEAD` or a moved `current` symlink does not tell you
what an already-running process loaded. Ask its health endpoint and the service
manager.

Compare schemas directly: the declaration lives in
[`state.py`](../src/steward_harness/state.py). An epoch number from a fork does not
establish compatibility, and internal Python APIs are not compatibility promises.

## Inventory the actual state

Look without publishing or restarting anything. Locate:

- controller config, secrets, service units and protected source;
- SQLite with its journal/WAL state;
- adjacent files: pause and withdrawal markers, result and transport receipts;
- private controller Git stores, candidate refs and deployed refs;
- agent repositories, task branches, world history and rhythm refs;
- native provider homes, original transcripts, memories and unfinished checkouts;
- release artifacts, receipts, `current` pointers and failed markers;
- ignored environments and anything else the installation quietly depends on.

Task status is derived from accepted Git decisions, checkpoint trailers, observed
remote ancestry and live locks together. SQL alone cannot tell you whether work
is done. Do not flatten a task's questions, pending notes, retained branch or
provider lineage into a guessed terminal status.

Find prepared world updates, possibly-pushed publications, pending operator
replies and unfinished result assessments. These are obligations someone is
still owed.

Inspection must really be read-only. `git status` rewrites the index; use
`--no-optional-locks` and plumbing, and pass `-c safe.directory=` on the command
line rather than editing configuration. A fatal Git error exits non-zero exactly
like a `false` from `merge-base --is-ancestor`, so check which one you got. Open
SQLite through a `?mode=ro` URI; a WAL reader still needs write access to the
directory holding `-shm`.

Check every remote tip against the actual remote (`git ls-remote`), not a
remote-tracking ref, which gives confident wrong answers. The controller's own
observation is `refs/steward/remote/<branch>`. To decide whether work landed, compare
patches (`git cherry`, patch IDs), not SHA containment: rebased work lands under a
different SHA. Size a fork's divergence as the net diff from the true merge base, not
by commit count; carry fork capabilities through configuration, drivers, procedures
and instance files rather than as kernel patches, because every retained kernel patch
makes the next migration expensive again.

## Rehearse the conversion

Take a consistent, recoverable copy first. SQLite's backup API can copy a live
database, but it does not atomically capture the adjacent files, Git refs and
active writers. You need a quiescent boundary. `/pause` is not one: repository
convergence, result assessment and already-submitted jobs keep running.

A snapshot that is incomplete or size-capped is not rollback custody. A delta
backup is only as good as its base plus an inventory of deletions. If the host
disk is small, stream the snapshot off the machine instead of squeezing it on.

Work on an isolated copy, and make sure copied credentials and endpoints cannot
cause production effects by accident. Keep source IDs and exact Git revisions
wherever the new representation needs them. Anything the new representation
cannot carry gets classified, never silently dropped.

Before you delete a configuration key the new schema rejects, find out what
behaviour it was carrying. After conversion, diff the converted configuration
against the live one, key by key.

The rehearsal has to establish that:

1. the target opens the converted database without resetting it;
2. native session originals and the right provider homes resume under the real
   execution identity (an empty directory is not a login);
3. prepared world work applies or stays explicitly unresolved, independently
   admitted tasks survive without duplicate admission, and interrupted unprepared
   work keeps its actual files;
4. task branches, pending inputs, withdrawals and origins survive;
5. publication observes the trusted remote before claiming a result, including
   across a rebased push;
6. releases remain verifiable, and prior release or absence can be restored;
7. ingress and result delivery resume without losing an obligation or treating
   a repeated receipt as new work.

Use the existing behavioural tests and installation acceptance paths, and keep
acceptance runs from sending unsolicited messages to real people. Record the
revision, command, identity and observed result. A suite passing in a
temporary repository proves neither authentication nor a real systemd restart.

## Cut over once

Stop the old writers. Take the final consistent snapshot and apply the rehearsed
conversion to it. Install config, code and permissions, run `steward check` under
the service identity, then start exactly one controller. Telegram allows one
poller per bot token, so copy the update offset across only after the old ingress
has stopped.

Verify the running identity, prepared-work recovery, provider continuity and an
operator exchange. Push one small authorized task through its real gates,
publication and result return. Where an external platform owns deployment,
observe that platform's exact release separately. Watch configured rhythms
produce accepted work, because process health alone does not prove any of them
ran. A forced `/rhythm run` proves the procedure, not the schedule, and nothing
observed while paused proves recurrence.

Keep the recovery copy until all of that holds. Rollback restores compatible code
**and state**, not just an older binary. An old binary that refuses an unfamiliar
schema is not a rollback target.

## The epoch-50 converter

One schema change folded world acceptance into the per-turn rows. Its converter
is kept as a worked example of the shape an explicit, stopped-copy conversion
should take:

```sh
uv run python scripts/upgrade-epoch49.py \
  --source /private/quiescent-copy/state.db \
  --destination /private/converted-state
```

It never edits its source and never launches a controller. The destination must
be new and outside the source directory. It refuses running turns, pending world
acceptance, unresolved completion files and incompatible epochs; preserves the
old database, adjacent files, task history, turn and source identities, and native
lineage; and writes `epoch50-conversion.json`, which must say `complete: true`
before the result is deployable.

## Callable notifications and task closure

Drain active executions through the existing release path before switching code;
`/pause` alone is not a writer barrier. New invocations receive the native tool
and updated procedure instructions together. Remove any rhythm `deliver` keys
from installed configuration before startup; unknown keys remain a validation
error. Preserve task refs, native homes,
world-turn records and adjacent result/transport receipts. Existing prepared
messages replay exactly; unprepared final markers acquire no sending authority.
A retained query-shaped waiting question is now ordinary operator input, so
inspect any such waiting tasks and answer or retry them deliberately.

World rhythms and procedure tasks use callable notification. Successful native
completion no longer requires a close call; task waits and explicit continuation
still use the existing tool until native callbacks are connected. No notification
capability means recorded-only findings. Ordinary conversations also accept an empty
reply after successful native completion, preserving that turn's own world changes
and action receipts rather than passing them to another source.
Verify tonight's schedule, current procedure files, provider MCP startup, a
notification receipt, settled task closure and brief document freshness on
the deployed host. Local scripted-provider tests do not establish those live
facts. Do not manually replay accepted messages to test delivery.

## Report the outcome

Say what version now runs, what was converted, what evidence survived, what
acceptance passed and what is still open. Keep a local rehearsal, a staged release
and a production cutover distinct. And do not carry a historical test total
forward as evidence for a different revision.


## World-rhythm obligations

World-rhythm interruption leaves a held obligation across interval rollover.
Existing interrupted turns also remain open on upgrade: inspect `/rhythm list`
and retained native evidence, then use `/rhythm run <name>` to authorize one
continuation. An accepted predecessor can wake its original interval's dependent
after rollover. This can expose previously abandoned intervals in existing
history; review them before resuming. No state-schema migration is needed:
attempt source keys and ordinary turns/receipts own this behavior. Older releases
do not understand continuation keys when deciding interval eligibility, so
rolling back after a continuation can misreport completion or strand its chain.
See the [world-rhythm contract](rhythms.md#world-rhythms).


## One inbox for Telegram and the desk

Telegram updates and desk messages now share one inbox format and one drain.
The desk inbox, its event log and the `desk` configuration keys are unchanged,
so desk clients need nothing. Telegram's retained updates move from
`<state_db>.telegram-receipts/<chat>/<update>.json` to
`<state_db>.telegram-inbox/<chat>/`, and the poll offset, which used to live
only in memory, is persisted there as `offset`.

The conversion is automatic and needs no operator step. On its first start,
before polling, the Telegram ingress queues every unfinished receipt with its
saved reply and confirmed pieces and sets the offset past every receipt. The
previous release removed a receipt only after a poll had acknowledged its
update, so every update it answered but had not yet acknowledged still has
one, and none is answered twice. The persisted offset marks completion, so
later starts skip the step. Receipts are never edited or removed, and
`task-results/` there still holds result delivery receipts. If the receipts
cannot be read, the controller logs `Telegram ingress NOT started` at critical
level and runs without Telegram rather than polling from offset 0; desk
messages, tasks and rhythms continue, and Telegram results stay pending.
Repair the receipt directory and restart.

Rolling back after the new controller has answered messages is not clean: the
old release ignores the inbox and starts from offset 0.
