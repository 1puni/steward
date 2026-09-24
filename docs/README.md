# The harness, from the outside in

Start with the operator journey. Then learn the few facts that make it work. There are
fewer than you expect, which is the entire point.

The [root README](../README.md) is the command and configuration overview. This map
gives each deeper explanation a home, and says why you would open it.

## Fork and operate

| Read | For |
| --- | --- |
| [Fork it. Give it a world. Let it work.](getting-started.md) | The executable tour, then a real host installation, then a first useful task you can actually believe |
| [Ways to use and connect stewards](stewardship-arrangements.md) | Single repositories, organisations, personal stewardship and a person's own company; ownership and current connection limits |
| [Give it a body of work](ongoing-work.md) | What a task is, how to admit one, how it carries across executions, when to reach for a rhythm instead, and what the harness will not do for you |
| [Organisation-onboarding skill](../skills/org-onboarding/SKILL.md) | Inspect what each repository's release path *already* is, and generate only configuration that selects implemented behavior |
| [Automatic deployment](automatic-deployment.md) | Native systemd releases, self-deployment, and the platforms that deploy off a Git push without asking the harness |
| [Native provider runtime](native-provider-runtime.md) | Private homes, session continuity, model policy, live input and where provider storage actually goes |
| [Controller and agent identities](execution-boundary.md) | Host provisioning, and the boundary that has to be enforced rather than promised |
| [Task board](task-mini-app.md) | The read-only browsing surface: where each displayed fact comes from now that status is Git, and why no write crosses the socket |
| [Upgrade an existing steward](../migration-handoff.md) | Preserving actual state, and rehearsing a cutover before performing one |
| [Runtime readiness](handoff.md) | What verification covers and, more usefully, what it does *not* |
| [Usage after the refactor](usage-regressions.md) | Reproduced failures, changed behavior, and boundaries nobody has proved yet. Read this before trusting a guarantee |

## Understand the machine

Each of these owns one coherent responsibility. If two of them seem to describe the same
fact, one of them is wrong and worth fixing.

| Contract | Owns |
| --- | --- |
| [Git-native rewrite](git-native-rewrite.md) | Task rewrite, deployment targets, migration and evidence scope |
| [Kernel](kernel-contract.md) | Authority, state owners, the two acceptance paths, deliberate limits and concurrent dispatch |
| [Execution lifecycle](execution-lifecycle.md) | Task files, status precedence, closure syntax, continuation and cancellation |
| [Durable world turns](world-turn-durability.md) | Candidate custody, world application, dependent effects and replay |
| [Live task understanding](live-task-understanding.md) and its [implementation](live-task-understanding-implementation.md) | Accepted task-account updates without ending a native turn |
| [Native rhythms](rhythms-direction.md) | Recurring files, target refs, due calculation and operator controls |
| [Failure boundaries](failure-boundaries.md) | Operational errors, controller faults and shutdown — and which is which |
| [Native provider substrate](native-provider-substrate.md) | Which provider-native surfaces the harness interfaces with directly, why a signal is the wrong shutdown layer, and what is still unbuilt |
| [Working environments](working-environments.md) | Where a checkout comes from and when the controller may discard it |
| [Hierarchy and memory](steward-hierarchy-and-memory.md) | Where durable truth belongs, at the lowest scope that owns it |
| [Native record provenance](native-record-provenance.md) | Original records, derived memories and attributable evidence |
| [Task files and Git history](provenance-discovery.md) | Native provenance without injected snapshots or copied history |
| [Stewardship visualisation](stewardship-visualisation.md) | Inspect lifecycle signals and render cognition prompts through the runtime's own utilities |

## Work on the harness

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
journeys. Neither is interchangeable with live deployment evidence, and neither
substitutes for the other. [Open decisions](backlog.md) keeps the unresolved product
questions where they cannot be mistaken for contracts.

The voice is deliberate. Read [Git, Sleep, and Executive Function for Non-Executive
Fucks](git-sleep-and-executive-function.md) for why the machine exists at all, and
[a nod to the ancestors](doctrine-ancestry.md) for its intellectual debts.

## What is deliberately not here

Dated records of individual installations, migration diaries and superseded proposals
are not part of this documentation. An observation from one host at one revision is
evidence about that host at that revision; it is not a default and it is not a contract.

Keep a current fact with its owner. Link to it from elsewhere instead of copying its
implementation history, because a copied fact is a fact that will go stale somewhere you
are not looking. Evidence needs a revision and a scope. A proposal needs a label. A new
reader should not have to reconstruct the week to find out what is true today.
