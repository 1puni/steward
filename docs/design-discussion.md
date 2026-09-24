# Harness design discussion

This document records decisions from the operator discussion following the
alignment review (kept in the private records). Agreed product behavior and
unresolved implementation choices are kept explicit. It does not describe
features already implemented or authorize runtime changes.

## D01 — The steward owns the broader outcome

Agreed with the operator on 2026-09-05:

> The steward owns the broader outcome until it is resolved, with bounded,
> preferably cheap subagent execution or other execution underneath.

Outcome responsibility stays with the steward when it delegates a piece of
work. A bounded execution finishing supplies a result for the steward to
evaluate against that outcome. The execution may be a subagent or another
appropriate mechanism; subagents are not mandatory for every step.

This extends the intended operator experience beyond the current repository
task abstraction. An outcome can involve investigation, repository changes,
waiting for people or permissions, and verification across multiple executions.

Prefer cheap execution. Exact model selection, budget measurement, escalation
conditions remained undecided initially; D13 now defines execution ownership. D04 sets the initial delegation
policy; D05 sets the allocation model. The existing separation between
cognition and controller publication/deployment authority remains a constraint
on the design.

## D02 — Evidence permits autonomous outcome closure

Agreed with the operator on 2026-09-05:

The steward may infer a sensible completion criterion from the operator's
intent, make that understanding visible, and close the outcome when sufficient
evidence satisfies it. Routine operator sign-off is not required.

The steward involves the operator when ambiguity would materially change the
work or when the operator's judgment is itself part of completion. This does
not expand execution authority. Completion evidence must support the accepted
outcome, including its required publication or deployment conditions.

How criteria, evidence, and closure are represented and validated remains an
implementation question. In particular, the harness can enforce concrete
mechanical guarantees; judging an open-ended outcome also requires steward
interpretation. The design must make that judgment inspectable.

## D03 — Git owns the durable outcome account

Agreed with the operator on 2026-09-05:

The owning Git world contains the complete durable account of an outcome,
including its intent, completion criterion, progress and evidence, outstanding
work, next action or waiting condition, and the reason and evidence for closure.
A fresh steward must be able to recover this understanding without its
predecessor's private provider conversation.

SQLite owns the mechanics of carrying the outcome forward, such as execution
claims and due follow-ups. Each fact has one canonical owner. Operational
records reference the relevant outcome and evidence rather than becoming a
second editable account of its meaning.

Editing or deleting an outcome document does not silently discharge an
accepted obligation. Closure must cross an explicit, evidenced boundary.
The exact representation and enforcement of that boundary remain undecided.

This division does not make it safe to discard execution state or blindly
repeat external actions. Recovery of in-flight work and upgrade continuity
remain requirements to resolve in B06 and B14 of the backlog.

## D04 — The steward owns decomposition initially

Agreed with the operator on 2026-09-05 as an initial policy:

The steward decomposes outcomes and assigns bounded executions. Those
executions do not create further delegated executions. They may choose their
method within the assignment; additional assignments or requests to expand
scope, authority, or resource bounds return to the steward for a decision
within its own authority.

This keeps outstanding assignments and their resource allocation accountable
to one outcome owner. Nested delegation may be reconsidered when a concrete
need justifies it; this is not a permanent architectural prohibition.

The assignment format and enforcement mechanism remain undecided. The proposed
assignment content is a recognizable question or deliverable, relevant context
and source references, allowed scope and authority, and resource bounds with a
stopping condition. Results would carry evidence, changes, and unresolved
questions for the steward to evaluate.

## D05 — One operating allowance, allocated by the steward

Agreed with the operator on 2026-09-05:

The operator sets an overall operating allowance. The steward allocates it
across outcomes, with optional explicit limits for individual outcomes.
Steward reasoning, delegated work, and retries all count toward the allowance.
Individual executions remain bounded and return their partial work and
evidence when they reach a limit, allowing the steward to reassess the next
step within its remaining authority and resources.

Amounts, accounting units and period, reservation for concurrent work,
measurement accuracy, and behavior when the overall allowance is exhausted
remain undecided. This decision specifies the allocation model rather than
selecting providers, prices, or a billing implementation.

## D06 — YAML can grant complete organisation stewardship

Operator requirement recorded on 2026-09-05:

