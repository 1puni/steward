# Give it a body of work

You have something you want the steward to carry: a migration, a backlog, a
standing review, an investigation that will take more than one sitting. This
page is how you express that, and what the harness will and will not do with it
once you have.

Read [getting started](getting-started.md) first if you have not installed an
instance yet. This page assumes a running controller with at least one managed
repository and one conversation transport.

## A task is a Git record

There is no task table and no task queue file. A task is two Git records with
the same name.

**The accepted record** lives in a bare repository the controller owns
privately, at `<provider.state_db>.tasks.git` — with the default
`state_db: /var/lib/steward/state.db`, that is
`/var/lib/steward/state.db.tasks.git`, mode `0700`, owned by the controller
identity. One task is one ref, `refs/heads/tasks/<task-id>`, whose tree holds
exactly one file, `task.md`:

```markdown
---
version: 1
repository: app
title: Retire the CSV importer
origin: conversation
source: telegram:4211:9182
owner: telegram:4211
priority: 5
---

Remove the CSV importer and move its two callers to the streaming reader.
Keep the existing fixtures passing. If a caller needs behaviour the streaming
reader does not have, stop and ask rather than widening the reader's contract.
```

The frontmatter is the controller's; the body is the durable request. Every
decision about the task is a commit on that ref: an answer, a note, a
cancellation, a retained publication candidate, a repair demand. Nothing about
a task's progress is stored in SQL. The fields the controller maintains are
`repository`, `title`, `origin`, `source`, `owner`, `priority`, `hold`
(`proposed`, `blocked` or `cancelled`), `reason`, `work` (the retained work
commit), `resume`, and the `publication`, `repair` and `procedure` blocks.

**The working record** lives in the product repository, on a branch named
`tasks/<task-id>`, in a file at `tasks/<task-id>.md`. That file has two
authors. The harness owns the frontmatter and appends one numbered section per
execution; below the frontmatter the body is the session's to write — plan,
scope, crossed-off subtasks, an explanation of why something turned out to be
impossible. The session is told, in its prompt, that this file is where earlier
slices left their account.

```markdown
---
task: task-9f2c1ab5d4e6470fa1c3b8e7d0925a41
repository: app
origin: conversation
provider: codex
---

Remove the CSV importer and move its two callers to the streaming reader.
...

## 1 — continue · refactor: route reporting through the streaming reader

Moved `reports/weekly.py`. `reports/adhoc.py` still needs the column-order
shim; started it on this branch.
```

**Identity.** A task ID looks like `task-9f2c1ab5d4e6470fa1c3b8e7d0925a41`: the
literal prefix plus 32 hex characters. It is globally unique across every
managed repository, because it is one ref in one namespace. The same string is
the branch name, the worktree directory name and the path segment on the task
board, so it is spelled out rather than opaque — you will type it into `/task`
commands. `archive` and `recurring` are reserved and cannot be task IDs.

## Getting work admitted

### From a conversation

This is the ordinary route. Talk to the steward in a Telegram topic or at the
desk, agree on what the work is, and let its reply end with a single final line:

```text
TASK_PROPOSAL: {"repository":"app","title":"Retire the CSV importer","brief":"Remove the CSV importer and move its two callers to the streaming reader. Keep the existing fixtures passing. If a caller needs behaviour the streaming reader does not have, stop and ask rather than widening the reader's contract."}
```

Three constraints decide whether that becomes a task:

- The marker must be the **last** line of the reply, and there must be exactly
  one marker in it. A turn proposes at most one task.
- `repository` must name a configured repository whose `modes` include
  `core_requested`. Anything else is refused with a visible rejection; a
  conversation cannot grant itself a repository it was not given.
- `repository`, `title` and `brief` are all required and all strings. The title
  is capped at 256 characters, the brief at 8000.

An admitted conversation task is **queued immediately**. There is no
confirmation step for it — `/task confirm` exists for tasks that arrive held as
`proposed`, which today means incident escalations. The conversation that
proposed the task becomes its `owner`, which is where its result comes back.

The brief is the whole durable request. Write it as though the session that
reads it has never seen the conversation, because that is the case: the task
runs in its own worktree with its own provider session, and what it gets is the
brief plus whatever the account file already says. State the outcome you want,
the constraint you care about, and what should happen when the work hits
something you did not anticipate.

### The other routes

| Route | Origin | How it arrives |
| --- | --- | --- |
| Rhythm | `rhythm` | A configured `rhythms:` entry fires and creates a procedure task over an exact input commit |
| Publication or target requirement | `rhythm` | A `requires:` procedure is run against a prepared candidate; its verdict gates the push or the deployment |
| Incident repair | `incident_repair` | A configured pipeline probe stays unhealthy and `incident_policy` still allows a repair |
| Incident escalation | `incident_escalation` | Repeated repair failure; arrives `proposed`, so it waits for `/task confirm` or `/task reject` |
| Trusted task remote | any | `tasks.remote_url` is fetched, and any `refs/heads/tasks/*` it carries becomes an accepted task here |

