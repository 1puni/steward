# The harness, from the outside in

Start with the operator journey. Then learn the few facts that make it work. There are
fewer than you expect, which is the entire point.

The [root README](../README.md) is the command and configuration overview. This map
gives each deeper explanation a home, and says why you would open it.

## Fork and operate

| Read | For |
| --- | --- |
| [Fork it. Give it a world. Let it work.](getting-started.md) | The executable tour, then a real host installation, then a first useful task you can actually believe |
| [Ways to use and connect stewards](stewardship-arrangements.md) | Single repositories, organisations, personal stewardship, and the intertwined GG/1puni arrangement; ownership and current connection limits |
| [Give it a body of work](ongoing-work.md) | What a task is, how to admit one, how it carries across executions, when to reach for a rhythm instead, and what the harness will not do for you |
| [Organisation-onboarding skill](../skills/org-onboarding/SKILL.md) | Inspect what each repository's release path *already* is, and generate only configuration that selects implemented behavior |
| [Automatic deployment](automatic-deployment.md) | Native systemd releases, self-deployment, and the platforms that deploy off a Git push without asking the harness |
| [Native provider runtime](native-provider-runtime.md) | Private homes, session continuity, model policy, live input and where provider storage actually goes |
| [Controller and agent identities](execution-boundary.md) | Host provisioning, and the boundary that has to be enforced rather than promised |
| [Task board](task-mini-app.md) | The read-only browsing surface: where each displayed fact comes from now that status is Git, and why no write crosses the socket |
| [Upgrade an existing steward](../migration-handoff.md) | Preserving actual state, and rehearsing a cutover before performing one |
| [Runtime readiness](handoff.md) | The current operational handoff, including what verification does *not* cover |
| [GG deployment handoff, September 24](deployment-handoff-2026-09-24.md) | Native fixes, live understanding and simplification branches; independent evidence and the route to today's GG release |
| [GG handoff, September 12 22:10 UTC](gg-handoff-2026-09-12-2210.md) | Dated live evidence after GG left Docker, open work at that time, and diagnostic traps; current monitor behavior is in the desk-monitoring contract |
| [September 12 VPS repair handoff to Claude](vps-repair-handoff-2026-09-12.md) | How that state was reached and why each fix is shaped as it is. Its live-state section is superseded |
| [Usage after the refactor](usage-regressions.md) | Reproduced failures, changed behavior, and boundaries nobody has proved yet. Read this before trusting a guarantee |

## Understand the machine

Each of these owns one coherent responsibility. If two of them seem to describe the same
fact, one of them is wrong and worth fixing.

| Contract | Owns |
| --- | --- |
| [Git-native rewrite](git-native-rewrite.md) | Current task rewrite, deployment proposal, migration and evidence scope |
| [Kernel](kernel-contract.md) | Authority, state owners, the two acceptance paths, deliberate limits and concurrent dispatch |
| [Execution lifecycle](execution-lifecycle.md) | Task files, status precedence, closure syntax, continuation and cancellation |
| [Durable world turns](world-turn-durability.md) | Candidate custody, world application, dependent effects and replay |
| [September 14 cognition completion fixes](cognition-completion-2026-09-14.md) | Capture recovery, source attribution, quiet completion and the epoch-49 release boundary |
| [Native rhythms](rhythms-direction.md) | Recurring files, target refs, due calculation and operator controls |
| [Failure boundaries](failure-boundaries.md) | Operational errors, controller faults and shutdown — and which is which |
| [Native provider substrate](native-provider-substrate.md) | Which provider-native surfaces the harness interfaces with directly, why a signal is the wrong shutdown layer, and what is still unbuilt |
| [Hierarchy and memory](steward-hierarchy-and-memory.md) | Where durable truth belongs, at the lowest scope that owns it |
| [Native record provenance](native-record-provenance.md) | Original records, derived memories and attributable evidence |
| [Stewardship visualisation](stewardship-visualisation.md) | Inspect lifecycle signals and render cognition prompts through the runtime's own utilities |

## Work on the harness

[Active refactor integration](refactor-integration-2026-09-24.md) records exact inputs,
current acceptance results and the remaining path to deployment.

The [engineering doctrine](engineering-doctrine.md) and the operator's
[refactoring mandate](independent-refactor.md), with its
[representation example](engineering-doctrine-example.md), set the standard: understand
the structure that generated the mess, then let the unnecessary complexity disappear.

