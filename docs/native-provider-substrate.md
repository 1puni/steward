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

Native interruption now precedes bounded containment. Neither a successful
interruption acknowledgement nor process exit proves every native child saved
its work. Cooperation without containment would be a promise rather than an
invariant, which the [doctrine](engineering-doctrine.md) rejects.

A second consequence is worth stating separately: if a turn can be asked to wind
down and resume, then **a deadline stops being a guillotine**. Work that
genuinely needs longer turns can have them, instead of every lane sharing one
constant.

## Substrate members

Seven surfaces, initially reviewed on 2026-09-21. The original audit used checkout
`1d6ff07e07eb8ce657680413136a3b49b8e8f334` on a downstream migration branch,
not local `main`. The implementation described here includes the subsequent
working-tree repair reviewed on 2026-09-23; deployment is a separate acceptance
boundary.

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

The original investigation reported a repeated Codex refresh-token failure on
a downstream instance on September 16 and 21, with raw vendor prose in `status_reason`.
Those live records were not independently reverified in this code audit. A typed
authentication failure and a precise repair remain useful future work; no new
login workflow is claimed here.

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

The original investigation reported three folded inputs on a downstream instance, two
accepted and one rejected. The database disposition establishes delivery state,
not a model's judgement: adapters can reject input because their channel closed,
a write failed, or the provider protocol rejected it. The dated live rows were
not independently re-read during this audit.

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
unfinished task work for continuation. See the
[live-understanding implementation](live-task-understanding.md).

The ten-second native grace is policy, not a measurement of every provider's
checkpoint time. Acknowledgement does not prove that children saved all work.

### 5. Execution ownership

Linux configured host-UID invocations now use individual systemd transient
services with a pidfd guardian tied to the original controller process. The workload enters its owned service before
it can fork. Systemd cleans descendants on normal completion, cancellation and
controller death, including process-group escapees. A protected launcher drops
identity before executing the workload. Watching actual process death avoids
systemd stop-job ordering that could kill an invocation before the controller
drains. No manual controller cgroup delegation is required for systemd to own
separate services.

