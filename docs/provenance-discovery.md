# Read the task account and Git history

The prompt carries a task's identity, its accepted account (the `task.md` body,
which starts as the brief), attributed pending input and the execution protocol.
Everything else the agent finds for itself. Earlier slices' findings are the
messages of the work branch's commits, so `git log` and `git show` in its checkout
are the history. Repository and world README files point to the documentation
that already exists.

An admitted procedure's instructions are frozen into its accepted task document
once, at admission, and their text is hashed into the task's identity. Changing
the configured policy afterwards does not rewrite a run that already exists.

There is no generated history map, copied Git log, task-state snapshot or
provenance export directory. Git is the history. Copying it into the prompt would
make a second history that is already stale by the time the model reads it.

Pending inputs derive their source from the accepted input commit's
`Steward-Source` trailer. Direct operator commands, accepted assistant actions
and controller query observations remain distinct in both startup context and
live native input. Missing provenance is unverified, not assumed: it is never
inferred from prose and never backfilled. Consuming an input does not erase where
it came from.
An assistant answer to one task is evidence of that answer, not a standing
operator grant to other tasks. The speaker and the scope travel with every
decision that gets reused.

## Observed source and retained findings

Before an organisation rhythm thinks, the controller refreshes
every configured clone's `refs/steward/remote/<branch>`. Those refs are
convenience labels for remote commits the controller observed. They grant no
acceptance, publication or deployment authority. Working HEADs, local changes and
`origin/<branch>` are left alone, and can be old. Read source at the observed
ref, record the resolved SHA and when you observed it, and follow that revision's
README and docs map:

```sh
git -C app show -s --format=%H refs/steward/remote/main
git -C app show refs/steward/remote/main:README.md
```

Substitute the configured default branch for `main`. A read-only procedure's
findings can be kept in task-ref commit messages without being published to the
product's main branch:

```sh
git -C app log --all --decorate --oneline
git -C app show -s --format=full <evidence-commit>
```

These readable refs carry findings and provenance. Controller-private accepted
task Git, current locks and result-delivery receipts each have their own role.
Work missing from product main, or a private store you cannot read, does not
mean the work was lost or unaccepted. An earlier finding does not establish the
current source or live behaviour either: observe installed releases and public
responses again when they matter. Report missing delivery evidence as
unverified delivery, not as a failure.

## Current ownership on request

Historical Git evidence cannot prove who owns work now. An ordinary task calls
the native `steward_tasks` task tool with `operation="query"`, `repository="app"`
and `text="consumer"`. The bounded observation returns during the same execution
through the [existing task-call bridge](git-native-tasks.md#ownership-reads-during-task-execution).
Task execution cannot submit or steer peer work. It can also notify its bound
owner and declare its own closure intent. Historical query-shaped questions are
ordinary questions; there is no final-prose query fallback. No query task,
parallel registry or snapshot directory is created.

The configured repository set bounds the read. Results include up to ten accepted
unfinished tasks whose titles contain every requested word, their observed status,
accepted revision, and same-conversation/another-conversation/unowned labels.
They omit task bodies, completed results and private owner identities. Empty text reads the first ten
unfinished tasks in that repository. Truncation is explicit; no matching title does
not prove no differently titled task owns the finding. The timestamp describes the
read, not a lease. Subsequent task actions still cross ordinary ownership and
repository-authority checks. Model output and peer titles remain evidence, not
instructions or grants. Ordinary questions still wait for an operator answer.

Task cognition uses the live query; reflection procedures retain the closure
route. Conversation cognition can inspect its own work through live `list` and
`show` calls. No route injects peer state into every prompt.
