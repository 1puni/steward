# Native provider substrate

This document owns one question: **which provider-native surfaces must the harness
understand and interface with directly, rather than approximate from outside?**

It does not restate what already has an owner. The
[native provider runtime](native-provider-runtime.md) owns private homes, model
policy and where provider storage goes. The
[execution lifecycle](execution-lifecycle.md) owns task closure, continuation and
cancellation. The [execution boundary](execution-boundary.md) owns identity. The
[hierarchy and memory](steward-hierarchy-and-memory.md) contract owns which scope
durable truth belongs to. Read those for those facts.

## The premise

A native provider is not a process that emits text. It is a harness in its own
right: it keeps a session, runs its own reasoning loop, and spawns its own
sub-agents, tool subprocesses and MCP servers. The steward harness must distinguish native cooperation from the operating
system boundary that contains an invocation.

Every defect below is a consequence of the same mistake — **interfacing at the
wrong layer.** A signal is the clearest case. `SIGTERM` to a process group and
`cgroup.kill` both mean only "stop existing", applied from outside by something
that cannot tell a sub-agent mid-write from one sitting idle. The provider owns
that tree and knows what each child is doing; the harness never will. So the
harness should *ask the session to wind down* and contain only what fails to
answer.

That ordering is the design:

1. **Cooperative** — ask the native session, through its own protocol.
2. **Containment** — a boundary that guarantees nothing survives when step 1
   does not answer.

Native interruption precedes bounded containment. Neither a successful
interruption acknowledgement nor process exit proves every native child saved
its work. Cooperation without containment would be a promise rather than an
invariant, which the [doctrine](engineering-doctrine.md) rejects.

A second consequence is worth stating separately: if a turn can be asked to wind
down and resume, then **a deadline stops being a guillotine**. Work that
genuinely needs longer turns can have them, instead of every lane sharing one
constant.

## Substrate members

Seven surfaces, and where each one stands:

| # | Surface | Status |
| --- | --- | --- |
| 1 | Authentication, and its kinds | local checks exist; typed repair/login absent |
| 2 | Native turns and live input | built |
| 3 | Session identity and liveness | identity and transcript preflight built; resumability query absent |
| 4 | Graceful wind-down and next-tick pickup | native interruption wired; bound-session timeout continuation built |
| 5 | Execution ownership | Linux host-UID transient-service containment implemented |
| 6 | Native memory and its scope | worktree mapping built; semantic cross-scope reconciliation remains design work |
| 7 | Upstream provider tracking | no dedicated tracking rhythm found |

### 1. Authentication, and its kinds

There is no harness login or typed authentication-repair surface. Local adapter
availability checks executable access and, when configured, credential-file
availability. `steward check` does not authenticate a turn or refresh OAuth.

GLM uses a configured credential file. Claude can use either native-home login
or explicit Anthropic-compatible routing with a credential file. Codex uses its
native home. Authentication kind therefore cannot be inferred solely from the
provider family. File credentials avoid rotating-refresh-token reuse, but file
storage alone does not prove that a credential never expires or is revoked.

