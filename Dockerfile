# sales0 hosted image (Cloud Run). No Playwright package, no Chrome: scraping runs on the laptop (`sales0 worker`).
FROM python:3.12-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv
WORKDIR /app
# Third-party dependencies only (cached layer): everything except the browser driver.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-install-package playwright

FROM python:3.12-slim
# The app runs from the source tree (PYTHONPATH), so sales0.config.REPO_ROOT == /app and Alembic finds
# /app/alembic.ini and /app/migrations. The venv comes from the build stage (same base image, same python path).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src \
    SALES0_MODE=hosted SALES0_HOST=0.0.0.0 SALES0_DATA_DIR=/tmp/sales0 \
    SALES0_LOG_FORMAT=json SALES0_AUTO_MIGRATE=true
RUN useradd --system --uid 10001 --home-dir /app sales0 && mkdir -p /tmp/sales0 && chown sales0 /tmp/sales0
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
USER sales0
EXPOSE 8080
# Cloud Run injects $PORT.
CMD ["sh", "-c", "SALES0_PORT=${PORT:-8080} exec python -m sales0"]
