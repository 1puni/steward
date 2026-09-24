# Read the task file and Git history

The agent reads its `tasks/<task-id>.md` account and uses `git log` and
`git show` in its checkout for provenance and earlier work. Repository and
world README files point to their existing documentation and episodes.

An admitted procedure's frozen instructions are included once in its task
file. Changing the configured policy after admission does not replace them.
The prompt carries the task identity, task-file path, attributed task input
and execution protocol; it does not repeat the account or procedure text.

There is no generated history map, copied Git log, task-state snapshot or
provenance export directory. Git remains the history. The controller retains
its private admission, credential and execution boundaries; a Git publication
alone is not proof of live deployment.

Pending inputs derive their source from the accepted input commit's
`Steward-Source` trailer. Direct operator commands, accepted assistant actions
and controller query observations remain distinct in both startup context and
live native input. Missing historical provenance is unverified. New account
sections retain the source beside the input; consumption does not erase it.
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
