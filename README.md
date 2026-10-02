<p align="center">
  <img src="assets/steward-logo.png" width="180" alt="Steward Harness helm">
</p>

# Steward Harness

**Git is a filesystem with infinite history.**

You can put code in it. You can put knowledge in it. You can branch reality, let an
intelligence mess around in a private possible future, inspect exactly what changed,
test that exact universe, and then decide whether it deserves to become real.

Steward is what happens when you take that seriously.

It is not another agent framework. It is a tiny deterministic harness around
autonomous intelligence: the model gets somewhere durable to think, work, remember,
wander, sleep, wake up, and propose changes. The harness keeps the keys.

| Steward is | Steward is not |
| --- | --- |
| A controller around native agent CLIs (Claude Code, Codex, GLM) that bring their own reasoning loops, tools and subagents | An agent framework: no agent graph, role system or tool catalogue of its own |
| Git as the durable world: tasks, decisions, findings and knowledge are files and commits | A memory service, vector store or proprietary session format |
| One daemon with one worker budget, first asked, first served | A workflow engine, scheduler or message bus |
| Exact-revision gates: the commit that was tested is the commit that is pushed | A CI system: gates are your repository's own test and build commands |
| Target drivers that observe what a destination actually serves | A deployment platform: your release system still deploys, and Steward checks that it did |
| A separate Unix identity for everything the model runs, with credentials kept by the controller | A sandbox product: the boundary is OS permissions, or it isn't there |
| Operated from Telegram and the host CLI | A dashboard: the task board browses and changes nothing |

## What is this?

### Private possible futures

A task is one admitted intention attached to one retained Git world.

The agent does not "run a workflow". It gets a private possible future to inhabit: a
checkout of its own, where it can think, edit, commit, change its mind and try again.
Its files and commits retain what happened. When it believes the work is finished,
Steward tests the exact candidate revision, applies the required reviews and policy,
and publishes exactly that revision—or refuses to.

Git gives you what exists now, everything that existed before, and cheap alternate
futures branching from any point in that history. That is most of the primitives an
autonomous system actually needs:

- **files** are human- and model-readable current truth;
- **commits** are immutable snapshots with provenance;
- **diffs** say precisely what changed;
- **branches** are possible futures;
- **merges and rebases** reconcile those futures;
- **remotes** transport accepted reality;
- **checkout and revert** make recovery ordinary.

Durable knowledge lives beside the thing that owns it instead of inside a proprietary
memory palace. Repository knowledge lives in the repository. Organisation knowledge
lives in the organisation world. A future model from a provider that does not exist yet
can wake up, read the files, read the history, and understand what we currently believe
and how we got there.

### Executive function for non-executive fucks

Models are remarkably good at reasoning, language, invention, association, and
recognising that something is deeply fucked.

They are less reliably good at:

- choosing one bounded thing;
- not quietly turning it into twelve things;
- remembering exactly where they got to;
- distinguishing “I tried” from “it happened”;
- checking the exact thing they intend to ship;
- resuming honestly after interruption.

Humans, regrettably, are not universally excellent at these either.

Steward externalises that boring machinery. The intelligence gets a world in which it
can be intelligent. The harness remembers the sequence, owns authority, and refuses to
hallucinate completion.

## The trick

> **The model proposes; the harness disposes.**

A model's proposal is a branch. Only the harness turns an exact, tested revision into
reality.

> **The harness refuses to invent a story.**

If the process dies, another controller looks at Git, the evidence, and the outside
world, and continues. Crash recovery, exact-SHA publication, target observation, task
state and provenance are all the same refusal: nothing is true because someone said it
happened, only because the retained branch, the checkpoint, the test evidence, the
remote refs and the live release say so.

> **One fact, one representation.**

If the accepted outcome is commit `abc123`, we do not also maintain a database field,
provider session state, adapter object and workflow node all claiming to describe the
same reality. The alternative appears to be databases full of task state, proprietary
memory systems, agent graphs, message buses, orchestration fabrics, synchronisation
layers, and adapters translating between six different representations of what was
supposedly one fact. We would rather have one. Usually a Git commit.

That is why Steward deletes machinery so aggressively. It is not minimalism for sport:
every second representation is a translation layer, and every translation layer is a
place for two descriptions of reality to disagree.

> **Imagination is broad. Authority is narrow.**

Models can be given extraordinary freedom precisely because the boundary around reality
is tiny and deterministic. Providers, model-controlled Git, gates, probes and adapters
run as an untrusted identity. The landing credential, tokens, release mutation and service
control stay with the controller. The model may create a brilliant future or a terrible
one. Either way, it created a branch.

