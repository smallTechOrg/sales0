# sales0 — Architecture

The product narrative is in `roadmap.md` and `capabilities/`. This file is the HOW: system overview, stack, layout, conventions, configuration, inter-slice contracts, and the fixture LinkedIn corpus. The AI composition is in `agent.md`. The schema is in `data.md`. Routes are in `api.md`.

## System Overview

sales0 is a single Python process: one uvicorn worker serving a FastAPI app. It contains:

1. **Web layer.** Server-rendered Jinja2 pages with htmx partial updates, on one origin at `http://127.0.0.1:8765/`.
2. **Services.** Synchronous domain functions over SQLAlchemy sessions. Routes and jobs call only these.
3. **Job runner.** A single-flight background thread that runs browser jobs: `connect`, `check`, `discovery_run`. Only one runs at a time, because the persistent browser profile cannot be opened twice.
4. **Scraper.** Playwright (sync API, Chromium) on a persistent profile under `data/linkedin-profile/` (live) or `data/linkedin-profile-fixture/` (fixture). It returns plain dataclasses and never touches the DB.
5. **Triage agent.** A deterministic graph of plain-Python nodes around ONE structured Haiku call per enquiry (`agent.md`).
6. **LLM layer.** The internal `LLMClient` protocol and its Anthropic adapter: retry/backoff, token usage, cost, and one structured log per call.
7. **Persistence.** SQLite file `data/sales0.db` (WAL mode), with migrations managed by Alembic.

```
Browser (htmx) ──HTTP──> FastAPI routes ──> services ──> SQLAlchemy ──> SQLite (data/sales0.db)
                               │                │
                               │                └─> jobs.runner (1 background thread)
                               │                        └─> jobs.discovery_job
                               │                               ├─> scraper (Playwright, persistent profile)
                               │                               │      └─> linkedin.com  |  /__fixtures/linkedin (fixture mode, same server)
                               │                               ├─> services.dedupe (persist/merge)
                               │                               └─> agent.graph.run_triage ─> llm.AnthropicClient ─> Anthropic API (Haiku)
                               └─> htmx polling: /runs/{id}/progress, /setup/linkedin/status (every 1s while active)
```

### Discovery run data flow

1. `POST /runs` calls `services.runs.start_run`. This checks eligibility, inserts a `scrape_runs` row with status `queued`, and submits `discovery_job.execute(run_id)` to the runner.
2. The job sets status `running`. It selects keywords: active ones, ordered by `last_run_at` (never-run first) then `id`, capped at `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN`. It opens the persistent context and runs a session check on `{base}/feed/`. If the session is not logged in, the run aborts with reason `logout`.
3. For each keyword: `LinkedInSearcher.search(keyword, max_posts)` returns a list of `ScrapedPost`. For each post, `services.dedupe.upsert_post` returns `new | dupe_urn | dupe_content`. It inserts or merges the enquiry, its source and the keyword hit. The job then commits and updates the run and run-keyword counters.
4. For each NEW enquiry from that keyword: `services.triage.triage_enquiry(db, enquiry_id, llm)` loads the offerings and few-shot examples, calls `agent.graph.run_triage`, and persists the result, the `llm_calls` row and a `status_history` row.
5. A paced pause runs between keywords. The job stops early when the daily post cap is reached.
6. `AccountSafetyStop` (logout / checkpoint / captcha / rate_limited) closes the browser immediately. The run becomes `aborted` and the session becomes `needs_attention`. Enquiries already saved but still untriaged ARE triaged, because that makes no LinkedIn request.
7. The run is finalised: status `succeeded` (or `aborted`/`failed`) and `finished_at` are set, and on success `linkedin_session.last_successful_run_at` is updated.

At startup, any run left in `queued` or `running` is marked `failed` with the error "Interrupted by server restart".

## Stack

User-binding constraints: Python 3.12 via asdf, `uv` with a project-local `.venv`, Anthropic Claude Haiku ONLY (`claude-haiku-5-5`, env-configurable), port other than the default, runs on a Mac now and must be portable to a VPS, secrets in `.env`.

| Concern | Choice | Rationale |
|---|---|---|
| Language/runtime | Python 3.12, pinned by `.python-version` (`3.12`), uv-managed `.venv` | Binding environment. Playwright, the Anthropic SDK and SQLAlchemy are all first-class in Python. |
| Web framework | FastAPI + uvicorn (`uvicorn[standard]`), **workers=1** | Light and typed, with an easy JSON envelope and Jinja2 support. One worker because the in-process job runner and browser profile lock must be singletons. |
| Frontend | Server-rendered Jinja2 + **htmx 2.x vendored** at `web/static/vendor/htmx.min.js` + one small `app.js`, plain CSS | Zero-build and single-origin. The UI is lists, cards, forms and polling, with no complex client state. |
| Database | **SQLite (production engine)** with SQLAlchemy 2.0 ORM + Alembic | 1–3 users and low thousands of rows on one laptop. Zero ops, single-file backup. |
| Browser automation | Playwright for Python (sync API) with Chromium `launch_persistent_context` | It can drive the team's real logged-in profile in headed or headless mode. The same library drives the E2E tests. |
| LLM | Anthropic Python SDK, model `claude-haiku-5-5` from `SALES0_LLM_MODEL` | Binding (Haiku only, cheapest). Structured output via forced tool use. |
| Config | pydantic-settings (`env_prefix="SALES0_"`, `env_file=".env"`, `extra="ignore"`), `SecretStr` for keys | Typed and validated at startup. Secrets cannot be printed. |
| Logging | structlog → JSON lines on stdout (and `SALES0_LOG_FILE` if set) | Structured request, LLM and scrape logs from day one. |
| Tests | pytest, pytest-playwright (headless Chromium), httpx (FastAPI TestClient), pytest-timeout | One runner for unit, integration and live E2E tests. |
| Timezone | `zoneinfo` + `tzdata`, display/"today" boundaries in `SALES0_TIMEZONE` (default `Asia/Kolkata`) | Daily caps and daily/monthly cost totals need a defined "day". |

