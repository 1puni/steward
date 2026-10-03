# Execution boundaries after live input and persistent native homes

Revision-scoped audit, 2026-10-02. Source inspected:
`29b968667dc4378ad9c80d9953e811974f26e2f4`; retained audit branch initially
continued at `f9d5ace8`. Recommendations below are proposals, not implemented
contracts or deployment claims at that revision. The October 3 correction below
records subsequent implementation of B and V's clarification of notification policy.
Attribution: `task:task-24c4f2f2dc5f539ba93beb7bcaab5431`.

The central remaining problem is that execution completion still carries facts
whose natural owner is smaller or longer-lived: action authority, a read query,
a result delivery, or a scheduled obligation. Several boundaries already make
the right distinctions. Removing all structured output or keeping every process
alive would destroy useful guarantees.

## Ownership and scope

The supported `TASK_QUERY` observation at `2026-10-02T13:12:57.337116+00:00`
returned one unfinished peer, without truncation:

| Task | Observed state | Accepted revision | Ownership |
| --- | --- | --- | --- |
| `task-adcf068357cc5979ace0c3116ec3c2bf`, Decouple task operations from conversation completion | running | `5fdd04f305414736bae11d82c46f0a37cb522188` | same conversation |

This is a timestamped observation, not a lease or authority to change that task.
Its implementation was not inspected or modified. Findings A and D should inform
that owner's interface and validation, not create a parallel operation mechanism.
No additional tasks were launched. The empty-title query covers the repository's
unfinished tasks at that instant; it cannot establish future ownership.

Coverage includes conversation/source admission, task continuation, result
delivery, steering and cancellation, account acceptance, dispatch and rhythms,
both native adapters, prompts, documentation and relevant Git history. Tests use
local fixtures and scripted providers. No production incident rate, deployed
version, real-provider latency or Linux containment result was measured here.

## Boundary vocabulary

| Fact | Natural owner | What it does not establish |
| --- | --- | --- |
| Message/source event | Immutable source identity, speaker and receipt | A new execution, a grant, or completed work |
| Native input acceptance | Provider delivery acknowledgement | Model compliance or accepted filesystem changes |
| Execution | One bounded custody interval over a writer | One message, one operation, or the whole task |
| Native session | Provider lineage and its native state | Controller authority or publication eligibility |
| Durable operation | Accepted intent, stable identity and outcome | A particular final prose line or process lifetime |
| Task account | Accepted Git body with exact-base update | Product checkpoint or permission to land |
| Product checkpoint | Settled tree and explicit disposition | Tested, published or deployed work |

## Priorities

| Priority | Finding | Smallest follow-up scope |
| --- | --- | --- |
| P1 | A. First source's speaker governs later actions | Source-bound authority in the already-owned operation refactor |
| P1 | B. Retained results wait for cognition and its worker budget | Deliver existing evidence independently of assessment |
| P1 | C. Interrupted world execution consumes a scheduled interval | Distinguish attempt failure from obligation settlement |
| P2 | D. Ownership reads require ending native work | Live bounded read using the same controller interface |
| P2 | E. Final prose syntax controls task recovery | Separate explicit disposition from final narration |
| P2 | F. Documentation still describes superseded boundaries | Correct current claims; retain historical proposals as history |
| Deferred | G. A persistent home still has an invocation-scoped process | Prove writer exclusion before process reuse |

P1 denotes consequential authority or progress coupling, not a claim of an
exploited vulnerability. B and C include deliberate policies worth revisiting
under longer executions; their current implementations match their tests.

## A. Action authority follows the first source, not the action's source

**Evidence.** [ConversationService.run_turn](../src/steward_harness/conversations.py)
(lines 219–265) attaches later sources with their own `operator_id` and native
delivery disposition. But [StateDatabase.accept_turn](../src/steward_harness/state.py)
(633–710) calls `_apply_task_action` with the execution row's `operator_id` and
execution ID. `_apply_task_action` (720–757) rejects desk-watch answers/retries
and automated steering of unowned tasks based on that one speaker. The later
sources are not consulted. The final-marker parser also allows only one operation.

