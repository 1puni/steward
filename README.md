<p align="center">
  <img src="assets/steward-logo.png" width="180" alt="Steward Harness helm">
</p>

# Steward Harness

An organisation pipeline that moves work forward: a request comes in, a model does
the thinking in a checkout, and nothing lands until the harness has checked the exact
revision. Declarative, provider-neutral, and built on Git.

> The model proposes; the harness disposes.

If a design needs a translation layer between two things that should already speak
the same language, the model is broken, and the layer is where it shows. This
harness has one representation for each fact (usually a Git commit) and deletes the
adapters that would have hidden the disagreement.

Native agents reason in retained task checkouts. Accepted Git refs and their
committed task documents own requests, decisions, findings and work; a fresh
controller can execute them without prior SQL task rows or native sessions.
The controller owns admission, exact-input gates, publication and target policy.

```text
operator / rhythm / incident → accepted Git task → native work and checkpoint
                                        │
                             one integrated outcome commit
                                        │
                              gates / required reviews → push
                                        │
                     named targets observe refs → apply exact revision → observe
```

Product tasks finish when their exact accepted outcome is observed on the remote.
Read-only procedure tasks finish as retained evidence. Targets converge independently;
one repository can feed several targets following different refs. Conversation
edits cross their [world acceptance boundary](docs/world-turn-durability.md).

## Start here

New fork? Start with [Fork it. Give it a world. Let it work.](docs/getting-started.md)
For an organisation, use the [onboarding skill](skills/org-onboarding/SKILL.md).
For repository, organisation and personal setups, read
[ways to use and connect stewards](docs/stewardship-arrangements.md).

- [Documentation map](docs/README.md): current contracts and the operator journey.
- [Kernel contract](docs/kernel-contract.md): authority, state owners and concurrency.
- [Engineering doctrine](docs/engineering-doctrine.md): understand the structure, then remove unnecessary machinery.

## Guarantees

- **Git owns tasks.** Controller-private accepted refs contain complete task
  documents and retain native work as parents. Input consumption and decisions
  are commits. SQL remains for transport turns, provider lineage and incidents.
- **The whole document is editable.** Agents can revise plans, scope and findings.
  Prior versions remain in Git. Concurrent edits return both versions to the
  owner for reconciliation; body text cannot change protected authority fields.
- **Exploration survives integration.** Native commits and merges stay on task
  refs. Main receives one single-parent outcome, constructed before gates and
  reviews. The exact checked SHA is pushed with an exact-base lease.
- **Repair uses the owning task.** Conflicts and actionable gate failures return
  exact inputs and diagnostics to authorized cognition. Repeated unchanged failure
  is visible as no-progress; missing intent uses the ordinary question/answer path.
- **Publication recovers from interruption.** Work/base/candidate provenance is
  retained before push. Recovery observes that candidate on the remote without
  retipping away the original task history. Cancellation is checked at push.
- **One execution engine.** Procedures provide instructions, model and access;
  tasks retain concrete runs; rhythms trigger procedures; requirements enforce
  exact-input evidence. Reviewer count and provider family are configuration.
- **Targets observe external truth.** Installed finite observe/apply executables
  own platform details. Successful application alone cannot prove readiness.
  Failures and timeouts are isolated per target.
- **Authority stays separated.** Models, gates and builds use the untrusted
  execution broker. Push, transport and deployment credentials remain outside
  model-controlled processes. OS permissions and protected refs enforce grants.

See [the rewrite contract](docs/git-native-rewrite.md) for privacy, crash boundaries,
accepted-ref discovery, single-controller fencing and migration requirements.

## Run it

```sh
uv sync --extra dev
uv run steward check config/steward.example.yaml
uv run steward run --config /absolute/path/to/steward.yaml
```

The [example configuration](config/steward.example.yaml) is a provisioning
template, not a starting point that happens to need editing: its accounts,
directories, provider logins and remote URLs must already exist on the target
host. Run `check` under the controller's service identity; on an unprovisioned machine it
refuses, correctly, with "untrusted execution: unavailable". It checks the
configured identity, filesystem grants and provider executable access. Linux
invocation ownership additionally requires an actual launch on the provisioned
host. The check does not prove provider
authentication, and it does not prove a complete deployed journey. Do not read
a green `check` as a working steward.

Production authority requires an enforceable execution boundary. The published
host setup uses a separate `execution.user`; see
[controller and agent identities](docs/execution-boundary.md).
Production uses native Linux execution under a separate user. The former container
execution backend is removed; Docker is only an optional local Linux test fixture.

## Configuration

YAML is trusted policy. Unknown fields fail validation, because a field the
loader silently ignores is a field the operator believes is doing something.