> **Assumed:** stack = Python 3.12 + FastAPI + Jinja2/htmx + SQLAlchemy 2.0/Alembic + Playwright + Anthropic SDK + structlog. There was no user preference. This set is the simplest single-process, zero-build, portable fit for a scraper, a cheap LLM triage step and a CRUD UI.

> **Assumed:** **SQLite is the production engine** (`sqlite:///data/sales0.db`), so tests run on real SQLite files (a fresh file per test or per server, migrated by Alembic), never in-memory. Portability to Postgres on a VPS means changing `SALES0_DATABASE_URL` to `postgresql+psycopg://…` and adding the driver. Code and migrations must therefore stay dialect-neutral: no raw SQLite SQL, generic `JSON` type, `render_as_batch=True` for SQLite ALTERs, and explicit constraint names via a naming convention. If the VPS move switches the engine, the gate switches to Postgres in that same change.

> **Assumed:** no LangGraph. The agent is a small explicit node graph in plain Python (`agent/graph.py`), because triage is one structured call with a repair edge. A framework would add weight without any capability the design uses. Phase 3's research loop is also hand-rolled (≤4 steps). Native tracing therefore does not apply. Observability is the structured logs plus the `llm_calls` table.

> **Assumed:** Haiku pricing defaults are `SALES0_LLM_PRICE_INPUT_USD_PER_MTOK=1.00` and `SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK=5.00` (the Haiku-class price point). They are configurable via env, and the user should confirm them against current Anthropic pricing.

> **Assumed:** no prompt caching in Phase 1. The system prompt plus few-shot is ~1.5k tokens, below the size where caching pays off reliably. Cost math therefore covers plain input and output tokens only.

Dependency rule: everything needed at runtime or deploy time goes in `[project].dependencies`: fastapi, uvicorn[standard], jinja2, python-multipart, sqlalchemy>=2.0, alembic, pydantic>=2, pydantic-settings, structlog, anthropic, playwright, tzdata, httpx. Only pytest, pytest-playwright and pytest-timeout go in `[dependency-groups].dev`. Versions are the latest at scaffold time, locked by the committed `uv.lock`. `uv sync` installs both groups.

**Run command (documented, canonical):** `uv run python -m sales0`. This runs `.venv/bin/python -m sales0` on the pinned 3.12 and serves `http://127.0.0.1:8765/`. Override with `SALES0_PORT` or `SALES0_HOST`.

## Layout

```
/Users/sai/Workspace/Code/sales0/
├── .python-version                 # 3.12
├── pyproject.toml                  # project "sales0", src layout, deps (see Stack)
├── uv.lock
├── alembic.ini                     # script_location = migrations; URL from sales0.config
├── .env.example                    # every var in "Configuration"
├── .gitignore                      # .env, data/, .venv/, __pycache__/, .pytest_cache/, test-results/
├── README.md
├── spec/                           # this spec
├── data/                           # GITIGNORED runtime dir (created at startup)
│   ├── sales0.db                   # SQLite production DB
│   ├── linkedin-profile/           # persistent Chromium profile (live mode)
│   ├── linkedin-profile-fixture/   # persistent profile (fixture mode)
│   └── logs/                       # optional SALES0_LOG_FILE target
├── migrations/
│   ├── env.py                      # imports sales0.db.models metadata; render_as_batch=True
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_baseline.py        # scaffold: app_meta
│       └── 0002_phase1_discovery.py
├── src/sales0/
│   ├── __init__.py
│   ├── __main__.py                 # load settings → configure logging → verify DB at head → uvicorn.run(app, host, port, workers=1)
│   ├── config.py                   # Settings + get_settings() (lru_cache) + reset_settings()
│   ├── logging.py                  # structlog JSON config; bind/unbind contextvars
│   ├── errors.py                   # DomainError(code, message, http_status=400)
│   ├── clock.py                    # utcnow(), local_today(tz) — the injectable time seam
│   ├── cli.py                      # `python -m sales0.cli check-llm`
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                 # DeclarativeBase + MetaData(naming_convention)
│   │   ├── engine.py               # get_engine(), session_scope(), reset_engine(); SQLite pragmas
│   │   ├── migrate_check.py        # current_revision(), head_revision(), assert_at_head()
│   │   └── models/                 # slice A: one module per aggregate (see data.md)
│   │       ├── __init__.py         # exports every model (Alembic autogenerate target)
│   │       ├── meta.py  users.py  offerings.py  keywords.py  linkedin.py
│   │       ├── runs.py  enquiries.py  decisions.py  llm_calls.py  history.py
│   ├── llm/                        # scaffold: LLM abstraction layer
│   │   ├── __init__.py             # get_llm_client(), reset_llm_client()
│   │   ├── client.py               # LLMClient Protocol, StructuredRequest, LLMResponse
│   │   ├── anthropic_adapter.py    # AnthropicClient: forced tool use, retries, usage, logging
│   │   ├── pricing.py              # cost_usd_micros(input_tokens, output_tokens, settings)
│   │   └── errors.py               # LLMAuthError, LLMModelNotFound, LLMTransientError, LLMBadResponse
│   ├── prompts/                    # slice B: prompts as files
│   │   ├── triage_system.md
│   │   └── triage_user.md.j2
│   ├── agent/                      # slice B: mirrors agent.md
│   │   ├── __init__.py
│   │   ├── schemas.py              # TriageInput, OfferingRef, DecisionExample, TriageResult, TriageOutcome
│   │   ├── state.py                # TriageState
│   │   ├── nodes.py                # build_prompt, call_model, validate_and_guard, repair, finalize
│   │   ├── graph.py                # run_triage(inp, deps) — assembly + routing
│   │   ├── guards.py               # pure output guardrails
│   │   └── fewshot.py              # select_examples (pure) + load_candidates(db)
│   ├── scraper/                    # slice C: no DB access
│   │   ├── __init__.py
│   │   ├── types.py                # ScrapedPost, KeywordScrapeResult, SessionCheckResult, ConnectResult
│   │   ├── errors.py               # AccountSafetyStop(kind, url), ScrapeError
│   │   ├── browser.py              # open_context(profile_dir, headless) → persistent context
│   │   ├── urls.py                 # base_url(settings), search_url(base, keyword, date_filter)
│   │   ├── selectors.py            # ALL DOM selectors in one place
│   │   ├── detection.py            # detect_safety_stop(page, response) → kind | None
│   │   ├── pacing.py               # Pacer(min_s, max_s, rng, sleep)
│   │   ├── relative_time.py        # "5h"/"2d"/"1w"/"3mo" → date estimate
│   │   ├── session_check.py        # check_session(context, base) → SessionCheckResult
│   │   ├── connect.py              # connect_interactive(settings, on_tick, should_cancel) → ConnectResult
│   │   └── search.py               # LinkedInSearcher (context manager) .search(keyword, max_posts)
│   ├── linkedin_fixture/           # slice C: fake LinkedIn, mounted at /__fixtures/linkedin ONLY in fixture mode
│   │   ├── __init__.py
│   │   ├── routes.py               # login, feed, authwall, search/results/content, checkpoint/challenge
│   │   ├── posts.json              # the corpus (see "Fixture LinkedIn corpus")
│   │   └── templates/              # DOM shaped to match scraper/selectors.py
│   ├── services/                   # slice D: the only DB-writing domain layer
│   │   ├── __init__.py
│   │   ├── views.py                # typed view models returned to routes (see Contracts)
│   │   ├── keywords.py  offerings.py  session.py  runs.py
│   │   ├── dedupe.py               # normalize_text, content_hash (pure) + upsert_post(db, …)
│   │   ├── triage.py               # triage_enquiry / retry_triage (persist agent outcome)
│   │   ├── inbox.py                # list/decide/update/search
│   │   └── costs.py                # cost_summary
│   ├── jobs/                       # slice D
│   │   ├── __init__.py
│   │   ├── runner.py               # JobRunner: single-flight daemon thread; get_runner()/reset_runner()
│   │   ├── connect_job.py  check_job.py
│   │   └── discovery_job.py        # execute(run_id)
│   └── web/
│       ├── __init__.py
│       ├── app.py                  # scaffold: create_app(); registers routers; static; startup hooks
│       ├── envelope.py             # ok(data), err(code, message, status) + exception handlers
│       ├── middleware.py           # request_id + http_request log
│       ├── templating.py           # Jinja env + filters: usd, tokens, ago, localdt, verdict_label
│       ├── context.py              # slice E: shell_context(request, db) → header/banner/cost
│       ├── routes/
│       │   ├── __init__.py  health.py (scaffold)
│       │   ├── stubs.py (E)  setup.py (F)  runs.py (G)  inbox.py (H)
│       ├── templates/
│       │   ├── base.html  _shell/ (E)   stubs/ (E)
│       │   ├── setup/ (F)   runs/ (G)   inbox/ (H)
│       └── static/
│           ├── css/app.css (E)   js/app.js (E)   vendor/htmx.min.js (scaffold)
└── tests/
    ├── __init__.py
    ├── conftest.py                 # scaffold: env isolation, tmp SQLite + alembic upgrade, resets
    ├── support/                    # fake_llm.py (B), live_server.py (I)
    ├── unit/                       # pure; no network; agent/, scraper/, services/
    ├── integration/                # real SQLite files; real Haiku where marked; db/, agent/, scraper/, services/, jobs/, web/
    └── e2e/                        # live server subprocess + headless Chromium + real Haiku
```