## Sixty-second architecture

Underneath the philosophy, Steward is an organisation pipeline built on Git.

Native agents reason in retained task checkouts. Accepted Git refs and their committed
task documents own requests, decisions, findings, and work. A fresh controller can
execute them without prior native sessions or a database pretending to be the canonical
task. The controller owns admission, exact-input gates, publication, credentials, and
target policy.

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
one repository can feed several targets following different refs. Conversation edits
cross their [world acceptance boundary](docs/world-turn-durability.md).

That sounds almost insultingly simple.

It is.

Everything else should justify its existence.

## What's in the workshop

- **Tasks.** One admitted intention, one controller-owned Git ref, one `task.md`
  document. Every decision is a commit. A task can work for days, ask a question
  and wait, be steered mid-flight, and survive the controller being killed.
- **Native sessions.** The thinking is done by the real Codex (App Server) and
  Claude Code CLIs, GLM included through Claude Code, in their own private homes,
  with their own tools and subagents. The harness does not reimplement an agent
  loop. It owns only the authority that loop cannot grant itself.
- **A world.** A Git repository of durable knowledge the steward wakes up in:
  who it serves, what it owns, what it has learned. Conversation edits to the
  world cross an acceptance boundary, like everything else.
- **Rhythms.** Interval triggers for procedures: a nightly review, a weekly
  consolidation, a security pass. Each run is an ordinary task with its exact
  inputs captured.
- **Surfaces.** Telegram for talking to it, with forum topics per conversation. A
  filesystem desk inbox for a web front end. A read-only task board as a Telegram
  Mini App. A host CLI for filing work.
- **Gated publication.** One integrated, single-parent commit per task, rebased
  onto the observed base, run through your repository's own gates and required
  reviews, and pushed with an exact-base lease. What got tested is what lands.
- **Named targets and self-deployment.** Installed drivers observe and apply an
  exact revision and report whether it actually came up healthy. The included
  systemd driver can deploy the harness to itself, with rollback.

## Who it's for

People who want AI to *finish* things, not just generate them. An individual with
more projects than executive function. A small organisation that wants one
accountable steward per repository or per company. Anyone who reads "the agent
has full access to production" and feels their stomach turn.