| Block | Owns |
| --- | --- |
| `identity`, `provider` | Steward identity, provider order, model profiles, private native homes, workdir and state database |
| `execution` | Untrusted execution identity and environment boundary |
| `controller` | Polling, health listener and one background-worker budget |
| `tasks` | Optional private remote for accepted `tasks/*` refs |
| `repositories` | Trusted remotes, default branches, gates and publication requirements; configuring one is the authority to work in it |
| `pipelines`, `incident_policy` | Probes, failure confirmation and repair allowance |
| `world` | The Git world for durable knowledge; optional named reconciliation procedure |
| `procedures` | Accepted instructions, model and access settings |
| `rhythms` | Non-overlapping interval triggers for procedures |
| `targets` | Desired refs, installed drivers and required evidence |
| `telegram`, `desk` | Optional ingress and result transports |

Each selected built-in provider needs an explicit private `native_homes` entry.
[Native runtime setup](docs/native-provider-runtime.md) defines authentication,
model selection, session continuity and live input.

Rhythms are configured interval triggers for procedures. Each run becomes a task
with captured exact inputs. See [procedures and targets](docs/automatic-deployment.md).

## One budget, no scheduler

`controller.workers` defaults to **8**, accepts **1–32**, and is the whole
scheduling policy. Everything the steward schedules for itself competes for the
same slots: task slices, repository publication and deployment, rhythms, desk
messages, incident probes, result assessment. The executor's own queue is the
assignment — first asked, first served. There is no per-lane reservation and no
fairness ordering, because both are a scheduler, and this does not have one. A
scheduler wants a fairness policy, a starvation story and a tuning knob, and
nothing here has yet needed any of the three.

A pass holds at most one queued entry per owner, so nobody waits behind a
duplicate of themselves. That bookkeeping is not the exclusion. The task lock
and the repository lease are, and unlike an in-memory dict they survive the
crash that would otherwise strand a claim.

A live conversation never enters this budget. The operator talking to their
steward is answered on the ingress thread that received them, so the budget can
be full and they still get a reply. Spare capacity and a responsive
conversation are different claims; neither proves the other.

Pause is a filter, not a branch. It stops the steward taking on work — new task
slices, probes, rhythms and desk intake — while repository convergence and
result assessment keep going. It is not a quiescence barrier, so do not use it
as one before an upgrade.

A days-long task turn holds its slot for all of those days; eight of them hold
all eight. That is the budget working, not a reason to add preemption.

This bounds simultaneous operations. It is not a time budget and not a spending
limit, and nothing here will stop a provider from being expensive.

## Working with tasks

A task owns an accepted ref in `<state_db>.tasks.git` and a `task.md` document.
Its product work uses a `tasks/<id>` branch.
See the [Git-native contract](docs/git-native-rewrite.md) for admission, remote
replication, authorship, private metadata and migration requirements.
Its native session can commit locally; the harness records the slice's findings and
disposition in a checkpoint commit:

- `continue`: run another slice with the retained work and native lineage;
- `ask`: wait for an answer;
- `idle`: make the finished branch eligible for publication.

Investigations use the same closure. Findings are the entire product of a task
that changed no product files; those findings live in the checkpoint message,
so an idle investigation can still run gates and publish. If every push to your main branch
starts an expensive deployment, know that before you onboard the repository.

Malformed closure retains the work and blocks the task, rather than guessing
what the session meant. Ordinary task turns have no routine provider deadline:
a native parent may work for days with its own subagents, and makes its
understanding durable by offering its task account while it keeps working. The
controller accepts that account into task Git and acknowledges the accepted
revision; acceptance neither ends the turn nor grants publication. See
[live task understanding](docs/live-task-understanding-implementation.md).

