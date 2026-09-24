#!/bin/sh
# Real Linux UID and systemd invocation ownership acceptance.
# Native: sudo STEWARD_TEST_PYTHON=/protected/venv/bin/python scripts/linux-boundary-acceptance.sh
# Local Docker Desktop fixture: scripts/linux-boundary-acceptance.sh --docker
# The image is a test host only; production execution uses native Linux services.
set -eu
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${1:-}" = --docker ]; then
    shift
    image=steward-systemd-acceptance:local
    name="steward-boundary-acceptance-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
    docker build -t "$image" -f "$SOURCE/testenv/systemd.Dockerfile" "$SOURCE/testenv"
    if docker inspect "$name" >/dev/null 2>&1; then
        echo "Refusing to use existing container $name" >&2
        exit 1
    fi
    docker create --name "$name" --privileged --cgroupns=private \
        --tmpfs /run --tmpfs /run/lock \
        --mount "type=bind,source=$SOURCE,target=/source,readonly" "$image" >/dev/null
    trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT INT TERM
    docker start "$name" >/dev/null
    attempts=0
    until docker exec "$name" systemctl list-units --no-legend >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 30 ] || { docker logs "$name"; exit 1; }
        sleep 1
    done
    docker exec "$name" /source/scripts/linux-boundary-acceptance.sh "$@"
    exit
fi
[ "$(uname -s)" = Linux ] && [ "$(id -u)" = 0 ] || {
    echo 'Native acceptance requires Linux root; use --docker for the local test host.' >&2
    exit 1
}
[ -d /run/systemd/system ] || { echo 'A real systemd system manager is required.' >&2; exit 1; }
python="${STEWARD_TEST_PYTHON:-python3}"
python="$(command -v "$python")"
# All dependencies must already exist; the runner never modifies host packages.
"$python" -c 'import pytest, pydantic, yaml, httpx'
scratch="$(mktemp -d /var/lib/steward-boundary-acceptance.XXXXXX)"
unit="steward-boundary-acceptance-$(cat /proc/sys/kernel/random/uuid).service"
trap 'systemctl stop "$unit" >/dev/null 2>&1 || true; rm -rf "$scratch"' EXIT INT TERM
# The launcher runs as root under PID 1. A user-writable checkout is not a
# trusted executable: copy it into a private controller-owned namespace.
cp -R "$SOURCE/src" "$SOURCE/tests" "$scratch/"
chmod 755 "$scratch"
chown -R root:root "$scratch"
chmod -R go-w "$scratch"
if [ "$#" = 0 ]; then
    set -- tests/test_boundary_acceptance.py tests/test_linux_deployment_acceptance.py tests/test_host_ownership.py tests/test_git_metadata.py tests/test_git_operation_paths.py
fi
systemd-run --quiet --pipe --wait --collect --unit="$unit" \
    -p "WorkingDirectory=$scratch" -p "Environment=PYTHONPATH=$scratch/src" \
    -p 'Environment=PYTHONDONTWRITEBYTECODE=1' \
    "$python" -m pytest -q -s -p no:cacheprovider "$@"
