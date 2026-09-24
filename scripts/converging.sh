#!/bin/sh
# Are we converging? Arithmetic, not opinion.
#
# Usage: scripts/converging.sh [window]     (default: last 4 commits)
#
# Prints size and representation counts for the convergence audit in
# docs/demolition.md. These are diagnostic signals; the audit judges
# removed responsibilities and preserved behavior from the diff.
set -eu

WINDOW="${1:-4}"
BASE="${CONVERGING_BASE:-main}"
SINCE="HEAD~${WINDOW}"

echo "== cumulative vs ${BASE} =="
git diff --numstat "${BASE}" -- src \
  | awk '{a+=$1; d+=$2} END {printf "src   %+d   (added %d, deleted %d)\n", a-d, a, d}'

echo
echo "== window (last ${WINDOW} commits) =="
git diff --numstat "${SINCE}" -- src \
  | awk '{a+=$1; d+=$2} END {printf "src   %+d   (added %d, deleted %d)\n", a-d, a, d}'

echo
echo "== files created / deleted in window =="
git diff --diff-filter=A --numstat "${SINCE}" -- src | awk '{printf "  NEW      %5s  %s\n", $1, $3}'
git diff --diff-filter=D --numstat "${SINCE}" -- src | awk '{printf "  DELETED  %5s  %s\n", $2, $3}'

echo
echo "== concept counts =="
printf "  tables         %s\n" "$(grep -c 'CREATE TABLE' src/steward_harness/state.py 2>/dev/null || echo 0)"
printf "  python modules %s\n" "$(find src -name '*.py' | wc -l | tr -d ' ')"
printf "  src lines      %s\n" "$(find src -name '*.py' -exec cat {} + | wc -l | tr -d ' ')"

echo
echo "== interpretation =="
echo "  Review removed concepts and their owning primitives: docs/demolition.md"
echo "  Source size is a signal, not a fixed-ratio target or proof of correctness."
