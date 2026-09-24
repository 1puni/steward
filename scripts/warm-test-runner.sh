#!/bin/zsh
# Warm test runner.
#
# The full suite is ~5 minutes, almost none of which is the tests: it is
# interpreter start, the uv resolve and the import graph, paid again on every
# run. This keeps one long-lived loop holding all of that hot, so re-running a
# single file costs seconds. That is the difference between migrating twenty
# tests in a session and migrating two.
#
# Start it once and leave it running:
#
#   nohup scripts/warm-test-runner.sh > /tmp/steward-warm-runner.out 2>&1 &
#
# Then, per check:
#
#   echo "tests/test_telegram.py" > /tmp/steward-warm/request   # empty = full suite
#   sleep 4                                                     # see below
#   cat /tmp/steward-warm/status                                # done rc=0 in 3s ...
#   cat /tmp/steward-warm/latest.log
#
# Two things to know:
#   - Arguments are word-split, so `-k 'a or b'` does not survive. Pass paths.
#   - `status` still holds the *previous* run's `done` line until the loop picks
#     the request up, so poll only after a short wait or you will read a stale
#     pass as if it were yours.
set -u

ROOT="${STEWARD_WARM_ROOT:-${0:A:h}/..}"
Q="${STEWARD_WARM_QUEUE:-/tmp/steward-warm}"

mkdir -p "$Q"
cd "$ROOT" || exit 1

# Warm the venv and import graph once so the first real request is already hot.
uv run python -c "import steward_harness, pytest" >/dev/null 2>&1
echo "idle" > "$Q/status"

while true; do
  if [[ -f "$Q/request" ]]; then
    args="$(cat "$Q/request")"
    rm -f "$Q/request"
    echo "running ${args:-<full suite>}" > "$Q/status"
    start=$(date +%s)
    # shellcheck disable=SC2086
    uv run pytest -q -p no:randomly --tb=line ${=args} > "$Q/latest.log" 2>&1
    rc=$?
    end=$(date +%s)
    printf 'done rc=%s in %ss args=%s\n' "$rc" "$((end-start))" "${args:-<full suite>}" > "$Q/status"
  fi
  sleep 2
done