## Conventions

**Python**
- Module, function and variable names are `snake_case`, classes are `PascalCase`, constants are `UPPER_SNAKE`. Enum-like values are lowercase snake strings stored as `String` columns, validated by `Literal`/`StrEnum` in Python. SQLite has no native enums, and plain strings keep the schema portable.
- Every function crossing a module boundary takes and returns typed objects (Pydantic models or frozen dataclasses), never raw dicts. Routes never touch ORM objects; they receive `services.views.*`.
- Services are synchronous and take `db: Session` first. Time comes from `sales0.clock` (`now: datetime | None = None` parameters where tests need control). Randomness comes from an injected `random.Random`.
- Singletons expose reset seams: `get_settings`/`reset_settings`, `get_engine`/`reset_engine`, `get_llm_client`/`reset_llm_client`, `get_runner`/`reset_runner`.
- Settings: `extra="ignore"`. Enum-like env values (`linkedin_mode`, `log_format`) are resolved through a property that strips inline `# comments` and whitespace and then validates. Startup fails loudly when `SALES0_ANTHROPIC_API_KEY` is missing, when the DB is not at head ("Run: uv run alembic upgrade head"), or when an invalid mode is set.
- Starlette ≥1.0 form only: `templates.TemplateResponse(request, "x.html", {...})`.
- No raw SQL strings except Alembic data seeds via `op.bulk_insert`. All queries go through the ORM or Core with bound parameters.
- Money is stored as integer micro-USD (`cost_usd_micros`). It is displayed with the `usd` filter (≥4 decimals below $0.01, e.g. `$0.0012`).
- Datetimes are timezone-aware UTC in the DB (`DateTime(timezone=True)`) and are shown in `SALES0_TIMEZONE` via `localdt` and `ago`.

**SQLite engine setup** (`db/engine.py`): `connect_args={"check_same_thread": False}`. On every connect it sets `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000`. It uses `sessionmaker(expire_on_commit=False)`, and `session_scope()` commits on success and rolls back on exception. The background thread opens its own sessions and never shares one with a request.

**Error shape**
- JSON endpoints (`/api/*` and any `Accept: application/json`) return `{"ok": true, "data": {...}}`, or `{"ok": false, "error": {"code": "snake_code", "message": "Human sentence with next step."}}` with the matching HTTP status.
- htmx endpoints raise `DomainError`. The handler returns HTTP 200 with an HTML toast fragment `<div class="toast toast--error" role="alert">message</div>` and headers `HX-Retarget: #toasts` and `HX-Reswap: beforeend`, so the page never breaks. A non-htmx full-page request with an error renders `error.html` (owned by E) with a link back.
- Unhandled exceptions are logged with `exc_info` and shown to the user as the generic message "Something went wrong — details are in the server log (request_id=…)". Stack traces never reach the user.
- `DomainError` codes are listed in `api.md`.

