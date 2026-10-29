#!/usr/bin/env bash
# Re-record the LLM cassettes (tests/fixtures/llm_cassettes/**) against REAL Haiku.
#
# Run from a checkout that has a real SALES0_ANTHROPIC_API_KEY in .env (the main checkout), one pytest
# at a time. It costs real tokens (the llm-marked tests only) and is the ONLY thing that hits Anthropic;
# the default test run replays the cassettes from disk.
#
#   scripts/record_llm_cassettes.sh                      # every llm-marked integration test
#   scripts/record_llm_cassettes.sh tests/integration/suggest tests/integration/digest   # chosen paths
#
# Record whole test FILES (a module's cassette is rewritten from scratch when its first test records).
# Review `git diff --stat tests/fixtures/llm_cassettes` and commit the result.
set -euo pipefail
cd "$(dirname "$0")/.."

paths=("$@")
if [ ${#paths[@]} -eq 0 ]; then
  paths=(tests/integration)
fi

export SALES0_TEST_LLM_REFRESH=1
# -n 0: recording is serial (one writer per cassette file)
exec uv run pytest "${paths[@]}" -m "llm and not eval and not e2e" -n 0 -p no:logging -q