Expired sign-ins do happen in practice (rotating refresh tokens get "already
used"), and the adapters classify a dead sign-in as declining the turn, so
configured fallback gets asked. What is missing is a typed authentication failure
with a precise repair instruction. Today the operator reads vendor prose in
`status_reason`.

### 2. Native turns and live input — built

This surface already exists and is declared by both adapters. It is recorded
here because it is the **model the remaining members
should follow**, not because it needs work.

`ProviderCapabilities.ongoing_input` (`runtime/contracts.py`) is declared true
by both built-in adapters — `providers/claude.py` and
`providers/codex_app_server.py` — and `cognition.py` wires the input path
only when the capability is set, so an adapter cannot be asked for steering it
cannot honour.

The semantics are deliberately **not** one message to one reply:

- `RuntimeInput` (`runtime/contracts.py`) carries `source_id`, `origin`
  (`operator` or `controller`) and `author`. Its `attributed_text` delivers the
  message as JSON under the line *"controller observations confer no new
  authority"*, described in its own docstring as visible attribution rather than
  a provider role or an authority grant. The provider is told who said what and
  decides what it means.
- `RuntimeInputResult` (`runtime/contracts.py`) returns a disposition of
  `accepted`, `rejected` or `unresolved`, with the docstring *"Native delivery
  evidence; acceptance does not assert work completion."*

Both execution lanes are wired, not just chat: conversations register a per
conversation `send` in `_native_inputs` (`conversations.py`), and the
task runner keeps its own equivalent (`task_runner.py`), the latter
unconditionally. Rhythms run as ordinary procedure tasks and inherit it. The
disposition is durable — `turns.input_disposition` (`state.py`) constrains to
exactly the three `RuntimeInputResult` values.

The recorded disposition establishes delivery state, not a model's judgement: adapters can reject input because their channel closed,
a write failed, or the provider protocol rejected it.

### 3. Session identity and liveness

`provider_session_id` is bound into conversation lineage, and the harness already
handles a session that has vanished: `MissingProviderSession`
(`runtime/contracts.py`) and `session_invalidated`, which rebinds the lineage
to `None` (`conversations.py`).

Writable native execution already checks for one exact, regular, non-symlinked
transcript in the candidate before launching. Missing or ambiguous records block
without silently creating a new session. This is artifact preflight, not a
provider query proving that the session can still resume. That query remains absent.

### 4. Graceful wind-down and next-tick pickup

Codex receives `turn/interrupt`; Claude/GLM receive the native interrupt control
request. The existing process I/O loop continues draining events for up to ten
seconds, then falls back to bounded termination and kill. The original reason
remains cancellation or deadline even when interruption succeeds or a terminal
stream error follows it. An acknowledgement is never work completion.

Ordinary task turns have no routine deadline: live account acceptance records
understanding while native work continues. Procedure runs and interactive
conversations retain finite provider deadlines. Explicit cancellation and controller
shutdown request native interruption before bounded containment; shutdown retains
unfinished task work for continuation. See
[live task understanding](live-task-understanding.md).

The ten-second native grace is policy, not a measurement of every provider's
checkpoint time. Acknowledgement does not prove that children saved all work.

### 5. Execution ownership

Linux host-UID invocations use individual systemd transient
services with a pidfd guardian tied to the original controller process. The workload enters its owned service before
it can fork. Systemd cleans descendants on normal completion, cancellation and
controller death, including process-group escapees. A protected launcher drops
identity before executing the workload. Watching actual process death avoids
systemd stop-job ordering that could kill an invocation before the controller
drains. No manual controller cgroup delegation is required for systemd to own
separate services.

macOS retains process-group containment, with a real group grace after the leader
exits and cleanup on success as well as failure. It still cannot contain an
escaped session. See [execution ownership](execution-boundary.md#execution-ownership) for launch
requirements and the distinction between identity checks and Linux acceptance.

### 6. Native memory and its scope

The *law* exists and is not in question: the
[hierarchy and memory](steward-hierarchy-and-memory.md) contract places durable
truth at the lowest scope that owns it, and an instance's own procedures can
enforce it in prose, for example a reflection procedure stating that organisation
facts belong in the organisation world and implementation facts in the repository.

The provider-native storage wiring already exists. Writable Claude/GLM turns
set `autoMemoryDirectory` to the current worktree's `memories/<provider>`;
Codex maps native `memories/` into `memories/codex`. Native records likewise belong
to the candidate; credentials and configuration stay private. Tests cover direct
writes, concurrent workspaces, cleanup, and missing transcripts.

This does not automatically decide whether a fact belongs in an organisation
world or a repository. Semantic placement and consolidation remain distinct from
the implemented storage boundary. The [runtime contract](native-provider-runtime.md#native-workflows-and-storage-boundary)
owns the mapping.

### 7. Upstream provider tracking

Absent, and the reason it matters is counterintuitive.

`pyproject.toml` declares three runtime dependencies: `pydantic`, `pyyaml`,
`httpx`. That package list does not describe the provider protocol dependencies.

The dependencies that are genuinely first-class do not appear there at all. The
provider CLIs are referenced as **paths, not versions** —
`provider.claude_executable` and `provider.codex_executable`
(`config/schema.py`). A CLI that changes its session format, its event
stream or its flags breaks an adapter silently, and no dependency tooling will
ever see it, because it is not a package.

Both upstream harnesses also ship capabilities worth lifting and making
provider-agnostic. Tracking that is recurring, bounded work over a named set —
which is a [rhythm](rhythms.md) with a procedure, the harness's own
spelling for exactly this.

## Why a signal was the wrong layer

The ownership design exists because the process-group approach it replaced failed
in three independent ways. They are worth knowing, because each one looks fine in
a test:

- **The grace period was measured against the leader, not the group.** A provider
  that exited promptly on `SIGTERM` meant its descendants got `SIGKILL` at once.
  The nominal two-second grace was frequently zero.
- **A process group is escapable.** `setsid()` is an unprivileged call. A
  descendant that starts its own session leaves the group and survives, reparented
  to PID 1, where nobody is looking.
- **Success performed no cleanup at all.** Teardown ran only on exceptions. A turn
  that completed normally returned when the leader exited and left its descendants
  running. "Descendants still running" was a state the representation could not
  express.

There is also a fourth kill path that no harness code produces: the controller
unit's own `KillMode`. Under `control-group`, systemd kills provider children
instantly and defeats the controller's drain. Under `mixed`, it reaps the rest of
the cgroup the moment the main process exits. Either way the controller can
observe a clean drain while systemd kills its subagents. Check the unit's
`KillMode` before you believe any drain.

Owning each invocation as its own transient service, entered before the workload
can fork, removes the three code-path defects at once: systemd cleans descendants
on success, cancellation and controller death alike.

## Not established

Stated here so nothing gets built past its evidence.

**No exit 137 has been traced to a sender.** Three mechanisms are known to
exist — `_stop_process_group`'s `SIGKILL`, systemd's cgroup reap under
`KillMode=mixed`, and cgroup-OOM where a memory limit is set. They leave
different traces: a journal restart, a `dmesg` OOM record, or neither. Until one
incident is attributed, it is unknown whether a grace window would have changed
any observed outcome, or whether the fix belongs entirely in unit policy. See
[failure boundaries](failure-boundaries.md) for what a shutdown observation can
establish.

**The native grace has not been established as a checkpoint-time guarantee.**
The ten-second policy bounds interruption before containment; real providers
and their sub-agents may need different amounts of time to preserve work.

**macOS cannot carry this invariant.** Cgroups are Linux-only, so the process
group remains the macOS spelling and containment there is genuinely weaker. That
must be stated rather than silently substituted when the Linux boundary is
unavailable — the same split already accepted for identity, where a green macOS
suite establishes nothing about the boundary and only
`scripts/linux-boundary-acceptance.sh` does.