That last one is the hand-authored route. If you configure

```yaml
tasks:
  remote_url: git@example.com:ops/steward-tasks.git
```

then a `task.md` you commit on `refs/heads/tasks/task-<32 hex>` in that remote
is admitted on the next sync. It is trusted configuration, so treat write
access to that remote as equivalent to operator authority. The fetch is
fast-forward only: it retains local decisions, refuses divergence rather than
guessing whose decision wins, and treats a deleted remote ref as nothing at all
rather than as a cancellation. Product work branches never confer admission —
pushing a `tasks/<id>` branch to an application repository creates no task.

## How a task carries across executions

Work happens in **slices**. One slice is one provider execution holding the
task's lock, in a retained checkout of `tasks/<task-id>`, ending in one commit
the harness makes.

The session ends its final response with three lines:

```text
COMMIT: refactor: route reporting through the streaming reader
DISPOSITION: continue
QUESTION: NONE
```

- `continue` — more work remains on this exact task. It will be picked up again
  on a later pass, with the same branch and, when it survives, the same provider
  session.
- `ask` — it cannot proceed without an answer, which goes in `QUESTION:`. The
  task waits.
- `idle` — finished. Workspace-write work now owes a publication.

If the closure is malformed, the harness keeps the work, commits it, and blocks
the task rather than guessing what the session meant. A slice that crashes or is
killed leaves no half-state: the lock is a `flock`, so it dies with the process,
and the task simply never left the queue.

**What carries.** Everything durable is in Git before the slice is over:

- the work commit, retained in the accepted record's `work` field and kept in
  the accepted graph as a parent, so another machine can restore it;
- the account file, appended to and committed by every slice — including a
  slice that changed no product file;
- the disposition and any blocking question, as `Disposition:` and `Reason:`
  trailers on that commit.

**Answers, notes and retries** are commits on the accepted record carrying a
`Steward-Input:` trailer, and a slice that consumes one records that with a
`Steward-Consumed:` trailer. They behave differently on purpose:

- `/task answer <id> <text>` resumes a waiting task. The next slice receives the
  answer and any pending notes.
- `/task note <id> <text>` records context **without** resuming. A waiting task
  keeps its question. A task that is otherwise finished but carries an
  unconsumed note still owes another slice. While a native session is live, a
  new note is also offered to it on its input channel; acknowledgement there is
  delivery evidence, not proof of consumption, so an unacknowledged note stays
  pending. A completed or cancelled task refuses notes.
- `/task retry <id> [note]` applies only to blocked or cancelled work, and
  clears the hold so the retained branch gets another slice. Incident-owned
  tasks refuse operator retry; their own policy drives them.

**What "ongoing" means here.** A task is dispatchable when it has never run,
when its last slice said `continue`, when it has been answered or retried, or
when it holds unconsumed input. That is the entire mechanism. There is no
"long-running task" object, no scheduled next-run time on a task, and no
per-task budget. A body of work stays ongoing exactly as long as its slices keep
saying `continue` or you keep steering it.

**Across restart.** Nothing needs recovering. Locks vanish with their processes;
the accepted record and the work branch are on disk; an interrupted task is
yielded by the next pass like any other. Two things do not survive a move to
another machine: the native provider session (a new one starts from the durable
record, which is why the record has to be good enough to work from) and anything
left uncommitted or ignored in the retained worktree. If an invocation deadline
is hit while the provider session is still alive, the harness commits whatever is
in the tree and marks the task to resume, so the next tick continues rather than
starting over.

## One-off work versus recurring work

A task is a *concrete* execution of a *definite* request. It does not repeat,
and there is no field that makes it repeat. If what you want is "look at this
every week" or "review whatever changed once things go quiet", do not try to
express it as a task with a standing brief. Use a procedure and a rhythm.

A **procedure** is reusable instructions plus the model and access policy to run
them under. A **rhythm** is the trigger that turns a procedure into a task over
an exact input commit.

```yaml
procedures:
  security-review:
    instructions: /etc/steward/procedures/security-review.md
    provider: codex
    model: {model: gpt-5-codex, effort: high}
    access: read-only

rhythms:
  weekly-review:
    schedule: 604800
    procedure: security-review
    input: repositories/app/main
    owner: telegram:4211

  settled-changes:
    schedule: {quiet: 900}
    procedure: security-review
    input: repositories/app/main
    owner: null
```

