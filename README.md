# sales0

AI sales and marketing agent for smallTech: scrapes requirement posts from LinkedIn on the
team's own logged-in session, triages each enquiry with one cheap Claude Haiku call, and
puts everything in a human review inbox. (Scaffold stage: the runnable skeleton only.)

## Requirements

- Python 3.12 (pinned in `.python-version`; asdf works) and [uv](https://docs.astral.sh/uv/)
- An Anthropic API key

## Setup

```bash
# from the repo root
cp .env.example .env            # then set SALES0_ANTHROPIC_API_KEY in .env (never commit .env)
uv sync                         # creates .venv and installs app + dev dependencies
uv run playwright install chromium
uv run alembic upgrade head     # creates data/sales0.db
uv run alembic current          # shows: 0001_baseline (head)
uv run python -m sales0.cli check-llm   # one real Haiku call: LLM OK model=claude-haiku-5-5 ...
```

## Run

```bash
uv run python -m sales0
```

Open http://127.0.0.1:8765/ — you should see "sales0 is running".
Health check: http://127.0.0.1:8765/api/health

Override the bind address with `SALES0_HOST` / `SALES0_PORT` in `.env`. Phase 1 refuses to
bind to a non-loopback host. Every setting is documented in `.env.example`.

## Tests

```bash
uv run pytest tests -q
```

Integration tests make real Anthropic calls with the key from `.env` (a few tokens each)
and fail the session if the key is missing. Tests always use a fresh temporary SQLite
file, never `data/sales0.db`.

## Layout

See `spec/architecture.md` → Layout. Source lives in `src/sales0/`, migrations in
`migrations/`, tests in `tests/`. Runtime data (DB, browser profiles, logs) lives in the
gitignored `data/` directory.
