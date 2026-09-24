# Controller and Agent OS Identities

The production shape is deliberately two identities:

| Identity | Owns | Must not own |
| --- | --- | --- |
| Controller (`root`, initially) | Telegram intake, SQLite state, Git landing credentials, exact-SHA push, release activation, service restart | Model-authored command execution |
| Agent (`steward`) | Provider login state, repository/worktree edits, builds, tests, command probes, product adapters | sudo, Docker/containerd sockets, controller tokens, deploy/signing/cloud credentials |

`root` is a practical controller identity for the first deployment. It is not the
desired long-term capability model: landing, deployment, and signing can later move
behind narrower brokers. The important boundary now is that no model-controlled process
executes with UID 0 or inherits the root daemon's environment.

## Configuration

```yaml
execution:
  user: steward
  group: steward
  home: /home/steward
  tmpdir: /tmp
```

The account remains an ordinary, useful VPS account. Give it normal ownership of
repositories, provider configuration, caches, build directories, and any other working
data the agents should use. Do not add it to `sudo`, `docker`, or another group that
confers equivalent host control. File and socket permissions are the authority boundary.

The configuration is rejected if controller state or the Telegram token is placed below
a declared model-writable root (the execution home or tmp directory, provider work
directory, repository, or enabled Git world). Deployment release paths may not overlap
those roots in either direction. This prevents a permissive layout from defeating the
identity split before any process starts; the host permission checks below still cover
credentials and sockets that are not named in the harness configuration. The default
Git-world lease is stored beside the controller state database, not in the
model-writable world. An explicit `world.lock_dir` is treated as a controller-owned path
and exists only for coordination with another process. Before enabling an explicit lock
directory, provision the directory as controller-owned, execution-group-owned `0750` and
pre-create its sole `world.lock` as a regular, non-symlinked, single-link file with the
same owner and group at `0660`. The harness refuses to create this shared inode. The
execution identity and any cooperating external writer can open it for an advisory lock,
but cannot replace its controller-owned namespace. This is an intentional bounded
availability capability: a cooperating writer can delay world mutation by holding the
lock, without gaining controller state or secret access. Existing installations that set
`world.lock_dir` must repair this layout before upgrading. Each repository also declares
its trusted `remote_url`. HTTPS and SSH URLs may not contain credentials, and a
split-identity instance rejects local filesystem remotes. The controller binds that URL
and the default branch to a private bare store beside the state database; a later
configuration change cannot silently reuse the store for a different remote.

When this block is enabled, the harness launches provider CLIs, repository gates,
command probes, and Telegram product adapters with the configured UID and GID, an empty
supplementary-group list, and umask `077`. Provider children receive only a small
locale/network/TLS environment allowlist plus provider-specific API variables. Gates,
command probes, and product adapters receive the ordinary allowlist and harness-owned
adapter metadata but never provider API variables. Variables such as `GITHUB_TOKEN`,
`GH_TOKEN`, `SSH_AUTH_SOCK`, and Telegram credentials are not inherited by either lane.
The environment policy applies even when `execution.user` is unset; configuring a user
adds the separate UID/GID and filesystem boundary.

Native Claude and Codex sandboxes still apply inside this OS boundary. Writable native
executions receive the same configured agent directories that the broker checks:
provider workdir, native homes, managed repository paths, and Git world. They can
investigate and work across that operating environment; cwd is a working location, not
the extent of the steward's filesystem grant. Read-only operations receive no extra
writable roots. Controller credentials and release mutation remain outside this grant
and protected by the OS boundary.

Owning scope determines where canonical work belongs and which controller path
accepts/publishes it. It is not an additional filesystem partition between every
repository. Separate session checkouts avoid incidental mixed edits; they do not claim
to prevent an agent deliberately editing another granted directory. Edits outside the
execution's checkout are not included in its world checkpoint and must use the owning
repository/world's acceptance path.

## Execution ownership

On Linux, a configured host execution identity requires a root controller,
cgroup v2, kernel pidfds and the systemd system manager. General brokered execution
runs in a unique transient service. A protected guardian pins the original
controller process with a pidfd before starting the workload. Systemd owns descendants before
the workload starts, including children that call `setsid()`, and tears down the
invocation on normal completion, cancellation, or controller death. Other
invocations have separate units. The trusted launcher and interpreter must have
root-owned, non-writable namespaces. The launcher uses the resolved interpreter
with isolated mode and site initialization disabled; credentials travel in a private temporary
file, never service arguments. The guardian's workload child drops supplementary
groups, GID and UID before executing model-controlled code.

