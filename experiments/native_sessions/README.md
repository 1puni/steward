# Native session probes

Manual, model-backed experiments for [native session alignment](../../docs/native-session-alignment.md).
These exercise installed provider transports through Steward's process broker
and bounded ongoing-stdin primitive. They do not alter downstream instances.
The task journey uses the shipped runtime, a disposable SQLite database and
bare Git remote, and a locally running release. Temporary workspaces, provider
homes and fixture processes are removed on exit.

Run from the repository root with Python 3.11+, Git, and the named provider CLI
on PATH. Do not use `python -O`: assertions verify the fixture outcomes.

```bash
uv run python experiments/native_sessions/codex_probe.py
uv run python experiments/native_sessions/glm_probe.py
uv run python experiments/native_sessions/codex_probe.py --fanout-only
uv run python experiments/native_sessions/codex_probe.py --fanout-write
uv run python experiments/native_sessions/codex_probe.py --runtime-only
uv run python experiments/native_sessions/codex_probe.py --task-only
uv run python experiments/native_sessions/codex_probe.py --live-sources
uv run python experiments/native_sessions/glm_probe.py --zaude-login-shell --correlation-only
uv run python experiments/native_sessions/glm_probe.py --zaude-login-shell --live-sources
uv run python experiments/native_sessions/codex_probe.py --runtime-only --interrupt-only
uv run python experiments/native_sessions/codex_probe.py --runtime-only --fanout-write
uv run python experiments/native_sessions/glm_probe.py --zaude-login-shell --runtime-only
uv run python experiments/native_sessions/glm_probe.py --zaude-login-shell --memory-only
```

Add `--interrupt-only` to exercise a foreground shell interruption instead of
the continuity/memory journey. This checks the PID written by the fixture and
the native terminal event separately. Cleanup also stops that known fixture
shell if it escaped the provider's process group. This is fixture-specific
cleanup, not a general process-ownership implementation.

Codex `--interrupt-cleanup` additionally lists native terminals before/after
interruption and calls the native cleanup endpoint before checking the fixture
PID. It passed where interruption alone left the shell running. The shipped
adapter now uses that endpoint: `--runtime-only --interrupt-only` passed actual
shell termination, cancellation error, and same-session resume. Native cleanup
is bounded by the broker's cooperative-stop grace; hard fallback does not prove
arbitrary escaped descendants stopped.

For Codex, `--disconnect-only` sends a uniquely marked steering input, waits for
the local transport buffer to drain, and disconnects before consuming its
acknowledgement. It resumes the native thread and looks for the marker in
history. This distinguishes local byte delivery from durable native evidence;
it never retries the input or treats missing history as proof of rejection.

Codex copies only `~/.codex/auth.json` into a private temporary home; an alternate
provider auth file can be supplied with `--auth-file`. It pins `gpt-5.6-sol`,
disables apps and goals for this fixture, and uses workspace-write/never-approve.
No personal configuration or plugin directory is copied. The probe runs under
the invoking OS account; it does not prove production identity isolation.

GLM expects `ZAI_AUTH_TOKEN` in its environment. On the inspected local Zaude
setup, `--zaude-login-shell` obtains that variable through Zaude's clean zsh
login-shell procedure without printing it. It calls Claude Code directly with
the z.ai Anthropic-compatible endpoint and `glm-5.3`; there is no provider
fallback. The Zaude wrapper's non-TTY buffering would prevent live input.

GLM uses a fresh `CLAUDE_CONFIG_DIR`, explicit empty setting sources, strict MCP
configuration, `acceptEdits`, allowed tools, and the native sandbox with
`failIfUnavailable` and `allowUnsandboxedCommands: false`. It requests a USD 1
provider cap per connection; this is not evidence of cross-provider allowance
accounting. Both probes have a 150-second deadline per connection and stop/reap
their process groups in cleanup. Their prompts' “no network” restriction applies
only to synthetic marker work; it is not a production network policy.

## Shipped task journey

`--task-only` exercises ordinary task admission in a real Codex steward session,
a separate native repository task that provides its own closure, the configured
gate, exact-SHA publication to a disposable bare remote, and automatic deployment
convergence. A local service controller starts the staged Python HTTP application;
its health response reports the SHA captured from its loaded release directory.
The result outbox then resumes the original owning native session and accepts
its assessment. Its Telegram sink is in-memory; no message is sent to a person.

Observed 2026-09-06 with Codex 0.153.4: the complete journey passed. Exactly three
native calls ran (admission, task, assessment), with no separate wrap-up. Original
native records followed both Git worlds, credentials remained private, and
replaying source/deployment/outbox state did not repeat admission, cognition,
service restart or delivery. Tested/published/healthy release:
`47ed028af56e799b8078abf3ec791c3cafe2de73` in the disposable remote.

