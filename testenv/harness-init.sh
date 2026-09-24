#!/bin/sh
# Entry for the follow-through acceptance harness container.
#
# Mirrors the production topology in one container: the controller (root)
# owns landing and the state database; every model-controlled process —
# here, the scripted stub CLI — is dropped to the ordinary `steward`
# account first. The UID transition is the boundary the macOS suite
# cannot observe; this environment exercises it on every turn.
#
# Ownership follows the broker's policy exactly:
#   controller-owned (root:root): the state-database namespace
#     /var/lib/steward itself, inbound media, /etc/steward
#   agent-writable (steward): world/, work/, native homes, delivery
#     roots, the managed repository clone and its bare remote
#
# MODE=acceptance seeds only the conversational world.
# MODE=selftest  additionally clones this checkout as the managed
#                `steward-harness` repository with a bare remote, so the
#                harness can be given a task about itself.
set -eu

# Docker is only a local packaging fixture. The controller and every child
# invocation use the same real systemd ownership as a native Linux install.
if [ "$(cat /proc/1/comm)" != systemd ]; then
    mkdir -p /run /etc/systemd/system/multi-user.target.wants /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/acceptance.conf <<'MANAGER'
[Manager]
DefaultEnvironment=GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=* GIT_CONFIG_KEY_1=http.sslVerify GIT_CONFIG_VALUE_1=false
MANAGER
    python - <<'ENV'
import json, os
from pathlib import Path
Path('/run/acceptance.env').write_text(''.join(
    name + '=' + json.dumps(os.environ.get(name, default)) + '\n'
    for name, default in [('MODE', 'acceptance'),
                          ('CONFIG_SRC', '/testenv/steward.acceptance.yaml'),
                          ('STUB_PLAN', '/testenv/plans/acceptance.json')]))
ENV
    cat > /etc/systemd/system/acceptance-init.service <<'UNIT'
[Unit]
After=network.target
[Service]
Type=oneshot
EnvironmentFile=/run/acceptance.env
ExecStart=/bin/sh /testenv/harness-init.sh
TimeoutStartSec=300
StandardOutput=journal+console
StandardError=journal+console
[Install]
WantedBy=multi-user.target
UNIT
    ln -sf ../acceptance-init.service /etc/systemd/system/multi-user.target.wants/acceptance-init.service
    exec /lib/systemd/systemd
fi

SOURCE=/opt/steward-bootstrap
LIB=/var/lib/steward
MODE="${MODE:-acceptance}"
# The bind-mounted source config carries host ownership, which can collide
# with the execution uid; the daemon requires its configuration to be
# controller-owned, so run from a root-owned copy.
CONFIG_SRC="${CONFIG_SRC:-/testenv/steward.acceptance.yaml}"
CONFIG=/etc/steward/steward.yaml

# Identities ---------------------------------------------------------------
id steward 2>/dev/null || useradd --create-home --shell /usr/sbin/nologin steward

# Git trust, before any git runs: the state volume can be reused across
# container restarts, and ownership of a previously seeded tree belongs to
# the steward identity this script itself creates. The agent's isolated git
# environment voids config files, so this travels as env-based config; the
# git origin's certificate is self-signed by the fake container, and the
# controller's trust of it is a property of this environment, declared here.
export GIT_CONFIG_COUNT=2
export GIT_CONFIG_KEY_0=safe.directory
export GIT_CONFIG_VALUE_0=*
export GIT_CONFIG_KEY_1=http.sslVerify
export GIT_CONFIG_VALUE_1=false

# Controller-owned namespace -------------------------------------------------
mkdir -p "$LIB" "$LIB/inbound-media" "$LIB/outbox-quarantine" /etc/steward

# Snapshot current source, including new implementation files, into a protected
# namespace. Never seed a self-deployment from the host's older committed HEAD.
python - <<'SNAPSHOT'
import pathlib, shutil, subprocess
source, destination = pathlib.Path('/source'), pathlib.Path('/opt/steward-bootstrap')
paths = subprocess.check_output(['git', '-c', 'safe.directory=*', '-C', str(source),
                                 'ls-files', '-z']).split(b'\0')