Controller-selected Git path queries have a narrower direct lane: built-in
`rev-parse --show-toplevel`, `--git-common-dir`, and `--git-path` for the fixed
interruption markers, optionally with absolute path output. Interruption guards
resolve all markers in one query, including linked-worktree paths, and reject
ambiguous output. Unknown commands, extra environment and input use owned units.

The direct lane checks fixed root-owned executable namespaces and uses a fixed
environment. A controller-owned timeout watchdog drops Git to the execution UID
and GID with empty supplementary groups and no-new-privileges before entering
the repository. These admitted queries do not spawn helpers; repository config
is still parsed as the agent. The watchdog bounds blocked reads and orphan
lifetime after controller death to at most 60 seconds. Cancellation kills the
process group. Arbitrary Git and model-selected commands retain systemd ownership.

Native interruption precedes containment when the provider protocol remains
available. Invocation termination has its own bounded systemd grace. This does
not claim that killing a stuck provider preserves all its unfinished writes.
Controller shutdown discards queued work, interrupts running task turns (which
have no routine deadline) and drains running writers while holding its lease.
The controller unit's stop timeout must cover the longest remaining bounded
writer (a conversation, world or procedure turn up to the configured provider
deadline) plus interruption and containment time; a forced service stop can
still cut that drain short. Invocation services do not inherit the controller
unit's stop job: the guardian observes actual controller process death, allowing
the controller to drain while it handles a graceful service stop. Standalone root
controller processes use the same lifetime binding.

Invocation services live separately in `system.slice`. A memory or CPU limit on
the controller service alone does not limit these workloads; resource policy
must cover their slice or invocation units. This ownership boundary adds no
per-invocation memory or CPU budget.

Before upgrading an older installation, verify the installed launcher namespace
is protected. Older release exports could produce `0775` directories and `0664`
files; the updated exporter uses `tar.umask=0022`. Extraction preserves those
archive modes even when staging runs unprivileged with umask `077`, so service
identities can traverse directories and execute committed executables without
granting group or other write access. Existing releases are not
rewritten automatically. Stage with the updated trusted driver, or correct the
controller-owned installation's permissions as part of the explicit upgrade,
before enabling the new launcher.

Systemd owns these transient service cgroups, so the controller does not need
`Delegate=yes` for this design. A Linux launch without the required ownership
boundary fails explicitly. `steward check` verifies identity and filesystem
grants; it does not substitute for an actual launch on the provisioned host.
macOS and inert local brokers retain weaker process-group ownership: detached
children can escape. Identity-only Docker tests do not prove systemd containment.

## Host layout

A minimal Linux setup is:

```bash
useradd --create-home --shell /bin/bash steward
install -d -o steward -g steward -m 0750 /home/steward/.config
install -d -o steward -g steward -m 0770 /var/lib/steward/work
install -d -o root -g steward -m 0750 /var/lib/steward/inbound-media
install -d -o root -g root -m 0700 /etc/steward
```

Keep controller credentials in `/root` or root-owned `0600` files. Git remote URLs must
not embed tokens in repository configuration because the agent can read its
repositories. Let controller-side Git use root's credential store or a later scoped
landing broker.

Repositories, task worktrees, and configured Git worlds must remain writable by
`steward`. All Git that opens those paths runs through the agent broker; the controller
does not repair their ownership by writing as root. Verify the resulting permissions on
a real task worktree and world. Do not solve access errors by granting sudo. Git-world
`episodes.md` contains newline-delimited structured episode records; free-form turn text
cannot impersonate a record boundary. It is created and appended by the agent identity
and must be a regular file owned by that identity. Before upgrading an older
installation that created it as root, change that one file's ownership to the configured
agent; the harness deliberately fails closed instead of preserving mixed ownership.
Git-world checkpoints ignore system and global Git configuration, reject local
clean/process filters, disable signing, and disable hooks and fsmonitor. This prevents
staging model-authored attributes from becoming another command- execution path outside
the provider sandbox. Orientation and light-rhythm file reads are bounded and performed
by the agent identity through no-follow path traversal. The controller never opens
model-writable world content on the model's behalf.

Provider authentication belongs to the agent identity. Log Claude/Codex in as `steward`,
or place only the provider API credentials it needs in the agent's home. Deployment, Git
push, Telegram, signing, Docker, and cloud-administration credentials stay
controller-only.