This proves a runnable standard-library Python artifact on the invoking account.
It does not establish systemd/split-UID installation, dependency builds, live
Telegram steering or live rollback. Existing deterministic convergence tests
cover failed health and rollback, including absence; B08 remains open for the
complete downstream artifact/identity boundary.

## Durable live sources journey

`--live-sources` extends the shipped task journey above. After its real gate,
exact publication and healthy release, the original owning native session starts
world work with an active shell. An authenticated correction and the task result
outbox enter that execution through the production conversation input callbacks.
The durable sources await the same world acceptance; replay of either source
returns that receipt without starting another model call or admitting work.

Observed 2026-09-06 with Codex 0.153.4: passed. The accepted world contained the
corrected marker and a separate observation file carrying the exact release SHA,
`e91446afe1b322abcdd27ad0b310f8b3dca2d878`. Both operator and controller origins
were preserved. Exactly three calls ran: admission, repository task, and active
owner execution. Result assessment did not start a fourth call. Source,
deployment and outbox replay repeated no cognition, admission, service restart,
or delivery; the native session identity stayed the same.

The active Codex transport carries an attributed text envelope through
`turn/steer`; this is visible controller-origin evidence, not a native tool-output
role or a new authority grant. This fixture uses disposable repositories, local
process service management, and an in-memory Telegram sink. It does not establish
real Telegram networking or split-identity service cutover.

The same full journey also passed through the shipped GLM/Claude streaming
adapter on 2026-09-06. After native Write/Read activity began in the owner's
candidate, both the operator correction and controller result were delivered
and completed in the same execution. The accepted marker was CORRECTED, and
observation.txt contained tested/published/healthy SHA
`cfdc6ca5ccee193d56590e00e256e8e841ad357f`. The original native session
`2fb49238-9c21-442a-93e1-98fc6986dbb8` remained the owner. Exactly three calls ran;
source/deployment/outbox replay repeated no cognition, admission, service restart
or delivery. Its file-tool fixture preserves the production sandbox and the
Bash limitation recorded below.

## Claude/GLM native command correlation

`--correlation-only` uses Claude Code 2.1.220 through GLM and the broker. It
records native command identities and ordering without exposing credentials or
full transcript content. Observed 2026-09-06: the initial input and an input
folded into its active Bash operation each emitted `command_lifecycle` for their
client-supplied UUIDs. The folded command completed before the first result; the
initial command completed after it. Another input offered at that result
boundary received its own later result and command completion. Both corrections
produced the expected file contents. A user-message echo alone supplied neither
of those completion facts.

The installed CLI's own command-lifecycle schema documents this ordering and
marks result `user_message_uuid` as optional. The shipped GLM streaming
shared-write fanout/resume probe (`--runtime-only`) initially exposed that missing
optional field after native Agent work. The adapter now uses the native serial
command root when that timing field is absent, while rejecting a conflicting
explicit identity. The repeat passed: two native children wrote ALPHA/BETA,
the parent aggregated them, and the same native session resumed successfully
(`9da4cdad-593e-44ff-b75a-f6b10bf3eb32`). No result-count inference is used.
The Python SDK currently does not expose command-lifecycle messages in its
[typed messages](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py);
adding that client would not replace this required correlation evidence.

The initial full GLM deployed-result fixture could not start Bash inside the
resumed world worktree. Native tool results reported E2BIG: approximately 1 MB
of command arguments, 1.8 KB of environment, and a sandbox profile with 207
filesystem deny paths, including three registered Git worktrees. The transcript
recorded the correct current candidate cwd and the intended shell command;
wrong-workspace prompting was not the observed failure. The fixture's subsequent
active phase uses native Write/Read tools; production sandbox policy remains
unchanged. These statistics identify the observed boundary, not a complete
upstream diagnosis or proof that every deployment host is affected.

## What is checked

- Input arrives during native execution and the actual target file reflects
  the correction, rather than relying on the final reply.
- A commit reflection and subsequent work reuse the connection/session.
- Codex rejects steering after its turn has completed.
- Native session records are written through directory symlinks into the world.
- An ordinary Git checkpoint retains the fixture and native evidence.
- Disconnect/reconnect resumes the explicit native session ID.
- GLM writes native auto memory through `autoMemoryDirectory`; a fresh session
  reads that memory and produces the expected artifact.

The Codex memory directory is mapped, but automatic background memory generation
is not exercised. Resume retains the private runtime databases alongside the
world-backed records: these probes do not establish restoration from Git alone.
The GLM fresh session can also inspect other files in the shared world; this is
a practical shared-read journey, not a controlled test excluding every other
source of the marker.

The broker is configured for this local inert probe, with explicit inheritance
of the two temporary provider-home variables. It strips controller credentials
and supplies the actual account names. A dedicated production UID/GID and
protected controller paths still require the complete-instance broker.

