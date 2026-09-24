#!/bin/sh
# The follow-through acceptance run.
#
# Usage: scripts/follow-through-acceptance.sh [A|B|C|D|all]   (default: all)
#
# A green `uv run pytest` says nothing about Telegram follow-through: the
# suite mocks the transport inside the process. This run stands the whole
# daemon up in Linux containers against a fake Bot API and a scripted model
# CLI, then asserts DID-HAPPEN facts — message in, reply out, world edit on
# the accepted branch, episode recorded — plus the failure modes a live
# Telegram cannot produce on demand.
#
#   A  a plain message follows through to the accepted world
#   B  a cancelled turn leaves no phantom episode (the accepted-world doctrine)
#   C  the poll loop survives a dead getUpdates and still follows through
#   D  one org: conversation → task → question/answer → gate → self-deploy → conversation
#
# Scenarios may run standalone or together; episode-count assertions are
# relative to a snapshot at scenario start, so either order tells the truth.
# KEEP=1 keeps the containers for inspection after a run.
#
# Assertions are plain functions (never `sh -c` strings): a child shell
# cannot see this script's functions, and every assertion here needs one.
set -eu

SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SOURCE"

ONLY="${1:-all}"
[ "$ONLY" = all ] || ONLY="$(echo "$ONLY" | tr 'a-z' 'A-Z')"
case "$ONLY" in A|B|C|D|all) ;; *) echo "Usage: $0 [A|B|C|D|all]" >&2; exit 2 ;; esac

FAKE_PORT="${FAKE_PORT:-$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')}"
FAKE="http://127.0.0.1:${FAKE_PORT}"
RUN_ID="$(date +%s)-$$"
ACCEPT_PROJECT=${ACCEPT_PROJECT:-followthrough-$RUN_ID}
SELFTEST_PROJECT=${SELFTEST_PROJECT:-selfimprove-$RUN_ID}
ACCEPT_CREATED=0
SELFTEST_CREATED=0


PASS=0
FAIL=0

# -- plumbing -----------------------------------------------------------------

fake() { # fake <path> [json-body]
    if [ "$#" -gt 1 ]; then
        curl -sf -X POST -H 'Content-Type: application/json' -d "$2" "$FAKE$1"
    else
        curl -sf -X POST "$FAKE$1"
    fi
}

inject() { # inject <message-text>
    fake /__test/inject "{\"text\": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1")}" >/dev/null
}

sent_texts() { # every text the harness sent, one per line
    fake /__test/journal | python3 -c '
import json, sys
for entry in json.load(sys.stdin)["journal"]:
    if entry.get("method") == "sendMessage":
        print(entry.get("text") or "")
'
}

sent_has() { sent_texts | grep -qF "$1"; }
sent_has_i() { sent_texts | grep -qiF "$1"; }

journal_entries() { # raw journal entries, one JSON per line
    fake /__test/journal | python3 -c '
import json, sys
for entry in json.load(sys.stdin)["journal"]:
    print(json.dumps(entry))
'
}

journal_polled() { journal_entries | grep -q '"method": "getUpdates"'; }
journal_shows_hang_and_recovery() {
    journal_entries | grep -q getupdates_hang && journal_entries | grep -q '"returned": 1'
}

up() { # up <project> <mode> <config-source> <plan>
    if [ "$1" = "$ACCEPT_PROJECT" ]; then ACCEPT_CREATED=1; else SELFTEST_CREATED=1; fi
    MODE="$2" CONFIG_SRC="$3" STUB_PLAN="$4" FAKE_PORT="$FAKE_PORT" \
        docker compose -f testenv/compose.yaml -p "$1" up -d --build --wait
}

down() { # down <project> — only resources created by this invocation
    case "$1" in
        "$ACCEPT_PROJECT") [ "$ACCEPT_CREATED" = 1 ] || return 0 ;;
        "$SELFTEST_PROJECT") [ "$SELFTEST_CREATED" = 1 ] || return 0 ;;
        *) echo "Refusing to remove an unowned project: $1" >&2; return 1 ;;
    esac
    docker compose -f testenv/compose.yaml -p "$1" down -v >/dev/null 2>&1 || true
}

rexec() { # rexec <project> <argv...> — run inside the harness container
    _project="$1"; shift
    docker compose -f testenv/compose.yaml -p "$_project" exec -T harness "$@"
}

world_show() { # world_show <project> <main:path>
    rexec "$1" git -c safe.directory='*' -C /var/lib/steward/world show "main:$2" 2>/dev/null
}

