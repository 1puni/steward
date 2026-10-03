# Execution lifecycle

How a task actually runs, slice by slice: what it is made of, how its status is
derived, how a slice closes, and what cancellation does and does not do. The
[kernel contract](kernel-contract.md) owns the invariants.

## Task representation

A task is `refs/heads/tasks/<id>` and `task.md` in controller-private task Git.
Its globally unique identity spans managed repositories. The accepted graph retains
the canonical task account, decisions, inputs and checkpoint objects. Product work uses
a separate `tasks/<id>` branch; each slice's findings are its closing commit message.

## Admission and authorship

[Accepted task Git](../src/steward_harness/task_store.py) is enumerated directly.
An optional trusted `tasks.remote_url` fetches and publishes its protected task refs.
A fresh harness needs neither a task SQL row nor an old native session. Product
work refs confer no admission or privileged authority.

The request exists once, in the accepted document; the prompt carries it. Nothing
in the product tree can replace the accepted repository or reply binding. Findings
publish with the product work as commit messages; request bodies and routing do not.
Every slice receives every accepted input, because a saved session may prove missing or
cognition may fall back to another provider; only the pending ones are consumed.

The [Git-native task contract](git-native-tasks.md) specifies replication,
conflicts and private metadata. [Git journeys](../tests/test_git_tasks.py) exercise
fresh intake, authorship, answers, cancellation and publication recovery.

## Execution state

[`Task.status`](../src/steward_harness/task_store.py) is the one derivation. A task is
landed when the controller-observed remote tip contains a commit whose `Steward-Work`
trailer names its current work.
Landed work reports `done`, even after a late withdrawal. A live task lock reports
`running`. Otherwise committed holds take effect. An unanswered `ask` waits; an
unlanded `idle` owes publication. An answer/retry or pending context after idle owes
another slice. A missing native session simply starts fresh from durable context.

Task documents and input history are read from Git. The store keeps one parsed record
per task keyed by its accepted revision, and per repository which task SHAs the observed
tip contains; both are disposable and never grant authority. No task progress is stored
in SQL.

### Persistent task-lock files

`<state_db>.tasks.git.locks/<task-id>.lock` is a persistent file used for
kernel `flock` coordination. The runner and repository publisher take the same
lock; release closes the descriptor without removing the file. Process exit
also releases the kernel lock. Files therefore accumulate across finished tasks
by design: their existence, count and modification times do not establish which
tasks are live.

Use `/tasks` or `/task show <id>` for task status. `GitTaskStore` and
`locked_tasks()` read the same exclusion: a non-blocking exclusive lock probe
identifies held locks, while an existing
unlocked file contributes no running task. Run probes as the controller identity
with access to the lock files; an unreadable file is skipped, so an unprivileged
scan cannot establish that no tasks are running.

Do not reap these files based on terminal status or age. Unlinking a file while
another process holds or has opened it lets a later acquisition create a different
inode, permitting two independent locks for the same task. Retaining the path
preserves exclusion across retries and publication.

## Execution closure and continuation

A task's live slice owns a retained checkout under its task lock. Native commits
remain intact. The session calls the native `steward_tasks` task tool:

```json
{"operation":"close","key":"finish","subject":"feat: implement the request","disposition":"idle"}
```

`continue` means more work remains; `ask` additionally requires a `question`
string; `idle` requests landing through the configured gates. Omit `question`
for other dispositions. The subject is one nonempty line of at most 120
characters; a question is at most 1,000 characters. Keys have 1–128 ASCII
letters, digits, `_`, `.`, `:` or `-`.

The call returns **pending intent**, bound to the task, this execution and the
accepted task revision. An identical replay returns the same intent. Conflicting
calls invalidate the decision; no later valid call overrides them. A later task
revision invalidates stale intent. A replacement native writer (fallback or
session rotation) gets a new execution identity and cannot inherit pending intent. The controller revokes the native capability,
waits for writer teardown and outstanding account acceptance, then binds the
intent to the settled tree in its checkpoint commit. The commit carries execution
and task-revision trailers alongside disposition. No tool call publishes mid-write.

Final narration is retained verbatim as findings. Extra text, marker-like text,
or an empty final reply cannot change the disposition. Missing or invalid intent
retains work without granting publication; the task requires retry. A crash
before the checkpoint cannot carry an old pending intent into a new execution.
Even a findings-only slice commits. `idle` leaves a publication obligation;
`continue` resumes ordinary execution; `ask` waits for an operator answer.
Cancellation and pending operator input still prevent publication at the
publisher's final boundary. Receipt-only native answers cannot replace the
working result or its callable intent.

Candidate review procedures still return one `VERDICT: PASS` or `VERDICT: FAIL`
line as structured gate evidence. This is a separate candidate-bound review
contract, with missing or duplicate verdicts failing the gate; it cannot select a
task disposition or send a message. Replacing the review evidence schema is a
separate gate-interface change. Ordinary procedures and rhythms have no verdict
requirement.