YAML must be able to grant a stewardship authority over an entire organisation
through repository publication and deployment. Once that authority is granted,
ordinary actions inside it should proceed without repeatedly returning to the
operator for permission. The operator must also be able to narrow permissions
easily and robustly in YAML as needs change.

The desired onboarding journey includes discovering and fetching organisation
repositories, establishing the steward's service identity, assigning bounded
workflow investigations, creating the appropriate steward branches, and
establishing minimal integration with each repository's PR, direct-publication,
and deployment workflow. An organisation with several repositories is the target for trying this journey.

Full stewardship authority belongs to the stewardship/controller boundary.
Bounded execution underneath still receives its assignment scope; publication
and deployment credentials remain controller-owned. Workflow discovery tells
the steward how to use existing authority; it does not itself grant authority.

The exact meaning of a full preset, finer-grained capabilities, inheritance,
policy reload/revocation semantics, service credentials, and repository-specific
workflow configuration remain design work. An initial proposal and verified
GitHub identity options are in the organisation-onboarding proposal (private records).

## D07 — Upgrade the actual database explicitly

Agreed with the operator during implementation on 2026-09-05:

The harness preserves an incompatible database and reports the schema mismatch.
An agent can inspect the actual downstream database and upgrade it by hand.
Do not build speculative migration scripts for assumed historical state.

This replaces the automatic epoch reset and the proposed built-in known-schema
upgrades. It does not authorize discarding in-flight work or blindly replaying
external effects. Downstream code readiness and database restart readiness are
separate facts. An older binary that resets unfamiliar schemas must not be
started against the upgraded database as a rollback procedure.

The operator additionally requested a direct
[migration handoff](../migration-handoff.md): explain the deliberate changes in
thinking, inspect actual downstream state, and communicate verified changes in
a new operator message rather than hiding them behind compatibility behavior.
The handoff contains suggested wording; no live message has been sent.

## D08 — Start with the existing conversation and rhythm loop

Operator clarification on 2026-09-05:

Ordinary exchanges already become episodes in the organisation world, where
reflection and sleep can consolidate them. A longstanding thread can discuss
different things; finishing a turn must leave it available to continue.

Inspect and exercise that existing loop before adding outcome or investigation
machinery. D01–D07 specify behavior and ownership; they do not establish that
another runtime entity is necessary. A repository task's no-change disposition
does not diagnose ordinary conversation continuity.

The continuity review (private records) records the current path,
its integration test, and the narrower findings. The proposed outcome schema
below remains unselected.

## D09 — One steward conversation can own multiple tasks

Confirmed with the operator after the conduct review:

The ongoing steward conversation can discuss different subjects and hold
multiple tasks. A task has its own execution identity and returns results to
its originating conversation. Starting or finishing a task does not take over
that conversation.

Topic `/model` and `/model_family` address the steward conversation. Controls
for a particular task identify that task explicitly. There is no implicit
active-task selector. This removes the permanent conversation-to-first-task
binding; the existing task origin columns already express ownership.

The implementation exposes `/task model <id> [profile]` and
`/task model_family <id> [provider]`. B17 records the regression and verification;
the [migration handoff](../migration-handoff.md) describes the schema and
operator-visible routing change.

## D10 — Task results return to the owning session

The operator selected return to the owning session, rather than relying on a
later light turn to notice execution state. A result is evidence for the
steward to assess against its existing intent and authority. Finishing one
task does not close the broader outcome.

Implementation uses the existing result outbox, conversation turn, world
acceptance, and task-proposal contract. The steward may preserve progress in
its world and propose an authorised follow-up. Accepted assessments replay on
delivery failure without repeating cognition or task admission. A task result
does not create a new operator grant.

This slice covers tasks with an originating conversation. Rhythm and incident
origins do not acquire a fictional conversation parent. Model-issued task
answers/retries use D11; a prose answer alone does not resume waiting work.
The existing outbox now dispatches independent owning sessions concurrently
(B03). Per-turn timeouts remain; B15 tracks the overall allowance.

## D11 — Steer the existing task through world acceptance

Following the operator's instruction to work through the remaining backlog,
the harness now accepts one bounded `TASK_ACTION` as an alternative to proposing
a new task. An answer, retry, or note addresses one task ID and reuses that
task's existing state transition. The world update, action, and receipt cross
the same acceptance transaction, with current ownership, repository authority,
and task-state checks.

