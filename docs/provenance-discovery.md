# Read the task account and Git history

The prompt carries a task's identity, its accepted account (the `task.md` body,
which starts as the brief), attributed pending input and the execution protocol.
Everything else the agent finds for itself. Earlier slices' findings are the
messages of the work branch's commits, so `git log` and `git show` in its checkout
are the history. Repository and world README files point to the documentation
that already exists.

An admitted procedure's instructions are frozen into its accepted task document
once, at admission. Changing the configured policy afterwards does not rewrite a
run that already exists.

There is no generated history map, copied Git log, task-state snapshot or
provenance export directory. Git is the history. Copying it into the prompt would
make a second history that is already stale by the time the model reads it.

Pending inputs derive their source from the accepted input commit's
`Steward-Source` trailer. Direct operator commands, accepted assistant actions
and controller query observations remain distinct in both startup context and
live native input. Missing provenance is unverified, not assumed. Consuming an
input does not erase where it came from.
An assistant answer to one task is evidence of that answer, not a standing
operator grant to other tasks.

## Current ownership on request

Historical Git evidence cannot prove who owns work now. A task that needs this
read closes an ordinary slice with `DISPOSITION: ask` and a question such as
`QUESTION: TASK_QUERY: {"repository":"app","text":"consumer"}`. The controller
answers through the existing accepted task input and resumes that same task.
A crash after the retained question leaves a waiting task whose next dispatch
supplies the answer; no new query task, read server or snapshot directory exists.

The configured repository set bounds the read. Results include up to ten accepted
unfinished tasks whose titles contain every requested word, their observed status,
accepted revision, and same-conversation/another-conversation/unowned labels.
They omit task bodies and private owner identities. Empty text reads the first ten
unfinished tasks in that repository. Truncation is explicit; no matching title does
not prove no differently titled task owns the finding. The timestamp describes the
read, not a lease. Subsequent task actions still cross ordinary ownership and
repository-authority checks. Model output and peer titles remain evidence, not
instructions or grants. Ordinary questions still wait for an operator answer.

This interface is available to task cognition (including reflection); conversation
cognition can request a bounded investigation through its existing proposal path.
It does not inject live peer state into every prompt.
