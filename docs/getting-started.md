# Fork it. Give it a world. Let it work.

Steward is opinionated about authority and deliberately indifferent to your application
stack. The agent works; the controller accepts. Git holds the work. Your repository's
gates decide what can land. Your chosen release system decides how that commit reaches
users.

This guide takes a fork to its first useful task. For an existing instance, use [the
upgrade procedure](upgrading.md) instead — do not read this page as a
migration.

## Try the complete loop

Fork this repository into your account, then clone your fork. Python 3.11+, Git and `uv`
are required for development.

```sh
git clone https://github.com/YOUR-OWNER/steward-harness.git
cd steward-harness
uv sync --extra dev
uv run pytest -q
```

With Docker Compose available, exercise a real daemon against the bundled fake Bot API
and scripted provider:

```sh
ACCEPT_PROJECT=my-first-steward SELFTEST_PROJECT=my-first-steward-selftest \
  FAKE_PORT=8099 sh scripts/follow-through-acceptance.sh
```

This needs no real Telegram or model credentials. It carries a message in, gets a reply
out, edits an accepted world, asks a task question, publishes through a gate and
checks the resulting revision — all in disposable fixtures. The runner recreates its named projects **and
volumes**, so pick names you do not mind losing. See [the follow-through
environment](follow-through-environment.md) for inspection commands and for the
difference between its service adapter and real systemd.

It is the executable tour. It is not your production installation, and a green run of it
is not evidence about your host.

## Choose who deploys each repository

Decide this before you write any YAML, because the answer changes what you configure and
what stays where it already is.

| Existing release path | Harness configuration | Release owner |
| --- | --- | --- |
| Service on the controller's Linux host | Repository `gates` plus a named target bound to an installed systemd driver | Installed release driver, systemd and exact-revision health check |
| GitHub Actions on a branch push | Repository `gates`; bind a target driver for external convergence | Existing workflow and its deployment target |
| Vercel Git integration | Repository `gates`; bind a target driver for external convergence | Connected Vercel project |
| Another platform triggered by Git | Repository `gates`; bind a target driver for external convergence | That platform's configured integration |
| PR-only, tag-only or manual release | An explicit integration decision is still needed | Preserve the existing owner; do not pretend a main push completes it |

The current publisher pushes the gated revision directly to `default_branch`. It does
not open PRs, mint release tags or dispatch arbitrary deployment APIs. A branch rule
requiring a PR cannot be satisfied by a YAML switch that does not exist, and the fix is
not to remove the branch rule: do not relax branch protection to get onboarding green.
If that is your release path, the integration is still an open decision and this guide
will not close it for you.

The [deployment guide](automatic-deployment.md) shows both native and Git-triggered
release paths. The [onboarding skill](../skills/org-onboarding/SKILL.md) can inspect an
organisation, produce the supported configuration per repository, and name the specific
external setup still missing.

## Install one real steward

The published production layout is a Linux controller and a separate execution account.
The controller may initially run as root. Models must not, ever, and no amount of
convenience changes that. Use this native host layout. The former container
execution backend has been removed.

Install a reviewed harness revision under a controller-owned path such as
`/opt/steward-harness`, then run `uv sync --frozen` there as its owner. The source, venv
and parents must be readable and traversable by the execution account, because brokered
Python helpers run that interpreter. Keep the source controller-owned; managed task
checkouts live elsewhere.

For a **new** Linux account and empty installation, provision as root:

```sh
useradd --create-home --shell /bin/bash steward
install -d -o root -g root -m 0700 /etc/steward /var/lib/steward-controller
install -d -o steward -g steward -m 0700 \
  /home/steward/work /home/steward/world /home/steward/repos \
  /home/steward/native /home/steward/native/claude /home/steward/tmp
install -d -o root -g steward -m 0750 /var/lib/steward-inbound
```

Install one supported provider CLI at a stable executable path. Authenticate it as
`steward` using the **configured native home**, not root's personal login — a session
that lives in the wrong home is a session the steward cannot resume. For Claude, set
`CLAUDE_CONFIG_DIR=/home/steward/native/claude` during login; for Codex, use its
configured `CODEX_HOME`. Follow [native runtime setup](native-provider-runtime.md).
Enable only providers you actually provisioned, because `check` cannot prove an
authenticated turn and will happily pass a provider that has never spoken to anyone.

Create an agent-owned Git repository for each managed remote. Public repositories can be
cloned as `steward`; private repositories need read-only source access or
controller-provided objects. The controller's Git identity needs the authorized push
capability and trusted host keys. Do not copy its write key, token or SSH agent socket
into the agent account — that single shortcut collapses the entire boundary this layout
exists to create. Each remote needs an existing default branch with at least one commit.

