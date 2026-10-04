# Git-native tasks

A task is not a row, a worker or a workflow. It is one controller-accepted Git ref
holding one Markdown document, and every decision about it is a commit. This page
is the contract for that representation: admission, privacy, integration and
recurrence. Deployment targets have [their own page](automatic-deployment.md).

## Task contract

A task is one controller-accepted Git ref and one Markdown document. Product work
branches are execution output, not admission authority. The accepted document owns
the durable request, owner, decisions and retained findings. Execution and publication
are questions about its retained Git commits and live locks. Native sessions and
transport receipts stay private and machine-local; neither is needed for discovery.

Use a globally unique task ID, including across managed repositories. Controller
storage is `<state_db>.tasks.git`, holding `refs/heads/tasks/<id>`.
`tasks.remote_url` optionally configures a separate private Git remote whose
protected `refs/heads/tasks/*` supply tasks to a fresh controller; an agent-writable
work branch never supplies that authority. Fetch must only accept fast-forward changes, retain local decisions,
and refuse divergence rather than guessing whose decision wins. Remote deletion
is not cancellation. Accepted records and reply routing are private by default;
public product commits contain only the task ID and deliberately shareable findings.
Do not grant agents write access to the accepted prefix or controller storage.

The accepted document carries the one canonical task account; its commit history carries
decisions, answers and notes, and the work branch's commit messages carry findings.
No product file duplicates either, so no two copies of task prose need reconciling.
Agents may revise the whole document (plans, scope and findings), and prior
versions stay in Git. Body text cannot change the protected authority fields.
Every mutation uses Git compare-and-swap. Concurrent work and
operator decisions meet when the controller accepts a checkpoint; a new note must
survive and a cancellation must not be overwritten. A task lock excludes two native
executions; controller decision writes use a short separate lock, never the long
native-execution lock. A fresh execution receives the entire durable context.

The gated candidate is a single commit whose message names the work it lands
(`Steward-Work: <work sha>`). A task is landed when the observed remote tip contains
a commit naming its current work, so a push whose response or observation was lost
is recognized on the next pass without any record written before the push, and
without retipping the native work branch. A gate attempt is evidence, not an
irreversible external effect; only the externally observed push establishes landing. Rejected or blocked work
retains its context and work. Retry and answer authorize another slice explicitly.

SQL owns conversation transport ingestion, world-turn acceptance and incident
observations. It must not own task records or task-input consumption.
Acceptance crossing SQL and Git uses deterministic source identity: retrying an
accepted source finds the same task, including after a crash before SQL commits.
Commit trailers are trusted only on the accepted decision line. Recovery reads refs and
ancestry; product history is retained but never accepted, so a trailer a model writes
into its own commits (`Steward-Offer`, `Steward-Work`) proves nothing.
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
The repository must be configured. The UTF-8 brief
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

## What counts as deployment evidence

A status-only health endpoint is not proof of running identity. Neither is a
vendor dashboard saying "deployed", an SSH pull-and-restart of a moving `main`, or
a script that copies files and writes `deployed.json`. None of those can say which
exact revision is running, or whether an interruption left it half-applied. The
included systemd driver can; other release paths need a driver that observes the
exact revision before they satisfy a target.

A task carried across a fresh database takes a deterministic `uuid5` ID from a
`migration:<old id>` source, an owner resolved to a valid conversation, and only work
that is actually retained. Import it blocked rather than let it dispatch at cutover,
and create its ref with a zero-OID compare-and-swap. Before archiving the old store,
verify that every delivery was sent and every input consumed.

## Operational scope and exposure

The controller owns one private task Git store and its daemon lease. Multiple local
writers serialize decisions and compare-and-swap refs. A remote permits independent
operator edits, with divergence reported for explicit reconciliation. This is **not**
a distributed execution lease: migrating to a different host requires fencing the old
controller first. Two active machines against replicated stores are not supported.

