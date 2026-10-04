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

A model call needs new input; a clock tick is not input. A procedure rhythm is
admitted only when its input holds a commit that none of its finished runs
captured. The input follows what the rhythm reads. A repository rhythm reads its
`input` ref, so its input is that ref's candidate commit. An organisation rhythm
(`workdir`, below) reads across everything, so its input is also every observed
`refs/steward/remote/*` head of every configured repository and the accepted
work commit of every ordinary task. Accepted runs already record exactly these
commits (`candidate`, and `activity` for an organisation run), so the rhythm's
own finished runs are the cursor; there is no second record of what it has seen.
Deleting a branch or naming an already seen commit again is not new input.

Admission reads observed refs; it is not a second fetcher. The pass that asks
runs every few seconds, and fetching every repository on each one kept an idle
controller busy doing network round trips that found nothing. A repository
some configured target follows is fetched by that target's own observation,
at least once a minute, and a rhythm reads its refs as that fetch left them. The
rhythm's `input` repository, and every repository no target follows, are
fetched once, on the rhythm's first observation in each bucket: its interval,
or for a quiet schedule its quiet period. An idle bucket then costs local ref
reads and no network. The price is latency for input nothing else fetches: a
commit on such a repository is seen at the next bucket, never missed. The
input repository's once-per-bucket fetch also keeps a rhythm current when its
input's target is stopped or failing. `/rhythm run` still fetches everything
the rhythm reads before it captures input.

Reading everything is not the same as being for everything. An organisation
rhythm can set `paths` to the activity it exists to follow, as prefixes of its
activity keys, `repositories/<name>/<branch>` and `tasks/<id>`:

```yaml
rhythms:
  launch-reflection:
    workdir: /srv/organisation
    schedule: 10800
    procedure: launch-reflection
    input: repositories/company/main
    paths: [repositories/landing/main, repositories/app/main]
    owner: telegram:3
```

Its runs then capture only those keys, and only a new commit on those lines
admits a run. The `input` candidate is still recorded, but it wakes the rhythm
only when a path names it: the paths say what the rhythm is for, and its input is
where it works, not what it waits on. The procedure can
still read every repository and task; the paths decide what wakes it, not what
it may see. Before admission only the input repository and the repositories
the paths name are fetched. A launch reflection is for launch progress, so a harness redeploy or
a repair task's work in progress should not call a model. `repositories/app/`
follows every branch of `app`, and `tasks/` every ordinary task's work.
Configuration refuses a prefix that names neither `tasks/` nor a configured
repository, because it would match nothing and silence the rhythm.

The steward's own bookkeeping cannot appear in that input. Task documents,
their `steward: accept task` commits and holds live in the controller's task
store, never in a repository remote. Procedure runs, a rhythm's own included,
are excluded from task work, so a run's evidence can never make its successor
fire. What does count is a real change to what the rhythm reads: a product
task's outcome landing on the input branch, or native work retained on an
ordinary task, is new input even though the steward made it.

For repository and organisation procedure rhythms, an integer schedule uses
interval seconds. At most one run is accepted for a rhythm in each interval bucket, on the first poll in that bucket that sees new
input; a bucket without new input admits nothing and calls no model. An
incomplete run prevents overlap with later buckets. A reopened idle run, a
waiting run, and workspace-write work still awaiting publication remain
incomplete. Only a finished run covers its input. A blocked run reported
nothing, so the next bucket supersedes it even on unchanged input; a cancelled
run likewise permits a future bucket once its live execution has stopped, but
not a replacement in its own bucket. After downtime, only the current interval
is considered. `/rhythm run <name>` is an explicit additional request on the
current input, while `/rhythm list` shows configured schedules, accepted-run
counts (including no recorded run) and admission pause state. Edit schedules in
controller configuration; there is no second persisted override.