**Logging** (structlog JSON; one object per line)
- Every line has `timestamp` (ISO UTC), `level`, `event`, `logger`, plus bound context when present: `request_id`, `run_id`, `job`, `enquiry_id`.
- `http_request`: `method`, `path`, `status`, `latency_ms`, `htmx` (bool), `input_summary` (query keys + form field names, no values for password fields), `output_summary` (content-type + bytes).
- `llm_call`: `feature`, `model`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `status` (`ok|error`), `error_type`, `fewshot_count`, `input_summary` (first 80 chars of the enquiry text), `output_summary` (verdict + priority + offering_id).
- `scrape_keyword`: `keyword`, `posts_seen`, `posts_new`, `posts_dupe`, `errors`, `duration_ms`, `status`.
- `scrape_run`: `status`, totals, `abort_reason`, `duration_ms`.
- `linkedin_session`: `action` (`connect|check`), `result`, `reason`.
- Never log secret values (`api_key_present: bool` only). Never log full raw post text above DEBUG.

**Templates/CSS contract** (so slices E/F/G/H stay disjoint)
- `base.html` (E) defines blocks `title`, `page_header`, `content`, and receives `shell` (from `web.context.shell_context`). Every page template `{% extends "base.html" %}`.
- `app.css` (E) defines exactly these component classes, which F/G/H use and never redefine: `.page`, `.page__header`, `.card`, `.card__header`, `.card__body`, `.card__footer`, `.stack`, `.row`, `.grid-2`, `.btn`, `.btn--primary`, `.btn--danger`, `.btn--ghost`, `.btn[disabled]`, `.badge`, `.badge--genuine`, `.badge--discard`, `.badge--high`, `.badge--medium`, `.badge--low`, `.badge--stub`, `.chip`, `.field`, `.field__label`, `.field__error`, `.input`, `.select`, `.table`, `.tabs`, `.tabs__tab`, `.tabs__tab--active`, `.empty-state`, `.spinner`, `.progress`, `.toast`, `.toast--error`, `.toast--ok`, `.banner`, `.banner--alert`, `.status-light`, `.status-light--connected`, `.status-light--not-connected`, `.status-light--needs-attention`, `.status-light--busy`, `.bulk-bar`, `.muted`, `.mono`, `.visually-hidden`. Tokens are CSS custom properties on `:root`: `--space-1..6`, `--font-size-sm/base/lg/xl` (base 16px), `--color-bg/surface/text/muted/primary/danger/ok/warn/border`, all colour pairs meeting WCAG AA. It includes a visible `:focus-visible` ring and `prefers-reduced-motion`.
- `app.js` (E): the only custom JS. It configures htmx (`htmx.config.defaultSwapStyle='outerHTML'`), adds the toast auto-dismiss (6s, not for errors), handles bulk-select (select-all checkbox and selected count in `.bulk-bar`), and handles `hx-confirm` dialogs. It must contain the string `htmx`.
- Element ids that tests rely on are listed in `ui.md` → Test hooks.

**Tests**
- Layout: `tests/unit` is pure (no network, no browser; `FakeLLMClient` from `tests/support/fake_llm.py`; fixed clock and seeded RNG). `tests/integration` uses real SQLite files, the real Haiku where the feature needs it, and real headless Chromium against the fixture server for scraper tests. `tests/e2e` runs the live server subprocess, headless Chromium and the real Haiku.
- Names are sentences: `test_upsert_post_merges_same_text_with_different_urn`.
- `tests/conftest.py` (scaffold) loads `.env` via `get_settings()` and fails the session with "SALES0_ANTHROPIC_API_KEY missing in .env" if the key is absent for `integration`/`e2e` (the gate is BLOCKED, not skipped). It provides a `db_url` fixture: `sqlite:///{tmp_path}/test.db` migrated with `alembic.command.upgrade(cfg, "head")`. It sets `SALES0_DATA_DIR={tmp_path}/data` and resets all singletons per test.
- Markers (declared in `pyproject.toml`): `llm` (real Haiku), `browser` (Playwright), `e2e`. Nothing is skipped by default.
- Fixture-mode scraper tests run the fixture router in a uvicorn thread on a free port (fixture `fixture_linkedin_server` in `tests/integration/scraper/conftest.py`, owned by C).
- Pacing in tests: `SALES0_SCRAPE_DELAY_MIN_S=0.05`, `MAX_S=0.15`, `KEYWORD_PAUSE_MIN_S=0.05`, `MAX_S=0.1`, `MAX_RUNS_PER_DAY=50`.
- Real-LLM assertions check structure and the stable facts in the fixture expectation table, never prose.
- pytest-timeout: 120s per test, 600s for `e2e`.
- **E2E harness** (`tests/support/live_server.py`, slice I):
  1. Create a temp dir and run `alembic upgrade head` against `sqlite:///<tmp>/e2e.db`.
  2. Spawn `[sys.executable, "-m", "sales0"]`. Under `uv run pytest`, this is the same `.venv` interpreter as the documented run command. The env is: `SALES0_PORT=8766`, `SALES0_LINKEDIN_MODE=fixture`, `SALES0_DATABASE_URL` and `SALES0_DATA_DIR` set to the temp locations, the test pacing values, and the real key from `.env`.
  3. Poll `/api/health` until 200 (30s max), capturing stdout lines for the log assertions.
  4. Provide `restart()`, which keeps the DB for the state-survival check.
  5. Terminate the server on teardown.

  The browser is pytest-playwright's headless Chromium `page`. The checkpoint, captcha and logout E2E uses its own server instance.

**Startup hooks** (`web/app.py` lifespan):
- The scaffold version runs `configure_logging`, ensures `data/` exists, and calls `assert_at_head()`.
- In Phase 1, slice **D** (the only Phase-1 slice allowed to edit `web/app.py`) adds exactly one line, `services.runs.recover_interrupted_runs(db)`. This marks queued/running runs `failed` with "Interrupted by server restart".
- The scaffold's `/` route lives in the `web/routes/stubs.py` placeholder and renders `home.html`. Slice E replaces it with the 303 redirect to `/inbox`.

