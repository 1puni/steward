# Source-derived working environments

A checkout is a place to work, not a place to keep things. What survives is what
Git holds: accepted world history, controller candidate objects, task branches.
Everything else in a working directory is rematerialised from its owning source
when it is next needed, and ignored files are disposable.

## Problem and conduct

The failure this prevents is easy to walk into. A native session survives from one
message to the next, but its checkout does not, so an absolute output path it
mentioned now points at storage that no longer exists. Checkpoint lifetime and the
lifetime of ongoing work are different things, and the owning source, not the
directory, is what carries work forward.

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

| Restriction | Current behaviour and reason |
| --- | --- |
| Conversation and rhythm cwd follows execution ID. | Use an owner-specific checkout while work is unprepared; after checkpoint or acceptance, rematerialize from controller Git or the accepted world. |
| Broker checks configured writable directories. | Cognition passes that same directory list to native adapters. Workdir, native homes, managed repositories, and Git world are available to writable executions. |
| Native adapters already support extra writable roots, local tools, and network access. | Reuse those mechanisms. No new permission configuration or provider-specific lifecycle branch. |
| Broker separates controller credentials, releases, and execution UID. | Retain: these protect publication/deployment authority, not the location of ordinary investigation. |
| Read-only requests and explicit attachment delivery roots. | Retain their distinct purposes. Inspection requests remain read-only; filesystem access alone does not authorise sending every readable file to Telegram. |

Extra access does not automatically checkpoint changes in every directory.
Repository-local work still belongs in its repository and follows the existing
candidate/gate/publication path. Parent worlds carry their own decisions and
references, not copied child truth.

## Evidence and limits

Real Git/SQLite tests exercise accepted-world rematerialization, native session
identity reuse, interrupted partial-work continuation, source-derived startup
cleanup, full-disk cleanup fallback, and concurrent session checkpoints. Existing
tests exercise crash boundaries, receipt replay, cancellation, and native input
ownership. Runtime tests verify that writable operations receive configured
directories and read-only operations do not.

These tests use controlled cognition; they do not demonstrate a new live provider
session, Telegram delivery, or deployment. Durable delivery of a file should
identify the accepted owner, relative path and revision, and use the configured
attachment transport. An ignored file is not protected by a Git checkpoint.

Older checkouts are retained only while durable ownership says they hold
unprepared work. Inspect such records and partial work before an
[upgrade](upgrading.md).
