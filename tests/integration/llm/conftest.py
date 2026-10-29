"""The cassette fixtures are normally registered globally (``pytest_plugins = ["tests.llm_cassette"]`` in
tests/conftest.py); importing them here keeps this directory working standalone."""

from tests.llm_cassette import _llm_cassette, _llm_cassette_module  # noqa: F401
