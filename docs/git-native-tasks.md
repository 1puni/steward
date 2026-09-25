# Git-native tasks and named deployment targets

Implementation based on e2a7777. No live cutover has been performed. The handoff
includes local operator journeys and identifies platform checks still requiring
the destination environment.

## Task contract

A task is one controller-accepted Git ref and one Markdown document. Product work
branches are execution output, not admission authority. The accepted document owns
the durable request, owner, decisions and retained findings. Execution and publication
are questions about its retained Git commits and live locks. Native sessions and
transport receipts stay private and machine-local; neither is needed for discovery.

Use a globally unique task ID, including across managed repositories. Controller
storage is `<state_db>.tasks.git`, holding `refs/heads/tasks/<id>`. `tasks.remote_url` optionally configures a separate private Git remote whose
protected `refs/heads/tasks/*` supply tasks to a fresh controller; an agent-writable work branch never supplies
that authority. Fetch must only accept fast-forward changes, retain local decisions,
and refuse divergence rather than guessing whose decision wins. Remote deletion
is not cancellation. Accepted records and reply routing are private by default;
public product commits contain only the task ID and deliberately shareable findings.
Do not grant agents write access to the accepted prefix or controller storage.

The accepted document carries the one canonical task account; its commit history carries
decisions, answers and notes, and the work branch's commit messages carry findings.
No product file duplicates either, so no two copies of task prose need reconciling. Every mutation uses Git compare-and-swap. Concurrent work and
operator decisions meet when the controller accepts a checkpoint; a new note must
survive and a cancellation must not be overwritten. A task lock excludes two native
executions; controller decision writes use a short separate lock, never the long
native-execution lock. A fresh execution receives the entire durable context.

The gated candidate is a single commit whose message names the work it lands
(`Steward-Work: <work sha>`). A task is landed when the observed remote tip contains
a commit naming its current work, so a push whose response or observation was lost
is recognized on the next pass without any record written before the push, and
without retipping the native work branch. A gate attempt is evidence, not an irreversible external effect;
only the externally observed push establishes landing. Rejected or blocked work
retains its context and work. Retry and answer authorize another slice explicitly.

SQL may still own conversation transport ingestion, world-turn acceptance and
incident observations. It must not own task records or task-input consumption.
Acceptance crossing SQL and Git uses deterministic source identity: retrying an
accepted source finds the same task, including after a crash before SQL commits.
Result routing is a durable owner plus controller transport policy; an arbitrary
address authored in work cannot direct messages. Receipts are local delivery
bookkeeping, and a fresh machine may redeliver an outcome.

## Operator CLI admission

Run under the controller's service identity, using its configuration:

```sh
steward task add --config /absolute/path/to/steward.yaml --repository app --title "Fix the settings page" --owner telegram:123 --brief-file /tmp/request.md --priority 10
```

The command prints the new task ID after accepting it into
`<state_db>.tasks.git` through the store's normal writer lease. It can run while
the daemon is running; no provider session or SQLite task row is needed.
The repository must be configured and permit `core_requested`. The UTF-8 brief
must contain 1–8000 characters after trimming; the title allows 1–256, and
priority is -100–100 (default 0). The reply owner must be a `telegram:` or `desk:`
conversation, using the same owner key as the existing transport conversation.
Delivery remains subject to the controller's transport policy.

Each invocation creates a separate task, with conversation origin and a unique
`operator-cli:` source in its private accepted metadata. The running controller
handles execution, optional task-remote replication and normal publication gates.
The CLI itself does not start execution or publish work. Invalid input or a store
failure returns a nonzero exit status and a diagnostic on stderr.

## Procedures, requirements and targets

The implemented contract is in [the target guide](automatic-deployment.md).
Procedures capture reusable instructions and model/access settings. Tasks retain
concrete execution and evidence. Rhythms trigger procedures at explicit intervals.
Named targets follow fetched refs and enforce required evidence before applying
and reporting satisfaction. The kernel has no provider-specific reviewer counts,
deployment vendor enum, default light/sleep/rem lifecycle, or workflow graph.

## Read-only deployment evidence

Downstream release paths seen so far bind systemd transient exact-SHA releases,
including the harness itself. A status-only health endpoint is not exact
running-identity proof. A vendor-hosted interface deploy, an SSH moving-main
pull and restart, a downstream command that copies mutable outputs and writes a
`deployed.json`, and a signed-artifact delivery cannot yet satisfy
exact-revision observation, or prove running identity or interruption safety.
Device-install paths are distinct from explicit device engagement.
These are local source inspections, not observations of live installations.

## Migration boundary

Preserve the old database, lineage, all refs, retained worktrees, accepted-world
records and result receipts. Export task intent and pending input from the old
schema explicitly; reconcile duplicate slugs across repositories, missing refs,
private metadata and already-pushed rebased candidates before importing accepted
refs. Rehearse fresh discovery and publication on copies. Do not reset a database
or claim that copying its task rows verbatim is the completed migration.

## Operational scope and exposure