The instructions file must live outside every model-writable root — outside the
repositories, outside the world, outside the provider homes — because it is
authority, not content.

### Interval or quiet

`schedule: 604800` is an **interval in seconds**. Time is divided into fixed
buckets of that length and at most one run is accepted per bucket. A moving
input does not fan out extra runs inside a bucket, and an incomplete earlier run
blocks the next one rather than overlapping it. After downtime, only the current
bucket is considered; there is no backlog to work through. Reach for this when
the work is worth doing whether or not anything changed — a weekly audit, a
dependency sweep, a standing report.

`schedule: {quiet: 900}` waits for **quiet** instead. The controller watches the
observed tips of every configured repository's remote branches, the retained
work of every ordinary task, the native branch heads, and the world head. Any
newly observed commit restarts the window; 900 seconds with nothing new admits
one run over exactly that snapshot. No activity means no run at all. The
rhythm's own runs are excluded from the activity it watches, so a review cannot
retrigger itself. The first observation after startup establishes a baseline
without running. Reach for this when the work is *about* what changed — a
review, a reflection pass, a coherence check — and you want it to fire after the
dust settles rather than mid-edit.

The `input:` is always `repositories/<configured name>/<branch>`. The run
captures that branch's current commit as its candidate and the candidate's first
parent as its base, and those exact SHAs are written into the accepted task
record. Evidence binds those inputs: last week's verdict never authorizes a
different candidate.

### What `owner:` does

`owner:` is required on every rhythm — you must write it, even to write `null`.

A configured `telegram:<topic-id>` or `desk:<conversation>` becomes the
protected result owner of every task that rhythm creates. When the run finishes,
asks or fails, its outcome goes through the ordinary task-result assessment path
in that conversation: the owner's session sees the brief and the findings as
*evidence*, can record what matters in the world, can propose a follow-up task
within the repository authority it already has, and returns a concise update
through its transport. It can also decide nothing needs saying and reply
`SILENT`, which retains the evidence and sends nothing. Assessment cannot grant
itself repository access it did not have, and cannot steer tasks owned by
another conversation.

`owner: null` means the run's evidence is retained in its accepted task record
and nothing else happens: no assessment turn, no message, no follow-up
proposal. That is the right choice for a check whose value is that the answer
exists when you go looking, and the wrong choice for anything you expect to be
told about. Requirement-only review tasks — the ones created by `requires:` —
also retain evidence without an owner.

Changing a rhythm's owner affects future runs. A run already accepted keeps the
owner it was created with.

### Read-only versus workspace-write

`access: read-only` means the procedure examines a candidate and produces a
verdict. Its workspace must stay byte-identical to the candidate it was given —
any changed tracked file or new untracked file other than the task's own account
file rejects the evidence. It ends with `VERDICT: PASS` or `VERDICT: FAIL`
alongside the ordinary closure, and it **never publishes product work**.

`access: workspace-write` means the procedure changes the repository, and its
task follows the ordinary publication path below.

Only read-only procedures can be named in `requires:`:

```yaml
repositories:
  app:
    requires: [security-review]     # checked on the integrated candidate, before push
targets:
  app-prod:
    requires: [security-review]     # checked on the full candidate tree, before apply
```

## What publication does

When a workspace-write task closes `idle`, it owes a publication. The publisher
holds the repository's lock and:

1. collapses the task's whole tree into one outcome commit, leaving native
   merges and exploration intact on the task branch;
2. rebases that onto the currently observed default branch, so the final
   candidate has exactly one parent;
3. runs the repository's `gates` against that exact candidate — gates must leave
   it unchanged;
4. runs any `requires:` procedures against that exact candidate and base;
5. retains work, base and candidate in the accepted record, rechecks
   cancellation, and pushes the exact checked SHA with an exact-base lease.

The task is `done` when that candidate is observed in the remote's ancestry, not
when the push returns. A conflict or an actionable gate failure comes back to
the same task as a repair input with the exact SHAs and diagnostics; the
publisher never edits code to make a check pass. If a repair leaves the product
tree unchanged on the same base and still red, the task blocks and waits for a
decision rather than burning attempts.

**A findings-only task still publishes its account.** Every valid slice appends
its section to `tasks/<task-id>.md` and commits, so a task that changed no
product file at all still has a commit ahead of the base. That commit goes
through gates and lands on your default branch. The reason is that the account
*is* the product of an investigation, and a record that only exists in
controller-private storage is a record nobody on the team can read. The
consequence is worth stating plainly: if every push to your default branch
starts an expensive deployment, an investigation will start one too. Know that
before you onboard such a repository.