## Configuration

Every variable below appears in `.env.example`, with comments, in this order. All are read by `sales0.config.Settings` (prefix `SALES0_`).

| Variable | Default | Purpose |
|---|---|---|
| `SALES0_ANTHROPIC_API_KEY` | — (required) | Anthropic key (SecretStr). The user creates `.env`. |
| `SALES0_LLM_MODEL` | `claude-haiku-5-5` | Model id. Haiku only by constraint. |
| `SALES0_LLM_MAX_OUTPUT_TOKENS` | `700` | Triage output cap |
| `SALES0_LLM_TIMEOUT_S` | `30` | Per-request timeout |
| `SALES0_LLM_MAX_RETRIES` | `3` | Retries on 429/5xx/timeouts (exponential backoff with jitter, base 1s) |
| `SALES0_LLM_PRICE_INPUT_USD_PER_MTOK` | `1.00` | Input price per 1M tokens |
| `SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK` | `5.00` | Output price per 1M tokens |
| `SALES0_HOST` | `127.0.0.1` | Bind host |
| `SALES0_PORT` | `8765` | Bind port (not a default port) |
| `SALES0_DATA_DIR` | `data` | Runtime dir (DB, profiles, logs), relative to the repo root |
| `SALES0_DATABASE_URL` | `sqlite:///data/sales0.db` | SQLAlchemy URL (Postgres-swappable) |
| `SALES0_TEST_DATABASE_URL` | (unset) | Optional. Tests default to a fresh `sqlite:///<pytest tmp>/test.db` per test, never the dev DB. |
| `SALES0_TIMEZONE` | `Asia/Kolkata` | "Today"/"this month" boundaries and display |
| `SALES0_LOG_LEVEL` | `INFO` | |
| `SALES0_LOG_FORMAT` | `json` | `json` or `console` |
| `SALES0_LOG_FILE` | (unset) | Optional JSONL file path |
| `SALES0_LINKEDIN_MODE` | `live` | `live` or `fixture` |
| `SALES0_LINKEDIN_HEADLESS` | `false` | Live runs headed by default (lower detection risk). Fixture mode is always headless. Connect is always headed in live mode. |
| `SALES0_LINKEDIN_CONNECT_TIMEOUT_S` | `300` | How long Connect waits for a login |
| `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` | `10` | Cap |
| `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` | `15` | Cap |
| `SALES0_SCRAPE_MAX_POSTS_PER_DAY` | `150` | Cap across runs (posts seen, local day) |
| `SALES0_SCRAPE_MAX_RUNS_PER_DAY` | `3` | Cap (runs started, local day; aborted runs count) |
| `SALES0_SCRAPE_DELAY_MIN_S` / `_MAX_S` | `3` / `9` | Random delay between page actions (scrolls, expands) |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S` / `_MAX_S` | `20` / `60` | Random pause between keywords |
| `SALES0_SCRAPE_MAX_SCROLLS` | `4` | Max scrolls per keyword results page |
| `SALES0_SCRAPE_DATE_FILTER` | `past-week` | `past-24h`, `past-week` or `past-month` |
| `SALES0_FEWSHOT_MAX_EXAMPLES` | `6` | Few-shot cap per triage call |
| `SALES0_FEWSHOT_TEXT_CHARS` | `600` | Truncation per example |
| `SALES0_TRIAGE_MAX_INPUT_CHARS` | `4000` | Truncation of the enquiry text sent to the LLM |

Phase 2 adds `SALES0_APP_PASSWORD` and `SALES0_SESSION_SECRET`. Phase 3 adds `SALES0_GOOGLE_CLIENT_SECRETS_FILE`, `SALES0_GMAIL_SENDER`, `SALES0_GMAIL_TEST_RECIPIENT` and `SALES0_LINKEDIN_MAX_PROFILES_PER_DAY` (30). Phase 5 adds `SALES0_WHATSAPP_INGEST_TOKEN`. Each is appended to `.env.example` in its phase.

> **Assumed:** Phase 5's WhatsApp bridge is a small Node sidecar (`whatsapp-bridge/`, using a maintained WhatsApp Web linked-device library). It POSTs messages to `/api/ingest/whatsapp` with a bearer token. The Python ecosystem has no mature linked-device client. Keeping the bridge out of process isolates its session and crashes. Until Phase 2, startup refuses any `SALES0_HOST` other than `127.0.0.1`/`localhost` with the message "Set SALES0_APP_PASSWORD (Phase 2) before binding to a public host".

> **Assumed:** Phase 1 is local-only with no login. Startup refuses a non-loopback bind. The shared login arrives in Phase 2 (before any VPS deploy).

## Cost computation

`llm/pricing.py`:

```
cost_usd_micros = round(input_tokens * price_in_per_mtok + output_tokens * price_out_per_mtok)
```

The prices are USD per 1M tokens, and 1 micro-USD = 1e-6 USD, so tokens × $/MTok yields micro-USD directly. `input_tokens` and `output_tokens` come from `response.usage`. Each Anthropic request (including a repair retry) is one `llm_calls` row. Per-enquiry cost is the sum over that enquiry's rows. Today and month totals sum `llm_calls.cost_usd_micros` where `created_at` falls in the local day or month (`SALES0_TIMEZONE`).

## LLM layer contract (scaffold)

```python
class StructuredRequest(BaseModel):
    feature: Literal["triage", "ping", "research", "drafting", "suggest"]
    system: str
    messages: list[dict]            # Anthropic message format: [{"role":"user","content":"..."}]
    tool_name: str                  # e.g. "record_triage"
    tool_description: str
    input_schema: dict              # JSON Schema of the tool input
    max_tokens: int
    prompt_version: str
    log_context: dict = {}          # enquiry_id, run_id, fewshot_count, input_summary

class LLMResponse(BaseModel):
    tool_input: dict | None         # parsed tool_use.input (None if model returned no tool_use)
    text: str | None                # any text block
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd_micros: int
    latency_ms: int
    request_id: str | None
    stop_reason: str | None