episode_count() { # episode_count <project>
    rexec "$1" git -c safe.directory='*' -C /var/lib/steward/world log main --format='%(trailers:key=Steward-Turn,valueonly)' | grep -c '^turn_' || true
}

world_has() { world_show "$1" "$2" | grep -qF "$3"; }
world_lacks() { ! world_show "$1" "$2" | grep -qF "$3"; }
episodes_have() { rexec "$1" git -c safe.directory='*' -C /var/lib/steward/world log main --format=%B | grep -qF "$2"; }
episodes_lack() { ! episodes_have "$1" "$2"; }
episodes_eq() { [ "$(episode_count "$1")" -eq "$2" ]; }
repo_file_has() { # repo_file_has <project> <repo-dir> <branch:path> <pattern>
    rexec "$1" git -c safe.directory='*' -C "$2" show "$3" 2>/dev/null | grep -qF "$4"
}
tasks_branch_exists() { # tasks_branch_exists <project> <repo-dir>
    rexec "$1" git -c safe.directory='*' -C "$2" for-each-ref --format='%(refname:short)' refs/heads/tasks/ | grep -q 'tasks/'
}
remote_main_has() { # remote_main_has <path> <pattern> — the fake origin's default branch
    docker compose -f testenv/compose.yaml -p "$SELFTEST_PROJECT" exec -T fake-botapi \
        git -c safe.directory='*' --git-dir=/git/steward-harness.git show "main:$1" 2>/dev/null \
        | grep -qF "$2"
}

task_disposition() {
    rexec "$SELFTEST_PROJECT" git -c safe.directory='*' -C /home/steward/steward-harness \
        log -1 --format=%B "tasks/$TASK_ID" | grep -qFx "Disposition: $1"
}

release_healthy() {
    # Compare the remote, symlink, process cwd, daemon HTTP response and
    # deployed ref. No controller database or private Python APIs.
    rexec "$SELFTEST_PROJECT" python -c '
import json, os, pathlib, subprocess, urllib.request
store = "/var/lib/steward/git/steward-harness.git"
def ref(name):
    return subprocess.check_output(["git", "-c", "safe.directory=*", "-C", store,
                                    "rev-parse", name], text=True).strip()
sha = ref("refs/steward/remote/main")
root = pathlib.Path("/opt/steward-current").resolve(strict=True)
pid = pathlib.Path("/run/acceptance-steward.pid").read_text()
with urllib.request.urlopen("http://127.0.0.1:8420/healthz", timeout=3) as response:
    health = json.load(response)
assert root.name == sha == health["sha"] == ref("refs/steward/targets/selftest/healthy")
assert pathlib.Path(os.readlink(f"/proc/{pid}/cwd")) == root
'
}

wait_until() { # wait_until <seconds> <description> <command...>
    _seconds="$1"; _what="$2"; shift 2
    _deadline=$(( $(date +%s) + _seconds ))
    while :; do
        if "$@" >/dev/null 2>&1; then
            echo "    waited-for: $_what"
            return 0
        fi
        if [ "$(date +%s)" -ge "$_deadline" ]; then
            echo "    TIMED OUT after ${_seconds}s: $_what" >&2
            return 1
        fi
        sleep 2
    done
}

assert() { # assert <name> <command...>
    _name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        PASS=$((PASS + 1)); echo "  PASS  $_name"
    else
        FAIL=$((FAIL + 1)); echo "  FAIL  $_name"
    fi
}

assert_not() { # assert_not <name> <command...>
    _name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        FAIL=$((FAIL + 1)); echo "  FAIL  $_name (was expected to be absent)"
    else
        PASS=$((PASS + 1)); echo "  PASS  $_name"
    fi
}

await_polling() { wait_until 120 "daemon polls getUpdates" journal_polled; }

logs_on_failure() { # logs_on_failure <project>
    if [ "$FAIL" -gt 0 ]; then
        echo
        echo "-- last daemon logs ($1) --"
        docker compose -f testenv/compose.yaml -p "$1" logs --tail 40 harness || true
    fi
}

start_acceptance() {
    down "$SELFTEST_PROJECT"
    down "$ACCEPT_PROJECT"
    up "$ACCEPT_PROJECT" acceptance /testenv/steward.acceptance.yaml /testenv/plans/acceptance.json
    await_polling
}

# -- scenarios ----------------------------------------------------------------