**Example and impact.** A controller result assessment starts an execution. An
operator then supplies the missing answer through live input. Its final answer
marker is still judged as `harness:task-result`; an unowned rhythm task remains
waiting and the rejection is suppressed from the visible reply. The operator
must issue another action after that execution. In the other direction, an
operator-started execution receiving a desk-watch observation judges its final
action as operator-origin, bypassing that observation's action-kind restriction.
This demonstrates missing operation/source binding; it does not establish that
a real model will choose the impermissible action or that arbitrary external
users can reach this lane.

A local reproduction used the existing `_service`, `_waiting_rhythm_task`,
`_answer` and `_reply` helpers from
[test_conversations.py](../tests/test_conversations.py). A fake cognition bound a
session, registered live input, acknowledged a second `_turn`, caught its
`ConversationBusy`, then returned an answer marker for the waiting task:

| Initial speaker | Attached speaker | Observed final state |
| --- | --- | --- |
| `harness:task-result` | `operator` | waiting; “only an operator turn may steer rhythm work” |
| `operator` | `harness:desk-watch` | queued; no rejection |

The standalone-source tests
`test_an_automated_result_review_cannot_steer_the_task_it_reviews` and
`test_an_operator_turn_can_steer_a_task_its_rhythm_created` do not cover that mix.
[test_world_durability.py](../tests/test_world_durability.py)'s
`test_completion_custody_keeps_live_controller_input_attributed` proves stored
attribution, not its use in an action decision.

**History.** The origin gate is already present in public snapshot `2ffe3c9e`.
Its test explains the intent: an automated review must not approve its own
unowned work. **Inference:** using the root speaker was adequate when the action
and source shared one turn; adding live input left the authorization lookup at
the old boundary. Private origin history was not independently recovered.

**Correction and validation.** The existing operation refactor should bind each
request to controller-validated source evidence and ownership, not accept an
agent-supplied claim to be the operator or treat every attached source as a
grant. Preserve same-conversation/repository restrictions and cancellation.
Test both source orders, multiple actions, rejected/unresolved source delivery,
clear/provider-generation races, duplicated calls after restart and an action
racing withdrawal. An action's idempotency identity must not collapse every
operation in one execution to one source key.

## B. Evidence delivery waits for model assessment and long-running work

**Evidence.** [ConversationService.deliver_task_result](../src/steward_harness/conversations.py)
(519–606) retains a result, obtains `_assess_task_result` through `run_turn`,
then saves and sends its reply. Provider failure has a raw-result fallback;
`ConversationBusy` instead defers delivery. A result joining active cognition
waits for that execution's prepared receipt. After acceptance it can send the
original result, so that delay need not have produced any useful assessment.
[Daemon._pass](../src/steward_harness/daemon.py) schedules `("result", owner)`
through the same [Dispatch](../src/steward_harness/kernel.py) executor as tasks,
publication, targets and probes. Task calls can run without a routine deadline.

**Example and impact.** A task's question is already durable while its owning
conversation is busy; the result send waits for that conversation to finish.
Independently, all worker slots can be occupied by other unbounded tasks while a
retained question or outcome waits in the queue. The operator can still use live
ingress, but unsolicited results cannot reach them until a slot becomes free.
This is a capacity scenario derived from the executor, not a measured starvation
incident. No second operator message is required by code; one becomes a practical
way to discover evidence the controller already holds.

**History.** `38091372` explicitly replaced multiple background loops and fairness
ordering with one budget and per-owner exclusion. That removed real duplicate
publication drivers. The result prompt asks cognition to assess broader intent
and choose authorized follow-through. Both are defensible purposes; neither
requires delaying delivery of an already accepted question. Existing
[result tests](../tests/test_task_result_delivery.py) establish assessment/send
retry receipts and crash replay, not latency under saturated workers.