It is not a hosted service, a chat app, or a no-code builder. You provision a Linux
host, you log in to your providers, you decide what it may touch. It is also not a
personality. The harness has no voice of its own; whatever voice you give your
steward lives in its world, not here. ([GuruGee](https://1puni.com) is one such
voice, built on top. GG is the voice. Steward is the workshop.)

## Five minutes, then an afternoon

Python 3.11+, Git and [`uv`](https://docs.astral.sh/uv/). macOS or Linux is fine
for this part.

```sh
git clone https://github.com/1puni/steward.git steward-harness
cd steward-harness
uv sync --extra dev
uv run steward check config/steward.example.yaml
```

That last command will **refuse**, with "untrusted execution: unavailable". Good.
The example config describes a provisioned host with a separate execution account,
and your laptop is not one. A harness that said yes here would be lying to you.

With Docker available, run the whole loop against a fake Telegram and a scripted
model, with no credentials at all: message in, reply out, world edited, task asks
a question, gate goes green, commit lands.

```sh
ACCEPT_PROJECT=my-first-steward SELFTEST_PROJECT=my-first-steward-selftest \
  FAKE_PORT=8099 sh scripts/follow-through-acceptance.sh
```

Then spend the afternoon on [getting started](docs/getting-started.md), which takes
a fork to a real Linux host and a first task you can actually believe. For an
organisation with several repositories, point your setup agent at the
[onboarding skill](skills/org-onboarding/SKILL.md).

## Guarantees

- **Git owns tasks.** Accepted task refs hold the complete task document and keep
  native work as parents. Inputs, answers and decisions are commits. SQLite keeps
  only conversation turns, provider lineage and incidents: four tables.
- **The whole document is editable.** Agents may revise plans, scope and findings.
  Earlier versions stay in Git, and body text cannot change protected authority
  fields.
- **Exploration survives integration.** Native commits and merges stay on the task
  ref. Main receives one outcome, fixed *before* the gates run. The exact checked
  SHA is what gets pushed.
- **Repair goes back to the owner.** Conflicts and red gates return their exact
  inputs to the task's own session. An unchanged failure is reported as no
  progress, not retried forever.
- **Publication recovers from interruption.** Work, base and candidate are
  retained before the push; recovery observes the remote instead of guessing.
  Cancellation is checked at the only place it matters: the push.
- **Targets observe external truth.** A successful apply is not readiness. The
  driver has to see the exact revision running and healthy.
- **Authority stays separated.** Models, gates and builds run under a separate OS
  identity through the execution broker. Push, transport and deployment
  credentials never enter a model-controlled process.

The [kernel contract](docs/kernel-contract.md) states all of this precisely,
including what happens when a process dies at each boundary.

## One budget, no scheduler

`controller.workers` (default **8**, range 1–32) is the entire scheduling policy.
Task slices, publication, deployment, rhythms, desk messages, probes and result
assessment all compete for the same slots, first asked, first served. No
per-lane reservation, no fairness ordering, because both of those are a
scheduler, and a scheduler wants a starvation story and a tuning knob that
nothing here has needed yet.

A live conversation never waits in that queue. The operator talking to their
steward is answered on the thread that received the message, so the budget can be
full and they still get a reply.

`/pause` is a filter, not a barrier. It stops new task slices, probes, rhythms and
desk intake; repositories that owe a publication keep publishing. Do not use it
as a quiescence point before an upgrade.

A days-long task holds its slot for days. Eight of them hold all eight. That is
the budget working. And it bounds concurrency, not money: nothing here stops a
provider from being expensive.

## Configuration

One YAML file, trusted, owned by the controller. Unknown fields fail validation,
because a field the loader silently ignores is a field the operator believes is
protecting them.

| Block | Owns |
| --- | --- |
| `identity`, `provider` | Steward identity, provider order, model profiles, private native homes, workdir and state database |
| `execution` | The untrusted execution identity and its environment |
| `controller` | Polling, health listener and the worker budget |
| `tasks` | Optional private remote for accepted `tasks/*` refs |
| `repositories` | Remotes, default branches, gates and review requirements; configuring one *is* the authority to work in it |
| `pipelines`, `incident_policy` | Probes, failure confirmation and repair allowance |
| `world` | The Git world for durable knowledge; optional named reconciliation procedure |
| `procedures` | Accepted instructions, access, and a preferred provider, model and effort that falls back through the provider order unless `fallback: false` |
| `rhythms` | Non-overlapping interval (with optional `offset`) or quiet triggers for procedures, admitted only on new input; `input: world` runs one world turn per interval, optionally only on change under `paths`, or `after` another world rhythm |
| `targets` | Desired refs, installed drivers and required evidence |
| `telegram`, `desk` | Optional conversation and result transports |

Start from [the minimal config](config/steward.minimal.yaml); the
[full example](config/steward.example.yaml) shows every block. Providers need
[native runtime setup](docs/native-provider-runtime.md).

## Talking to it

| Command | Purpose |
| --- | --- |
| `steward task add --config C --repository R --title T --owner telegram:N --brief-file F [--priority P]` | File a task from the host CLI ([details](docs/git-native-tasks.md#operator-cli-admission)) |
| `/status`, `/tasks` | Daemon and backlog state |
| `/pause`, `/resume` | Stop or resume new slices, probes, rhythms and desk intake |
| `/model [fast\|balanced\|deep]` | Inspect or change this conversation's profile |
| `/model_family [provider]` | Inspect or switch this conversation's provider |
| `/clear` | Start a fresh conversation generation |
| `/cancel` | Interrupt the active conversation turn |
| `/task show <id>` | Inspect one task |
| `/task model <id> [profile]`, `/task model_family <id> [provider]` | Per-task model controls; `/model` never silently retargets a task |
| `/task confirm <id>`, `/task reject <id> [reason]` | Admit or reject a proposed task |
| `/task answer <id> <text>` | Answer a waiting task |
| `/task note <id> <text>` | Add context, or steer a running native turn |
| `/task retry <id> [note]` | Retry blocked or cancelled work |
| `/task cancel <id> [more ids] :: [reason]` | Withdraw work |
| `/task priority <id> <n>` | Reprioritise inactive work |
| `/rhythm list`, `/rhythm run <id>` | Inspect rhythms, or request a run now |
| `/git reconcile <repo>` | Publish whatever this repository owes, now |
| `/git target <name>` | Converge a target and report what was observed |
| `/git retarget <task> <repo>` | Move never-started work to another repository |

With `controller.health_bind` and `telegram` configured, the same loopback
listener serves the [task board](docs/task-mini-app.md): read-only, signed by
Telegram `initData`, and in need of your own TLS proxy to reach a phone. Register
it in BotFather and set `telegram.task_app_url` to enable task links in chat.
Task cards show titles and short references such as `#a1b2c3d4`; `/task` commands
accept those references alongside full IDs.
Organisation-specific commands go in `telegram.adapter_commands` as a declared argv;
they run without a shell, through the same broker as everything else a model can
reach.

## Where it actually is

Pre-1.0 and MIT licensed, with real work behind it and unfinished business in plain
sight. It has run real organisational work, and those instances found real defects,
which is the point: real worlds find the gaps, and Git carries the lessons home.

It was built for its first month in a private repository, alongside the instances
that ran it, and that repository keeps the complete history. The first commits here
are clean snapshots of it. From September 27, 2026 the harness is developed in the
open, in this repository; the instances keep private copies that follow it, and
their fixes come back as pull requests.

What that does **not** mean:

- **Linux-first.** Production authority needs a Linux host with a separate
  execution user; systemd owns supervision and invocation ownership. macOS is a
  fine place to read, develop and run the suite (there is a
  [warm test runner](scripts/warm-test-runner.sh) for the edit loop), and it says
  nothing about the UID boundary. Only
  [`scripts/linux-boundary-acceptance.sh`](docs/execution-boundary.md#enforcement-and-verification)
  does.
- **Native processes restart between turns.** Retained owners keep private native
  homes, including provider runtime databases, across invocations. Writable turns
  keep their original transcripts in Git and resume the same session. No provider
  process runs queues, goals or background jobs between invocations; retained state
  alone does not establish that a native job will resume. See the
  [storage boundary](docs/native-provider-runtime.md#native-workflows-and-storage-boundary).
- **Not a hosted service.** You provision the host, the execution account, the
  provider logins and the Telegram bot. The guide walks it; none of it is one
  command.
- **`check` is not a working steward.** It proves paths, identities and
  executables. It does not prove a provider login or a deployed journey. The test
  suite runs against fixtures and a scripted provider.
- **Publication pushes to the default branch.** No pull requests, no release tags.
  If your branch rules require PRs, that integration is still an open decision.
  A findings-only task still lands a commit (no file changes, just its trailers), so
  if every push to `main` starts an expensive deploy, know that before you onboard
  the repository.
- **No spending limit.** The worker budget bounds concurrency, invocation deadlines
  bound calls, and nothing bounds total spend. Model pins in `provider.models` are
  not watched either; a newer model can ship while you stay on the old one.
- **Cancelling does not stop a running gate.** Withdrawn work will not land, because
  the push checks, but a gate already running finishes first.
- **Delivery is at-least-once.** Results and replies are retained with receipts and
  survive restarts; a crash between the transport accepting a message and the
  receipt being written can send it twice.
- **One controller, one instance.** No delegation between stewards and no
  multi-world routing today. See
  [ways to use and connect stewards](docs/stewardship-arrangements.md).
- **Reflection is yours to define.** The harness ships the trigger (rhythms), a
  generic [reflection skill](src/steward_harness/skills/steward-reflection/SKILL.md)
  and the exact-input plumbing. It ships no rhythm policies of its own.

## Read on

The [documentation map](docs/README.md) says what each page is for. The short
version:

- [Getting started](docs/getting-started.md): fork to first real task.
- [Give it a body of work](docs/ongoing-work.md): tasks, rhythms, and what the
  harness will not do for you.
- [Kernel contract](docs/kernel-contract.md): authority, sources of truth, crash rules.
- [Engineering doctrine](docs/engineering-doctrine.md): how to think about all of it.
- [Git, Sleep, and Executive Function for Non-Executive
  Fucks](docs/git-sleep-and-executive-function.md): why it is built like this, and
  why live systems rather than feature lists write the roadmap.

## Development

```sh
uv run --extra dev python -m pytest -q -p no:cacheprovider
python3 scripts/check-docs.py
scripts/analyze.sh
```

The suite checks local behaviour. The docs check proves every local link and anchor
resolves, which says nothing about whether the sentence around it is still true.
`analyze.sh` prints a read-only complexity and dead-code worklist. The
[validation map](docs/kernel-contract.md#validation-map) links each boundary to the
check that actually establishes it.

Upgrading a running steward is not a `git pull`. There is no automatic migration,
and an incompatible database is preserved and refused. Read
[upgrading](docs/upgrading.md) first.

Contributions come the same way the harness's own do: a bounded change on a branch,
with evidence. Live stewards fix the harness that runs them and send the fix home.

---

Built by Cookie, with AI in the workshop, from a hundred-year-old boat.