For Light-style reflection, use `schedule: {quiet: 300}`: the rhythm waits until
its new input has stopped moving for five minutes, then admits one run over
exactly that input. No new input means no run, including after restart; a
restart only re-arms the full quiet period for input still unseen. Read the complete
[quiet-period contract](automatic-deployment.md#git-quiet-periods-and-intervals)
for its timing and failure semantics. The harness ships a generic
[reflection skill](../src/steward_harness/skills/steward-reflection/SKILL.md);
the procedure that names when and how an instance reflects belongs to that instance.

Organisation-wide rhythms can set `workdir: /srv/organisation` on the rhythm.
Provision that directory with the managed repositories as siblings and an entry
README; existing checkouts can be linked into it. A reviewing rhythm's native
cognition starts there, while its task account and checkpoint remain in a
separate retained task worktree. The accepted run captures the directory so retries keep the same scope.
Before cognition the existing credential-free Git transfer refreshes observed
remote refs in the configured repositories, as `refs/steward/remote/<branch>`,
without moving their HEADs, changing `origin/<branch>` or touching local changes.
Those refs are observations, not new authority: read source at the configured
default branch under that namespace. See
[source and retained findings](provenance-discovery.md#observed-source-and-retained-findings).
Manual `/rhythm run` uses the same working directory.

An organisation rhythm whose procedure is `workspace-write` consolidates instead
of only reporting. It reads the organisation from `workdir` exactly as above, but
works in its `input` repository's task worktree, so what it writes there is
ordinary task work: gated, published to the input branch and landed like any
task's. A company-knowledge repository is the natural input: the rhythm keeps it
current as the products move, rather than retaining the same facts as evidence
nobody publishes. A world rhythm takes no `workdir`.

Four things follow from its landing where it works:

- Its landings move its own input repository, so configuration requires `paths`
  that leave that repository out. Otherwise every landing would wake it again.
- Only its worktree publishes. Like any writable execution it holds the
  [agent's writable directories](execution-boundary.md), sibling checkouts
  included, so its prompt names the worktree and says nothing beneath the
  organisation root publishes.
- A writable run keeps its [native session records](native-record-provenance.md)
  in its worktree, so every run writes one. That record alone is not a change:
  a run whose only difference is its own provider's session state lands nothing,
  and the record stays with the run on its task ref. A run that changed a real
  file publishes its session record with its work, as provenance.
- A blocked run is superseded at the next admission, like any rhythm run, and its
  unpublished work is dropped with it. The next run reads everything again.

A release that admits a writing organisation rhythm is a rollback floor. Older
releases refuse its task documents, and one unreadable document stops the
controller. Roll back below it only after removing those runs' task refs.

An organisation run's captured `activity` remains controller-owned admission
metadata in accepted Git. It is not copied into the task brief or prompt. The
procedure discovers relevant changes from repository files and Git history. An
organisation-root reflection returns findings, not a PASS/FAIL verdict about its
anchor commit. Missing policy files or rejected admissions are logged without
stopping other rhythms or operator ingress, and failed admission consumes no input.

Each run captures exact input commits, instruction text, execution settings and a
configuration digest in its accepted Git task document. Its native session is a
local convenience; another machine can execute from accepted Git alone. A
read-only run retains findings and its accepted verdict without publishing a
product change. A workspace-write procedure follows the ordinary task publication
path. Agents may revise the whole task understanding; they cannot rewrite the
protected procedure binding or configured publication/target requirements.

Every rhythm explicitly declares `owner`. A configured `telegram:<topic>` or
`desk:<conversation>` retains that protected result owner in each accepted task.
Reportable completion, questions and failures enter the retained-result transport
path independently of cognition. Later optional assessment in the owner's native
conversation can commit world knowledge and propose useful follow-up tasks within
configured repository authority. Assessment cannot grant itself new
repository access. Durable delivery receipts prevent a transport retry from
repeating accepted world edits or follow-up admission.

A finished rhythm run queues a message only through a native `notify` call ([what a rhythm sends](#what-a-rhythm-sends)). The shared turn prompt supplies that tool
contract once; the conversation runner supplies the capability. Procedures say
when a notification is useful, without repeating the tool instructions. Instance
procedures may point to world-owned conduct instead of copying its authority and
workflow into a second editable home.
Otherwise the result lane never selects it: no assessment turn, no model call,
no message. What it did is kept either way. A review's findings are its evidence
commit, retained on the task ref. A writing run's work publishes like any task's,
because consolidation is the run's job and notification is a separate choice.
A writing run that changed no file lands nothing, whatever its findings say:
what it saw is already on its input branch, so it is done at that candidate
without an empty commit, and its findings stay on its task ref as a review's do.
Its result says it changed no files. A question or a blocked run still enters
assessment as above.

`owner: null` deliberately retains the task's evidence without assessment or
notification. Requirement-only review tasks also retain evidence without an owner.
Read-only results identify their evidence commit and reviewed candidate; they never
claim that the evidence commit landed on the product branch. An owner's native
session is optional and can be reconstructed on a fresh controller. Changing the
configured owner affects future runs; an already accepted bucket keeps its owner.

## Model preference

A procedure's `provider` and `model` (or its [ordered model list](#ordered-model-list)) are a preference: this provider, running
this model at this effort. `effort` is part of the choice and reaches Codex as
its reasoning effort and Claude as `--effort`; leave it out and the provider
decides. If the preferred provider cannot take the run, for whatever reason, the
run falls back through `provider.family_order`, and each fallback runs its own
configured model for the profile, never the preferred model under another
provider's name. Conversations and ordinary tasks work the same way, led by
their own selected provider and profile.

When a procedure needs exactly that model, say so:

```yaml
procedures:
  audit:
    instructions: /etc/steward/procedures/audit.md
    provider: claude
    model: {model: configured-audit-model, effort: high}
    fallback: false
```

A pinned run tries its provider and nothing else. When that provider cannot
take it, the run fails like any other unavailable provider: a task blocks and a
world rhythm holds its interval for explicit continuation.

### Ordered model list

A procedure may instead carry an ordered `models` list, each entry a provider,
its model and its effort:

```yaml
procedures:
  light-review:
    instructions: /etc/steward/procedures/light-review.md
    models:
      - {provider: codex, model: configured-model-a, effort: high}
      - {provider: claude, model: configured-model-b, effort: medium}
      - {provider: glm, model: configured-model-c}
```

The list is walked in order until an entry takes the run. An entry is passed over
for any reason the provider cannot take it: not installed or unavailable, a usage
limit, or a refusal of the turn. An entry runs its own provider, model and effort
exactly and never borrows another entry's model; a missing `effort` means that provider decides. Effort reaches
Codex as reasoning effort and Claude as `--effort`, as for a single preference.
When every entry has declined, the run fails like any unavailable provider,
naming each entry's reason: a task blocks, a world rhythm holds its interval for explicit continuation.
Nothing else changes: the change guard, blocked-run supersession and read-only
rules apply as before.

Precedence and validation, all at config load:

- `models` replaces `provider` and `model`; naming both is refused, and one of
  the two forms is required.
- The list is the whole order. `provider.family_order` is not appended, so a
  provider absent from the list is never tried.
- `fallback: false` keeps only the first entry: a pin to that entry.
- The list must be non-empty, name each provider once, and name only providers
  in `provider.family_order` (the provisioned ones). Effort must be one of
  `low`, `medium`, `high`, `xhigh`, `max`; the vocabulary is shared, and a
  gateway family such as `glm` is not promised to accept it.
- Without `models`, behaviour is exactly the single preference above.

### Bounds

A procedure run is bounded by its work, not by how long it takes:

```yaml
procedures:
  sleep:
    instructions: /etc/steward/procedures/sleep.md
    provider: claude
    model: {model: configured-night-model}
    token_budget: 250000
    timeout_seconds: 7200
```

`token_budget` counts output tokens, reasoning included, as the provider reports
them: Claude (and `glm` through the same CLI) per finished message, so a run can
pass its budget by at most one response; Codex as running totals for the turn's
thread and its native children. At the budget the run is stopped exactly as a
deadline stops it: a native interrupt, then containment. The run fails with
`provider used N output tokens of its B budget`. A world rhythm holds its
interval and its uncommitted work stays in the rhythm's retained workspace; it
is not offered to a fallback provider. Unset, a run is unmetered, and Claude is
not asked for the partial messages metering needs.

`timeout_seconds` replaces `provider.timeout_seconds` for this procedure only.
With a budget, it only has to catch a provider that has stalled, so it can be far
longer than the conversational deadline. Without either, a procedure keeps
`provider.timeout_seconds`.

## World rhythms

A rhythm over `input: world` consolidates the world itself, such as a nightly
sleep over accumulated episodes. It is not a task. Each captured interval is one obligation, with ordinary
world-turn attempts in the rhythm's own conversation, `rhythm:<name>`, taking the same
lease, checkpoint and acceptance as an operator's message. Its text is the
procedure's instructions, and it runs on the procedure's
[model preference](#model-preference). Each interval starts a fresh native
session; the world, not the previous session, carries what earlier runs
consolidated.

Consolidating means rewriting. A world rhythm's edits and its reply are accepted
as one world commit, so its prompt says so: the files state what is true now,
and anything earlier, including who changed what and why, is in `git log` and
that commit's reply. Told only to replace obsolete claims, a nightly sleep kept
appending dated sections and correction notes, and its world grew by the night.
A procedure that asks for in-file history or attribution works against this.

```yaml
procedures:
  sleep:
    instructions: /etc/steward/procedures/sleep.md
    provider: codex
    model: {model: configured-sleep-model}
    access: workspace-write
rhythms:
  sleep:
    schedule: 86400
    procedure: sleep
    input: world
    owner: telegram:3
```

A world rhythm needs a configured `world`, a `workspace-write` procedure and an
integer interval or an `after` (below); configuration refuses anything else,
including a `workdir`.
The key `rhythm:<name>:<interval index>` identifies one logical obligation and
its result receipt. Its first attempt uses that source key; an explicit
continuation uses `<key>:continue:<previous turn id>`. Every attempt retains its
own input, outcome and native evidence. Only accepted completion meets the
obligation. A returned provider failure, timeout or cancellation leaves an
interrupted attempt and a visible hold; it does not settle the interval.

`/rhythm run <name>` durably queues one continuation with the original instruction text and
the retained workspace, naming the previous attempt and directing the provider
to reconcile possible external effects before doing remaining work. Routing and
limits use current procedure configuration so an operator can repair unavailable
providers or insufficient bounds. If the policy file could not be read at all,
its text is first captured when a continuation can read it. The command does no provider work: the ordinary rhythm worker executes the
reserved source within the shared budget, and duplicate requests find that same
source. A restart preserves a queued source whose checkout was never claimed.
Each explicit request permits one attempt; polling and restarts never authorize provider
retries. A held interval prevents newer intervals of that rhythm from replacing
it, while unrelated rhythms remain eligible. Only a later *accepted* interval
settles an older interrupted one, because a procedure reads from its own cursor
and that run already covered the gap; a dependent owes nothing for a settled
predecessor interval. Without this, gg's inbox and night chain stopped on
interruptions days older than their latest accepted runs (October 3, 2026). A continuation can recover an
already accepted notification by replaying its original `source_id`, key and
text through the notification tool; that returns the existing receipt without
queuing another send. Changed text is a different intent and cannot overwrite
that receipt.

An uncertain crash that retained no provider completion remains fenced for
inspection of native evidence. `/rhythm run` cannot override that custody.
Retained completion and accepted turns recover through the existing acceptance
boundary without another provider call, including when the result receipt was
not saved. Accepted obligations cannot be run again. Clock intervals never
captured by an attempt or predecessor are not backfilled. `86400` fires once per UTC day
on the first poll after midnight UTC; `offset: 3600` moves the start of every
interval an hour later, so the same rhythm fires on the first poll after 01:00
UTC. The offset must be shorter than the interval, and it works the same way
for a procedure rhythm's interval.

A world rhythm without `paths` runs every interval. With `paths`, a model call
needs new input there, as it does for a procedure rhythm:

```yaml
rhythms:
  staging:
    schedule: 3600
    procedure: staging
    input: world
    paths: [episodes/]
    owner: null
```

The rhythm is admitted on the first poll in an interval that finds the world
changed under those paths since its last accepted run, and a poll that finds
nothing calls no model and records nothing. The rhythm's last accepted turn is
the cursor, and nothing else is: its captured candidate, the world as that turn
left it, is compared with the accepted world by `git diff`. The comparison
starts from the candidate rather than the turn's base, so whatever the rhythm
wrote under its own paths is on both sides and never makes it fire again, while
anything another turn wrote after its base still counts. With no accepted run
yet, it runs. Paths are relative to the world root.

Only something new to read counts: a file added or modified under the paths.
A deletion, a move out of the paths and a move within them are not input, so
Sleep archiving `episodes/<day>.md` into `episodes/archive/` does not wake a
Staging gated on `episodes/`, while an episode appended to after archiving
does. The comparison is `git diff --find-renames --diff-filter=AMT`; a move
that also rewrites most of a file is no longer a rename and counts as new.

A world rhythm can follow another instead of keeping a clock:

```yaml
rhythms:
  sleep: {schedule: 86400, offset: 3600, procedure: sleep, input: world, owner: telegram:3}
  rem: {after: sleep, procedure: rem, input: world, owner: telegram:3}
  dream-away: {after: rem, procedure: dream-away, input: world, owner: telegram:3}
```

`after` takes the place of `schedule`. The dependent shares its predecessor's
captured interval index and waits until that interval has an accepted attempt.
An interrupted predecessor holds the chain. When explicit continuation finally
succeeds, the dependent becomes eligible for the original interval even after
clock rollover or a controller restart. REM then wakes Dream Away in the same
way. A predecessor excluded by its `paths` gate captures no obligation and
wakes no dependent.
`after` must name a configured world rhythm, and configuration refuses a cycle.

World rhythms run one at a time because they all write the same world. The
controller schedules them as a single owner within its shared worker budget.
When that owner runs, it chooses the eligible captured interval with the earliest
start; configured order breaks ties, including members of a night chain. This
keeps a short-interval inbox from continually jumping ahead of due hourly or
nightly work. Selection is recomputed when the worker starts, so time spent in
the shared queue cannot capture an expired, previously unseen bucket. Existing
obligations survive that rollover. There is no catch-up queue or second
scheduling cursor. A rhythm can still miss an uncaptured interval if the shared
worker budget or world writer remains occupied through its end. Manual
continuation admission takes the same world-rhythm lease as automatic execution;
a busy owner defers the request without creating another source.

Failures before cognition, including an unreadable policy file, are retained as
interrupted attempts and hold the interval just like provider failures. Ordinary
world/conversation lease deferrals remain eligible for retry. A completed turn
without its result receipt is eligible for replay to finish that receipt, not a
second model invocation.

`/rhythm list` reports the current world admission state and observation time.
The existing `/healthz` endpoint also carries `world_rhythms`: the controller's
last admission observation, its age and freshness, pause state, shared worker
pressure, and each world rhythm's effective interval/offset or predecessor,
input paths, logical obligation and latest attempt source keys, source
admission/completion times and evidence age. A queued continuation's admission
time precedes its provider execution. The snapshot derives from the same evaluator as dispatch; it
never controls scheduling. It becomes stale after the greater of 60 seconds
and three controller polls. A missing sample is unknown, not healthy.

`scheduled` means a result receipt settled this interval; `active` means a turn
is running, and `receipt_pending` means acceptance needs its receipt completed.
`continuation_queued` names an authorized source awaiting its worker;
`acceptance_pending` names retained completion awaiting world acceptance.
`held` and `predecessor_held` expose interrupted obligations awaiting intervention.
`recovery_held` means a source still owns uncertain work but no rhythm writer
holds the execution lease; inspect its retained evidence before proceeding.
Offline observations cannot establish that liveness distinction. Meanwhile,
`awaiting_input` and `awaiting_predecessor` explain why admission owes no run.
`due`/`overdue` means eligible now with no current turn. `due_at` is the interval
boundary, and `overdue_seconds` is its age, not proof of continuous eligibility:
new input can arrive partway through a bucket. Neither an old completion nor
absent history alone establishes missed execution. An offline observer unable
to read the path guard or receipts says `input_unobserved` or
`accepted_receipt_unobserved` instead of inventing a due/settled verdict.

Health `ok` and `sha` still attest only the loaded release. They do not certify
scheduler freshness, successful firing, or the quality of a reflection. Verify
release identity, current admission state, and accepted execution separately.

The turn cannot propose or steer tasks, because a rhythm has no transport to
receive their results; it records suggested work in world files instead.
`/rhythm list` shows both the current clock interval and the selected obligation
key, which may be older. `/rhythm run` explicitly continues a held obligation or
recovers retained completion; it refuses an extra accepted run. Admission pause
also prevents manual continuation.

## What a rhythm sends

A rhythm's final reply is recorded only. To send, it calls the native
`steward_tasks` task tool with `operation="notify"`, `key` and `text`. The
controller binds the owner and source; the caller cannot choose a destination.
An owner of `null` rejects sending while preserving the findings.

The key distinguishes independent messages. Exact retries reuse the existing
receipt; a changed payload under the same key is rejected. A receipt confirms
durable queuing, not transport completion. Calls can occur anywhere during the
execution, zero, one or several times. A provider failure after queuing does not
withdraw the message. A provider without a working tool channel records its
reply and sends nothing. Final markers, silence tokens and trailing narration
have no notification meaning.

Notification intents use the existing controller-owned result receipts, with
at-least-once transport semantics: a crash after external send but before the
transport receipt may duplicate delivery. Replay of the operation itself does
not create another intent. The native invocation capability expires when the
execution ends; a later execution must use its current tool.

A rhythm's document is read by its consumers. For example, `morning_brief.md`
contains its own committed date; the voice host, dashboard and operator judge
freshness from that document. A file change never instructs the harness to send
it. The rhythm may deliberately notify its owner when a message is needed.

Every reply is kept. The turn keeps it in state and the world commit keeps its
text. Its result receipt keeps it as `result_text` and marks the interval
`recorded_only` when nothing was sent. `/status` counts what rhythms recorded
without sending in the last 24 hours, rhythm reviews below included, so an absent
message can be told apart from a run that never happened. The controller also logs
`world rhythm <key>: reply recorded, not delivered`.

A procedure rhythm's full final narration is its evidence commit. Notification
calls queue their own receipts without an assessment turn; they neither replace
nor depend on that narration. Questions, failures and explicitly requested runs
keep their outcome-report route. Already prepared delivery receipts replay their
frozen messages through upgrades. An automatic assessment's final reply is also
recorded only; any additional message requires its own notification call.
A refused task operation in an automatic turn is retained and logged.

There are no built-in light, sleep or REM rhythms and no seeded world files; the
harness never invents a schedule for you. A world rhythm is configured like any
other, and the instance supplies its procedure file. A chain is one predecessor
per rhythm, not a graph: there are no calendars, fan-in, hooks or conditions
beyond "the previous one was accepted".

Publication requirements check the final integrated candidate before it can be
pushed. Target requirements review the complete candidate tree before application
and before satisfaction is reported. Evidence binds exact inputs and accepted
procedure configuration; an old weekly result never authorizes a different
candidate. Any number of independently configured reviewers uses the same path.

See [procedure construction](../src/steward_harness/procedures.py),
[task execution](../src/steward_harness/task_runner.py),
[convergence journeys](../tests/test_rewrite_convergence.py), and the
[target contract](automatic-deployment.md).
