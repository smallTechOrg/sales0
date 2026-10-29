#!/usr/bin/env bash
# Fast, parallel test runner (no make). Usage:
#   scripts/test.sh              unit + integration (parallel), then e2e (capped workers)
#   scripts/test.sh fast         unit + integration without llm/slow tests (the dev loop)
#   scripts/test.sh unit|integration|e2e   one layer
#   scripts/test.sh -- <pytest args>       anything else, e.g. scripts/test.sh -- tests/unit/crm -k keys
# Env: SALES0_TEST_WORKERS (default: min(cores, 6)), SALES0_TEST_E2E_WORKERS (default 3; each e2e
# worker is a Chromium + a server, ~1 GB). The real Anthropic key is read from .env by the app;
# with no .env and no SALES0_ANTHROPIC_API_KEY a dummy is used and `llm` tests are deselected.
set -euo pipefail
cd "$(dirname "$0")/.."

cores=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
workers=${SALES0_TEST_WORKERS:-$(( cores > 6 ? 6 : cores ))}
e2e_workers=${SALES0_TEST_E2E_WORKERS:-3}
mode=${1:-all}
if [ $# -gt 0 ]; then shift; fi

llm_filter=()
if [ ! -f .env ] && [ -z "${SALES0_ANTHROPIC_API_KEY:-}" ]; then
  export SALES0_ANTHROPIC_API_KEY=sk-test-dummy
  llm_filter=(-m "not llm")
  echo "[test.sh] no .env / key: using a dummy key and deselecting llm tests" >&2
fi

run() { echo "+ pytest $*" >&2; uv run pytest "$@"; }

case "$mode" in
  unit)        run tests/unit -n "$workers" "$@" ;;
  integration) run tests/integration -n "$workers" ${llm_filter[@]+"${llm_filter[@]}"} "$@" ;;
  e2e)         run tests/e2e -n "$e2e_workers" "$@" ;;
  fast)        run tests/unit tests/integration -n "$workers" -m "not llm and not slow and not eval" "$@" ;;
  all)
    run tests/unit tests/integration -n "$workers" ${llm_filter[@]+"${llm_filter[@]}"} "$@"
    run tests/e2e -n "$e2e_workers" "$@"
    ;;
  --)          run "$@" ;;
  *)           echo "usage: scripts/test.sh [all|fast|unit|integration|e2e|-- <pytest args>]" >&2; exit 2 ;;
esac