Native credentials, provider session IDs and transport receipts stay local. The owner
address is in the private accepted record; Telegram delivery uses the controller's
configured chat. Work refs cannot retarget
it. Task document bodies and findings currently travel with product work to its remote;
operators must treat that content as visible to that repository's readers. The accepted
record's private routing fields and native session tokens are not rendered into it.

A checkpoint first commits native work, then fetches its closure into task Git through
the agent's upload-pack and accepts the decision with the work as a second parent. A crash before
acceptance leaves the prior accepted context plus the retained local worktree; a fresh
machine can recover only accepted commits. Uncommitted/local-only work still requires
copying its machine's worktrees when moving hosts. No claim of distributed atomicity is
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
and finite procedure deadlines retain unfinished work for continuation. If
validation remains red on the same base without a change outside the task document, reconciliation reports
explicit no-progress and waits. Changes that make progress have no arbitrary
third-attempt cliff. Changing log timestamps or wording does not count as progress.
Missing intent uses ordinary task question/answer behavior.

Concurrent whole-document changes preserve both the accepted and native versions
and enqueue reconciliation in the owning task. A new operator note survives a
checkpoint unless consumed; cancellation remains authoritative. Unaccepted local
work is retained and returned to its owner for reconciliation, never overwritten.

The daemon never publishes unowned ambient branches. Every product publication
has an accepted task and retained provenance, which also prevents duplicate outcome
commits after a crash. Source Git history remains recoverable even when its native
merge commits do not appear on the publication branch.

## Recurrence and evidence boundaries

A procedure rhythm admits a run only when its input holds a commit none of its
finished runs captured: the candidate of its `input` ref, and for an
organisation (`workdir`) rhythm also every observed remote head and ordinary
task's accepted work. The runs' retained inputs are the cursor. An interval
rhythm starts at most one task per interval bucket, and none in a bucket without
new input; a moving ref does not fan out additional runs within the interval. An
incomplete earlier run prevents overlapping subsequent runs. Recovery starts at
most the current interval, without accumulating a backlog. A quiet rhythm
(`schedule: {quiet: 300}`) instead admits one run once its new input has stopped
moving for the quiet duration. Task-store bookkeeping and procedure evidence,
the rhythm's own included, are never input. See [schedule semantics](automatic-deployment.md#git-quiet-periods-and-intervals).

Manual runs are explicit new requests. Every rhythm explicitly names a configured
result owner or `null` for retained evidence only. Completed rhythm findings
remain evidence. Deliberate notification calls queue delivery receipts directly,
independently of final narration or assessment. Questions and failures retain
ordinary result delivery. A checkpoint itself does not broadcast to Telegram.

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
release/rollback logic lives in the installed driver, and it still counts as
production code: moving complexity out of the kernel's config is not deleting it.


## Live conversation task calls

Codex App Server and Claude Code (including GLM) receive an execution-scoped
`steward_tasks` MCP server with one `task` tool. The native CLI launches a small
stdlib Python stdio client under the existing provider identity. It forwards
JSON over a temporary controller-owned Unix socket. Kernel peer credentials
bind each caller to the provider's controller-owned systemd invocation; local
inert brokers use the process session instead. Another invocation cannot borrow
that authority by discovering the socket path. The client holds no task state or controller configuration.
Rhythms receive no capability. There is no additional cognition or confirmation
step.

| Operation | Required string fields besides `operation` | Receipt |
| --- | --- | --- |
| `submit` | `key`, `repository`, `title`, `brief` | Durable `task_id`, accepted Git `revision` |
| `list` | none | Up to 200 task summaries, with `truncated` |
| `show` | `task_id` | Current status, revision, brief, findings and reason |
| `answer`, `retry`, `note`, `cancel` | `key`, `task_id`, `text` | Durable task ID and operation revision |

Mutations return `accepted: true` and `replayed`. A refusal returns
`accepted: false` and an error. An unavailable connection or `accepted: null`
means the outcome is unknown: retry the exact request with its original key.
The model must use receipts to claim admission, not its final narration.
Independent submissions need no intervening operator message or task completion.