macOS retains process-group containment, with a real group grace after the leader
exits and cleanup on success as well as failure. It still cannot contain an
escaped session. The former container execution backend has been removed.
See [execution ownership](execution-boundary.md#execution-ownership) for launch
requirements and the distinction between identity checks and Linux acceptance.

### 6. Native memory and its scope

The *law* exists and is not in question: the
[hierarchy and memory](steward-hierarchy-and-memory.md) contract places durable
truth at the lowest scope that owns it, and instance procedures enforce it in
prose — one downstream instance's reflection procedure states that organisation
facts belong in the org-world and repository implementation in its own
repository.

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

## Execution ownership before the repair

Historical code evidence at `1d6ff07`, 2026-09-21. The line references below
describe that pre-repair checkout, not the current implementation. These defects
explain the repair; they do not attribute a particular production kill.

### The host-UID path

The provider starts with `start_new_session=True` (`runtime/process.py:137`), so
it leads its own process group, and stopping is `os.killpg`
(`runtime/execution.py:235`, falling back to signalling the immediate process on
`PermissionError`). There are two stop paths and they differ:

- **Cancellation** — `stop()` (`runtime/process.py:166-175`) sends `SIGTERM` to
  the group and nothing else. It starts no grace timer and does not shorten the
  invocation deadline. A provider that ignores `SIGTERM` therefore holds its
  execution slot until the *original* deadline, which was 900s on one downstream instance, and the
  error that finally surfaces is `ProcessTimeout` rather than a cancellation.
- **Teardown** — `_stop_process_group` (`runtime/process.py:272-279`) sends
  `SIGTERM`, waits up to `_TERMINATION_GRACE_SECONDS` (2.0,
  `runtime/process.py:22`), sends `SIGKILL`, then waits.

Three defects, with different causes. They must not be merged:

**The grace period is measured against the leader, not the group.** The wait at
`runtime/process.py:274` is guarded by `if process.poll() is None`. A provider
that exits promptly on `SIGTERM` means its descendants receive `SIGKILL`
immediately; if the leader was already gone when teardown began, there is no wait
at all. The nominal two seconds is frequently zero, and this produces abrupt
kills with no process-group escape involved.

**Ownership is a process group, which is escapable.** `setsid()` is an
unprivileged call. A descendant that starts its own session leaves the group and
becomes both invisible and unreachable; it survives, reparented to PID 1, inside
the service cgroup. The class docstring concedes this: *"children that escape
need separate ownership"* (`runtime/process.py:86`).

**The success path performs no cleanup at all.** `_stop_process_group` is
reachable only from `except BaseException` (`runtime/process.py:254-256`). A turn
that completes normally returns as soon as the leader exits and its pipes close,
leaving descendants running. "Done" is defined as the leader's exit, and
"descendants still running" is a state the representation cannot express.

One related correction: the docstring at `runtime/process.py:115-119` argues that
the stopper is stored nowhere and so never needs expiry or deletion.
`Cognition._active` does store it (`cognition.py:102`) and does delete it on
completion (`cognition.py:251`). The defensible claim is that this is *ephemeral
routing* rather than durable cancellation truth — durable withdrawal is separate,
and stays separate.

### The container path already solves this

`runtime/container.py:186-202` runs **every invocation as its own transient
systemd service**:

- `systemd-run --unit=steward-exec-<uuid>.service`, with `BindsTo=` and `After=`
  the controller unit, so controller death tears the invocation down without
  relying on a Python `finally` block.
- `ExecStartPre=/bin/mkdir <subgroup>` establishes cgroup membership *before* the
  workload can run, which closes the fork race that moving a PID afterwards would
  leave open.
- `runc exec --cgroup <invocation>` places the workload in that subgroup.
- `ExecStopPost=<helper>` runs the cleanup on normal exit, on cancellation **and**
  on controller death.

A cancellation acceptance test, `test_container_execution.py:216`, is named `test_streaming_cancellation_cleans_descendant_and_preserves_peer`, and
its escapee is literally `os.setsid(); time.sleep(100)` — the exact escape the
old host-UID path cannot reach. It asserts cancellation completion and peer
survival, but does not directly check the escaped child. A separate deadline test
checks that an escaped writer cannot write later. Neither test runs without its
provisioned Linux fixture.

Two qualifications, both load-bearing for the lift:

- **The cleanup is not graceful.** `cleanup()` (`runtime/container.py:215-224`)
  writes `cgroup.kill` *immediately*, then polls `cgroup.events` until
  `populated 0`. There is no `SIGTERM`-and-wait phase for the workload subgroup.
  The unit's own `TimeoutStopSec=2s` governs the unit's cgroup, which holds the
  `runc exec` leader — not the workload. So the requested graceful phase lands
  inside this one function.
- **The sampled instance configs use host execution.** One records that a
  container *"was tried and withdrawn"*; another declares none. These configurations do not establish the execution
  backend of every live instance.

### Unit policy is a third kill path

One downstream provisioning script explains why the controller unit uses
`KillMode=mixed`: `control-group` killed provider children instantly, defeated
the controller's own drain, and turned every deploy into a blocked task — *"forty
deploys in one day"*. The same comment states the consequence plainly: *"systemd
reaps the rest of the cgroup the moment it exits."*

So on controller stop, systemd `SIGKILL`s every provider descendant as soon as
the main process exits. The controller observes a clean drain while its
sub-agents are killed by systemd — an outcome no harness code path produces and
none can see. This is deploy-correlated by construction.

The original investigation recorded these properties of one downstream instance on 2026-09-21;
this audit did not independently reread them: `KillMode=mixed`, `KillSignal=15`,
`TimeoutStopUSec=21min 40s`, `OOMPolicy=stop`, `MemoryMax=infinity`,
**`Delegate=no`**. Two consequences. The unit has no local memory
cap, but ancestor limits must also be inspected before excluding cgroup-OOM.
Another instance configures `MemoryMax=6G`. The
`Delegate=no` setting prevents supported direct management of a controller
subtree; it does not prevent asking systemd to own separate transient services.

## Not established

Recorded so the repair is not built past its evidence.

**No exit 137 has been traced to a sender.** Three mechanisms are now known to
exist — `_stop_process_group`'s `SIGKILL`, systemd's cgroup reap under
`KillMode=mixed`, and cgroup-OOM where a memory limit is set. They leave
different traces: a journal restart, a `dmesg` OOM record, or neither. Until one
incident is attributed, it is unknown whether a grace window would have changed
any observed outcome, or whether the repair belongs entirely in unit policy.
Prior operational records on this harness have overstated what shutdown
observations proved; see the failure-boundaries contract for what a shutdown observation can establish.

**The native grace has not been established as a checkpoint-time guarantee.**
The ten-second policy bounds interruption before containment; real providers
and their sub-agents may need different amounts of time to preserve work.

**macOS cannot carry this invariant.** Cgroups are Linux-only, so the process
group remains the macOS spelling and containment there is genuinely weaker. That
must be stated rather than silently substituted when the Linux boundary is
unavailable — the same split already accepted for identity, where a green macOS
suite establishes nothing about the boundary and only
`scripts/linux-boundary-acceptance.sh` does.

## Provenance

The original document attributes the execution-ownership findings to two independent reviews of this
repository on 2026-09-21 — one by Claude within the working session, one by a
separate `codex exec` session given the problem statement and no proposed
solution. It reports that they converged on the same architecture. The container backend's
existing machinery, the leader-versus-group grace defect, the absence of cleanup
on the success path, and the `KillMode` kill path were found by the Codex review;
the full output was reported as retained outside the repository. Those external
review transcripts were not reread during this repair. The historical code
findings above were checked against the named checkout.

The September 23 repair also incorporates the work identified by the Claude
session `notification-derivation-ownership`: route eligibility before dispatch,
durable unowned target transitions, settled-target eligibility before queuing,
and deployment observation without unrelated build-readiness probes. The
[target contract](automatic-deployment.md) owns those behaviors.

The combined local suite passed **893 tests, with 29 skipped**. Additional
adapter/process/task tests passed **105 tests**, including actual subprocess
fixtures for Codex cancellation and deadlines. These prove protocol handling,
bounded fallback, retained writes and continuation against scripted providers;
they do not measure real provider checkpoint latency. Linux ownership and full
daemon acceptance are separate from this macOS result.

Native Linux acceptance through `scripts/linux-boundary-acceptance.sh --docker`
established **17 checks**: the final ownership run passed all **12** cases, with
the **five** identity/deployment cases passing in the preceding broader run.
Docker supplied only the local Linux test host: the workload used real systemd
services, cgroup v2 and pidfds. The checks cover escaped descendants on success,
cancellation and launcher-client death, peer survival, controller death, stale
controller refusal, identity/privacy, large input and HTTP deployment rollback.
A real service-stop test verifies that native work can checkpoint while the
controller drains, before actual controller exit triggers containment. The same
runner executes directly on a provisioned disposable Linux host without Docker.
These are fixture checks, not a production rollout or live provider timing test.

The [full-daemon fixture](follow-through-environment.md) passed **12 assertions**
for conversation, cancellation and poll recovery, followed by **22 assertions**
on the final guardian-based self-deployment snapshot. That final scenario covers
an unowned target report, task question/answer, publication, exact-revision
health, controller replacement and a subsequent accepted conversation. Its dated
record also documents the accidental loss of the old `selfimprove` fixture and
the new safeguards against removing existing test projects. Release/archive and
runner-safety regressions passed in a separate **75-test** focused run.