**October 3 correction.** V authorized delivering retained evidence independently of
assessment. The implementation gives each retained result a frozen delivery decision
and transport receipt, with optional assessment settled separately. One transport
worker progresses under full cognition occupancy; this is an explicit narrow
exception to the shared budget. The [current contract](kernel-contract.md#task-results)
states latency, status rechecks and at-least-once semantics. Local regressions cover
full worker occupancy, busy/cleared conversation, failed send, crash after send before
receipt, late assessment, ownership routing and duplicate source replay. Deployed-host
latency and real-provider behavior are not established by those fixtures.

## C. A failed world execution settles the interval without completing its work

**Evidence.** [Procedures.due_world_rhythms](../src/steward_harness/procedures.py)
(254–277) skips an interval when its turn is interrupted, just as a receipt
settles it. `run_world_rhythm` (293–337) logs provider failure and returns;
each new interval resets native lineage. Dependent rhythms require a completed
predecessor in the same interval. Conversation execution uses the provider
deadline; procedure task execution, by contrast, can retain a bound-session
timeout as continuation in [TaskRunner._run_owned_turn](../src/steward_harness/task_runner.py)
(476–511).

**Example and impact.** A nightly consolidation reaches its deadline after
partial work. That interval will not resume; its dependent review will not run.
The next interval must absorb the backlog, or the operator must arrange separate
work. `/rhythm run` refuses an extra world run in that interval. The source
identity prevents duplicate effects but also represents “attempt made” as
“nothing more owed.” Retained partial files are not accepted consolidation.

**History.** `bf2913bf` deliberately made the interval source key the whole
idempotency mechanism and specified failure consumes the interval.
`315da0ec` added chains; `26049780` serializes world rhythms to avoid unnecessary
reconciliation. The code states the failure policy prevents retry storms.
Commits `517e42e9` and `efadf83a` report a real consolidation backlog and increase
the configured provider timeout; those reports are historical evidence, not a
fresh reproduction of that host incident.

**Correction and validation.** Retain one logical obligation per captured
interval while distinguishing accepted completion, interrupted attempt and a
hold needing intervention. Permit explicit continuation under the same intent
and evidence; never blindly replay a provider that may already have acted.
Keep bounded retry/backoff or an explicit hold, stable deduplication and no
overlap. A longer timeout alone does not correct the distinction. Validate
timeout after edits, unavailable provider before tools, crash after world
acceptance before receipt, interval rollover, explicit cancellation and
dependent-chain wakeup. Review existing
[world-rhythm tests](../tests/test_world_rhythms.py), especially
`test_failed_interval_is_consumed_rather_than_retried`,
`test_rhythm_command_reports_the_interval_and_refuses_an_extra_run` and
`test_a_failed_predecessor_ends_the_chain_for_the_interval`, as policies to change
deliberately rather than tests to bypass.

## D. A read-only ownership query forces a task checkpoint and another execution

**Evidence.** [task_query.py](../src/steward_harness/task_query.py) recognizes a
waiting task's `TASK_QUERY` reason. [TaskRunner._answer_query](../src/steward_harness/task_runner.py)
(389–403) reads and answers it after closure or on the next dispatch.
[The prompt](../src/steward_harness/prompts.py) and
[ownership contract](provenance-discovery.md#current-ownership-on-request)
require `DISPOSITION: ask`. The current slice can record the answer immediately
after closing, but the model still needs a new execution to receive it.

**Example and impact.** This audit needed current ownership, closed its first
execution, and resumed to read one task title. A task with active native children
must arrange a safe stopping point just to ask that read, paying checkpoint and
restart costs unrelated to the answer's authority.

**History.** `0c91c67a` introduced the query as observations returned to existing
owners. Its documentation intentionally avoids a new read service or snapshot
directory. Crash recovery and privacy tests in
[test_feedback_queries.py](../tests/test_feedback_queries.py) substantiate that
choice. **Inference:** reuse of the question protocol minimized machinery but
made a read depend on task suspension.

**Correction and validation.** Offer the existing bounded, repository-authorized
read through the controller interface used by callable operations, if that
owner's scope supports it. Otherwise specify a bounded extension after the
refactor; do not build another operation subsystem. Preserve timestamp,
accepted revision, lock-derived status, metadata limits and evidence-not-grant
semantics. Test a live parent/child continuing during the read, cancellation,
repeated/lost responses, ownership changes during lookup and absence of task
publication or authority changes. A read requires neither tree acceptance nor
a completion disposition.

## E. A malformed final narration blocks otherwise retained task work

**Evidence.** [parse_tick_closure](../src/steward_harness/landing/checkpoint.py)
requires three exact trailing lines. [TaskRunner._work](../src/steward_harness/task_runner.py)
(731–755) creates a continuation checkpoint on parsing failure and raises;
the error path then retains blocked work. The native parent can have completed
all requested investigation or edits and still require a retry solely to state
the closure again. See
[test_invalid_working_session_closure_retains_retryable_work_without_publication](../tests/test_task_no_changes.py).

**Example and impact.** A valid `idle` answer followed by an extra explanatory
sentence fails parsing. Work survives, but an operator or authorized assessment
must retry it. A native receipt-only turn historically caused exactly this sort
of failure: `bf6e9412` records five retries after “Acknowledged” replaced a valid
closure. That specific Claude/GLM defect is fixed: `receipt=True` results no
longer supersede the working result. `a09fb05f` likewise fixes background-notice
continuations being rejected as unowned. Neither fixed bug is a new finding.

**History and correction.** The parser explicitly refuses to infer publication
intent, and every slice commits so the work tip has controller disposition
trailers. Keep those guarantees. The incidental part is attaching the structured
decision to the final prose suffix. A future typed closure should bind explicit
disposition to the task/execution and settled candidate, accepting it only after
writer teardown; no automatic `idle` inference and no mid-write publication.
A bounded same-session repair is an alternative only after proving continued
custody. Validate missing/duplicated/conflicting closure, receipt-only answers,
late operator corrections, cancellation after `idle`, crash before checkpoint,
and unresolved native children. Do not make “last valid marker anywhere” the
replacement: it could discard a later change of intent.

## F. Current documentation sometimes describes the old implementation

The root README still said launch homes were disposable and native databases
were not carried over. [native_workspace.py](../src/steward_harness/runtime/native_workspace.py)
and [test_native_workspace.py](../tests/test_native_workspace.py) instead retain
homes by owner/provider/generation; `f1d4d285` introduced that behavior.
[The runtime contract](native-provider-runtime.md#native-workflows-and-storage-boundary)
already describes it correctly. The live-understanding guide said a queued
Claude receipt must produce another closure, despite `bf6e9412`. The execution
lifecycle guide said rhythms had no separate world-turn path, despite
`bf2913bf`.

These claims can send follow-up work toward already completed refactors or teach
a model to repeat unnecessary output rituals. This audit corrects those current
claims. [native-session-host.md](native-session-host.md) is a dated design and
measurement record; its old observations are not present implementation facts.
Its future process-reuse design remains relevant. Documentation-link checking
establishes links, not behavioral truth. Owner-home persistence, receipt-only
results and world-rhythm fixture tests validate the corrected distinctions;
they do not prove native jobs actually resume on a deployed provider.

## G. Persistent state does not imply a persistent process

[CodexRuntime.execute](../src/steward_harness/runtime/providers/codex_app_server.py)
launches App Server per request, cleans background terminals at terminal
completion and closes transport. It refuses a completed parent with active
native children. [ClaudeRuntime](../src/steward_harness/runtime/providers/claude.py)
drains correlated commands/results, including supported native continuations,
then closes input. [ProcessController](../src/steward_harness/runtime/process.py)
contains descendants on success as well as failure. Ordinary native state in
owner homes persists, but no process runs queued work between invocations.

**Example and impact.** A session's background terminal or native goal cannot
independently keep working after its harness invocation ends. A later legitimate
invocation is needed for further native activity, and filesystem persistence
alone does not prove the provider will resume a job. This limits longer native
workflows, but is not permission to keep writers alive during acceptance.

**Current direction (2026-10-03).** The operator authorized extending stage
three of the [session-host design](native-session-host.md), correcting any
reading of this audit as a recommendation to keep per-invocation teardown as
permanent policy. Preserve writer exclusion; replace its mechanism where proved.
The cancelled lifecycle task is historical ownership, not a reason to defer the
accepted work or create a second process-host project.

The [stage-three evidence](native-session-host.md#stage-three-writer-exclusion-evidence)
now includes installed Codex and Claude Code with deliberately writing MCP
fixtures: writes continued while idle and while the provider leader was stopped;
Codex terminal cleanup also failed to stop those writes. Shared agent filesystem
grants additionally permit peer writers outside a session's cgroup. Whole-interval
exclusion, stale-checkout safety, child ownership, generation retirement,
cancellation, crash recovery and resident credential boundaries are not proved.
Keep teardown for all providers until they are. No process reuse was enabled;
the fail-closed adapter tests remain intact. The evidence table distinguishes
measured counterexamples from controller/root-host and authenticated-provider
checks that remain unperformed.

## Boundaries to preserve and design guidance

Account offers already separate understanding from stopping and publishing:
immutable blob, exact accepted base, replay identity and body-only acceptance.
The watcher does not certify a coherent product tree. Procedure runs omit that
watcher and retain deadlines; this is an explicit scope distinction, not evidence
that every procedure needs to become an unbounded task.

Task notes are durable before live delivery; only acknowledged inputs are
consumed by the closing checkpoint. Unresolved and late inputs remain pending.
A note does not answer a waiting question. Conversation input similarly keeps
accepted/rejected/unresolved delivery distinct from execution acceptance; a
proved rejected source can retry after its execution stops. Uncertain side
effects must not be replayed merely to make the queue look empty.

Cancellation is a durable withdrawal plus cooperative interruption followed by
containment. The publisher's final withdrawal check, exact candidate gates,
task locks, repository leases and generation fences remain necessary. Cancelling
a task need not kill a running gate to prevent publication. Capturing files
before containment is verified would weaken the actual safety boundary.

**Decision update, 2026-10-03:** V overruled this audit's recommendation to
retain the notification marker. Quiet automatic runs remain policy; sending is
now a callable operation, independent of final narration. Dream Away's all-`SILENT`
reply (`turn_1bcb357e056b442da5004d29de2f3707`) is further evidence that a prose
ritual distorts the work. V also removed the `deliver` exception: changing a
morning brief is document authorship, not a sending decision. Its readers own
date-based freshness; only a callable notification sends it to an owner.
The current contracts are [notification](rhythms.md#what-a-rhythm-sends) and
[typed closure](execution-lifecycle.md#execution-closure-and-continuation).
Findings D and E above retain their revision-scoped evidence; current ownership
queries and closure now use the native tool. The former `QUESTION: TASK_QUERY`
fallback is removed. The separate candidate-review verdict remains structured
gate evidence, with its reason for staying recorded in the closure contract.

The final-prose sweep also found two transport representations that stay in this
change. `[[send_image:...]]` and `[[send_document:...]]` name message attachments,
subject to existing path and receipt checks; they do not decide whether an
automatic run sends. Configured Telegram pin directives stay with the transport
receipt because `pin_reply` needs the actual delivered message identity and
pin retries share that message's durable delivery record. They retain the existing
configured-chat permission checks and operator-request policy; they do not
select execution closure or notification opt-in. Moving these actions to a separate API must preserve that
message/receipt binding; this change does not introduce an independent pin queue.
The retired `TASK_PROPOSAL`/`TASK_ACTION` parser remains rejection diagnostics
only: it cannot admit or steer work, including on recovery. Its original raw
output remains recorded. No `SILENT`, `QUESTION: NONE`, `DISPOSITION:` or
`NOTIFY:` token controls execution or automatic sending.

Bound follow-ups by the fact they move, not by the module they edit. A and D
belong beside the existing callable-operation work. B concerns delivery and
capacity; C concerns scheduled obligation recovery; E concerns closure
acceptance. Each should preserve one canonical accepted representation and
derive status from it. A separate generic event bus, mirrored task database or
provider workflow engine is not needed to establish any finding here.

For every correction, test the point between durable acceptance and lost
acknowledgement, not just successful output. Test concurrent sources with
different authority, retry after uncertain effects, interruption during writes,
owner/generation change and a late result. A controller receipt, input delivery,
task completion, publication and transport delivery must remain different facts.

## Validation and remaining uncertainty

The mixed-source reproduction above establishes the root-speaker lookup with
real local state/task fixtures and scripted cognition. It is not a live-provider
or transport exploit test. Findings B and G are code-path/capacity analyses;
no timing measurements were invented. History explaining private development is
limited to retained public snapshots and available commit messages.

Focused validation: **240 passed in 78.43 seconds** using the command below.
Documentation validation: **697 local links checked, 0 broken**.
`git diff --check` also passed. No runtime source changed during validation.

The subsequent integrated gate reported 230 failures and 21 errors, including
filesystem errors in upstream-guard fixtures. On the retained branch at
`598eb9f1`, all 21 upstream-guard tests passed independently, then the full run
`uv run --extra dev pytest -q -x --tb=short` passed with **1,483 passed,
22 skipped in 500.07 seconds**. Source and tests matched candidate `78e0d1e7`;
the requested base `29b96866` was already an ancestor. The original failure did
not reproduce. Limited free space in `/tmp` was observed but does not establish
the failure's cause. No runtime, test or gate changes were made; integrated
candidate acceptance still requires the controller's gates.

```sh
uv run --extra dev python -m pytest -q -p no:cacheprovider \
  tests/test_feedback_queries.py tests/test_task_result_delivery.py \
  tests/test_world_rhythms.py tests/test_claude_stream.py \
  tests/test_codex_app_server.py tests/test_native_workspace.py \
  tests/test_task_no_changes.py
python3 scripts/check-docs.py
```

These tests check existing behavior and preserved recovery boundaries. Passing
them does not implement the recommendations or establish production health.
Recheck the action findings against the callable-operation owner's final tree
before assigning any implementation work; the audit deliberately inspected its
own retained source revision rather than concurrent, unaccepted edits.

The mixed-source reproduction can be repeated from the repository root after
the test dependencies are installed. It uses temporary state only:

```sh
PYTHONPATH=src:tests .venv/bin/python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from test_conversations import _service, _turn, _waiting_rhythm_task, _answer, _reply
from steward_harness.runtime.contracts import RuntimeInputResult
from steward_harness.state import ConversationBusy

for first, later in [('harness:task-result', 'operator'),
                     ('operator', 'harness:desk-watch')]:
    with TemporaryDirectory() as root:
        class Live:
            def run(self, request, *, execution_id=None):
                request = request() if callable(request) else request
                request.on_session_started('codex', 'session-1')
                request.on_input_ready(lambda source: request.on_input_result(
                    RuntimeInputResult(source.source_id, 'accepted')))
                try:
                    _turn(service, 'later', 'Answer the waiting task.', operator_id=later)
                except ConversationBusy:
                    pass
                return _reply(_answer(task, 'The package index.'))
        service = _service(Path(root), Live())
        task = _waiting_rhythm_task(service)
        result = _turn(service, 'root', operator_id=first)
        print(first, '+', later, '=>', service._state.tasks.get(task).status.value,
              '| rejection:', result.task_rejection)
PY
```