One conversation can own several tasks. Its model controls and each task's model
controls address separate sessions, so `/model` never silently retargets work
you forgot was running. Task results return for assessment in the owning
conversation; receipt identity and the current transport limits are in the
[kernel contract](docs/kernel-contract.md#task-results).
See [execution lifecycle](docs/execution-lifecycle.md) for closure syntax,
status, notes and cancellation.

## Operator commands

| Command | Purpose |
| --- | --- |
| `steward task add --config C --repository R --title T --owner telegram:N --brief-file F [--priority P]` | File an operator task from the host CLI; see [task admission](docs/git-native-rewrite.md#operator-cli-admission) |
| `/status`, `/tasks` | Show daemon and backlog state |
| `/pause`, `/resume` | Pause new task slices, probes, rhythms and desk intake; repository convergence and results continue |
| `/model [fast\|balanced\|deep]` | Inspect or change the steward conversation's profile |
| `/model_family [provider]` | Inspect or switch the steward conversation's provider |
| `/clear` | Start a fresh ordinary conversation generation |
| `/cancel` | Request native interruption of the active conversation turn, followed by bounded containment |
| `/task show <id>` | Inspect one task |
| `/task model <id> [fast\|balanced\|deep]` | Inspect or change that task's profile |
| `/task model_family <id> [provider]` | Inspect or switch that task's provider |
| `/task confirm <id>` | Admit a proposed operator-validation task |
| `/task reject <id> [reason]` | Reject a proposed task without execution |
| `/task answer <id> <text>` | Answer a waiting task on its retained branch |
| `/task note <id> <text>` | Retain task context; steer an active native turn without reopening stopped work |
| `/task retry <id> [note]` | Retry blocked/cancelled work |
| `/task cancel <id> [more ids] :: [reason]` | Cancel work; an optional reason is retained when inactive work closes immediately, and applies to every id given before `::` |
| `/task priority <id> <n>` | Change inactive task priority |
| `/rhythm list` | Inspect configured procedures, inputs and intervals |
| `/rhythm run <id>` | Request an explicit new task using that procedure and current input |
| `/git reconcile <repo>` | Publish whatever this repository owes, now |
| `/git target <name>` | Converge a configured target and report external observation |
| `/git retarget <task> <repo>` | Retarget never-started work |

Where `controller.health_bind` and `telegram` are both configured, that same
loopback listener also serves a read-only task board at `/tasks`, signed by
Telegram `initData`. It browses; it changes nothing — every action above stays
the one command that performs it. Telegram opens a Mini App over HTTPS only, so
reaching it from a phone means putting a TLS proxy in front, which is the
instance's to provide. See [the task board](docs/task-mini-app.md).

Organisation-specific commands belong in `telegram.adapter_commands` as one
declared argv and argument shape. They run without a shell through the same
untrusted broker. A model turn can reach anything that broker runs, so an act
only the operator may cause — minting a phone pairing link, say — declares
`authority: controller` instead: the controller runs it as itself, with an empty
environment, from an executable the boundary audit proves the agent cannot
replace.

## Where it actually is

Pre-1.0. It has run real organisational work for downstream instances, and those
instances found real defects, which is the point: real worlds find the gaps, and Git
carries the lessons home.

What that does **not** mean:

- The suite runs against fixtures and a scripted provider. `check` proves paths,
  identities and executables, not a working provider login and not a deployed journey.
- Nothing here is a hosted service. You provision a Linux host, an execution account,
  provider logins and a Telegram bot yourself; the guide walks it, and none of it is
  one command.
- Production authority needs the separate execution user. A macOS laptop is a fine
  place to read the code and run the tests, and says nothing about that boundary.
- Native processes currently restart between turns. Writable turns retain original
  transcripts in Git, but use disposable launch homes and do not preserve the
  provider's runtime SQLite databases between launches. This is not durable native
  queue, goal or background-job continuity. See the
  [storage boundary](docs/native-provider-runtime.md#native-workflows-and-storage-boundary).
- Recurring reflection is yours to define. The harness ships the trigger
  (rhythms), a generic [reflection skill](src/steward_harness/skills/steward-reflection/SKILL.md)
  and the exact-input plumbing. It ships no personal or organisational rhythm
  policies, and a reflection policy you have used elsewhere does not arrive with a fork.
- One controller serves one instance's state. There is no delegation transport between
  stewards and no multi-world routing today; see
  [ways to use and connect stewards](docs/stewardship-arrangements.md).

Read [usage after the refactor](docs/usage-regressions.md) before you trust a
guarantee. It separates what is reproduced from what is only code-inspected
from what still needs a forced-crash test, and it names the places where the
documentation has promised more than the implementation delivers. Trust that
page over this one where they disagree.

## Development and upgrades

```sh
uv run --extra dev python -m pytest -q -p no:cacheprovider
python3 scripts/check-docs.py
scripts/analyze.sh
```

The suite checks local behavior. The docs check resolves local links and
heading anchors, which proves a path exists and proves nothing about whether
the sentence around it is still true. The analysis script produces a read-only
complexity and dead-code worklist. The
[validation map](docs/demolition.md#validation-map) links important boundaries
to the checks that actually establish them, including Linux identity and the
full message-to-result fixture. A green macOS suite says nothing about the UID
boundary.

Startup opens the current SQLite schema or creates a fresh database. It
preserves an incompatible one and refuses to reinterpret it. Follow the
[upgrade procedure](migration-handoff.md) before changing an existing instance;
an upgrade includes the adjacent files and Git stores, not just SQLite. Neither
automatic migration nor a database reset is a rollout strategy.

For the opinionated version of why any of this fits together, read
[Git, Sleep, and Executive Function for Non-Executive
Fucks](docs/git-sleep-and-executive-function.md).