class LLMClient(Protocol):
    def structured(self, req: StructuredRequest) -> LLMResponse: ...
    def ping(self) -> LLMResponse: ...  # max_tokens=5, "Reply with OK"
```

`AnthropicClient.structured` calls `messages.create(model=settings.llm_model, system=..., messages=..., tools=[{name, description, input_schema}], tool_choice={"type":"tool","name":tool_name}, max_tokens=...)`. The SDK's internal retries are disabled (`max_retries=0`); the adapter retries itself on `RateLimitError`, `APIStatusError` ≥500, `APIConnectionError` and `APITimeoutError`, up to `SALES0_LLM_MAX_RETRIES` with backoff. It maps 401/403 to `LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env")` and 404 to `LLMModelNotFound("Model '<id>' not found — check SALES0_LLM_MODEL")`. It emits one `llm_call` log line per attempt-final outcome. It does NOT write to the DB; persistence of `llm_calls` is done by `services.triage` from the returned `LLMResponse`, which keeps the layer DB-free.

`python -m sales0.cli check-llm` calls `ping()` and prints `LLM OK model=<model> input_tokens=N output_tokens=M` with exit 0. On error it prints the mapped error message to stderr with exit 1.

## Contracts (Phase 1 inter-slice interfaces)

These signatures are binding. Slices code against them in parallel.

### Agent (slice B) — consumed by D

```python
# agent/schemas.py
class OfferingRef(BaseModel): id: int; name: str; group: str; description: str
class DecisionExample(BaseModel):
    enquiry_id: int; text: str; decision: Literal["accept","discard"]
    ai_verdict: Literal["genuine","discard"] | None; is_override: bool
    reason: str | None; decided_at: datetime
class TriageInput(BaseModel):
    enquiry_id: int; text: str; author_name: str | None; author_headline: str | None
    source_url: str | None; date_posted: date | None
class TriageResult(BaseModel):          # == the tool input schema (see agent.md)
    verdict: Literal["genuine","discard"]; reason: str
    poster_type: Literal["buyer_company","training_agency","individual_trainer_seeker","job_seeker_or_self_promo","other"]
    requirement_summary: str; priority: Literal["high","medium","low"]; offering_id: int | None
    company: str | None; contact_person: str | None; email: str | None; phone: str | None
    city: str | None; mode: Literal["online","offline","hybrid","unknown"]
    dates_mentioned: str | None; topic: str | None; language: str
class TriageOutcome(BaseModel):
    status: Literal["done","failed"]; result: TriageResult | None; error: str | None
    llm_responses: list[LLMResponse]; fewshot_count: int; prompt_version: str
@dataclass(frozen=True)
class TriageDeps: llm: LLMClient; offerings: list[OfferingRef]; examples: list[DecisionExample]; settings: Settings

# agent/graph.py
def run_triage(inp: TriageInput, deps: TriageDeps) -> TriageOutcome  # raises LLMAuthError / LLMModelNotFound (fatal)
# agent/fewshot.py
def load_candidates(db: Session, exclude_enquiry_id: int, limit: int = 200) -> list[DecisionExample]
def select_examples(candidates: list[DecisionExample], max_n: int, text_chars: int) -> list[DecisionExample]
```

### Scraper (slice C) — consumed by D

```python
# scraper/types.py
@dataclass(frozen=True)
class ScrapedPost: urn: str; url: str; author_name: str | None; author_profile_url: str | None
                   author_headline: str | None; posted_relative: str | None; posted_date: date | None; text: str
@dataclass(frozen=True)
class KeywordScrapeResult: keyword: str; posts: list[ScrapedPost]; scrolls: int; parse_errors: int; drift_warning: bool
@dataclass(frozen=True)
class SessionCheckResult: logged_in: bool; reason: Literal["ok","no_cookie","redirected_login","checkpoint","captcha","rate_limited","error"]; url: str
@dataclass(frozen=True)
class ConnectResult: connected: bool; reason: Literal["ok","timeout","window_closed","cancelled","error"]; detail: str | None

# scraper/errors.py
class AccountSafetyStop(Exception): kind: Literal["logout","checkpoint","captcha","rate_limited"]; url: str
class ScrapeError(Exception): ...

# scraper/session_check.py
def check_session(settings: Settings, *, headless: bool = True) -> SessionCheckResult   # opens+closes own context
# scraper/connect.py
def connect_interactive(settings: Settings, on_tick: Callable[[int], None], should_cancel: Callable[[], bool]) -> ConnectResult
# scraper/search.py
class LinkedInSearcher:
    def __init__(self, settings: Settings, pacer: Pacer | None = None): ...
    def __enter__(self) -> "LinkedInSearcher": ...   # opens persistent context; verifies session → AccountSafetyStop("logout") if not
    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult: ...   # raises AccountSafetyStop, ScrapeError
    def __exit__(self, *exc) -> None: ...            # always closes the context
```

### Services + jobs (slice D) — consumed by E/F/G/H

