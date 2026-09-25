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

## Rehearse the conversion

Take a consistent, recoverable copy first. SQLite's backup API can copy a live
database, but it does not atomically capture the adjacent files, Git refs and
active writers. You need a quiescent boundary. `/pause` is not one: repository
convergence, result assessment and already-submitted jobs keep running.

Work on an isolated copy, and make sure copied credentials and endpoints cannot
cause production effects by accident. Anything the new representation cannot
carry gets classified, never silently dropped.

The rehearsal has to establish that:

1. the target opens the converted database without resetting it;
2. native session originals and the right provider homes resume under the real
   execution identity (an empty directory is not a login);
3. prepared world work applies or stays explicitly unresolved, with no repeated
   dependent admission;
4. task branches, pending inputs, withdrawals and origins survive;
5. publication observes the trusted remote before claiming a result;
6. releases remain verifiable, and prior release or absence can be restored;
7. ingress and result delivery resume without losing an obligation or treating
   a repeated receipt as new work.

Record the revision, command, identity and observed result. A suite passing in a
temporary repository proves neither authentication nor a real systemd restart.

## Cut over once

Stop the old writers. Take the final consistent snapshot and apply the rehearsed
conversion to it. Install config, code and permissions, run `steward check` under
the service identity, then start exactly one controller.

Verify the running identity, prepared-work recovery, provider continuity and an
operator exchange. Push one small authorized task through its real gates,
publication and result return. Watch configured rhythms produce accepted work,
because process health alone does not prove any of them ran.

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

## Report the outcome

Say what version now runs, what was converted, what evidence survived, what
acceptance passed and what is still open. Keep a local rehearsal, a staged release
and a production cutover distinct. And do not carry a historical test total
forward as evidence for a different revision.
