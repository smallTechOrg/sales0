"""Operator CLI: ``uv run python -m sales0.cli check-llm``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

# CLI ``type=`` labels (architecture.md -> LLM layer). Most specific class first: a
# workspace-required error is also an ``LLMAuthError``.
_ERROR_LABELS: tuple[tuple[str, str], ...] = (
    ("LLMWorkspaceRequired", "workspace-required"),
    ("LLMAuthError", "auth"),
    ("LLMModelNotFound", "model-not-found"),
    ("LLMTransientError", "transient"),
    ("LLMBadResponse", "bad-response"),
)


def error_label(exc: Exception) -> str:
    """The ``LLM ERROR type=<label>`` value for an adapter error."""
    from sales0.llm import errors

    for class_name, label in _ERROR_LABELS:
        if isinstance(exc, getattr(errors, class_name)):
            return label
    return "bad-response"


def _check_llm() -> int:
    from pydantic import ValidationError

    from sales0.config import get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"LLM ERROR invalid configuration: {exc.error_count()} error(s) — see .env.example", file=sys.stderr)
        return 1

    from sales0.llm import LLMError
    from sales0.llm.anthropic_adapter import AnthropicClient
    from sales0.logging import configure_logging

    # Logs go to stderr so stdout carries only the result line.
    configure_logging(settings, stream=sys.stderr)
    try:
        response = AnthropicClient(settings).ping()
    except LLMError as exc:
        print(f"LLM ERROR type={error_label(exc)} {exc.message}", file=sys.stderr)
        return 1
    print(
        f"LLM OK model={settings.llm_model} "
        f"input_tokens={response.input_tokens} output_tokens={response.output_tokens}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sales0.cli", description="sales0 operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-llm", help="One real Haiku call to prove the key and model slug are alive")
    args = parser.parse_args(argv)
    if args.command == "check-llm":
        return _check_llm()
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
