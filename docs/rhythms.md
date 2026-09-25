# Rhythms invoke procedures

A rhythm is a clock with no opinions. It says *when*; a procedure says *what*; a task
is the one concrete run that actually happened. None of them is a workflow engine.

A procedure is accepted instructions plus model and access settings. A task is a
concrete execution over captured inputs. A rhythm supplies a recurring trigger.
A target follows a ref and can require accepted procedure evidence; a gate enforces
that requirement. These roles share `Cognition` and ordinary task execution.

```yaml
procedures:
  review:
    instructions: /etc/steward/procedures/security-review.md
    provider: codex
    model: {model: configured-review-model}
    access: read-only
rhythms:
  weekly-review:
    schedule: 604800
    procedure: review
    input: repositories/app/main
    owner: telegram:3  # A configured topic's id, not its name; null retains evidence only.
```

`owner` names a topic by the **id** it maps to in `telegram.topics`, not by the
name it maps from. `telegram:3` above assumes an entry such as `steward: 3`;
configuration is refused if no configured topic carries that id. Copying an
example's number into an instance whose topics use different ids is the usual way
this fails.

An integer schedule uses interval seconds. At most one run is accepted for a
rhythm in each interval bucket, and an incomplete run prevents overlap with later
buckets. A reopened idle run, a blocked or waiting run, and workspace-write work still
awaiting publication remain incomplete.
Cancellation permits a future bucket once its live execution has stopped; it does
not create a replacement in the cancelled run's own bucket. A moving input does not
create another run within the same bucket. After
downtime, only the current interval is considered. `/rhythm run <name>` is an
explicit additional request, while `/rhythm list` shows configured schedules,
accepted-run counts (including no recorded run) and admission pause state. Edit
schedules in controller configuration; there is no second persisted override.

For Light-style reflection, use `schedule: {quiet: 300}`: newly observed commits
across configured repositories and the Git world reset a five-minute quiet window.
No new activity means no run. Captured inputs in accepted task Git prevent repeat
admission of a consumed batch, including after restart. The first observation on
a controller with no accepted batch establishes a baseline. Read the complete
[quiet-period contract](automatic-deployment.md#git-quiet-periods-and-intervals)
for native work, self-reflection exclusions and restart semantics. The harness ships
a generic
[reflection skill](../src/steward_harness/skills/steward-reflection/SKILL.md);
the procedure that names when and how an instance reflects belongs to that instance.

Organisation-wide read-only rhythms can set `workdir: /srv/organisation` on the
rhythm. Provision that directory with the managed repositories as siblings and
an entry README; existing checkouts can be linked into it. Native cognition starts
there, while its task account and checkpoint remain in a separate retained task
worktree. The accepted run captures the directory so retries keep the same scope.
Before cognition the existing credential-free Git transfer refreshes observed
remote refs in the configured repositories without moving their HEADs or touching
local changes. Manual `/rhythm run` uses the same working directory.
Workspace-write procedures cannot select an external working directory.

Quiet snapshots remain controller-owned admission metadata in accepted Git. They
are not copied into the task brief or prompt. The procedure discovers relevant
changes from repository files and Git history. An organisation-root reflection
returns findings, not a PASS/FAIL verdict about its anchor commit. Missing policy
files or rejected admissions are logged without stopping other rhythms or operator
ingress, and failed admission does not consume the activity batch.

Each run captures exact input commits, instruction text, execution settings and a
configuration digest in its accepted Git task document. Its native session is a
local convenience; another machine can execute from accepted Git alone. A
read-only run retains findings and its accepted verdict without publishing a
product change. A workspace-write procedure follows the ordinary task publication
path. Agents may revise the whole task understanding; they cannot rewrite the
protected procedure binding or configured publication/target requirements.

Every rhythm explicitly declares `owner`. A configured `telegram:<topic>` or
`desk:<conversation>` retains that protected result owner in each accepted task.
Completion, questions and failures then enter the existing task-result assessment
path: the owner's native conversation can commit world knowledge, propose useful
follow-up tasks within configured repository authority, and return a concise
result through its configured transport. Assessment cannot grant itself new
repository access. Durable delivery receipts prevent a transport retry from
repeating accepted world edits or follow-up admission.

`owner: null` deliberately retains the task's evidence without assessment or
notification. Requirement-only review tasks also retain evidence without an owner.
Read-only results identify their evidence commit and reviewed candidate; they never
claim that the evidence commit landed on the product branch. An owner's native
session is optional and can be reconstructed on a fresh controller. Changing the
configured owner affects future runs; an already accepted bucket keeps its owner.

There are no built-in light, sleep or REM rhythms and no seeded world files; the
harness never invents a schedule for you. Calendar and dependency schedules are not
implemented, and there is no hook system or workflow graph.

Publication requirements check the final integrated candidate before it can be
pushed. Target requirements review the complete candidate tree before application
and before satisfaction is reported. Evidence binds exact inputs and accepted
procedure configuration; an old weekly result never authorizes a different
candidate. Any number of independently configured reviewers uses the same path.

See [procedure construction](../src/steward_harness/procedures.py),
[task execution](../src/steward_harness/task_runner.py),
[convergence journeys](../tests/test_rewrite_convergence.py), and the
[target contract](automatic-deployment.md).