```python
# services/views.py  (all Pydantic, frozen)
KeywordView(id, text, active, last_run_at, created_at)
OfferingView(id, name, group_id, group_name, description, active, sort_order, enquiry_count)
OfferingInput(name: str 1..80, group_name: str 1..60, description: str 0..500)
SessionStatusView(status: Literal["not_connected","connected","needs_attention","busy"], busy_action: Literal["connect","check","run"] | None,
                  last_checked_at, last_successful_run_at, last_error: str | None, mode: Literal["live","fixture"], connect_elapsed_s: int | None)
Eligibility(can_run: bool, reason: str | None, runs_today: int, runs_cap: int, posts_today: int, posts_cap: int)
RunKeywordView(keyword_text, status, posts_seen, posts_new, posts_dupe, errors, duration_ms, error_message)
RunView(id, status, trigger, mode, started_at, finished_at, duration_ms, keywords_planned, keywords_done, current_keyword, current_step,
        posts_seen, posts_new, posts_dupe, enquiries_triaged, triage_failed, errors_count, abort_reason, error_message, cost_usd_micros)
RunDetailView(run: RunView, keywords: list[RunKeywordView])
EnquiryCardView(id, review_status, stage, triage_status, triage_error,
                requirement_summary, ai_verdict, ai_reason, ai_poster_type, priority, offering_id, offering_name,
                company, contact_person, email, phone, city, mode, dates_mentioned, topic,
                author_name, author_headline, source_url, raw_text, date_posted, date_added, keywords: list[str],
                discard_reason, is_override, decided_at, cost_usd_micros, tokens_total)
InboxPage(tab, q, items: list[EnquiryCardView], total, page, page_size, counts: dict[str,int])   # counts keys: review, accepted, discarded
CostSummary(today_usd_micros, month_usd_micros, today_calls, month_calls)

# services/keywords.py
list_keywords(db) -> list[KeywordView]
add_keyword(db, text: str) -> KeywordView                  # DomainError keyword_invalid | keyword_duplicate
remove_keyword(db, keyword_id: int) -> None                # DomainError not_found
set_keyword_active(db, keyword_id: int, active: bool) -> KeywordView
# services/offerings.py
list_offerings(db, include_inactive: bool = False) -> list[OfferingView]
list_group_names(db) -> list[str]
create_offering(db, data: OfferingInput) -> OfferingView   # DomainError offering_invalid | offering_duplicate
update_offering(db, offering_id: int, data: OfferingInput) -> OfferingView
delete_offering(db, offering_id: int) -> Literal["deleted","deactivated"]
# services/session.py
session_status(db) -> SessionStatusView
start_connect(db) -> SessionStatusView                     # DomainError job_busy
start_check(db) -> SessionStatusView                       # DomainError job_busy
# services/runs.py
run_eligibility(db, now: datetime | None = None) -> Eligibility
start_run(db, actor_user_id: int | None = None) -> RunView # DomainError run_in_progress|not_connected|needs_attention|no_keywords|daily_run_cap|daily_post_cap|job_busy
get_run(db, run_id: int) -> RunDetailView                  # DomainError not_found
list_runs(db, limit: int = 20) -> list[RunView]
active_run(db) -> RunView | None
recover_interrupted_runs(db) -> int                        # called once at startup; returns count marked failed
# services/inbox.py
list_enquiries(db, tab: Literal["review","accepted","discarded"], q: str | None, page: int = 1, page_size: int = 25) -> InboxPage
get_card(db, enquiry_id: int) -> EnquiryCardView
decide(db, enquiry_ids: list[int], decision: Literal["accept","discard"], discard_reason: str | None = None,
       actor_user_id: int | None = None) -> list[EnquiryCardView]       # DomainError not_found | decision_invalid
update_fields(db, enquiry_id: int, priority: str | None = None, offering_id: int | None = None) -> EnquiryCardView
retry_triage(db, enquiry_id: int) -> EnquiryCardView       # synchronous single real call; DomainError triage_failed
# services/costs.py
cost_summary(db, now: datetime | None = None) -> CostSummary
# services/dedupe.py
normalize_text(text: str) -> str
content_hash(text: str) -> str
upsert_post(db, post: ScrapedPost, run_id: int, keyword_id: int, keyword_text: str) -> tuple[Literal["new","dupe_urn","dupe_content"], int]  # (kind, enquiry_id)
# services/triage.py
triage_enquiry(db, enquiry_id: int, llm: LLMClient | None = None) -> TriageOutcome
# jobs/runner.py
class JobRunner: submit(name: Literal["connect","check","run"], fn: Callable[[], None]) -> None   # DomainError job_busy
                 busy() -> str | None; cancel_connect() -> None
get_runner() -> JobRunner; reset_runner() -> None
# jobs/discovery_job.py
execute(run_id: int) -> None
```

`web/context.py` (E): `shell_context(request, db) -> dict` with keys `session: SessionStatusView`, `cost: CostSummary`, `active_run: RunView | None`, `nav: list[NavItem(label, href, phase: int | None, active: bool)]`, `banner: str | None`, `mode: str`.

## Fixture LinkedIn corpus

**Purpose.** Fixture mode lets the SAME `LinkedInSearcher` and Playwright code run offline, for tests and demos. `scraper/urls.base_url(settings)` returns `https://www.linkedin.com` in live mode and `http://{host}:{port}/__fixtures/linkedin` in fixture mode. Every path below is relative to that base and is identical in shape to LinkedIn's.

**Fixture routes** (slice C, `linkedin_fixture/routes.py`; mounted only when mode=fixture):
- `GET /login`: sets cookie `li_at=fixture-session` (path `/`, httponly) and redirects 302 to `/feed/`.
- `GET /feed/`: with `li_at`, renders a feed page containing `nav.global-nav`. Without it, redirects 302 to `/authwall`.
- `GET /authwall`: a login wall page (no `global-nav`).
- `GET /search/results/content/?keywords=K&...`: without `li_at` → 302 to `/authwall`. When K (lower-cased, trimmed) is `checkpoint-test` → 302 to `/checkpoint/challenge/`. When K is `captcha-test` → renders a page containing `iframe[src*="captcha"]`. When K is `ratelimit-test` → HTTP 429. Otherwise it renders every post whose `keywords` contains K, in corpus order. Zero matches renders the no-results marker `.search-reusable-search-no-results`.
- `GET /checkpoint/challenge/`: a security-check page.

**DOM contract** (`scraper/selectors.py` is the single source; the fixture templates use exactly these):
- result card `div[data-urn^="urn:li:activity:"]` (URN from the `data-urn` attribute)
- author name `.update-components-actor__title span[aria-hidden="true"]`
- author profile link `a.update-components-actor__meta-link` (href)
- author headline `.update-components-actor__description span[aria-hidden="true"]`
- relative time `.update-components-actor__sub-description span[aria-hidden="true"]` (e.g. `2d •`)
- text `.update-components-text` (expand via `button.feed-shared-inline-show-more-text__see-more-less-toggle` if present)
- logged-in marker `nav.global-nav`
- no-results marker `.search-reusable-search-no-results`
- The post permalink is built as `{base}/feed/update/{urn}/`.

