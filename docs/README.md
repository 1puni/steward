# The harness, from the outside in

Start with the operator journey. Then learn the few facts that make it work. There
are fewer than you expect, which is the entire point.

The [root README](../README.md) is the front door: what this is, commands,
configuration and honest status. This map gives each deeper explanation one home
and says why you would open it. The operator controls live in
[give it a body of work](ongoing-work.md#the-controls-you-have); every configuration
block is in the [kernel contract](kernel-contract.md#configuration).

## Run one

| Read | For |
| --- | --- |
| [Getting started](getting-started.md) | The executable tour, a real host installation, and a first task you can actually believe |
| [Give it a body of work](ongoing-work.md) | What a task is, how to admit one, how it carries across executions, when to reach for a rhythm instead, and what the harness will not do for you |
| [Ways to use and connect stewards](stewardship-arrangements.md) | One repository, an organisation, a personal steward, a person and their company; who owns what, and the current connection limits |
| [Organisation onboarding skill](../skills/org-onboarding/SKILL.md) | Hand this to your setup agent: inspect each repository's *existing* release path and write only configuration that selects implemented behaviour |
| [Targets and deployment](automatic-deployment.md) | Named targets, the systemd release driver, self-deployment and its operating traps, and platforms that deploy off a Git push without asking |
| [Native provider runtime](native-provider-runtime.md) | Private homes, session continuity, model policy, live input, and where provider storage actually goes |
| [Controller and agent identities](execution-boundary.md) | Host provisioning, and the boundary that has to be enforced rather than promised |
| [Watching a live steward](watching-a-steward.md) | Why a failing steward looks idle, how a monitor avoids lying quietly, and what each number measures |
| [Task board](task-mini-app.md) | The read-only Mini App: where each displayed fact comes from, and why no write crosses the socket |
| [Upgrading](upgrading.md) | Carrying a running steward's obligations across a new version, rehearsed before performed |

## Understand the machine

Each of these owns one responsibility. If two of them seem to describe the same fact,
one of them is wrong, and that is worth fixing.

| Contract | Owns |
| --- | --- |
| [Kernel](kernel-contract.md) | Authority, configuration, sources of truth, the two acceptance paths, Telegram ingress, deliberate limits, concurrency, crash rules and the validation map |
| [Git-native tasks](git-native-tasks.md) | The task document, admission, procedures and targets, privacy, integration and recurrence |
| [Execution lifecycle](execution-lifecycle.md) | Task files, status, closure syntax, continuation and cancellation |
| [Durable world turns](world-turn-durability.md) | Candidate custody, world application, dependent effects and replay |
| [Live task understanding](live-task-understanding.md) | How a running agent's account becomes durable without ending its turn |
| [Rhythms](rhythms.md) | Procedures on a schedule or after quiet periods, world rhythms (`input: world`), due calculation, result owners and operator controls |
| [Failure boundaries](failure-boundaries.md) | Operational errors versus harness defects, and shutdown |
| [Native provider substrate](native-provider-substrate.md) | Which provider-native surfaces the harness talks to directly, and what is still unbuilt |
| [Working environments](working-environments.md) | Where a checkout comes from and when the controller may discard it |
| [Hierarchy and memory](steward-hierarchy-and-memory.md) | Where durable truth belongs: at the lowest scope that owns it |
| [Native record provenance](native-record-provenance.md) | Original records, derived memories and attributable evidence |
| [Task files and Git history](provenance-discovery.md) | Provenance without injected snapshots or copied history, and the bounded ownership query |
| [The world's Git store](world-git-store.md) | What makes a world Git store expensive, and what only looks expensive |
| [Stewardship visualisation](stewardship-visualisation.md) | Inspect lifecycle signals and render prompts through the runtime's own code |

## Work on the harness

The [engineering doctrine](engineering-doctrine.md) sets the standard: understand the
structure that generated the mess, then let the unnecessary complexity disappear. It
includes the method we refactor by (demolish, then fill) and the convergence check
that keeps a refactor honest. The [representation example](engineering-doctrine-example.md)
shows the doctrine on one concrete concept, and
[a nod to the ancestors](doctrine-ancestry.md) names the people we took it from.

The [follow-through environment](follow-through-environment.md) stands a whole daemon
up against a fake Bot API and a scripted native CLI, and asserts did-it-happen facts:
message in, reply out, edit on the accepted world, task landed through the gate. The
[native probes](../experiments/native_sessions/README.md) drive real installed
providers. Neither substitutes for the other, and neither is live deployment evidence.

The [dashboard evidence research](dashboard-evidence-research.md) inventories retained
task/session sources and proposes a bounded read-only detail interface and phased
implementation. It is a revision-scoped proposal, not an implemented API contract.

The [reflex layer survey](reflex-layer.md) maps the fast-decision landscape
(Jev, Laya, semantic routers, process reward models, learning-to-defer) and
reports a measured local prototype: sub-millisecond judgment heads for
should-wake, is-diff-safe and is-task-done, with calibrated escalation of
uncertain cases to the cortex.

For why any of this exists, read [Git, Sleep, and Executive Function for
Non-Executive Fucks](git-sleep-and-executive-function.md). The voice is deliberate.

## What is deliberately not here

Records of individual installations, migration diaries, handoffs, reviews and
superseded proposals. An observation from one host at one revision is evidence about
that host at that revision. It is not a default, and it is not a contract. When one
teaches a lasting lesson, write the lesson into the contract that owns it.

Describe the present, in positive terms. Do not define what the harness does by the
thing it replaced. Keep a fact with its owner and link to it from elsewhere, because
a copied fact is a fact that will go stale somewhere you are not looking. Evidence
needs a revision and a scope. A proposal needs a label. A new reader should not have
to reconstruct last month to find out what is true today.