This closes the practical loop from execution result to owner assessment to
continuation on the retained branch. No action queue, callback executor, or
implicit active-task binding is introduced. Notes remain next-execution input;
late context returns to the owner explicitly instead of silently disappearing.

## D12 — Reuse native agent loops and share their records through Git

Agreed 2026-09-06. Reuse the providers' native agent loops, including native
memory writing and session recording. Put their readable memory/session
artifacts in the owning Git world so authorised sessions and agents can inspect
each other's work. Prefer native path configuration and lightweight mapping
over a replacement memory or session engine.

The purpose of isolating provider configuration is to give the steward its own
integrations: a VPS must not inherit the operator's personal Gmail plugin.
Disabling all native customisation is not the requirement. Checkpoints can
continue per tick/turn through an in-session commit workflow or a lightweight
between-turn reflection.

The [native session alignment](native-session-alignment.md) records the reuse
map, documented storage controls, and remaining live verification. These are
agreed directions. The [native runtime](native-provider-runtime.md) now supplies
Codex steering callbacks, worktree-local Claude/GLM automatic memory and readable
Codex parent/child history snapshots. Durable ingress and the remaining native
storage mappings remain pending. Controller acceptance and
external authority persist.

## D13 — Independent work progresses under scoped ownership

Following the operator discussion on 2026-09-08, the harness no longer holds a
global claim across task cognition, gates, publication, and deployment. The
operator requested removing those bottlenecks while following the engineering
doctrine. Task isolation and controller authority remain the governing facts.

Implementation keeps one controller and the existing execution paths. Tasks
claim their own work; landing and deployment reserve their affected repository.
Unrelated background owners also dispatch independently. Bounded worker capacity
is a runtime limit, separate from D05's still-unresolved operating allowance.
The [kernel contract](kernel-contract.md#concurrent-dispatch) records the selected
ownership, ordering, recovery, and shutdown boundaries.

## Questions to settle next

**Native session alignment takes precedence over extending the current
message/turn machinery.** The operator reaffirmed on 2026-09-06 that both
provider harnesses should supply their native session experience, including
input while working. Native session resumption and Codex live-input callbacks
are implemented; durable ingress does not yet route into those callbacks. The
[alignment audit](native-session-alignment.md) separates the transport evidence
from the remaining source-ownership and acceptance work.

Engineering standard supplied by the operator on 2026-09-06:
[the repository engineering doctrine](engineering-doctrine.md).
It directs us to discover the real entities and invariants,
reduce representable states, and establish ownership before implementing.
Existing code and PRs are evidence, not a required architecture. Prefer fewer
concepts and authority holders; preserve complexity that comes from real
concurrency, external effects, and trust boundaries. Tests should protect those
invariants so incidental structure can be deleted. This is an additional design
standard, not a selection of the mechanisms below.

The smallest durable outcome obligation (an unbuilt proposal in the private records) makes the
outcome account, wake behavior, closure boundary, and Git/SQLite handoff
concrete for discussion. Its mechanisms remain proposals; D01–D06 above record
the agreed behavior.

- How are completion criteria and the evidence supporting the steward's
  closure judgment recorded and checked?
- What scope, expected result, authority, and resource bounds must accompany
  one delegated execution?
- How does the steward choose cheap execution and recognize when it should
  stop, revise the approach, or spend more?
- How does one organisation grant become effective repository and deployment
  authority, and how do later YAML restrictions affect queued and active work?

The file format for outcomes, their relationship to existing tasks, the Git
and SQLite coordination boundary, and scheduling will follow these decisions.
No new database entity, hierarchy, worker pool, or provider policy is selected
here.

## Working example

“Sam leads the interface team; get them set up” includes an organisation-owned
fact and an outcome the steward carries. Inspecting setup requirements could
be one bounded execution. A missing access grant could require an operator
decision. The steward retains the outcome while those pieces proceed.

Proposed completion criterion for discussion: Oskar can access the intended
repository and perform the agreed development workflow, with evidence of any
required setup checks. The criterion and the authority to grant access are
separate decisions. This example does not authorize contacting anyone or
changing access.