Initialize the world as `steward`, with a first commit containing a short `README.md`
and `docs/README.md`. State who the steward serves, what it owns, which repositories it
may work on and what good evidence looks like. This is orientation, not authority: it
tells the steward what it is for, and it grants nothing. Grants live in controller
config. Do not put credentials in the world. Install procedure instructions and target drivers outside model-writable roots; see [the current target contract](automatic-deployment.md). Rhythms are explicit procedure triggers; the controller never seeds recurring definitions.

Create a Telegram bot and place its token in `/etc/steward/telegram-token`, owned by
root with mode `0600`. Use the actual numeric chat and allowed user IDs. The allowed-user
list must be explicit; there is no wildcard, deliberately. Topic `0` is the general
conversation where no forum topic is used.

Copy [config/steward.minimal.yaml](../config/steward.minimal.yaml) to
`/etc/steward/steward.yaml` and replace the example remote, IDs and gate with the
repository's actual values. It selects one provider, one world, one Telegram
conversation and one repository. The annotated [full
example](../config/steward.example.yaml) adds incident policy, product adapters and
named target drivers. Keep only the pieces the installation actually uses; every block you
paste in hopefully is a block you will later debug.

That minimal file describes a publishing steward: a finished task can land on `main`.
Omitting `targets` means the harness will not stage a host release — it does not mean
nothing happens, because a connected remote platform may still deploy that push.

Gates run in the task's integration environment, so install its toolchain and use the
repository's real reproducible install, test and build commands. Do not copy the
illustrative npm gate into an unrelated stack. Gates must leave committed input and HEAD
unchanged; a gate that mutates the tree is gating something other than what gets pushed.

Protect the config as root-owned `0600`. Check it using the installed service
interpreter, not your shell's:

```sh
/opt/steward-harness/.venv/bin/steward check /etc/steward/steward.yaml
```

Fix the reported boundary or prerequisite. Do not add sudo to the execution account;
that is not a fix, it is the deletion of the thing being checked.
[Execution identities](execution-boundary.md) explains path ownership, private Git and
inbound media. A schema-only check is useful while you are still writing config, but it
cannot replace this host check.

## Keep it running

Install `/etc/systemd/system/steward.service` as root:

```ini
[Unit]
Description=Steward Harness
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/steward-harness
ExecStart=/opt/steward-harness/.venv/bin/steward run --config /etc/steward/steward.yaml
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

```sh
systemctl daemon-reload
systemctl enable --now steward.service
journalctl -u steward.service -f
```

Supervision belongs to systemd, not to the harness: an unexpected worker fault
propagates out and exits the process, and `Restart=on-failure` is the recovery
mechanism. This controller service is separate from the application's service. A
self-deploying harness must start through its release pointer and use
the installed systemd driver with a separate supervisor unit; a fixed bootstrap path does not become self-updating just because
its source repository is managed.

## Prove the first useful task

Send a normal Telegram message and verify both the reply and the accepted world edit.
Then request a small authorized repository change. Watch the task's checkpoint, green
gate, published SHA and returned result. If the repository deploys, inspect the release
system's result and the running application too.

A checkpoint notice means saved work. `done` means landed work. They are different
claims and the harness is careful to distinguish them, so you should be too.

Before you depend on unattended repair, exercise an `ask`/answer continuation and a red
gate in a repository you are willing to break. For native host deployment, also prove
unhealthy-release rollback — a rollback path you have never run is a rollback path you
do not have. Record the revision, service identity, command or message, and observed
outcome, because a memory of a working journey is not evidence of one.

Then read [where it actually is](../README.md#where-it-actually-is). It lists the current
limits a new installation must not mistake for guarantees.

## Use the onboarding skill

The fork includes [skills/org-onboarding/SKILL.md](../skills/org-onboarding/SKILL.md).
Ask your setup agent to read that file and onboard the named organisation or
repositories. It is an operator-side setup skill: it writes controller config and
inspects release ownership, which is why it is kept separate from the native commit and
Git-reconciler skills loaded into untrusted task sessions.

Example request:

> Read skills/org-onboarding/SKILL.md and onboard our three repositories onto
> the new steward host. Keep the web app on Vercel, the API on its existing
> GitHub Actions release, and the worker on host systemd. Generate the config
> and verify each authorized path; report any missing grant or unsupported bridge.

The skill ships with the fork and uses that checkout's schema. If you install it in your
agent's personal skill directory, keep the harness checkout available and name it in the
request, so its documentation and schema can still be inspected rather than guessed.