The controller owns one private task Git store and its daemon lease. Multiple local
writers serialize decisions and compare-and-swap refs. A remote permits independent
operator edits, with divergence reported for explicit reconciliation. This is **not**
a distributed execution lease: migrating to a different host requires fencing the old
controller first. Two active machines against replicated stores are not supported.

Native credentials, provider session IDs and transport receipts stay local. The owner
address is in the private accepted record; Telegram delivery additionally requires a
configured topic and uses the controller's configured chat. Work refs cannot retarget
it. Task document bodies and findings currently travel with product work to its remote;
operators must treat that content as visible to that repository's readers. The accepted
record's private routing fields and native session tokens are not rendered into it.

A checkpoint first commits native work, then fetches its closure into task Git through
the agent's upload-pack and accepts the decision with the work as a second parent. A crash before
acceptance leaves the prior accepted context plus the retained local worktree; a fresh
machine can recover only accepted commits. Uncommitted/local-only work still requires
copying its machine's worktrees during migration. No claim of distributed atomicity is
made. A note arriving during a successful slice remains pending unless explicitly
acknowledged; an interrupted slice preserves unacknowledged input. A publication push
holds the decision lease at its final cancellation check: cancellation committed first
prevents push, while a push already holding that lease may land before cancellation.

Result identity follows the latest outcome-changing accepted commit and outcome
kind. Priority changes and additional notes do not fabricate another completion.
Transport receipts and assessment-source deduplication are private local facts.

## Integration and automatic reconciliation

A completed task's native tree is collapsed to one outcome commit before rebasing.
After integration the final commit has exactly one parent: the observed destination
base. Stable commit metadata makes the same work/base pair reproduce the same
candidate. Gates run on that candidate; configured publication reviews receive
that exact candidate/base pair. The push is an exact-base lease after checking that
the candidate descends from the base, so it cannot rewrite published history.

Conflicts and actionable failed gates return exact work/base/candidate identities
and diagnostics to the owning task. Gates and the publisher never edit code to make
checks pass. Native cognition can merge or rebase retained exploration to preserve
both intents; a new single-parent candidate is then constructed and checked again.
Ordinary native task execution has no routine deadline. Explicit interruption
and finite procedure deadlines retain unfinished work for continuation. If validation remains red on the
same base without a change outside the task document, reconciliation reports
explicit no-progress and waits. Changes that make progress have no arbitrary
third-attempt cliff. Changing log timestamps or wording do not count as progress. Missing intent uses ordinary task question/answer behavior.

Concurrent whole-document changes preserve both the accepted and native versions
and enqueue reconciliation in the owning task. A new operator note survives a
checkpoint unless consumed; cancellation remains authoritative. Unaccepted local
work is retained and returned to its owner for reconciliation, never overwritten.

The daemon no longer publishes unowned ambient branches. Every product publication
has an accepted task and retained provenance, which also prevents duplicate outcome
commits after a crash. Source Git history remains recoverable even when its native
merge commits do not appear on the publication branch.

## Recurrence and evidence boundaries

An interval rhythm starts at most one task per interval bucket. The accepted run retains the
input snapshot chosen at that start; a moving ref does not fan out additional runs
within the interval. An incomplete earlier run prevents overlapping subsequent
runs. Recovery starts at most the current interval, without accumulating a backlog.
A quiet rhythm (`schedule: {quiet: 300}`) instead waits for newly observed Git
commits across configured repositories, native task branches, accepted task work
and the world, then admits one batch after the quiet duration. No activity means
no run. Exact source revisions are retained in the procedure task; its own
review and world-assessment commits do not create another batch. First startup
baselines without running; restart after accepted evidence re-arms a full quiet
period only for newly observed inputs. See [schedule semantics](automatic-deployment.md#git-quiet-periods-and-intervals).

Manual runs are explicit new requests. Every rhythm explicitly names a configured
result owner or `null` for retained evidence only. Owned findings enter ordinary
world assessment, authorized follow-up admission and durable result delivery;
there is no separate reflection notification lifecycle. Completed scheduled read-only
reviews notify only with their owner's material update; `SILENT` retains evidence
without sending. A checkpoint itself does not broadcast to Telegram.

Read-only access constrains mutations; it does not turn every procedure into a
full-tree audit. Scheduled and explicit procedure runs follow their accepted
instruction scope. Their evidence cannot substitute for an independently requested
requirement, even when procedure and input revisions match.

Target requirements review the full exact candidate tree, including existing
security defects. Base equals candidate for that full-tree request; it is not a
claim that a first-parent diff covers all changes since deployment. Publication
requirements additionally bind the true integration parent. Read-only procedures
cannot publish product work; changed tracked or untracked inputs are rejected,
including on resumed executions. Accepted verdicts and their work graph are one
checkpoint commit, so interruption cannot strand an idle run without its evidence.

Driver success is not target satisfaction. The external observation must report
readiness at the desired exact revision, and current configured procedure evidence
must pass. Each target has an independent lock and finite invocation. Driver
errors and descendant cleanup are isolated from other owners. Platform-specific
release/rollback logic remains in the installed driver and is counted in total
production code; moving it outside root configuration is not claimed as deletion.
