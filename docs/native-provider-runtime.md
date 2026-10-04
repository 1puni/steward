# Native provider runtime

The harness does not reimplement an agent. It runs the real ones: Codex through
App Server, and Claude Code (for both the Claude and GLM families) through its
stream-json protocol, each over the untrusted process broker and each in its own
private configuration home. Native tools, subagents and task decomposition stay
provider-owned. What the harness owns is the boundary around them: which home,
which identity, which input, and whether the result is accepted into Git.

Keep four things apart: a provider session, an incoming message, an active
execution and an accepted external effect. None of them maps one-to-one onto
another.

Production runs these on a Linux host under the separate execution identity.
There is no container execution backend.

Task-result assessment is completion cognition in the owning conversation.
Working tasks report findings and finish their native turn; they do not decide whether to
notify the operator or produce a silence token. Completion may finish without a
final message. Conversation, task and automatic turns allow empty output; only a correlated,
successful native terminal result can establish it. Codex commentary is never
used as final output. Missing completion, failure and truncated streams remain
errors. Accepted world changes and task actions survive a quiet completion;
transport records the empty reply without sending a message.

```yaml
provider:
  native_homes:
    codex: /home/steward/native/codex
    claude: /home/steward/native/claude
    glm: /home/steward/native/glm
```

Provision these directories as the execution account, including its provider
login and approved settings/plugins. They must be distinct and outside the Git
world and managed repositories: they can contain private authentication and live
databases. The host check includes each as an execution-writable path. Provider
home variables pass through the provider broker only; repository gates and
product commands do not inherit them. Adapters discard ambient provider-home
variables and use the explicit mapping. First-party Claude login sets
`CLAUDE_SECURESTORAGE_CONFIG_DIR` to its configured private seed home, preserving
one native credential store and refresh-lock location while `CLAUDE_CONFIG_DIR`
selects each owner's runtime state. Explicitly routed Claude/GLM calls instead
select a separate `.steward-routed-auth` path within the execution home, so router
authentication does not fall back to seed OAuth credentials. Do not clone or merge rotating OAuth credentials
between owner homes. Codex owner homes all link the one seed `auth.json`, so
concurrent Codex processes share a credential file; Codex's maintainers permit
bounded refresh-token reuse, so a duplicate refresh submission is not a failure.
None of this replaces an authenticated refresh or Linux boundary acceptance.

The runtime factory rejects missing homes for built-in providers in the configured
order. Provision each selected provider or remove unused fallbacks from that order.
It builds only selected built-ins and never substitutes a personal home or a
second restricted executor. Custom adapters supplied by an embedding application
remain independent of this native-runtime factory.

`steward check` validates configuration, the declared OS boundary, and local
launch prerequisites. Claude/Codex availability checks the executable; GLM also
checks local credential availability. These checks do not authenticate a native
turn, refresh OAuth, or prove retained history can resume. Configured fallback
skips unregistered, incapable or locally unavailable adapters. Once an adapter
executes, authentication and rate-limit failures retain the failed operation;
they do not automatically start the same work on another provider. Native hooks
or earlier activity can precede such failures. The sole missing-session retry
uses the same provider and current input under its existing provenance fence.

## Anthropic-compatible endpoint routing

The Claude and GLM families speak the Claude CLI's Anthropic-compatible
protocol, so one explicit endpoint selection routes either family through a
third-party gateway without touching lifecycle code:

```yaml
provider:
  glm_anthropic_base_url: "https://gateway.example.com"
  claude_anthropic_base_url: "https://gateway.example.com"
  claude_credential_path: "/etc/steward/gateway-token"
```

GLM targets `glm_anthropic_base_url` (default `https://api.z.ai/api/anthropic`)
and keeps reading its token from `glm_credential_path`. Claude keeps native-home
login unless the optional base URL and credential path are configured together;
that pair replaces any ambient Anthropic endpoint identity in the child
environment, mirroring GLM's existing credential isolation. Availability checks
read the credential file for both families, and any configured router token is
rejected if it appears in a recorded event stream.

Model IDs under `provider.models` must then match the gateway's exact catalog
IDs, not vendor aliases. A gateway that bills per request changes what a turn
costs; execution deadlines, one-prompt-per-execution, and session fencing are
unchanged. Codex routing stays provider-owned instead: configure a
`wire_api = "responses"` model provider inside the Codex native home so Codex
itself selects the gateway, with its key arriving through the daemon
environment the adapter already passes.

## Codex native turns and live input

