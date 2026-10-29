"""Canonical run command: ``uv run python -m sales0``.

load settings → configure logging → startup checks → verify DB at head → uvicorn (workers=1).
"""

from __future__ import annotations

import sys

from pydantic import ValidationError


def main() -> int:
    from sales0.config import StartupError, check_startup, get_settings

    try:
        settings = get_settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"SALES0_{'_'.join(str(p) for p in e['loc']).upper()}: {e['msg']}" for e in exc.errors()
        )
        print(f"sales0: invalid configuration — {problems}", file=sys.stderr)
        return 2

    from sales0.logging import configure_logging, get_logger

    configure_logging(settings)
    log = get_logger("sales0.main")

    try:
        check_startup(settings)
    except StartupError as exc:
        log.error("startup_refused", reason=str(exc))
        print(f"sales0: {exc}", file=sys.stderr)
        return 2

    from sales0.db.engine import get_engine
    from sales0.db.migrate_check import DatabaseNotAtHead, assert_at_head

    settings.data_path.mkdir(parents=True, exist_ok=True)
    if settings.auto_migrate:  # hosted: create the database if needed and upgrade it before serving
        from sales0.db.bootstrap import bootstrap

        try:
            bootstrap(settings.resolved_database_url)
        except Exception as exc:  # noqa: BLE001 - say what failed, never print the URL (it holds the password)
            log.error("startup_refused", reason=f"database bootstrap failed ({type(exc).__name__})")
            print(f"sales0: database bootstrap failed ({type(exc).__name__}: {str(exc)[:200]})", file=sys.stderr)
            return 1
    try:
        revision = assert_at_head(get_engine())
    except DatabaseNotAtHead as exc:
        log.error("startup_refused", reason=str(exc))
        print(f"sales0: {exc}", file=sys.stderr)
        return 1

    import uvicorn

    from sales0.web.app import UVICORN_OPTIONS, create_app

    log.info(
        "server_starting",
        url=f"{settings.base_url}/",
        db_revision=revision,
        mode=settings.mode,
        linkedin_mode=settings.linkedin_mode,
        model=settings.llm_model,
        api_key_present=settings.api_key_present,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        workers=1,
        log_config=None,
        access_log=False,
        **UVICORN_OPTIONS,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