paths += subprocess.check_output(['git', '-c', 'safe.directory=*', '-C', str(source),
                                  'ls-files', '-z', '--others', '--exclude-standard',
                                  'src', 'tests']).split(b'\0')
for raw in paths:
    if not raw:
        continue
    relative = pathlib.Path(raw.decode())
    if relative.parts[0] in {'.git', '.venv', 'output'} or relative.name.startswith('.env'):
        continue
    origin = source / relative
    if not origin.is_file():
        continue
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(origin, target)
SNAPSHOT

# Configuration inputs -------------------------------------------------------
printf 'acceptance-token' > /etc/steward/telegram-token
chmod 600 /etc/steward/telegram-token
install -m 0644 "$CONFIG_SRC" "$CONFIG"

# The stub CLI the provider section points at.
install -m 0755 "$SOURCE/testenv/claude_stub.py" /usr/local/bin/claude-stub

# Agent-writable territory ----------------------------------------------------
mkdir -p "$LIB/world" "$LIB/work" \
         /home/steward/native/claude \
         /home/steward/outbox /home/steward/artifacts /home/steward/shots

# Seed the conversational world once: a git repo with docs/, main branch.
if ! git -C "$LIB/world" rev-parse HEAD >/dev/null 2>&1; then
    git -C "$LIB/world" init -q -b main
    mkdir -p "$LIB/world/docs"
    printf '# World\n\nDurable knowledge of the acceptance instance.\n' \
        > "$LIB/world/docs/README.md"
    git -C "$LIB/world" add -A
    git -C "$LIB/world" -c user.name=Seed -c user.email=seed@localhost \
        commit -q -m "seed world"
fi

if [ "$MODE" = "selftest" ]; then
    # The self-improvement target. The split-identity boundary rejects
    # local-path remotes by doctrine, so the origin is the fake Bot API
    # container's HTTPS git service; this volume is the storage behind it.
    # The working clone at the configured path is the harness-managed agent
    # workspace: the controller packs remote truth into it, it is never
    # asked to reach the origin itself.
    if [ ! -d /git/steward-harness.git ]; then
        mkdir -p /git
        git -C "$SOURCE" init -q -b main
        git -C "$SOURCE" add -A
        git -C "$SOURCE" -c user.name=Fixture -c user.email=fixture@localhost commit -q -m "current working-tree acceptance snapshot"
        git clone -q --bare "$SOURCE" /git/steward-harness.git
        git -C /git/steward-harness.git config http.receivepack true
        git clone -q /git/steward-harness.git /home/steward/steward-harness
    fi
fi

# Ownership: agent territory only; the state namespace stays controller's.
chown -R steward:steward "$LIB/world" "$LIB/work" /home/steward
[ -d /git ] && chown -R steward:steward /git

# The harness package itself ------------------------------------------------
pip install --quiet --no-cache-dir "$SOURCE" pytest

install -m 0755 /source/testenv/services.py /usr/local/bin/acceptance-services
if [ "$MODE" = selftest ]; then
    install -m 0644 /source/testenv/systemd-target.yaml /etc/steward/target.yaml
    cat > /usr/local/bin/acceptance-target <<'DRIVER'
#!/bin/sh
exec python -B -m steward_harness.deploy.cli --config /etc/steward/steward.yaml --settings /etc/steward/target.yaml "$@"
DRIVER
    chmod 755 /usr/local/bin/acceptance-target
fi
cat > /etc/systemd/system/acceptance-steward.service <<'UNIT'
[Unit]
After=network.target
[Service]
Type=simple
EnvironmentFile=/run/acceptance.env
Environment=GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=* GIT_CONFIG_KEY_1=http.sslVerify GIT_CONFIG_VALUE_1=false
ExecStart=/usr/local/bin/acceptance-services
KillMode=mixed
TimeoutStopSec=90
StandardOutput=journal+console
StandardError=journal+console
UNIT
systemctl daemon-reload
systemctl start --no-block acceptance-steward.service
echo "[harness-init] MODE=$MODE CONFIG=$CONFIG — controller service started"