`CodexAppServerRuntime` maps `initialize`, `thread/start` or `thread/resume`,
`turn/start`, and `turn/steer`. Its reader matches the parent thread and exact
turn before accepting completion or final output. Child completions and previous
turns cannot complete the parent. An unfinished reported child prevents returning
a successful candidate. The broker retains deadlines, cancellation, environment,
identity, stream bounds, and process cleanup. The adapter requests native terminal
cleanup for the parent and reported children, then closes stdin after those
acknowledgements. Native records are written into the owner home; execution
returns after the process and owned descendants exit. Owner homes remain durable;
native records are captured into Git and anonymous homes are retained. A connection can
be recreated while resuming the same native thread.

Embedders may supply `CognitionRequest.on_input_ready(send)` and
`on_input_result(receipt)`. `send(RuntimeInput(source_id, text))` offers a durable,
caller-owned source to the current native turn. It rejects a source offered twice
on the same connection and refuses input after completion. Receipts distinguish:

- `accepted`: a matching native steering acknowledgement arrived;
- `rejected`: the provider explicitly rejected that request;
- `unresolved`: the connection ended without a valid acknowledgement.

Acceptance describes input delivery, not consumption or completed external work.
There is no automatic resend. The caller must keep its durable source until
its owning acceptance/recovery path resolves it. Callback code must return promptly.

Telegram text and attributed task results route into active native
conversations. The shared desk API supports the same mapping, but the daemon
orders desk turns within each topic and cannot inject a second desk message while
that lane is in cognition. The existing source turn records its owning execution and
native delivery disposition before sending. Accepted sources complete together
with that execution's world acceptance; replay uses its one receipt and cannot
admit a second task. An input acknowledged by the provider but interrupted before
world preparation remains retained evidence, not permission to repeat it.
Unresolved delivery remains retained for inspection. Explicit rejection proves
that the offered input was not applied: once the old writer stops, only that
rejected linkage is removed and the original durable source can retry through
the ordinary path. No accepted world effect exists for that linkage; unlinking
it does not undo independently accepted task calls.

`RuntimeInput` carries `origin` and `author` in a JSON text envelope so controller
observations remain visibly attributed. Codex maps this through `turn/steer`;
this is not a claim of native `toolOutput` role attribution. An idle session still
needs activation to assess a result. A superseded active result fences the owning
execution's unaccepted actions.

Cognition offers ongoing input opportunistically.
Callbacks are installed only when the selected
adapter supports them; a nonstreaming provider is not silently replaced merely
to obtain steering. Image messages wait for a new execution. Result assessment dispatches independently by owning conversation; existing
source receipts still determine when task evidence may be offered.

### Native questions within a turn