scenario_a() {
    echo "== A: a plain message follows through to the accepted world =="
    EPISODES_BEFORE="$(episode_count "$ACCEPT_PROJECT")"
    fake /__test/reset >/dev/null
    inject "/clear"; sleep 3
    inject "Please record the follow-through proof: write docs/follow-through-proof.md containing the line FOLLOW-THROUGH-PROOF-A4. Case A."

    assert "reply reached the operator" \
        wait_until 150 "stub reply sent" sent_has "Recorded the follow-through proof"
    assert "accepted world carries the proof file" \
        world_has "$ACCEPT_PROJECT" docs/follow-through-proof.md FOLLOW-THROUGH-PROOF-A4
    assert "accepted world records the exchange as an episode" \
        episodes_have "$ACCEPT_PROJECT" FOLLOW-THROUGH-PROOF-A4
    assert "episode count grew by exactly one" \
        episodes_eq "$ACCEPT_PROJECT" "$((EPISODES_BEFORE + 1))"
}

scenario_b() {
    echo "== B: a cancelled turn leaves no phantom episode =="
    EPISODES_BEFORE="$(episode_count "$ACCEPT_PROJECT")"
    fake /__test/reset >/dev/null
    inject "/clear"; sleep 3
    inject "CANCEL-CASE: write docs/cancel-proof.md containing CANCEL-CASE-B7. This turn will be cancelled."
    sleep 4   # the stub sleeps 10s; the turn is in flight now
    inject "/cancel"

    assert "operator received a cancellation notice" \
        wait_until 60 "cancel receipt" sent_has "🛑 Cancellation requested for turn_"
    sleep 15  # interruption grace must finish before checking accepted state
    assert "no episode narrates the cancelled exchange" \
        episodes_lack "$ACCEPT_PROJECT" CANCEL-CASE-B7
    assert_not "cancelled edit stayed out of the accepted world" \
        world_has "$ACCEPT_PROJECT" docs/cancel-proof.md CANCEL-CASE-B7
    assert "episode count is unchanged by the cancelled turn" \
        episodes_eq "$ACCEPT_PROJECT" "$EPISODES_BEFORE"
}

scenario_c() {
    echo "== C: the poll loop survives a dead getUpdates =="
    EPISODES_BEFORE="$(episode_count "$ACCEPT_PROJECT")"
    fake /__test/reset >/dev/null
    inject "/clear"; sleep 3
    fake /__test/fault '{"name": "getupdates_hang", "times": 1}' >/dev/null
    inject "HANG-RECOVERY: write docs/hang-recovery-proof.md containing HANG-RECOVERY-C2. Case C."

    assert "reply arrives after the dead poll" \
        wait_until 240 "post-hang reply" sent_has "Recovered after the dead poll"
    assert "accepted world carries the recovery proof" \
        world_has "$ACCEPT_PROJECT" docs/hang-recovery-proof.md HANG-RECOVERY-C2
    assert "episode count grew by exactly one" \
        episodes_eq "$ACCEPT_PROJECT" "$((EPISODES_BEFORE + 1))"
    assert "journal shows the hang and later live polls" \
        journal_shows_hang_and_recovery
}