Read-only procedure runs are the exception — they retain their evidence and
verdict in the accepted record and never become a publication candidate.

Publication and deployment are separate. Landing a commit does not converge a
target; a configured target follows a ref on its own schedule and reports what
the external system actually serves.

## The controls you have

| Command | What it does |
| --- | --- |
| `/tasks` | List up to 20 tasks with their current status |
| `/task show <id>` | Status, reason, recent checkpoints, pending input, brief |
| `/task answer <id> <text>` | Answer a waiting task; queues its next slice |
| `/task note <id> <text>` | Retain context without resuming; offered live to an active session |
| `/task retry <id> [note]` | Re-queue blocked or cancelled work on its retained branch |
| `/task cancel <id> [more ids] :: [reason]` | Withdraw work; the reason applies to every ID before `::` |
| `/task priority <id> <n>` | Change priority (`-100`–`100`) on inactive, unfinished work |
| `/task confirm <id>` / `/task reject <id> [reason]` | Admit or refuse a task that arrived `proposed` |
| `/task model <id> [fast\|balanced\|deep]` | Inspect or change that task's profile |
| `/task model_family <id> [provider]` | Inspect or switch that task's provider |
| `/rhythm list` | Configured rhythms: schedule, procedure, input, owner, accepted-run count, pause state |
| `/rhythm run <name>` | Request one extra run of that rhythm's procedure over the current input |
| `/git retarget <id> <repo>` | Move never-started work to another managed repository |
| `/git reconcile <repo>` | Publish whatever that repository owes, now |
| `/git target <name>` | Converge a configured target and report the external observation |
| `/pause`, `/resume` | Stop or resume taking on new work |

`/rhythm` accepts `list` and `run <name>`. Schedules are edited in controller
configuration; there is no second persisted override to drift from the YAML.

Cancellation is durable and a live turn is signalled, but the two are different
actions: stopping a running provider is a signal to a process, while withdrawing
a task is a commit. The withdrawal is checked under the decision lease
immediately before the push, so a cancellation committed first prevents the
push, and a push already underway may land. Work that reached the remote is
reported as landed, not relabelled cancelled.

`/pause` is a filter on what the steward takes on — new task slices, probes,
rhythm admission, desk intake. Repository convergence and result assessment keep
running, because a repository that owes a publication does not stop owing it. It
is not a quiescence barrier; do not use it as one before an upgrade.

## What this will not do for you

Be clear about these before you design a body of work around the harness.

- **No schedules beyond interval and quiet.** No calendar, no cron expression,
  no "first Monday", no time-of-day. Those are not implemented.
- **No dependencies between tasks.** No subtasks, no ordering, no "when this
  lands, start that". A task is admitted, runs and ends. If work has stages,
  either write the stages into one brief and let the slices carry it, or let the
  owning conversation propose the next task when it assesses the last result.
- **No time or money budget.** `provider.timeout_seconds` bounds a single
  invocation, not a task. A task that keeps closing `continue` keeps costing.
  Nothing here will stop a provider from being expensive.
- **No fairness scheduler.** `controller.workers` (default 8) is one shared
  budget for everything the steward schedules for itself. `priority` orders the
  queue; it reserves nothing and preempts nothing.
- **The publisher pushes to `default_branch`.** It does not open pull requests,
  cut tags or call a deployment API. If your release path requires a PR, that
  integration is still yours to own — and relaxing branch protection to make
  onboarding go green is not the fix.
- **Publication is not deployment.** Target convergence is independent, and a
  driver reporting success is not the same fact as the external system serving
  the revision.
- **Cancelling does not interrupt a running gate.** A withdrawn task can wait for
  a gate to finish before the publisher observes the withdrawal.
- **Findings are public.** The task brief and every slice's findings travel to
  the product repository with the work. Assume everyone who can read that
  repository reads them. Private routing and provider session tokens stay in the
  controller's record and are never rendered into product files.
- **One controller.** The accepted store is single-writer. A second live
  controller against a replicated store is not supported; moving hosts means
  fencing the old one first.
- **Notes cannot reach a finished task.** Once a task is done or cancelled, record
  the observation in the world and propose a fresh task if there is new work.

## Where to go next

- [Execution lifecycle](execution-lifecycle.md) — closure syntax, status
  precedence, continuation and cancellation in detail.
- [Rhythms invoke procedures](rhythms-direction.md) — the procedure/rhythm/target
  roles and their evidence boundaries.
- [Kernel contract](kernel-contract.md) — who owns what authority, and what is
  deliberately absent.
- [Automatic deployment](automatic-deployment.md) — targets, drivers and the
  quiet-period and interval semantics in full.
- [Task board](task-mini-app.md) — the read-only browsing surface over the same
  facts.