Ordinary task cognition has no routine invocation deadline; it ends at closure,
cancellation, controller shutdown or containment. Procedure runs keep the provider
deadline. A procedure deadline with the durably bound native session, or a controller
shutdown, autosaves partial work where possible and permits continuation on a later
tick. Authentication, protocol and workspace failures require their own handling; an
error does not prove that no tools ran. While a turn runs, its parent can offer its
account for [live acceptance](live-task-understanding.md). See [task
execution](../src/steward_harness/task_runner.py) and [task-runner
tests](../tests/test_task_runner_kernel.py).

## Provider lineage

Each task has its own provider lineage, separate from the parent conversation. Lineage is
the `conversations` table in controller SQLite, so a turn's completion and its session
binding commit in one transaction. Provider and generation
fencing prevent a late callback from restoring an obsolete token. Session tokens are
never rendered into product Git; no lineage is required for discovery.

Topic model commands select the parent conversation's provider/profile. `/task model`
and `/task model_family` select the task's. A provider change retains the brief and work
while selecting that provider's own lineage. Missing-session handling and post-start
failure policy belong to the [native runtime contract](native-provider-runtime.md).

## Publication and targets

A finished task is collapsed into one outcome, integrated onto the observed base,
and checked by repository gates and configured procedures. Conflicts and actionable
failures return to the owning task. Publication pushes the exact checked SHA,
which names its work in `Steward-Work`, with an exact-base lease. It never
rewrites the native task ref. Target convergence is independent and follows the
[installed-driver contract](automatic-deployment.md).

## Cancellation and retained work

`/task cancel` commits a withdrawal and reason, then requests interruption of a
live native turn. Native cooperation gets a bounded grace before containment;
the command acknowledgement does not mean the turn has already stopped.
It does not delete the task, worktree or pending context. Explicit retry clears the
hold. The publisher checks cancellation under the decision lease at push: a prior
withdrawal prevents it, while an already-started push may land. External observation
then reports that landing honestly. A failed gate cannot overwrite cancellation.

Ignored files and uncommitted local work remain in retained worktrees. A fresh machine
recovers accepted Git checkpoints; moving local-only work to another host is your job,
and part of any [upgrade](upgrading.md).

The controller checks retention on its first pass and hourly thereafter, through
its shared worker budget. A terminal (`done` or `cancelled`) task checkout can be
removed only under its task lock, repository lease and accepted-decision lease,
with status rechecked.
Its HEAD must be reachable in the accepted task graph. Unpublished idle tasks,
blocked tasks and tasks with live locks stay materialized. Removal uses Git
`worktree remove` without force, followed by `worktree prune`; it preserves task
refs, accepted history and provider records. Dirty, untracked or ignored files,
unfinished Git operations and unaccepted commits prevent removal and produce a
warning in the controller log. A retry can rematerialize accepted work.
[Retention tests](../tests/test_retention.py) cover these boundaries.

## Recurring work and world recovery

Repository rhythms create ordinary procedure tasks over captured inputs. One run
per interval and no overlap with incomplete runs are derived from accepted task
records. Rhythms configured with `input: world` instead run through conversation
world-turn acceptance, keyed by rhythm and interval; an interrupted world turn
holds that interval for explicit continuation. See [rhythms](rhythms.md), the
[convergence journeys](../tests/test_rewrite_convergence.py) and
[world-rhythm tests](../tests/test_world_rhythms.py).

## Running release identity

Health reports the loaded code's revision from a matching controller release receipt,
captured at import. Moving the `current` symlink cannot relabel an already-running
process. An explicit `STEWARD_RELEASE_SHA` attestation is available for other packaging
and cannot override an observed staged identity. [Health tests](../tests/test_health.py)
cover this distinction. A staged source tree alone does not establish a runnable
artifact; validate its configured build and actual runtime identity.

The [operator reference](../README.md#talking-to-it) defines the supported controls,
including `/git retarget` for never-started work.

### Task context and live steering

A note records pending context even when the task is waiting for an answer.
It keeps the existing question and does not resume execution. An explicit answer
requeues the retained task and its next slice receives both notes and answer.
Completed tasks reject execution notes: record completed-work observations in the
world, and propose a concrete follow-up if there is new repository work. Conversation
and rhythm prompts use the same Git-derived status, ownership and repository authority.

While native task execution is active, the daemon offers new persisted messages to
the provider's streaming input channel. The closing checkpoint consumes the prompt
messages and provider-acknowledged live inputs. Rejected, unresolved and late messages
remain recorded; an otherwise idle task stays queued when it still owes that context
a follow-up slice. A controller crash before checkpoint can replay retained context.
