#!/usr/bin/env bash
# Static-analysis worklist generator.
#
# Runs, over src/ by default (pass another path to scope):
#   1. radon cc  — complexity hotspots (blocks ranked C or worse)
#   2. radon mi  — maintainability index, worst files first
#   3. vulture   — dead code (min-confidence 80, see pyproject [tool.vulture])
#   4. pylint duplicate-code (R0801) — cross-file line clones
#   5. jscpd     — token-level clones, catches what R0801 misses (via npx)
#   6. ratio gate — files over 150 lines (a refactor signal, not a rule)
#
# Read-only: nothing here mutates the tree or gates CI. Exit codes of the
# analyzers are suppressed so the whole report always prints.
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:-src}"

echo "== 1. Complexity hotspots (radon cc, rank C or worse) =="
uv run --extra dev radon cc "$SRC" -s -n C || true

echo
echo "== 2. Maintainability index (radon mi, 15 worst) =="
uv run --extra dev radon mi "$SRC" -s | sort -t'(' -k2 -n | head -15 || true

echo
echo "== 3. Dead code (vulture, min-confidence 80) =="
uv run --extra dev vulture "$SRC" || true

echo
echo "== 4. Cross-file line clones (pylint R0801) =="
uv run --extra dev pylint "$SRC" --disable=all --enable=duplicate-code || true

echo
echo "== 5. Token-level clones (jscpd, min-tokens 70) =="
npx --yes jscpd "$SRC" --min-tokens 70 --format python || true

echo
echo "== 6. Files over 150 lines =="
find "$SRC" -name '*.py' -exec wc -l {} + | sort -rn | awk '$1 > 150 && $2 != "total"'