## Observed on 2026-09-06

After replacing the probe's private subprocess I/O with the shared runtime
primitive, the Codex continuity/checkpoint/resume journey and GLM interruption
journey both passed again. The shared primitive's regression suite completed
with 559 passed, 1 skipped (183.48 seconds).

The full GLM journey subsequently passed through the broker as well: active
correction, reflection, native memory/session writes, Git checkpoint,
same-process continuation, explicit resume, and a fresh session producing the
marker from shared memory. The assertions verified the actual files at each
step; a successful final reply alone was insufficient.

Installed Codex 0.153.4 passed steering on an observed command item, the exact
`no active turn to steer` rejection afterward, reflection, Git checkpoint,
same-connection continuation, and explicit resume. `thread/read` returned four
turns. The isolated home's default app discovery prompted an explicit
`features.apps = false`; a new local home alone does not disable account-backed
apps.

Claude Code 2.1.220 against GLM passed correction input and echo during a Bash
tool-use exchange, applied the correction before one successful result, then
continued and resumed. With native memory enabled and isolated configuration,
it created `MEMORY.md` and a topic file in the requested directory, recorded its
session in the mapped projects tree, and a fresh session used the shared memory.

Earlier probes failed on sandboxed DNS/keychain access, missing account-name
environment variables, and ambiguous relative marker paths. The latter produced
a successful provider reply but wrote under `$TMPDIR`; the artifact assertion
rejected it. Current fixtures specify exact paths where that failure occurred.

## Still unproven

`--fanout-only` is a narrow correlation probe. It provisions two read-only
custom native children in the private
fixture home, asks one parent to spawn both and aggregate their two fixed
tokens, and observes App Server `subAgentActivity` records. It requires both
children to complete before the parent turn may be checkpointed. The fixture
does not create harness tasks for children, and it leaves no records outside
the disposable world.

The first run incorrectly classified a child's terminal event as the parent's.
That diagnosis is withdrawn: the helper now matches both thread and turn IDs.
Codex 0.153.4 then passed both child completions, parent aggregation, and Git
checkpoint `9504e5855144d9db22c68589b45ac8bfd233aef2`. Deterministic regression
tests cover interleaved child/old-turn completions and failures. This narrow
fixture does not select production child permissions or concurrency limits.

`--fanout-write` gives both native children tools and a shared writable workspace.
Each searches synthetic input files for its own randomly generated marker and
writes a separate artifact. The parent reads and combines them. Assertions also
inspect both child native histories for actual tool work. Codex 0.153.4 passed,
with checkpoint `a9724992a96dac9158a9727f227b4856b5f67a0a`.

`--runtime-only` exercises the shipped runtime through `Cognition`, including
the execution broker. Codex passed correction during a shell command, accepted
input evidence, actual corrected contents, process exit and explicit resume.
GLM passed two native task-start records, shared child writes, parent aggregation
and explicit resume. An initial GLM run rejected a post-result event before
diagnostic instrumentation was present; its precise event type was not captured.
Later instrumented runs passed. A separate deterministic regression now permits
informational `task_notification` after a result while still rejecting a second
terminal result. This does not claim the initial failure was diagnosed.

Codex `--runtime-only --fanout-write` passed two observed native child starts,
shared artifacts, parent aggregation, and adapter-owned terminal cleanup. An
earlier invocation timed out at 150 seconds without diagnostic tracing; the
instrumented repeat passed. The timeout's cause is not established.

The production Codex probes now keep native storage private, without the older
transport fixture's sessions/memories symlinks. Writable turns export native
thread-read snapshots through the broker. `--runtime-only` verified two recorded
turns after resume and an exact `GitWorld` checkpoint; `--runtime-only --fanout-write`
verified snapshots for the parent and both observed children. The first checkpoint
attempt found a missing initial fixture commit; the seeded repeat passed. This
is readable native evidence, not proof that Git alone can restore a Codex session.

`--memory-only` exercises the shipped GLM adapter's worktree-local native memory.
It saves a random convention to native memory, checkpoints through `GitWorld`,
clones the Git repository, and recalls it with a fresh native home/session. No
session files or runtime databases are copied; the marker is absent from the
checkpoint's neutral episode text. This passed on 2026-09-06. It proves Git-only
**memory** restoration, not native conversation/session restoration or arbitrary
background-writer quiescence. Runtime probes leave native session storage in the
private home, unlike the older explicitly symlinked transport experiment.

These shipped-adapter probes do not connect Telegram or desk ingress, nor do
they establish cancellation of escaped process groups. The GLM adapter still
has a single initial input. See [runtime boundary](../../docs/native-provider-runtime.md).

