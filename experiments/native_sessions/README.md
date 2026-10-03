# Native session probes

Manual experiments that drive **real, installed** provider CLIs (Codex and
Claude Code, including Claude Code pointed at GLM) through Steward's own process
broker and its bounded ongoing-stdin primitive. The unit suite speaks to scripted
fake providers; these speak to the real thing, spend real tokens, and exist
because a fake provider can only ever confirm what you already believed about the
protocol. The task journey runs the shipped runtime against a locally running
release.

They run in disposable temporary workspaces, homes, bare remotes and SQLite
databases, and clean up after themselves. They do not touch any running steward.
The [native provider runtime](../../docs/native-provider-runtime.md) is the
contract these probes check against.

The task-call transport also has a no-inference probe:
`uv run python experiments/native_sessions/task_call_probe.py`. It uses a clean
Codex home and the installed App Server's MCP discovery/tool-call methods to
admit two tasks, replay a submission, inspect and cancel work while the
controller conversation remains running. It needs no provider login, spends no
model tokens, and does not prove a deployed release or split-UID enforcement.
The latter task-call check is in `tests/test_boundary_acceptance.py` and runs
with the Linux boundary acceptance script.

## Running them

From the repository root, with Python 3.11+, Git and the provider CLI on `PATH`.
Do not use `python -O`: the assertions *are* the probe.

```bash
uv run python experiments/native_sessions/codex_probe.py                 # continuity, checkpoint, resume
uv run python experiments/native_sessions/codex_probe.py --runtime-only  # through the shipped Cognition path
uv run python experiments/native_sessions/codex_probe.py --task-only     # a whole task journey
uv run python experiments/native_sessions/codex_probe.py --live-sources  # input arriving during work
uv run python experiments/native_sessions/codex_probe.py --fanout-only   # native subagents, read-only
uv run python experiments/native_sessions/codex_probe.py --fanout-write  # native subagents, shared writes
uv run python experiments/native_sessions/codex_probe.py --runtime-only --interrupt-only
uv run python experiments/native_sessions/codex_probe.py --disconnect-only  # lose the ack on purpose
uv run python experiments/native_sessions/glm_probe.py --login-shell --runtime-only
uv run python experiments/native_sessions/glm_probe.py --login-shell --memory-only
uv run python experiments/native_sessions/glm_probe.py --login-shell --correlation-only
uv run python experiments/native_sessions/direct_storage_probe.py codex
uv run python experiments/native_sessions/direct_storage_probe.py glm --login-shell
```

**Codex** copies only `~/.codex/auth.json` (or `--auth-file`) into a private
temporary home. No personal configuration or plugins come along. Apps are
explicitly disabled, because a fresh home alone does not turn off account-backed
apps.

**GLM** runs Claude Code against the z.ai Anthropic-compatible endpoint and
expects `ZAI_AUTH_TOKEN` in the environment. `--login-shell` reads it from a clean
zsh login shell without printing it. There is no provider fallback.

