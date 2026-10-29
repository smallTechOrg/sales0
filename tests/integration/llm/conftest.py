"""The cassette fixture is normally registered globally (``pytest_plugins = ["tests.llm_cassette"]`` in
tests/conftest.py); importing it here keeps this directory working standalone."""

from tests.llm_cassette import _llm_cassette  # noqa: F401
