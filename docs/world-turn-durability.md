# Durable world turns

A model finishing a turn is an intermediate fact. Acceptance requires its exact
world revision to be retained and applied, and source completion to be recorded.
Live task operations have their own [Git acceptance boundary](git-native-tasks.md#live-conversation-task-calls) and can be accepted before this boundary.

## The acceptance rule

```text
the turn's row claims the owner checkout
native work in an owner checkout
  → retain completed output and its execution generation in that row
  → commit the checkout: message = input and reply,
    trailers Steward-Turn / Steward-Base / Steward-Source
  → record the captured candidate in that row
  → under world lease: already in base..HEAD? else fast-forward,
    else rebase in scratch (model resolver) and fast-forward
  → complete source and record its conversation receipt
  → render recorded receipt
```

Git owns the content and the exchange. SQLite owns conversation acceptance. The two stores are not one transaction: the world branch only
fast-forwards under the lease, so "did this turn apply" is answered by the
`Steward-Turn` trailers in its history since the turn's base, and a crash
between application and SQL acceptance needs no application record.

## Execution and ownership

A conversation service binds one workspace: a configured world checkpoint, or
a read-only nonworld directory. Individual calls cannot replace that boundary.
Conversations and task-result assessments use this service. Rhythms run as
procedure tasks, not conversation turns.

One root execution owns its candidate writer. Further native inputs link to
that execution and retain `accepted`, `rejected` or `unresolved` delivery
evidence. Native acknowledgement establishes delivery only. Linked accepted
sources complete with the root acceptance; they cannot prepare a second update.
Unresolved delivery does not authorize an automatic resend.

Cognition runs outside the world lease. A conflict can be reconciled in isolated
integration storage through the configured Git reconciler. Application reacquires
the lease and rechecks the world HEAD. Unrelated world movement requires another
integration; unresolved conflict retains the actual candidate.

A clean retained session refreshes accepted knowledge before its next turn.
When its history outside the world is wholly covered by accepted candidate
receipts for that world, it advances to the current input revision: acceptance
may have rebased those candidates, and merging them again would replay old edits.
Unaccepted local commits still require a merge. Both paths refuse to overwrite
ignored local files. If that refresh conflicts, the harness aborts its merge
and reports the conflicting paths; it must not hand its conflict markers to the next
native turn. Both histories remain intact. This refusal does not resolve the content conflict.
Interrupted native edits and Git operations still belong to their session.

## Preparation and application

Capture stages the current files and commits them with the attributed exchange
as the message. The world records what is remembered, not what was asked: operator
text is recorded verbatim, while a harness-authored prompt is recorded in its short
form. The provider still receives the whole prompt. A resumed unprepared attempt amends its own earlier closing
commit (same `Steward-Turn`) rather than adding a second; once a candidate is
prepared, recovery replays that exact candidate instead. Native records remain
in their native format; credentials and private runtime state stay out of Git.

One `turns` row is one turn. Before the provider starts it claims the owner's
checkout (world, base and the input the world will record; the checkout path is
the owner's, derived from its conversation); when
the provider returns it retains the completed output, provenance and execution
generation; after Git capture it records the candidate; acceptance completes it
with the reply. Each step is one SQL update on that row.
If the checkout is contended before any provider starts, the turn is withdrawn and its
replay starts it fresh: no provider saw it, so it never happened. A turn that reached a
provider but has no retained output is never replayed automatically.
Recovery captures and accepts the original turn before admitting another
source; it does not call the working model again. A cleared or switched native
session cannot be restored by delayed completion from its previous generation.

A claimed row with no output keeps that owner fenced: startup does not
interrupt it, because its checkout may hold finished work whose reply was never
retained. Inspect its original native records and checkout before resolving
it; neither a fresh message nor a restart authorizes guessing a reply or
accepting those files under a different source. Once inspected, an operator
releases it with the controller stopped, by the same two updates a provider
failure makes:

```sql
UPDATE turns SET episode_input=NULL, world_root=NULL, base_sha=NULL
  WHERE turn_id=? AND output IS NULL;
UPDATE turns SET state='interrupted', status_reason='released by operator',
  completed_at=datetime('now') WHERE turn_id=?;
```

The checkout keeps its files for the owner's next source. Other owners can continue. A
returned provider failure releases the claim and leaves ordinary partial work
for the next source. The state database and unfinished checkouts must be
backed up together.

Never commit into the accepted world by hand to clear a blocker; move the offending file
out instead. A hand commit moves HEAD off the pending turn's base, and the retry then
fails with an unrecoverable "world moved". A dirty path defers application only when
it collides with a path the revision writes.

The trailer is as trustworthy as the world branch, which the agent identity can write:
an agent could make its own pending turn read as applied by landing a commit that
carries that turn's trailer. That only drops that turn's own edits; task calls
check repository authority independently at their own acceptance boundary.

The owner's retained checkout holds the candidate (a linked worktree's HEAD is a
Git reachability root). The row records its base, candidate, source
identity, output and provenance. After a rebase the closing commit's
`Steward-Base` is rewritten to the base it now sits on. The controller holds the
world lease through application and SQL acceptance so another harness writer
cannot interleave that boundary. A session whose last turn was rebased onto the
world moves onto the world at its next checkout instead of merging the pre-rebase
copy back.

## Finalization and replay

Acceptance completes the source and records the reply. Live task effects are
already durable in task Git and are not gated by world application. Unaccepted
historical final markers are refused; completed historical turns retain their
recorded receipts.

The world commit records the attributed exchange; SQL records the controller's
decision. Read-only conversations use the same completion and effect transaction
without a Git application. Conversation replies follow acceptance. Retained task
results already have their own accepted evidence and are delivered independently of
assessment/world acceptance; see [task results](kernel-contract.md#task-results).

## Recovery and workspace retention

Startup classifies prepared updates before abandoning unfinished conversations
or starting new world writers. An already-applied revision is recognized from
retained evidence and ancestry. Prepared work resumes acceptance, not cognition.
Unprepared work requires its actual checkout and native records; old output
alone cannot reconstruct missing edits. A turn that could not take its owner's
checkout never reached a provider, so it is withdrawn: its replay starts it
afresh from the same source.

Owner checkouts are retained between turns, including ignored files such as an
installed environment, until [idle retention](#idle-session-retention) retires a
clean one. Integration checkouts are always disposable.

Cancellation cannot erase applied world edits. It does not withdraw an independently admitted task. Clearing a session must not orphan its prepared update. Dirty or
unrelated operator work is not permission to reset the world.

## Idle session retention

An hourly controller pass retires owner checkouts after
`controller.world_session_idle_seconds` (default 86400, one day) since the
latest completed turn. Each checkout is a full copy of the world's files and
recreating one takes seconds (8 s for gg's 1.1 GB world), so one day keeps a
conversation in use warm without holding a week of copies. Owners without a
completed latest turn stay materialized.
Under the world lease, a SQL write transaction prevents new turn admission while
eligibility and removal are checked. Pending prepared turns or completion receipts
prevent removal. The checkout must contain no dirty or untracked files,
no unfinished Git operation, and no commits outside the accepted world's ancestry.
Ignored files do not block: the repository declares them regenerable, and Git's
`worktree remove` deletes them while still refusing modified or untracked files.
Finished task worktrees follow the same rule, which keeps a venv or
`node_modules` from retaining a done task's checkout indefinitely.
Refusals due to local work or pending recovery are logged. Removal uses Git
`worktree remove` without force and `worktree prune`, keeping accepted history and
native session records. The next turn recreates the checkout from the world.
[Retention tests](../tests/test_retention.py) exercise the age and custody boundaries.

## Verification

A world lease collision during initial workspace checkout happens before any
provider receives the source. The turn records that specific interruption and
replay reclaims the same turn identity, including after restart. Other interrupted
executions remain non-replayable without inspecting their retained work. Accepted
turns do not reacquire the world lease for workspace sweeping: owner workspaces
persist until eligible for background retention, application owns its integration
cleanup, and orphan sweeping is startup work. Unrelated cleanup contention
therefore cannot replace a valid reply.

[World checkpoint tests](../tests/test_world_turn_checkpoint.py) and
[world durability tests](../tests/test_world_durability.py) exercise real Git
and SQLite boundaries: preparation, application, acceptance, replay, conflict,
concurrent edits, retained evidence and independently durable task admission. They establish the
behavior exercised under their fixture identities, not native authentication
or live Telegram delivery.

An upgrade must preserve owner checkouts, world refs, native originals, unfinished
checkouts, SQL and adjacent receipts together; see [upgrading](upgrading.md).


A world-rhythm continuation can reserve its next source before the budgeted
worker runs. That unclaimed source survives restart and world-lease contention;
it is authorized work waiting for its first execution. Once the source claims
its checkout, ordinary custody applies: a crash without retained provider
completion requires evidence inspection, and cannot replay the provider. The
[world-rhythm contract](rhythms.md#world-rhythms) defines interval obligations and
explicit continuation.