> **Assumed:** the selectors model LinkedIn's current DOM as best known. Live LinkedIn changes markup often, so drift is handled operationally. If a live results page is logged in, shows neither result cards nor the no-results marker, then `drift_warning=True`. The keyword row is then marked `warning` with "No posts parsed — LinkedIn layout may have changed", and the run detail shows it. Fixing selectors is a one-file change (`selectors.py` plus the fixture templates).

**Corpus** (`linkedin_fixture/posts.json`). These are the exact URNs and keywords. The posts must contain the listed facts in their text, so the real-Haiku expectations are stable.

| ID | URN suffix (`urn:li:activity:73000000000000000NN`) | keywords | author / headline | Text must contain | Expected (real Haiku) |
|---|---|---|---|---|---|
| P1 | 01 | `corporate ai training`, `ai workshop` | Priya Menon / "Head of L&D at Acme Logistics" | Acme Logistics wants a **training partner** for a 2-day Generative AI workshop for 40 managers at its **Pune** office (**offline**), week of 17 Nov; proposals to `training@acmelogistics.example`, call `+91 98200 11111`; posted `1d` | verdict=genuine, poster_type=buyer_company, email=`training@acmelogistics.example`, company contains "Acme", mode=offline, city contains "Pune", offering ∈ {AI corporate workshops, Technical AI training}, priority=high |
| P2 | 02 | `corporate ai training` | Rahul Verma / "Talent Acquisition at SkillBridge Learning — corporate training company" | SkillBridge Learning is **hiring freelance Generative AI trainers** for its corporate clients; 5+ yrs; send trainer profile + per-day commercials to `trainers@skillbridge.example` | verdict=discard, poster_type ∈ {training_agency, individual_trainer_seeker} |
| P3 | 03 | `corporate ai training` | Neha Kapoor / "Freelance IT Recruiter" | Urgent: need an **individual freelance trainer** for a 3-day **online** batch on Prompt Engineering & ChatGPT, **hourly rate**, trainers DM profile | verdict=discard, poster_type ∈ {individual_trainer_seeker, training_agency} |
| P5 | 05 | `ai workshop` | Arjun Rao / "CTO at Zentrix Retail" | Hindi (Devanagari) + English + emoji: Zentrix Retail wants a hands-on "AI agents" workshop for its 30-person tech team at its **Bengaluru** office, needs a training partner, contact `arjun.rao@zentrix.example` | verdict=genuine, email=`arjun.rao@zentrix.example`, city contains "Bengaluru" or "Bangalore", requirement_summary non-empty |
| P6 | 06 | `ai workshop` | Meera Iyer / "VP Engineering at Orbit Health" | Wants a partner to run a "**Build your first AI agent**" workshop for 25 engineers, fully **online**, December, "reach out if you do this". **No email, no phone.** | verdict=genuine, email=null, phone=null, mode=online, offering=Technical AI training |
| P7 | 07 | `ai workshop` | Karan Shah / "Recruiter at SkillBridge Learning" | The P2 text with different line breaks, different casing in 3 words, plus trailing ` #hiring #trainers` | content duplicate of P2 (merged; NOT triaged again) |
| P8 | 08 | `ai consulting` | Sanjay Gupta / "COO at Meridian Insurance" | Meridian Insurance (**Mumbai**) needs a partner to **design and build** an internal AI assistant over its SOP documents and claims manuals; consulting + build; `it-projects@meridian.example` | verdict=genuine, offering=AI consulting & build services, email=`it-projects@meridian.example` |
| P9 | 09 | `ai consulting` | Vikram Shah / "Certified AI Trainer \| Open to work" | "I am a certified Generative AI trainer… **open to freelance** corporate training assignments. Connect with me!" | verdict=discard, poster_type ∈ {job_seeker_or_self_promo, other} |
| P10 | 10 | `claude code training` | Anita Desai / "Engineering Manager at QuantaWorks" | QuantaWorks wants **Claude Code training** for 15 developers, online, 2 half-days; `lead@quantaworks.example` | verdict=genuine, offering=Technical AI training |
| P11 | 11 | `claude code training` | "Briltus Academy" / "Hiring Trainers" | Briltus is **onboarding freelance trainers** for Claude Code and AI-agents batches; send CV and hourly rate to `hr@briltus.example` | verdict=discard |

P1 appears under two keywords with the same URN (URN duplicate). P7 is the same text as P2 under a new URN (content duplicate).

**Deterministic run expectations** (keywords added in this order: `corporate ai training`, `ai workshop`, `ai consulting`):
- Run 1: `posts_seen=9` (3+4+2), `posts_new=7` (P1,P2,P3,P5,P6,P8,P9), `posts_dupe=2` (P1 again, P7), `enquiries_triaged=7`. Enquiry P1 has keyword chips `corporate ai training` and `ai workshop`. Enquiry P2 has `corporate ai training` and `ai workshop` (via P7), and two `enquiry_sources` rows.
- Then add `claude code training` and run again. It runs first, because it has never run. Run 2: `posts_seen=11`, `posts_new=2` (P10, P11), `posts_dupe=9`, `enquiries_triaged=2`.

## Security & Safety

- Secrets live only in `.env`, read through `SecretStr`. Logs record `api_key_present` only. `.env` and `data/` are gitignored before they are first created.
- LinkedIn safety (see `capabilities/linkedin-discovery.md`): caps, human-like pacing, a stop on any safety page, no retries after a stop, and no automatic reconnect.
- Fixture routes are never mounted in live mode (a test asserts `/__fixtures/linkedin/feed/` returns 404 in live mode).
- All LLM output is untrusted. It is schema-validated and guarded (`agent.md`) and rendered with Jinja autoescape. Raw post text is escaped and never rendered as HTML.

## Portability to a VPS (later)

The same command runs on any Linux host after `uv sync && uv run playwright install --with-deps chromium`. The live headed **Connect** needs a display. On a VPS, either run it under `xvfb-run` and reach it through a VNC/noVNC tunnel, or log in on the laptop and copy `data/linkedin-profile/`. This is documented in the README in Phase 2, together with the shared login. A Postgres switch is a URL change (see Stack).
