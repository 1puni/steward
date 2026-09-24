---
name: commit
description: Preserve cohesive local Git progress during a steward task, before long operations, or when asked to commit. A local commit does not publish or deploy work.
---

Commit useful increments as work becomes ready, rather than waiting for the
execution to end. Inspect the diff and stage the files belonging to that increment.
Use a concise subject describing the change. Keep unrelated edits available.

Keep the assigned branch and preserve existing commits, including reconciliation
merges. Do not reset history to a task's starting revision. If committer identity
is absent, use command-local identity:

```sh
git -c user.name=Steward -c user.email=steward@localhost -c commit.gpgsign=false commit -m "<subject>"
```

Leave a cleanly finished Git operation. Findings or questions without file changes
do not need an empty commit. The harness autosaves remaining edits after execution;
an autosave is preserved work, not evidence that the task is finished or tested.
Publication and deployment follow the controller's gates and accepted disposition.