Everything runs under the invoking OS account. None of it proves production
identity isolation; that is
[`scripts/linux-boundary-acceptance.sh`](../../docs/execution-boundary.md#enforcement-and-verification).

## What they check

- **Input during work.** A correction sent while a native shell is running must
  show up in the actual target file, not merely in the final reply.
- **Continuity.** A checkpoint, a continuation on the same connection, and an
  explicit resume of the same native session ID.
- **Whole journeys.** `--task-only` runs admission, a native task with its own
  closure, a real gate, exact-SHA publication to a bare remote, a local service
  deployment whose health reports the SHA it loaded, and assessment back in the
  owning session. `--live-sources` then feeds an operator correction and the task
  result into the owner's running turn, and checks that replaying any of those
  sources repeats no model call, admission, restart or delivery.
- **Subagents.** Two native children, the parent aggregating their outputs, with
  terminal events matched on *both* thread and turn ID.
- **Memory.** GLM writes native memory, the world is checkpointed and cloned, and
  a fresh home and session recall it from Git alone.
- **Interruption.** A foreground shell is interrupted, the fixture's PID is
  checked separately from the native terminal event, and the same session
  resumes.

## Lessons they taught

These are the findings worth carrying, stated as rules:

- **An echo is not completion.** Claude Code's user-message echo proves nothing.
  Completion is the `command_lifecycle` event for the client-supplied UUID, and
  its `user_message_uuid` on results is optional, so the adapter falls back to
  the native serial command root rather than counting results.
- **"Interrupted" is not "stopped".** Codex acknowledged an interrupt and reported
  the turn interrupted while the fixture shell kept running in its own process
  group. The adapter calls the native terminal cleanup endpoint, and the
  broker's containment remains the backstop. Do not release a workspace on a
  terminal event alone.
- **Delivered bytes are not accepted input.** `--disconnect-only` drains the
  local buffer, disconnects before reading the acknowledgement, resumes, and
  looks for the marker in history. When it is missing, that outcome is
  *unknown*, not "rejected", and nothing retries it.
- **Arguments have a size.** In one resumed GLM world, native Bash failed to
  launch with `E2BIG`: about a megabyte of command arguments, with a sandbox
  profile carrying hundreds of deny paths, registered worktrees among them. The
  cause was not fully diagnosed, but "just keep the old worktrees around" is
  not free.
- **Relative paths lie.** A marker written to a relative path succeeded in the
  reply and landed under `$TMPDIR`. The fixtures use exact paths.

## What they do not prove

Telegram or desk ingress, split-UID installation, dependency builds, live
rollback, idempotent resubmission after an ambiguous disconnect, coherent
capture during native background memory writes, or restoring a *session* (as
opposed to memory) from Git alone. A model-backed happy path does not make any
of those go away.

## Rejecting unsafe process reuse

On Linux, run `/usr/bin/python3 experiments/native_sessions/writer_exclusion_probe.py`
and repeat with `--provider claude`. These use installed providers, fresh homes,
no inherited credentials and no inference. A deliberately writing MCP fixture
shows that idle state and suspending a provider leader do not exclude writes;
Codex additionally acknowledges terminal cleanup before the measurement. A peer
at the same UID then writes the disposable target with provider/MCP leaders
stopped. The script only signals its disposable provider group and identified
fixture processes. It needs Linux `/proc` and a Python build with pidfds; it checks that before
launching. Some uv-managed Python builds lack them.

A successful run reports `reuse_proven: false`: it reproduced a counterexample,
not a safe reuse boundary. See the existing
[session-host proof matrix](../../docs/native-session-host.md#stage-three-writer-exclusion-evidence)
for measured versions, scope, credential limits and remaining controller-host
checks. This probe must never become an enablement check for reuse.

## Native retention startup probe

Run `python3 experiments/native_sessions/retention_startup_probe.py` for two
credential-free initializations of each installed provider with old synthetic
session and memory fixtures. It reports hash survival and sanitized Codex config
readback. It does not run inference or prove a cleanup sweep/consolidation ran.
See the [retention audit](../../docs/native-session-host.md#native-evidence-retention-audit)
for scope, archive restore tests and remaining deployed/off-host coverage.

The retention initialization probe also tests a long private
`CLAUDE_CODE_TMPDIR` and an aged synthetic task-output fixture across restart.
It verifies native temp-directory creation, not Bash execution or a positive
retention sweep; no credentials or inference are used by this probe.

`uv run python experiments/native_sessions/claude_temp_probe.py` drives installed
Claude against a loopback synthetic Messages API, with no real credentials or
paid inference. It executes a native sandboxed Bash command, verifies the complete
200 KB result in both native temporary and persisted tool-output files, ages those
files plus its real transcript and synthetic native memory, injects actual Git
index-lock and commit-hook failures, then checks restart survival and restores
one selected result from a private snapshot with SHA-256 verification. The probe
uses disposable fixtures only; it does not prove every provider retention path.

`uv run python experiments/native_sessions/codex_evidence_probe.py` uses a
loopback synthetic Responses API and Steward's process containment to execute a
native Codex command, preserve its complete 200 KB result, then resume the actual
native session after Git index-lock and commit-hook failures. It checks the old
rollout prefix and synthetic memory, verifies a snapshot restore by SHA-256, runs
SQLite integrity checks, and verifies restored native thread-history rows. It
uses isolated homes and no provider credentials or paid inference. The fixture
turns code mode off to exercise the native shell tool directly; it does not cover
all tool families. Add `--retention` to enable the fixture memory pipeline and
seed old, recent and selected synthetic stage-1 rows. Native restart must delete
the old unselected row, retain the controls, and the independent pre-start copy
must restore all exact rows. This exercises one installed retention path without
changing production memory settings; it does not prove all consolidation paths.