Inbound Telegram media is the deliberate read-only exception. Split-identity instances
configure a dedicated `telegram.inbound_media_dir`; the controller owns the directory
and downloaded files, while the broker grants only group read/traverse (`0750`
directories, `0640` files) to the execution identity. The configured account must never
be able to write this spool.

Controller-only does not mean operationally unavailable. Narrow operations should cross
a typed harness boundary. Telegram pinning is the first example: an opted-in model
response can request `pin_reply` or a numeric `pin_message`, while the root transport
fixes the destination to configured `chat_id` and uses the token itself. Extend this
pattern instead of making secrets readable to the agent account.

## Enforcement and verification

`steward check config/steward.yaml` performs a real UID transition using the configured
account. It verifies that the agent-owned home, temporary directory, provider work
directory, repositories, and Git world are writable. It also walks each configured
controller namespace and rejects agent-owned, writable, or symlinked components; state
and token files and controller Git stores must remain outside agent write authority. Git
stores are created with an explicit private umask, and their namespace, store root, and
control files are checked rather than relying on the service umask. Run the check as the
same identity as the service (currently root). `steward run` repeats it, and
`StewardDaemon.run_forever()` also refuses startup. A configured broker repeats the
proof before its first local child launch, so direct embeddings cannot bypass it. Stable
controller ownership makes that result safe to cache for the broker lifetime.

Code embedding the daemon may explicitly inject an execution broker. That caller is part
of the controller trust boundary and owns the injected broker's host-policy proof;
omission always uses the fail-closed complete-instance factory. There is no boolean or
environment-variable bypass.

The boundary is mandatory when the instance can push a fast-forward landing, deploy a
systemd release, operate Telegram with a controller token. `systemctl` remains
controller-owned. The boundary is optional only for inert/local configurations with no
such controller authority.

Before production cutover, verify at least:

```bash
sudo -u steward id
sudo -u steward test ! -r /etc/steward/telegram-token
sudo -u steward test ! -w /var/run/docker.sock
sudo -u steward test ! -r /root/.git-credentials
sudo -u steward test -w /var/lib/steward/work
```

The first command should show neither UID 0 nor privileged supplementary groups. Missing
socket or credential files are fine; existing ones must fail the access checks.

## Current boundary

Local Git in repositories, worktrees, and configured Git worlds runs as the agent.
Declared repository gates and command probes use the same broker. The controller fetches
and pushes only an explicit configured URL from its private bare store; candidate
retention and release archive export use exact object IDs from that store. Branch names
in the agent repository are consistency labels, not authority. Release staging therefore
continues to work if the agent checkout disappears after the exact commit was imported,
while an agent-only commit cannot become a release.

Git worlds remain local durable knowledge. Their checkpoints are agent-brokered, and the
retired named-remote publication path cannot turn model-controlled Git configuration
into controller transport authority. Signed or otherwise credentialed product actions
still need a separately scoped broker; an untrusted adapter must not be given those
secrets directly.

The object handoff itself is a direct pipe between separate Git processes.
Controller-to-agent transfer creates only an untrusted convenience ref; the returned
exact SHA remains the input. Agent-to-controller transfer is a thin pack against the
last controller-observed base, parsed strictly in a private quarantine, and retained
under a content-addressed candidate ref only after the ancestry proof succeeds. Imported
candidates retain content-addressed refs; there is no durable publication receipt table.
Recovery fetches authoritative remote truth first: a remote that already contains the
tested SHA proves success even if local retention is missing; otherwise the branch must
be prepared and gated through the current publication path. `max_pack_input_bytes` is an
internal compressed input ceiling, not a complete CPU, decompression-memory, or
repository-size limit; Git's parser and the host storage quota remain part of the
controller host trust boundary.

## Managed source branches

Repository polling fetches all advertised heads from each configured trusted
remote and delivers their objects to the development repository under
`refs/steward/remote/<branch>`, including branches that have never reached `main`.
Use `git show refs/steward/remote/<branch>:path` or create a local worktree from
that ref. Existing task branches, checked-out files and native sessions stay in
place. Unchanged heads transfer no objects, and deleted remote labels are pruned.
Credentials stay with the controller; a worker's local remote settings cannot
change the authenticated destination. Only the configured default branch supplies
publication ancestry and deployment authority.

Source delivery uses the existing repository poll. Native pushes of arbitrary
branches are not implemented by this source replication path.