scenario_d() {
    echo "== D: one organisation converses, answers a task, and deploys itself =="
    down "$ACCEPT_PROJECT"
    down "$SELFTEST_PROJECT"
    up "$SELFTEST_PROJECT" selftest /testenv/steward.selftest.yaml /testenv/plans/selftest.json
    await_polling
    # First let the original revision deploy; the task must replace an already
    # running release, not merely start a daemon for the first time.
    assert "initial harness release is healthy" \
        wait_until 180 "initial deployment" release_healthy
    assert "unowned initial deployment reached the operator" \
        wait_until 60 "initial target notification" sent_has "selftest: satisfied"
    BEFORE_PID="$(rexec "$SELFTEST_PROJECT" cat /run/acceptance-steward.pid)"
    inject "ORG-HELLO"
    assert "conversation reached the operator" \
        wait_until 120 "greeting" sent_has "Ready to work on the harness"
    assert "conversation edited the accepted world" \
        world_has "$SELFTEST_PROJECT" docs/org-proof.md ORG-CONVERSATION
    assert "conversation recorded an episode" \
        episodes_have "$SELFTEST_PROJECT" ORG-HELLO
    inject "Please open a task on the steward-harness repository now. SELF-IMPROVE-PROPOSAL"

    assert "task admitted as a branch" \
        wait_until 150 "tasks/ branch exists" \
        tasks_branch_exists "$SELFTEST_PROJECT" /home/steward/steward-harness
    TASK_ID="$(rexec "$SELFTEST_PROJECT" git -c safe.directory='*' -C /home/steward/steward-harness for-each-ref --format='%(refname:short)' refs/heads/tasks/ | head -1 | sed 's|tasks/||')"
    echo "    task: $TASK_ID"
    assert "task saved a question checkpoint" \
        wait_until 120 "ask checkpoint" task_disposition ask
    assert "draft is retained on the task branch" \
        repo_file_has "$SELFTEST_PROJECT" /home/steward/steward-harness \
            "tasks/$TASK_ID:docs/self-improve-proof.md" SELF-IMPROVE-DRAFT
    assert_not "waiting work stayed off main" \
        remote_main_has docs/self-improve-proof.md SELF-IMPROVE-DRAFT
    assert "question reached its owning conversation" \
        wait_until 120 "waiting result" sent_has "Task waiting: Record the self-improvement proof"
    inject "/task show $TASK_ID"
    assert "operator can inspect the task question" \
        wait_until 60 "task inspection" sent_has "$TASK_ID — waiting"
    inject "/task answer $TASK_ID OPERATOR-ANSWER-D9"
    assert "answered task saved its idle checkpoint" \
        wait_until 120 "idle checkpoint" task_disposition idle

    assert "proof landed on the remote's default branch" \
        wait_until 300 "remote main carries the proof" \
        remote_main_has docs/self-improve-proof.md SELF-IMPROVE-PROOF-MARKER
    assert "controller's store observed the landed tip" \
        wait_until 120 "controller store carries the proof" \
        repo_file_has "$SELFTEST_PROJECT" /var/lib/steward/git/steward-harness.git \
            refs/steward/remote/main:docs/self-improve-proof.md SELF-IMPROVE-PROOF-MARKER
    assert "the canonical task account is retained in accepted task Git" \
        repo_file_has "$SELFTEST_PROJECT" /var/lib/steward/state.db.tasks.git "tasks/$TASK_ID:task.md" "SELF-IMPROVE-PROOF-MARKER"
    assert "operator received the completed task result" \
        wait_until 120 "done receipt" sent_has "Task done: Record the self-improvement proof"
    assert "operator answer landed in the same task's work" \
        remote_main_has docs/self-improve-proof.md OPERATOR-ANSWER-D9
    assert "published revision is running and recorded healthy" \
        wait_until 180 "self-deployment" release_healthy
    AFTER_PID="$(rexec "$SELFTEST_PROJECT" cat /run/acceptance-steward.pid)"
    assert "self-deployment replaced the daemon process" test "$BEFORE_PID" != "$AFTER_PID"
    inject "ORG-AFTER-DEPLOY"
    assert "replacement daemon answers the conversation" \
        wait_until 120 "post-deploy reply" sent_has "The replacement steward is answering"
    assert "replacement daemon accepts world edits" \
        world_has "$SELFTEST_PROJECT" docs/after-deploy.md ORG-AFTER-DEPLOY
    assert "replacement daemon records the exchange" \
        episodes_have "$SELFTEST_PROJECT" ORG-AFTER-DEPLOY
}

# -- run ----------------------------------------------------------------------

# Refuse existing resources, including explicitly requested project names.
# Never destroy another fixture's persistent state to prepare this run.
[ "$ACCEPT_PROJECT" != "$SELFTEST_PROJECT" ] || { echo "Project names must differ" >&2; exit 1; }
for project in "$ACCEPT_PROJECT" "$SELFTEST_PROJECT"; do
    containers=$(docker ps -aq --filter "label=com.docker.compose.project=$project")
    volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project")
    networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project")
    if [ -n "$containers$volumes$networks" ]; then
        echo "Refusing existing Compose project $project; choose a new project name." >&2
        exit 1
    fi
done
cleanup() {
    if [ "${KEEP:-0}" != 1 ]; then
        down "$ACCEPT_PROJECT"
        down "$SELFTEST_PROJECT"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$ONLY" = all ] || [ "$ONLY" = A ]; then
    start_acceptance
    scenario_a
fi
if [ "$ONLY" = B ]; then
    start_acceptance
fi
if [ "$ONLY" = all ] || [ "$ONLY" = B ]; then
    scenario_b
fi
if [ "$ONLY" = C ]; then
    start_acceptance
fi
if [ "$ONLY" = all ] || [ "$ONLY" = C ]; then
    scenario_c
    logs_on_failure "$ACCEPT_PROJECT"
fi
if [ "$ONLY" = all ] || [ "$ONLY" = D ]; then
    scenario_d
    logs_on_failure "$SELFTEST_PROJECT"
fi

echo
echo "follow-through acceptance: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
