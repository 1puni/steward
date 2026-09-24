# Source-derived working environments

> September 7 workspace decision, later superseded in part by a container-retention effort that was withdrawn. The text below describes rematerialized checkouts and is the current policy on native Linux execution.

Decided and implemented locally on 2026-09-07 following operator correction.

## Problem and conduct

A native session survived a message, but its working checkout did not. Accepted
turns removed the directory, including ignored environments and local artifacts.
Absolute output paths consequently described disposable storage. This confused
checkpoint lifetime with the lifetime of ongoing work.

The conduct rule in the [hierarchy contract](steward-hierarchy-and-memory.md) says:
“Intent flows down. Work happens at the owning scope. Evidence and references
flow up. Truth stays with its owner.” It is a statement about ownership of work and truth, not a mandate
for restrictive filesystem compartments. Filesystem access and native intelligence
should support exploration and emergence within the granted operating environment.

## Smallest change

A conversation or rhythm owns a stable checkout identity while it executes. Each execution records
its own checkpoint and acceptance receipt using the existing Git/SQL boundary.
No additional session table, scheduler, filesystem mirror, or cleanup timer is
introduced. Existing source identities determine workspace ownership. Provider
changes do not change that ownership.

New checkouts start from accepted world HEAD. An interrupted execution without a
prepared candidate retains its checkout because the tree may be its only evidence.
Once preparation succeeds, the controller Git object store owns the candidate;
once acceptance succeeds, the Git world owns the result. Those checkouts are
reclaimed and later execution rematerializes from the owning source. Ignored
files are not durable state. Pending world receipts remain authoritative and
prevent a dependent execution for their owner until resolved. Receipt replay
still does not rerun cognition or admission.

Independent sessions get different working directories. Integration reconciles
an exact retained candidate in disposable storage before accepted world mutation.
Repository landing and world acceptance use the same configured Git reconciler.
Resolution runs outside the world lease. Acceptance reacquires the lease and
rechecks the current world revision; a moved world causes bounded reintegration.
This preserves concurrent progress without treating checkout directories as a
second state database. Startup recovers prepared receipts, derives the live
unprepared owners from SQLite, and removes obsolete session, event, integration,
and task materializations.

## Permission audit

| Observed restriction | Action and reason |
| --- | --- |
| Conversation and rhythm cwd follows execution ID. | Use an owner-specific checkout while work is unprepared; after checkpoint or acceptance, rematerialize from controller Git or the accepted world. |
| Broker checks configured writable directories, but Cognition drops that grant before native execution. | Pass the broker's existing directory list through Cognition to native adapters. Workdir, native homes, managed repositories, and Git world are available to writable executions. |
| Native adapters already support extra writable roots, local tools, and network access. | Reuse those mechanisms. No new permission configuration or provider-specific lifecycle branch. |
| Broker separates controller credentials, releases, and execution UID. | Retain: these protect publication/deployment authority, not the location of ordinary investigation. |
| Read-only requests and explicit attachment delivery roots. | Retain their distinct purposes. Inspection requests remain read-only; filesystem access alone does not authorise sending every readable file to Telegram. |

Extra access does not automatically checkpoint changes in every directory.
Repository-local work still belongs in its repository and follows the existing
candidate/gate/publication path. Parent worlds carry their own decisions and
references, not copied child truth. The upstream change belongs here; recovering
a downstream instance's actual artefacts and assessing its deployed state
belongs downstream.

## Evidence and limits

Real Git/SQLite tests exercise accepted-world rematerialization, native session
identity reuse, interrupted partial-work continuation, source-derived startup
cleanup, full-disk cleanup fallback, and concurrent session checkpoints. Existing
tests exercise crash boundaries, receipt replay, cancellation, and native input
ownership. Runtime tests verify that writable operations receive configured
directories and read-only operations do not.

These tests use controlled cognition; they do not demonstrate a new live provider
session, Telegram delivery, or downstream deployment. Accepted SVG content now
survives rematerialization. Durable delivery should still identify the accepted owner,
relative file, and revision, and use the configured attachment transport.
An ignored file is disposable and is not protected by a Git checkpoint.

Legacy event checkouts are retained only while current durable ownership says
they contain unprepared work. Inspect such native records and partial work during downstream cutover.
See the [migration handoff](../migration-handoff.md).