A key is 1–128 ASCII letters, digits, `_`, `.`, `:` or `-`. Choose one distinct,
descriptive key per intent; reuse it only for an identical retry. Keys are scoped
to the owning conversation across executions, provider changes and session resets.
Submission keys name one task in that conversation. Steering keys are scoped
further to the target task, across all steering operations. Changing any request
field under an accepted key in its scope is refused. Different keys are
different intentions even when their briefs match. Observations always return
current state and require no key.

Accepted task first-parent commits own receipts through `Steward-Source`
trailers: conversation and key digests, a canonical JSON request digest, and the
controller-validated source ID (the initial execution or an attached input). The mutation and receipt are the same Git commit/ref
update. A retry after loss of the tool reply or controller restart recovers that
revision rather than repeating the mutation. Rejections create no task commit.
There is no operation table or second task registry. Native work parents cannot
supply controller receipts.

Each call can include `source_id`, referring to the initial Turn id or the
controller-provided source id of a live input. The controller resolves its
speaker from its own retained row; caller-supplied speaker claims are refused.
New calls require a source in the current execution and accepted native delivery
for attached inputs. Omitting `source_id` selects the initial input only while
there are no attached inputs; mixed-input executions require an explicit source.
An operator input can therefore authorize an operation during a controller-started
execution, while a desk observation keeps its restrictions during an
operator-started execution. A source is attribution and existing authority, not
proof that arbitrary model-selected work was requested. It must actually support
the requested operation. The model cannot turn a controller finding into an
operator instruction by naming its own sender.

The source id is covered by the request digest when supplied and retained in the
accepted Git source trailer. After restart, an exact accepted request may replay
with its old source; a new operation cannot borrow that source. The controller
checks the running execution and bound provider generation under the same SQL
transaction as authorization. Interrupted or cleared executions cannot mutate.
Calls cannot select a conversation, operator, provider or authorization policy. Submission requires a
currently configured repository. A conversation may inspect and steer its own
tasks; an operator turn can also address ownerless work. Automated turns cannot
steer ownerless work. Existing task status guards still apply.
Task notes and answers retain controller provenance, never operator provenance.

Admission is independent of conversation/world completion. An accepted task can
run before the parent finishes; it survives parent failure, cancellation, lost
final output, and failed world capture or acceptance. Revoking a repository
prevents further authorized operations but does not undo its accepted task ref.
The socket closes when native execution returns or fails; a later execution
gets a new capability and can retry the same keys. `/tasks`, `/task show`,
`/task cancel`, and the task board retain their existing operator roles. Parent
`/cancel` only interrupts the conversation; cancel each task to withdraw its work.
Task results use the task's durable owner and the existing result delivery path.

Final `TASK_PROPOSAL` and `TASK_ACTION` markers do not execute operations. They
are stripped and explicitly refused, including unaccepted historical outputs
recovered after an upgrade. Already completed historical turns replay their
recorded receipts without another admission. Do not resubmit uncertain historical
work without inspecting existing tasks first.

Validation: `tests/test_task_calls.py` runs real native adapter pipes and MCP
children against scripted Codex/Claude/GLM reasoning, including two receipts
before terminal completion. Conversation, task-result, provenance and world
recovery tests cover independent durability and routing. These local checks do
not establish a deployed release.

### Ownership reads during task execution

Repository task and procedure executions receive the same invocation-bound tool.
`query` takes `repository` and `text` and returns a bounded ownership observation:
at most ten unfinished matches and 7,500 JSON characters, with observation time,
revision, ownership relation and truncation. It excludes the querying task and
discloses no peer briefs or owner addresses. `notify` queues text to the task's
bound owner; optional `close` records a wait or continuation for acceptance after
successful native completion and writer teardown. Ordinary completion needs no call
([execution lifecycle](execution-lifecycle.md#execution-closure-and-continuation)).
Tasks cannot submit or steer peer work. World rhythms receive notification-only
authority. Public read-only conversations remain excluded.

Ownership reads use this tool only. A question containing `TASK_QUERY:` is an
ordinary operator question, never a controller command. Observations grant no
lease or authority to change another task. Missing native tool support fails
closed; final prose cannot substitute for a call.