Live steering and answering a provider question are different protocol operations.
Codex documents `item/tool/requestUserInput`, with structured questions and an answer
to the outstanding server request; Claude documents `AskUserQuestion` through its SDK
`canUseTool` callback. Both can keep the native execution open while it waits. See the
[Codex app-server contract](https://developers.openai.com/codex/app-server) and
[Claude user-input contract](https://code.claude.com/docs/en/agent-sdk/user-input).

The harness has not yet connected those question callbacks to controller cognition.
Codex's adapter answers every server request with an error, ordinary questions
included, because it never grants authority just to keep a transport moving.
Claude SDK support also does not prove the installed CLI adapter handles its control
protocol. This remains an explicit runtime gap, separate from live text steering.

The intended boundary is a typed question addressed to the owning controller session.
It may answer from accepted context within the same tick; missing operator decisions
must remain questions. Tool permission and MCP side-effect approvals can share these
protocols, so answering ordinary questions must not silently grant extra authority.
Implement and prove the native response correlation, cancellation and unresolved-question
behavior before claiming that this works for either adapter.

The [native probes](../experiments/native_sessions/README.md) include a live-source
journey through a real provider.

## Claude and GLM native input

With a configured native home, Claude/GLM use native streaming input when the
caller supplies ongoing-input callbacks. Native `command_lifecycle` records
correlate each supplied command UUID through queuing, start, and completion.
A correction folded into an active command completes before that command's
result; a command starting a fresh native turn completes after its own result.
The adapter requires every offered command to complete and every fresh native
turn to supply a successful result before closing the writer. It does not count
result messages or treat a user-message echo as completion.

Claude's `system/dev_intent` and `system/task_notification` notices are
informational and may arrive before initialization or during a turn without a
session identity. They neither establish a session nor complete a command.
The adapter still requires `system/init` with a valid session UUID and effective
model, the expected identity on resume, and a successful correlated terminal
result. A real Claude Code project scanner does emit `dev_intent` before `init`,
which is why the parser tolerates it; regressions live in
`tests/test_runtime_lifecycle.py` and `tests/test_claude_stream.py`.

The result's `user_message_uuid` is optional. If absent, the serial native
command lifecycle identifies its root; a conflicting explicit identity fails.
Cancellation, discarded commands, missing results, malformed protocol fields,
and unresolved disconnects cannot become accepted world updates. Supplied
operator/controller origin and author use the same JSON text envelope as Codex.
This preserves native queue semantics without claiming all providers steer in
the same way or introducing a second source ledger.

Installed Claude Code CLIs must support this lifecycle protocol. The Python agent
SDK drops the lifecycle frames, so wrapping it would remove the completion evidence
rather than simplify anything. The [native probes](../experiments/native_sessions/README.md)
record what the installed CLIs actually do.

GLM runs through `ClaudeRuntime(family="glm", base_url=..., credential_path=...)`
pointed at the z.ai Anthropic-compatible endpoint (`https://api.z.ai/api/anthropic`
by default). It reads only the configured `provider.glm_credential_path`.
A missing or empty file fails its local prerequisite check; an ambient
`ZAI_AUTH_TOKEN` cannot silently replace the configured identity. Experiments
that acquire a credential separately must write their explicit private token
file before constructing the runtime.

## Failure evidence for provider fallthrough

Writable native calls prepare their private launch links and candidate record
paths before starting the provider. A failed setup reports its bounded terminal
process diagnostic and known saved session ID; the harness does not replace all
setup errors with guessed history-import advice. This can distinguish missing
candidate history from a filesystem failure. It does not establish provider
authentication, authorize replay, or prove deployed workspace readiness.

Adapters retain native failure messages and known session IDs in
`RuntimeExecutionError`. `Cognition` preserves the exception when adding session
provenance. The harness does not classify provider billing/rate codes or copy
token accounting into its result; original telemetry remains in native records.
No lifecycle consumer used those projections. Explicit missing-session recovery
and process deadlines remain distinct because they control real behavior.

Limit telemetry and warnings alone do not fail a turn. A native recovery followed
by success remains successful; child failures and ordinary assistant/tool text
cannot replace the parent's native failure. Native error detail is reported at a
failed terminal boundary or failed process exit, after process cleanup.

An execution error does not prove that no tools ran. It introduces no automatic
provider switch, retry loop, cooldown database, or inferred reset time. The
existing availability fallback remains distinct from replaying interrupted work.
Fallthrough policy must retain partial work and resolve accepted live inputs;
it must never pass another provider's session ID onward. Every execution receives
one current prompt; replacing a missing session retries that same prompt.

Claude and GLM always send the initial prompt through the same native command
queue, including task and read-only executions without an ongoing-input hook.
The optional hook exposes further source delivery; it does not choose another
CLI or completion protocol. Session binding occurs once, from the first native event
rather than at completion, so a crash mid-turn resumes the session instead of
starting over. Every known native
command requires its terminal result and completion before input closes.
The [native probes](../experiments/native_sessions/README.md) exercise this against
installed CLIs, including fresh and resumed task calls and a real writer deadline.

Eligible conversation prompts describe the live task tool and transport markers
on every execution. Task operations use the tool, not final-output markers.
Interface changes do not clear native history.
Filesystem orientation remains in the turn. Agents read their
[brief and Git history](provenance-discovery.md) directly; no history map
or controller task-state snapshot is generated for cognition. The one exception is
task-result assessment: the world worktree has no Git path to the task record, so the
assessor receives a bounded copy of the brief as evidence. This
removes prompt compatibility tracking at the cost of repeating the compact
interface on resumed turns. Native model behavior with the shorter wording
still requires validation; local fixtures establish routing and enforcement.

Protocol references: [Codex App Server errors](https://learn.chatgpt.com/docs/app-server),
[Claude native SDK message parsing](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/message_parser.py),
and [z.ai business errors](https://docs.z.ai/api-reference/api-code).

## World Git reconciler

```yaml
world:
  # Other world settings omitted here.
  reconcile: resolve-world
procedures:
  resolve-world:
    instructions: /etc/steward/procedures/resolve-world.md
    provider: codex
    model: {model: configured-reconciliation-model}
    access: workspace-write
```

`world.reconcile` names the procedure for world-turn acceptance conflicts. Without
it, default cognition settings apply. Repository publication performs deterministic
integration only: conflicts and failed gates return evidence to the owning ordinary
task, preserving its native session and exploration history.

Already-reconciled history needs no model call. Divergent candidates receive
bounded rebase resolution; native resolver records and remaining edits are
committed before gates or world acceptance. Provider failure retains a bounded
reason and the original candidate. World resolution happens outside the lease;
only exact revision application and SQL acceptance hold it. A changed world is
rechecked before any application or dependent task admission.

## Bundled native skills

The package ships `skills/commit/SKILL.md` and `skills/git-reconciler/SKILL.md`.
Writable native launches expose them through `<launch-home>/skills/`, using
both providers' existing native skill discovery. Instance-provided skills remain
linked and take precedence over a bundled skill of the same name. The private
launch owns the overlay, so it never mutates shared configuration or adds skill
links to a candidate Git tree. Restricted
non-native launches retain their existing customization limits.

Both providers' own discovery (`skills/list` on Codex, SDK initialization on
Claude Code) reports the linked skills. The wheel includes both skills; discovery
and cleanup are regression tested.
See [Codex skills](https://learn.chatgpt.com/docs/build-skills) and
[Claude skills](https://code.claude.com/docs/en/skills).

## Native workflows and storage boundary

For discovering available tools and distinguishing missing integration from
missing host grants, follow the [onboarding discovery procedure](../skills/org-onboarding/SKILL.md#discover-capabilities-before-requesting-new-authority).
Native tools, repository/world instructions and installed target contracts are
the discovery surfaces; there is no separate harness capability registry.


The Codex App Server uses the configured native home without blanket suppression
of native goals, skills, plugins, or agents. Configure that steward's integrations
there. The disposable probe disables account-backed apps to exclude personal
integrations; that fixture flag is not a product policy.

With a native home, Claude/GLM do not run in safe mode and do not suppress settings
or MCP loading. Writable turns allow the native Agent tool alongside search, file,
and shell tools. The native sandbox and request-specific permission mode remain
enforced. Read-only turns retain their limited tool grants.

For writable Claude/GLM native turns, `autoMemoryDirectory` points to
`<current-worktree>/memories/<provider>` and automatic memory is enabled.
These ordinary files follow the same checkpoint and acceptance as other edits.
An isolated conversation/rhythm turn cannot change the accepted world's memory
before acceptance; a later checkout inherits the accepted files. Concurrent
edits use ordinary Git conflict handling, retaining conflicting candidates.
No mutable per-home symlink or cross-world memory synchronization is introduced.
Read-only and non-native turns still disable automatic memory.

The mapping uses Claude's [native memory directory setting](https://code.claude.com/docs/en/memory#storage-location).
Keep this directory tracked; repository ignore rules still apply to ordinary
checkpointing. Consolidated scope truth remains in `docs/`; native memory is
readable working knowledge, not a replacement authority.

Each conversation or task keeps its own provider home across turns and lineage
changes. Native turns with a retained lineage use a private
`.steward-owner-<hash>` home beneath the configured native home. The hash covers
the actual conversation or task session owner, the selected provider and the
lineage generation. Profile changes and controller restarts keep the same home;
clearing the conversation, switching provider or invalidating a missing session
selects a new generation. The home is selected on every adapter attempt,
fallback and recovery included. A checkout path or a per-turn execution ID is
never an owner. Anonymous conflict-resolution calls use separate retained launch homes.

Preparing a new generation preserves all older owner homes; nothing deletes
them. Claude/GLM launches also set `CLAUDE_CODE_TMPDIR` in the environment and inline
settings to an owned 0700 `.steward-tmp` directory inside that home. Preparation
rejects a redirected or non-private directory. Anonymous homes do not link to
the seed's temp root. Anonymous launch homes survive setup and provider failures.
A storage reserve stops new native admissions rather than filling the disk. See
[native records in Git](native-session-host.md#native-records-in-git).

Only native configuration, authentication and integration entries are linked in
from the configured seed home. SQLite, caches, queues, goals and jobs stay
provider-owned inside the owner home. If a configured Codex `auth.json` link is
replaced locally, the replacement is preserved, but the home refuses re-entry
until someone inspects it; preparation never copies it back to the seed. With no
seed credential, an independent native local login is kept. Bundled skills stay
available without modifying the configured skill links. Writable owners map
native records in their home and capture them into their retained candidate:

- Codex `sessions/` and `archived_sessions/` hold native rollouts, captured beneath
  `artefacts/codex/`; `memories/` links to `memories/codex/` in the candidate.
- Claude/GLM `projects/` holds native project/session files, captured beneath
  `artefacts/<provider>/projects/`; the memory setting points to `memories/<provider>/`.

The provider writes original files directly; the harness writes no
`thread/read` snapshots of its own. Capture stages the home's files into the
candidate's index after the provider process has exited, success or failure. An
existing home cannot silently retarget its memory link to another candidate.
Directory creation uses the execution broker, existing symlinks in mapped record
directories are rejected, and setup shares the execution deadline. Anonymous
launch homes (conflict resolution) hold and capture their records the same way.
Read-only runs capture every mapping, native memory included, into the record
checkout their caller names.

The home persists, the process does not. Every invocation starts a new provider
process, and no provider process is kept between turns. Goals and jobs therefore
have no process that could admit or publish work on its own outside a harness
invocation.

Read-only owners keep their records inside their private owner home and create
nothing in the candidate. For an existing saved lineage, preparation can import
that session's exact primary original from the seed home once. The copy uses
checked regular-file descriptors, fsync and an exclusive atomic link, so an
incomplete copy can never become a resumable original. No other session and no
seed database is ever imported, and the seed original is left untouched. Child
records, sidecars and any older leaked launch homes still need explicit
inspection when upgrading.

Removing an owner's workspace removes all of that owner's homes, under the same
fence as the workspace itself. A retained workspace keeps its homes. Read-only
owners without a world are never retired, because their home holds their only
records.

Conversation/rhythm checkouts use `session-<owner-hash>` while their owner is
executing. Native originals and memory are committed into the candidate; once
accepted, the Git world is their source and the materialized checkout is removed.
A latest interrupted turn without a prepared receipt retains its checkout as its
only evidence. Startup and turn completion derive that live set from root turns,
world receipts, and declared conversation/rhythm ownership. Obsolete worktrees are reclaimed; the controller object store owns every
prepared candidate. Ignored environments are disposable and must be recreated.
Task checkouts follow the same rule: queued or open pre-checkpoint work may still
own evidence; checkpoints and task branches own everything after that boundary,
so inactive and publication-owned materializations are reclaimed.

Native resume selects the exact session's original file from the current
candidate. It uses Codex's experimental native transcript path or Claude's
`--resume` file path. Missing/ambiguous records block with the saved session
identity; they do not silently start a fresh session.

Read-only calls retain private native storage and do not create candidate
record directories. Original records contain conversation and tool data and
follow the owning world's publication rules. Native background memory generation
and escaped descendants still require their own quiescence evidence; direct
foreground writes do not prove every background writer has stopped.

On cancellation or timeout, Codex receives `turn/interrupt`; Claude/GLM receive
the native `control_request` interruption. The broker keeps draining protocol
events for up to ten seconds, then requests termination with a bounded grace
before killing. Ten seconds is a containment policy, not a measured promise
that every provider has checkpointed. Cancellation/timeout remains an error even
when native interruption succeeds. Ordinary task turns have no routine deadline;
a bound-session procedure deadline, or a controller-shutdown interruption,
retains its ordinary next-tick continuation.

Cognition also checks cancellation after the final provider declines admission.
A cancellation observed during the last availability check or runtime refusal
remains a cancellation error, rather than being reported as provider exhaustion.
This does not alter fallback when no cancellation was requested.

The cleanup endpoint requires App Server's experimental API capability, which
this adapter declares. A failed/malformed cleanup acknowledgement cannot produce
a successful candidate. This uses the documented
[native terminal cleanup API](https://learn.chatgpt.com/docs/app-server#clean-background-terminals).

Codex will acknowledge an interrupt while a foreground shell it started keeps
running; native cleanup is what actually stops it, and the probes check both
cancellation and same-session resume afterwards. This is not proof of arbitrary detached descendants or cleanup after provider/host
failure. Linux host-UID invocations use individual systemd services for
descendant containment; macOS process groups retain the escaped-group limitation.
See [execution ownership](execution-boundary.md#execution-ownership).

## Verification

[Native workspace tests](../tests/test_native_workspace.py) cover candidate
storage and private launch setup. [Codex transport](../tests/test_codex_app_server.py)
and [Claude stream tests](../tests/test_claude_stream.py) cover protocol identity,
completion and live-input correlation. [Runtime lifecycle](../tests/test_runtime_lifecycle.py)
and [world durability](../tests/test_world_durability.py) cover retained work and
acceptance.

The [native probes](../experiments/native_sessions/README.md) drive installed
providers through correction during execution, resume, child artifacts and memory
recall from Git. A passing probe is evidence about that provider version on that
machine, not a suite total and not proof that a deployed rhythm completed. Use
runtime readiness to accept an installation. Before moving real native histories
between versions, follow the [upgrade procedure](upgrading.md).

## Trusted desk cognition policy

Optional `desk.provider` and `desk.profile` seed a new desk conversation through
the ordinary admission path, before its first native call. Omitted values inherit
`provider.family_order[0]` and `provider.default_profile`. The selected provider
must belong to the configured family order. An existing conversation keeps its
lineage: changing these defaults never silently retargets an active session.
Configured fallback providers still apply when they support the required access
boundary. Browser visitors cannot set either field. A configured desk profile
also suppresses the trusted inbox's per-message profile hint; leave it unset to
keep the phone ingress behavior.

For example, a desk can select `desk.provider: codex` and `desk.profile: fast`,
with `provider.models.codex.fast: gpt-5.6-luna`, once you have checked the
installed model catalogue and upgraded safely. That is a per-installation
override, not a generic model default, and no `gpt-6-luna` alias is supplied.

`desk.access` defaults to `operator`, which keeps full desk and phone authority.
`read-only` receives no native task-call capability. Unaccepted final task
markers, including historical prepared completions, are refused. It applies to
every desk topic, private bearer ingress included, and no client can override
it. Telegram conversations keep their configured authority. Public inputs and
replies are not appended to the shared Git world, and a writable world turn
prepared earlier cannot be accepted through the read-only desk.

`desk.readable_roots` is the trusted allowlist of public directories or
individual files. It requires read-only access and defaults to an empty list: no
product files are implicitly readable. The prompt gives only this public map,
with no private repository or world orientation. Select public deployed data,
not a whole checkout: tracked native transcripts, memories, task files, `.git`
and sibling worktrees can all be private. Symlinks cannot grant reads beyond the
selected paths. Never put credentials or private data inside a public grant.

The Codex adapter gives each read-only desk conversation a persistent private
native home under `<native_home>/.steward-read-scopes/<hash>`, with a separate
empty working directory. Only the provider authentication file is linked from the
operator home; operator configuration, plugins, skills, memories and session
histories are not imported. Use a canonical native-home path, because symlinked
private scope directories are rejected. The native process can keep its own
conversation, but model tools cannot read that home, another visitor's home or
the operator's home. Back up and upgrade these homes together with the
controller's conversation lineage. Native sessions that ran without a scope are
not imported into them.

The adapter requires the named `steward-public` permission profile on both thread
start/resume and turn start. It grants minimal system runtime reads plus the
explicit public paths and the empty working directory, with no writes and no
network. It disables apps, MCP inheritance, plugins, hooks, memory tools, host
skill discovery, browser/computer/image tools, delegation and approval
escalation. Shell children inherit no provider credentials and no host
environment beyond a fixed system PATH. Before a model turn, the adapter verifies
the native effective configuration and the confirmed permission profile;
conflicting managed settings or an unsupported protocol fail closed. Codex
0.153.4 was exercised with real sandbox reads, a symlink escape, writes and
loopback denial, plus app-server configuration and profile confirmation, without
starting model inference.
Code mode and its host remain enabled: Luna uses `exec` to dispatch even ordinary
shell reads. The dispatched commands still use the named permission profile;
disabling the dispatcher prevents permitted reads as well as denied operations.

The opt-in `test_live_luna_dispatches_confined_reads` additionally runs real
`gpt-5.6-luna` turns through the adapter. It checks native `exec` outputs for an
unknown public fixture value, then resumes the same session and checks actual
denials for sibling history, symlink escape, authentication, parent process
environment, writes and loopback access. Run it with
`STEWARD_LIVE_CODEX_AUTH_HOME=/absolute/private/codex-home uv run --extra dev python -m pytest -q tests/test_codex_read_scope.py -k live_luna`.
This consumes provider usage and links the existing login into an isolated test
home; credentials and unrelated transcripts are never printed. Ordinary source
gates skip this authenticated test.

Providers that do not declare and implement scoped reads are skipped for this
access mode; Claude and GLM do not. An unavailable Codex cannot silently fall
back to an unconstrained provider. Ordinary operator requests keep their normal
provider behavior. These local tests do not establish the installed Codex
provider's login, model availability or a real reply journey. Keep a public web
desk route contained until you have verified the upgraded runtime, the installed
native policy, two independent visitor conversations and rejected actions, and
had that reopening reviewed independently.