[Demolition](demolition.md) is the method, and it is not incremental. Understand the
whole responsibility, delete the redundant concept outright, fill the hole with the
primitive, prove the behavior. Editing around a bad concept is how a codebase acquires a
second one. Its [validation map](demolition.md#validation-map) connects contracts to the
checks that actually establish them.

The [follow-through environment](follow-through-environment.md) stands an entire daemon
up against a fake Bot API and a scripted native CLI, and asserts did-happen facts:
message in, reply out, edit on the accepted world, task landed through the gate. The
[native experiments](../experiments/native_sessions/README.md) cover installed provider
journeys. Neither is interchangeable with live deployment evidence, and none of the three
substitutes for the others. [Open decisions](backlog.md) keeps the unresolved product
questions where they cannot be mistaken for contracts.

The voice is deliberate. Read [Git, Sleep, and Executive Function for Non-Executive
Fucks](git-sleep-and-executive-function.md) for why the machine exists at all,
[a nod to the ancestors](doctrine-ancestry.md) for its intellectual debts, and
[the predecessor review](ancestors.md) for the capabilities a smaller design still has
to preserve — structural reduction is only progress if required behavior survives it.

## Instance operations

- [1puni company-world separation, September 24](1puni-world-separation-2026-09-24.md): one GG conversation world, a private company repository, scoped publication, and preserved private history.
- [nsnodes Light live acceptance, September 16](nsnodes-light-live-2026-09-16.md): organisation-root execution, existing sibling repositories, ref freshness and a completed automatic run.
- [GG message classification, September 14](gg-message-cognition-review-2026-09-14.md): verified send origins, repeated evidence gaps, failed acceptance leaking into world history, and unresolved topic routing.
- [GG controller resource repair, September 14](gg-controller-resources-2026-09-14.md): overnight watch follow-through, TLS memory diagnosis, CPU fix, deployment and verified storage cleanup.
- [GG prompt and transcript audit, September 14](gg-prompt-transcript-audit-2026-09-14.md): measured duplication, missing deployment/test context, rendering gaps and retirement of the AI watch.
- [Task files and Git history](provenance-discovery.md): native provenance without injected snapshots or copied history.
- [VPS Git-native migration, September 13](vps-git-native-migration-2026-09-13.md): live cutover, preserved state, validation and recovery.

These documents belong to named installations and dates. Their observations are not
universal defaults, and an old cutover record is not permission to repeat it.

- GG: historical, withdrawn [development-environment migration](development-environment-migration.md)
  and [dated run record](development-environment-run-2026-09-12.md). Production uses
  the [native Linux execution boundary](execution-boundary.md).
- GG: [desk monitoring](gg-desk-monitoring-handoff.md) — current observation behavior and dated instance evidence:
  what to probe first, and the rule that a silent monitor is a
  finding rather than good news. Also see
  [Git-store diagnosis](world-git-store.md), which is why the world reached 12 GB twice
  and why session transcripts were not the reason.
- GG: [the September 19 world deadlock](gg-world-deadlock-2026-09-19.md) — how one
  delivered file stopped every world application, why committing it makes the
  stall unrecoverable, and the two silences that kept it off anyone's screen.
- GG: [personal world and 1puni work](gg-personal-and-1puni-overlap-2026-09-21.md) —
  the repository named `1punicorn` is Bathy, which is why a venture page had
  nowhere to land but a shipping product. The cross-domain reach is deliberate;
  the naming is not.
- GG: [September 10 migration preparation](gg-migration-prep.md),
  [cutover record](gg-migration-2026-09-10.md), and
  [September 12 session handoff](session-handoff-2026-09-12.md).
- [1puni instance records](../instances/1puni/README.md).
- Crosstrees: [migration onto upstream main](crosstrees-migration-2026-09-18.md) and
  [instance records](../instances/crosstrees/README.md). The worked example of a
  worldless steward whose organisation is a single repository.
- [Boating migration gate](boating-migration-plan.md): downstream acceptance stays
  distinct from generic runtime capability.

## Design and history

These explain decisions, proposals and evidence at their recorded revisions. They do not
override current contracts and they do not supply accepted configuration syntax. A
proposal that reads like a contract has lost its label.

- [Design discussion and agreed decisions](design-discussion.md).
- [Durable understanding during native work](live-task-understanding.md): accepted task-account updates without ending native turns, and the implementation acceptance baseline.
- [Initial live-understanding review](live-task-understanding-review.md): independent doctrine and code review obligations before implementation.
- [Isolated GG implementation run](live-task-understanding-server.md): server workspace, authority, progress artifacts and dedicated Telegram topic.
- [Live task understanding: implementation](live-task-understanding-implementation.md): the offer representation, acceptance path, closure baseline, deadline and shutdown policy, and evidence.
- [September 21 GG boundary orientation](gg-boundary-orientation-2026-09-21.md): what GuruGee was meant to be, why its Telegram stewardship is the 1puni org layer, what the migration dropped, and the boundary questions still open.
- [September 15 rhythm intent reconstruction](rhythm-intent-reconstruction-2026-09-15.md): the earlier world-cognition model, operator corrections, and the shift to repository review procedures.
- [September 14 stewardship flow review](stewardship-flow-review-2026-09-14.md): current signals, missing deployment feedback, reflection visibility and scoped verification.
- [September 12–13 stewardship doctrine review](stewardship-doctrine-review-2026-09-13.md):
  repair assessment, reproduced monitor defects and responsibility-reduction priorities.
- [September 5 alignment review](harness-alignment-review.md) and
  [September 6–9 operator feedback](harness-feedback-2026-09-06_09.md).
- [Session/submission replacement proposal](harness-boundary-reduction.md) and
  [architectural subtraction review](architecture-subtraction-review.md).
- [Native session alignment](native-session-alignment.md),
  [conversation continuity review](conversation-continuity.md), and
  [source-derived workspace decision](working-environments.md).
- [Outcome obligation proposal](outcome-obligation.md),
  [organisation authority proposal](organisation-onboarding.md), and
  [operator onboarding fixture](goals.md).
- [Browser control design](browser-control-obscura.md),
  [historical hardening roadmap](maturity-roadmap.md),
  [founding cross-harness analysis](cross-harness-analysis.md) — the blueprint, four
  generations of it — and [September 10 static comparison](refactor-comparison-2026-09-10.md).
- [Historical schema changes](history/schema-changes.md).
- `superpowers/` holds historical implementation plans and specifications. Execution
  records, not current authority.

Keep a current fact with its owner. Link to it from elsewhere instead of copying its
implementation history, because a copied fact is a fact that will go stale somewhere you
are not looking. Evidence needs a revision and a scope. A proposal needs a label. A new
reader should not have to reconstruct the week to find out what is true today.

- [Task files and Git history](provenance-discovery.md): native provenance without injected snapshots or copied history.
