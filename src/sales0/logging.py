"""structlog configuration: one JSON object per line on stdout (and SALES0_LOG_FILE if set).

stdlib loggers (uvicorn, alembic, ...) are routed through the same renderer, so every
line in the server output has the same shape: ``timestamp``, ``level``, ``event``,
``logger`` plus any bound context (``request_id``, ``run_id``, ``job``, ``enquiry_id``).
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog

from sales0.config import Settings

_HANDLER_MARK = "_sales0_handler"


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]


def configure_logging(settings: Settings, *, stream: IO[str] | None = None) -> None:
    """Idempotently configure structlog + stdlib logging from settings."""
    renderer: Any
    if settings.log_format == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            root.removeHandler(handler)
            handler.close()

    handlers: list[logging.Handler] = [logging.StreamHandler(stream or sys.stdout)]
    log_path = settings.log_file_path
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    for handler in handlers:
        setattr(handler, _HANDLER_MARK, True)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Our middleware writes the http_request line; silence uvicorn's duplicate access log.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *_shared_processors(),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "sales0") -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def bind_context(**values: Any) -> None:
    """Bind values (request_id, run_id, job, enquiry_id) to every log line in this context."""
    structlog.contextvars.bind_contextvars(**values)


def unbind_context(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