The interruption follow-up on 2026-09-06 passed through GLM: its control response
acknowledged interruption, the terminal result was `error_during_execution` with
`is_error: true`, the shell stopped, and the same session answered `RECOVERED`.
The wire request was checked against the installed Python Agent SDK's
`interrupt()` implementation. This is cancellation evidence, not a generic
successful result and not proof that every such error means cancellation.

Codex 0.153.4 failed the shell-stop assertion twice: it acknowledged interruption
and emitted `turn/completed` with `status: interrupted`, but the fixture shell
remained running in its own process group after three seconds. The inspected
process was a live `Ss` shell, not a zombie. Do not release workspace ownership
on that event alone. App Server's documented `command/exec/terminate` controls
explicit standalone commands; it does not establish control over native tool
commands. No production workaround has been selected.

The disconnect follow-up drained the local input buffer before teardown and
resumed the native thread successfully, but its history did not contain the
unique input marker. This is an unresolved delivery outcome, not a successful
recovery or proof that the provider never accepted the input. The experiment
does not consume acknowledgements before disconnect; the provider might already
have produced one. Fixture cleanup also stops the known shell, so this is a
controller acknowledgement-loss boundary, not a network-fault simulation.

The probes do not establish automatic ambiguous-delivery recovery after disconnect,
idempotent provider resubmission, general interruption guarantees, coherent capture during
background memory writes, or atomic native-record/Steward-world acceptance.
They do not integrate Telegram, task-result attribution, or durable source IDs
with active native execution. Those boundaries require the next engineering
step; passing a model-backed happy path does not remove them.
# Direct native storage follow-up, 2026-09-06

The operator challenged the need for transcript exports. Both commands passed:

```bash
uv run python experiments/native_sessions/direct_storage_probe.py codex
uv run python experiments/native_sessions/direct_storage_probe.py glm --zaude-login-shell
```

With Codex 0.153.4 and Claude Code 2.1.220 via GLM, each probe observed original
JSONL under `artefacts/<provider>/` while a foreground shell was still active,
then verified native growth of that file. After process exit it checkpointed
through `GitWorld`, cloned the Git files, and resumed the same native session
from its transcript path using a fresh private runtime home. The second turn
recalled a random marker without tools. Codex used App Server's `thread/resume`
with `path`; Claude accepted a transcript path through `--resume`.

Each home had its own fixed directory mappings. No shared link was retargeted;
the original world's transcript remained unchanged by the restored run. Provider
credentials were separately provisioned outside Git. No native database or cache
was restored. The fixture does not establish native memory generation, arbitrary
background-writer quiescence, concurrent production integration, or restoration
of every native sidecar/tool artifact. Production runtime code is unchanged.
The [storage/provenance draft](../../docs/native-record-provenance.md) records
the proposed directory meaning and the actual native controls.

## Installed Claude/GLM queue check, 2026-09-08

The exact five-file queue change committed as `6e403eb` passed five calls using an
installed Claude Code **2.1.259**, using GLM **5.3** through the enforced UID/GID
978 broker. The isolated candidate was archive `53da518` plus those overrides;
no concurrent state/daemon changes were included. Its 126 source/dependency
manifest files and configured credential file remained unchanged throughout.

Actual `TaskRunner.prepare` created a file, accepted `continue`, resumed the same
native UUID, changed the file and accepted `idle`. Both checkpoints left clean
retained Git history. Two `Cognition` read-only calls retained their native UUID
and recalled prior context without changing the working tree. All four calls
had no ongoing-input hook, and emitted queued, started, completed result, then
command completion. Disposable remote Git, publication and deployment remained
untouched. Admission used private desk fixture state; there was no external desk
transport or native admission claim.

The fifth call started an intrinsically 90-second foreground writer under a
60-second provider deadline. A separate host observation verified the live
UID-978 writer and growing heartbeat before timeout. Its sandbox PID differed
from its host PID. The runtime raised `ProcessTimeout`, all broker-owned
provider PIDs were reaped, and no process belonging to the reserved scratch or
writer remained. This checks the actual writer's lifetime, rather than merely
accepting a terminal event.

Exactly five native calls ran, without fallback or provider retry. An earlier
fixture setup rejected an unsupported transport name before any native call;
that fixture alone was corrected to `desk`. All three reserved VPS namespaces
were removed after process checks, and the deploying session's coordination
notes record closure. Local evidence is retained in
`/private/tmp/steward-queue-verification.md`, the corresponding
`steward-queue-live-evidence.json`, and the frozen candidate manifest.

This proves the shared Claude CLI transport through GLM authentication. It does
not establish Claude OAuth availability, live production rhythm acceptance,
publication/deployment, or arbitrary escaped-descendant cleanup. Existing
ongoing-input correlation checks remain separate evidence. The exact staged
commit additionally passed 96 affected local tests after independent candidate
protocol review; the candidate itself passed 127 focused tests.
