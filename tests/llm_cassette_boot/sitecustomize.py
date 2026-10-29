"""Imported at interpreter start in a test's subprocess when PYTHONPATH includes this directory:
installs the LLM cassette (tests/llm_cassette.py) if SALES0_LLM_CASSETTE_FILE is set."""

import os

if os.environ.get("SALES0_LLM_CASSETTE_FILE"):
    try:
        from tests.llm_cassette import install_from_env

        install_from_env()
    except Exception as exc:  # noqa: BLE001 - never break an unrelated interpreter start
        import sys

        print(f"llm cassette boot failed: {exc!r}", file=sys.stderr)
