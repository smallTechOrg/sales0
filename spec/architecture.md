# sales0 — Architecture

The product narrative is in `roadmap.md` and `capabilities/`. This file is the HOW: system overview, stack, layout, conventions, configuration, inter-slice contracts, the LinkedIn browser design, and the fixture LinkedIn corpus. The AI composition is in `agent.md`. The schema is in `data.md`. Routes are in `api.md`.

> **Phase 1.1 change (binding; user gate evidence).** Phase 1's Connect opened Playwright's bundled Chromium on a fresh profile. To the user it looked like a separate incognito window. The user logged in (including 2FA), but sales0 stayed on "Waiting…", because the logged-in marker (`#global-nav` / `header.global-nav` / `nav.global-nav__nav`) does not exist on current LinkedIn, which ships obfuscated class names. Phase 1.1 makes these changes. Connect opens the user's **installed Google Chrome** (`channel="chrome"`) on a dedicated sales0 profile; the user logs in there once. Logged-in detection is based on the **`li_at` session cookie plus the final URL and HTTP status**, never on DOM classes. Post parsing uses stable attributes and URL patterns, with ordered fallbacks. Layout drift is always visible to the user. Everything else in Phase 1 is unchanged. The details are in `## LinkedIn browser (Phase 1.1)`. Out of scope: browser extensions, reading the user's own Chrome cookie store, pairing tokens.

> **Phase 1.2 change (binding; user gate evidence from one real headless capture on the sales0 profile).** LinkedIn content search now ships **SDUI** markup (`data-sdui-screen="com.linkedin.sdui.flagshipnav.search.SearchResultsContent"`). The page has NO `urn:li:activity` anywhere, no `data-urn`/`data-id`, no `/feed/update/` or `/posts/` links, and obfuscated classes, so the Phase 1.1 parser found 0 cards and raised the drift warning. Phase 1.2 parses SDUI posts from `data-view-name` / `data-testid` / `role` / `componentkey` / href patterns (never classes), keeps the Phase 1.1 URN shapes as the fallback of ONE ordered strategy, identifies posts without a URN (`ck:<KEY>` + content dedupe + a same-author fingerprint), and recovers the permalink cheaply through the post's own "Copy link to post" menu item with the page clipboard intercepted. The schema does not change (head stays `0002_phase1_discovery`). Details: `## LinkedIn SDUI search results (Phase 1.2)`.

> **Phase 1.4 change (binding; user gate evidence).** One keyword ("agentic ai training", a topic, not buyer intent) with caps of 15 posts / 4 scrolls / 1 keyword saw 9 posts, 5 new, and Haiku correctly discarded all 5. Searches were too shallow and too topical, and the inbox filled with discards. Phase 1.4 makes discovery thorough and idempotent: a seeded buyer-intent keyword library; deep scrolling (80 posts per keyword, sort=latest, past-week then past-month); every scanned post remembered in `seen_posts` (never re-triaged, never duplicated); `enquiries` rows created only for AI-genuine posts; authors remembered in `linkedin_profiles`; permalinks captured only for genuine posts, after triage, for every card tier (including `listitem`); triage with bounded concurrency; long resumable background runs with an honest scanned-vs-relevant summary. Migration `0003_phase1_4_seen_posts`. Details: `## Thorough discovery (Phase 1.4)`. Where it contradicts the Discovery run data flow, caps and defaults below, Phase 1.4 wins.

## System Overview

sales0 is a single Python process: one uvicorn worker serving a FastAPI app. It contains:

1. **Web layer.** Server-rendered Jinja2 pages with htmx partial updates, on one origin at `http://127.0.0.1:8765/`.
2. **Services.** Synchronous domain functions over SQLAlchemy sessions. Routes and jobs call only these.
3. **Job runner.** A single-flight background thread that runs browser jobs: `connect`, `check`, `discovery_run`. Only one runs at a time. The persistent browser profile must never be opened by two browser contexts at once. This rule is binding in Phase 1.1: Connect (headed), Re-check and runs (headless) all use the same profile directory.
4. **Scraper.** Playwright (sync API) `launch_persistent_context` on a dedicated sales0 profile under `data/`. It uses the installed **Google Chrome** (`channel="chrome"`), or the bundled Chromium only when Chrome is not installed (`## LinkedIn browser (Phase 1.1)`). Connect is the only headed launch. Every check and run is headless. The scraper returns plain dataclasses and never touches the DB.
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

> **Phase 1.4 supersedes steps 2 (keyword selection and caps), 3, 4 and 5** with the keyword loop in `## Thorough discovery (Phase 1.4)` → Job flow (scan → register → triage → permalink, per keyword). Steps 1, 2 (login check and its three outcomes), 6, 6a–6c and 7 stand; "interrupted by a server restart" now means `aborted` (not `failed`).

1. `POST /runs` calls `services.runs.start_run`. This checks eligibility, inserts a `scrape_runs` row with status `queued`, and submits `discovery_job.execute(run_id)` to the runner.
2. The job sets status `running`. It selects keywords: active ones, ordered by `last_run_at` (never-run first) then `id`, capped at `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN`. It opens the persistent context headless and runs the login check (`## LinkedIn browser` → Logged-in detection) on `{base}/feed/`:
   - **No `li_at` cookie in the profile at all.** No LinkedIn request has been made yet. This raises `SessionMissing`. The run ends `failed` with "Not logged in to LinkedIn in sales0's browser — press Connect LinkedIn in Setup". Every keyword becomes `skipped` "Not searched — not logged in". The session becomes `not_connected` with the same message. `abort_reason` stays null, so there is no cool-down and no banner. This is the first-run-after-upgrade case: the Phase-1 Chromium profile is not reused by Chrome.
   - **Cookie present, but `/feed/` lands on a login, authwall, checkpoint or captcha page, or returns HTTP 429/999.** This raises `AccountSafetyStop(<kind>)` (step 6).
   - **Any other HTTP status of 400 or above, or a navigation error.** This raises `ScrapeError`, and the run ends `failed` "Could not open LinkedIn: …" (the existing open-failure path).
3. For each keyword: `LinkedInSearcher.search(keyword, max_posts)` returns a `KeywordScrapeResult`. For each post, `services.dedupe.upsert_post` returns `new | dupe_urn | dupe_content`. It inserts or merges the enquiry, its source and the keyword hit. The job then commits and updates the run and run-keyword counters. **Phase 1.1 keyword status and message** are set by `services.runs.keyword_outcome(result)` (pure; Contracts), in this order:
   - `drift_warning` → `warning` "No posts parsed — LinkedIn layout may have changed (N result cards found)".
   - Otherwise, any field in `field_misses` missing on at least half of the parsed posts (and at least 2 posts parsed) → `warning` "Parsed N posts, but couldn't read <author / headline / post time / profile link> on M — LinkedIn layout may have changed".
   - Otherwise `done`. Any smaller misses or parse errors are still written to the Message column, e.g. "2 cards couldn't be read · headline missing on 1/8". Nothing is silent.
   - `parse_errors` are added to `errors` as before.
4. For each NEW enquiry from that keyword: `services.triage.triage_enquiry(db, enquiry_id, llm)` loads the offerings and few-shot examples, calls `agent.graph.run_triage`, and persists the result, the `llm_calls` row and a `status_history` row.
5. A paced pause runs between keywords. The job stops early when the daily post cap is reached.
6. `AccountSafetyStop` (logout / checkpoint / captcha / rate_limited) closes the browser immediately. The run becomes `aborted` and the session becomes `needs_attention`. Enquiries already saved but still untriaged ARE triaged, because that makes no LinkedIn request. **Phase 1.1:** the keyword whose search was running when the stop tripped gets `keywords.last_run_at = now` (Core `UPDATE`; a deleted keyword matches 0 rows). The next run therefore plans it last and does not open with the search that tripped LinkedIn. A stop raised by the run-start login check (step 2) stamps no keyword. The stop starts the safety cool-down (step 6c).
6c. **Safety cool-down (Phase 1.1).** `services.runs.safety_cooldown_until(db, now=None) -> datetime | None` reads the latest `scrape_runs` row with `status='aborted'` and `abort_reason` in (`rate_limited`, `captcha`, `checkpoint`, `logout`), ordered by `finished_at` desc. For `rate_limited`/`captcha` it returns `finished_at + SALES0_SAFETY_COOLDOWN_MIN` minutes. For `checkpoint`/`logout` it returns `finished_at + SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN` minutes. It returns None when that instant is not in the future, or when the setting that applies is 0. While the instant is in the future, `run_eligibility` fails with `safety_cooldown` (order in `api.md`), and the message shows the instant as `HH:MM` in `SALES0_TIMEZONE`. The cool-down is derived from existing columns; nothing new is stored and there is no migration. A green Re-check or a successful Connect clears `needs_attention` but never the cool-down.
6a. A fatal LLM error (`LLMAuthError` / `LLMModelNotFound`) mid-run stops the run. It ends `failed` with the adapter's message in `error_message`. Remaining keywords become `skipped`. Enquiries already scraped stay `triage_status=pending`, so the user can use Retry triage later. The `llm_calls` error row is kept. `abort_reason` stays null, because `aborted` is reserved for account-safety stops. Exception: if the fatal LLM error happens during the post-stop triage of step 6 (after an `AccountSafetyStop`), the run stays `aborted` with its `abort_reason`, and the LLM message is still recorded in `error_message`.
6b. The keyword list is snapshotted at step 2. A keyword deleted (or deactivated) mid-run does not change the run: it is still searched if planned, and its hits are saved with `keyword_text` set and `keyword_id` null (`SET NULL`, `data.md`). If the keyword is deleted while that keyword's posts are being saved, the insert of a keyword hit raises an `IntegrityError` (FK). The job rolls back that post and retries its `upsert_post` once with `keyword_id` null. Deletion never fails the run or a keyword row.
7. The run is finalised: status `succeeded` (or `aborted`/`failed`) and `finished_at` are set, and on success `linkedin_session.last_successful_run_at` is updated. **Phase 1.1:** `RunView.layout_warnings` is the number of the run's keyword rows with status `warning`. It is computed on read, not stored. When it is above 0, the Runs panel and the run detail show the run-level drift alert (`ui.md`), and the `scrape_run` log line carries `layout_warnings`.

At startup, any run left in `queued` or `running` is marked `failed` with the error "Interrupted by server restart". **Phase 1.4:** it is marked `aborted` (`abort_reason` null, no cool-down) with "Interrupted by server restart — the next run continues with the keywords it had not reached", and its unfinished keyword rows become `skipped` (`data.md` → Phase 1.4).

## Stack

User-binding constraints: Python 3.12 via asdf, `uv` with a project-local `.venv`, Anthropic Claude Haiku ONLY (`claude-haiku-5-5`, env-configurable), port other than the default, runs on a Mac now and must be portable to a VPS, secrets in `.env`.

| Concern | Choice | Rationale |
|---|---|---|
| Language/runtime | Python 3.12, pinned by `.python-version` (`3.12`), uv-managed `.venv` | Binding environment. Playwright, the Anthropic SDK and SQLAlchemy are all first-class in Python. |
| Web framework | FastAPI + uvicorn (`uvicorn[standard]`), **workers=1** | Light and typed, with an easy JSON envelope and Jinja2 support. One worker because the in-process job runner and browser profile lock must be singletons. |
| Frontend | Server-rendered Jinja2 + **htmx 2.x vendored** at `web/static/vendor/htmx.min.js` + one small `app.js`, plain CSS | Zero-build and single-origin. The UI is lists, cards, forms and polling, with no complex client state. |
| Database | **SQLite (production engine)** with SQLAlchemy 2.0 ORM + Alembic | 1–3 users and low thousands of rows on one laptop. Zero ops, single-file backup. |
| Browser automation | Playwright for Python (sync API) `chromium.launch_persistent_context`, **`channel="chrome"`** (the installed Google Chrome) on a dedicated sales0 profile. It falls back to bundled Chromium (`channel="chromium"`, new headless) only when Chrome is absent (Phase 1.1). | User gate (binding): the user logs in once in a real Google Chrome window, not in a window that looks like incognito. The installed Chrome engine and version match a normal user's fingerprint. The same library drives the E2E tests. |
| LLM | Anthropic Python SDK, model `claude-haiku-5-5` from `SALES0_LLM_MODEL` | Binding (Haiku only, cheapest). Structured output via forced tool use. |
| Config | pydantic-settings (`env_prefix="SALES0_"`, `env_file=".env"`, `extra="ignore"`), `SecretStr` for keys | Typed and validated at startup. Secrets cannot be printed. |
| Logging | structlog → JSON lines on stdout (and `SALES0_LOG_FILE` if set) | Structured request, LLM and scrape logs from day one. |
| Tests | pytest, pytest-playwright (headless Chromium), httpx (FastAPI TestClient), pytest-timeout | One runner for unit, integration and live E2E tests. |
| Timezone | `zoneinfo` + `tzdata`, display/"today" boundaries in `SALES0_TIMEZONE` (default `Asia/Kolkata`) | Daily caps and daily/monthly cost totals need a defined "day". |

> **Assumed:** stack = Python 3.12 + FastAPI + Jinja2/htmx + SQLAlchemy 2.0/Alembic + Playwright + Anthropic SDK + structlog. There was no user preference. This set is the simplest single-process, zero-build, portable fit for a scraper, a cheap LLM triage step and a CRUD UI.

> **Assumed:** **SQLite is the production engine** (`sqlite:///data/sales0.db`), so tests run on real SQLite files (a fresh file per test or per server, migrated by Alembic), never in-memory. Portability to Postgres on a VPS means changing `SALES0_DATABASE_URL` to `postgresql+psycopg://…` and adding the driver. Code and migrations must therefore stay dialect-neutral: no raw SQLite SQL, generic `JSON` type, `render_as_batch=True` for SQLite ALTERs, and explicit constraint names via a naming convention. If the VPS move switches the engine, the gate switches to Postgres in that same change.

> **Assumed:** no LangGraph. The agent is a small explicit node graph in plain Python (`agent/graph.py`), because triage is one structured call with a repair edge. A framework would add weight without any capability the design uses. Phase 3's research loop is also hand-rolled (≤4 steps). Native tracing therefore does not apply. Observability is the structured logs plus the `llm_calls` table.

**Confirmed pricing and model choice.** `claude-haiku-5-5` costs $0.10 per MTok input and $0.50 per MTok output for prompts up to 100K tokens. The pricing defaults are therefore `SALES0_LLM_PRICE_INPUT_USD_PER_MTOK=0.10` and `SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK=0.50`, still overridable via env. Above 100K prompt tokens the price is $0.50 input / $2.50 output. That tier is not modelled, because triage prompts (~1.5–3k tokens) are far below it. `llm/pricing.py` uses the flat ≤100K rate. The model is `claude-haiku-5-5` because it is the cheapest model available on this key: older Haiku models are not available, and `claude-haiku-4-5` costs about 10x more.

> **Assumed:** no prompt caching in Phase 1. The system prompt plus few-shot is ~1.5k tokens, below the size where caching pays off reliably. Cost math therefore covers plain input and output tokens only.

Dependency rule: everything needed at runtime or deploy time goes in `[project].dependencies`: fastapi, uvicorn[standard], jinja2, python-multipart, sqlalchemy>=2.0, alembic, pydantic>=2, pydantic-settings, structlog, anthropic, playwright, tzdata, httpx. Only pytest, pytest-playwright and pytest-timeout go in `[dependency-groups].dev`. Versions are the latest at scaffold time, locked by the committed `uv.lock`. `uv sync` installs both groups.

**Run command (documented, canonical):** `uv run python -m sales0`. This runs `.venv/bin/python -m sales0` on the pinned 3.12 and serves `http://127.0.0.1:8765/`. Override with `SALES0_PORT` or `SALES0_HOST`. **From Phase 1.1, no automated gate or test uses port 8765, and none connects to it**, because the user's own server may be running there (Conventions → Tests → Port isolation). E2E uses 8766 and the boot gate uses 8767. No gate or test reads or migrates `data/sales0.db`, and none opens a profile under the repo's `data/`.

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
│   ├── linkedin-chrome-profile/    # Phase 1.1: persistent Google Chrome profile (live, channel chrome)
│   ├── linkedin-profile/           # persistent bundled-Chromium profile (live, channel chromium; Phase 1's dir, reused only for the fallback)
│   ├── linkedin-profile-fixture-chrome/    # Phase 1.1: fixture mode, channel chrome
│   ├── linkedin-profile-fixture-chromium/  # Phase 1.1: fixture mode, channel chromium (Phase 1's linkedin-profile-fixture/ is unused)
│   └── logs/                       # optional SALES0_LOG_FILE target
├── migrations/
│   ├── env.py                      # imports sales0.db.models metadata; render_as_batch=True
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_baseline.py        # scaffold: app_meta
│       ├── 0002_phase1_discovery.py
│       └── 0003_phase1_4_seen_posts.py   # Phase 1.4. Actual later chain (linear, ids not numbers): 0003_sheet_sync_state > p3_outreach_a7c41e9d > 0004_opportunity_type > p4_ops_c3d8e1f2 > p5_replies_c3d81f20 > p6_crm_5b2e7d14 (Phase 2, head)
│   # Phase 2 adds (full tree: "## Phase 2 layout delta" at the end of this file): db/models/{enums_crm,crm,contacts,timeline,registries,sheetdata}.py,
│   #   services/crm/**, services/importer/**, services/sheetsync/{sql_repository,mapping,status_map,metrics}.py, web/auth.py, web/routes/{auth,pipeline,contacts,registries,importer,sync}.py
├── src/sales0/
│   ├── __init__.py
│   ├── __main__.py                 # load settings → configure logging → verify DB at head → uvicorn.run(app, host, port, workers=1)
│   ├── config.py                   # Settings + get_settings() (lru_cache) + reset_settings()
│   ├── logging.py                  # structlog JSON config; bind/unbind contextvars
│   ├── errors.py                   # DomainError(code, message, http_status=400)
│   ├── clock.py                    # utcnow(), local_today(tz) — the injectable time seam
│   ├── cli.py                      # `python -m sales0.cli check-llm` | `check-browser` (Phase 1.1)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── base.py                 # DeclarativeBase + MetaData(naming_convention)
│   │   ├── engine.py               # get_engine(), session_scope(), reset_engine(); SQLite pragmas
│   │   ├── migrate_check.py        # current_revision(), head_revision(), assert_at_head()
│   │   └── models/                 # slice A: one module per aggregate (see data.md)
│   │       ├── __init__.py         # exports every model (Alembic autogenerate target)
│   │       ├── meta.py  users.py  offerings.py  keywords.py  linkedin.py
│   │       ├── runs.py  enquiries.py  decisions.py  llm_calls.py  history.py
│   │       ├── seen_posts.py  profiles.py     # Phase 1.4: SeenPost, LinkedinProfile (table linkedin_profiles)
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
│   │   ├── fewshot.py              # select_examples (pure) + load_candidates(db)
│   │   └── batch.py                # Phase 1.4: triage_concurrently (bounded thread pool around run_triage)
│   ├── scraper/                    # slice C (Phase 1.1: R1 browser/login, R2 detection/parsing): no DB access
│   │   ├── __init__.py
│   │   ├── types.py                # ScrapedPost, KeywordScrapeResult, SessionCheckResult, ConnectResult  (R2)
│   │   ├── errors.py               # AccountSafetyStop(kind, url), ScrapeError, SessionMissing  (R2)
│   │   ├── browser.py              # resolve_browser, launch_options, profile_dir, open_context, user_agent_for  (R1)
│   │   ├── urls.py                 # base_url(settings), search_url, feed_url, login_url, permalink
│   │   ├── selectors.py            # ALL DOM selectors + ordered fallbacks, in one place  (R2)
│   │   ├── detection.py            # PageSignals, PageVerdict, classify_url, classify_page (PURE); SIGNALS_JS; read_signals; session cookie helpers  (R2)
│   │   ├── pacing.py               # Pacer(min_s, max_s, rng, sleep)
│   │   ├── relative_time.py        # "5h"/"2d"/"1w"/"3mo"/"2 days ago" → date estimate  (R2)
│   │   ├── parsing.py              # EXTRACT_JS (card discovery + per-field fallbacks), post_from_raw, ParseError  (R2; Phase 1.2: SDUI strategy first, URN fallback)
│   │   ├── permalink.py            # Phase 1.2: CLIPBOARD_HOOK_JS, TAKE_COPIED_JS, capture_permalinks(page, raws, *, cap, pacer, checkpoint) -> PermalinkStats
│   │   ├── session_check.py        # verify_in_context(context, base, timeout_ms), check_session(settings) → SessionCheckResult  (R1)
│   │   ├── connect.py              # connect_interactive(...) → ConnectResult; cookie poll + /feed/ verify  (R1)
│   │   └── search.py               # LinkedInSearcher (context manager) .search(keyword, max_posts); drift + safety latches  (R2)
│   ├── linkedin_fixture/           # slice C (Phase 1.1: R3): fake LinkedIn, mounted at /__fixtures/linkedin ONLY in fixture mode
│   │   ├── __init__.py
│   │   ├── routes.py               # login (2FA flow), feed, authwall, search/results/content, checkpoint/challenge
│   │   ├── corpus.py               # trigger keywords + corpus loader
│   │   ├── bulk.py                 # Phase 1.4: deterministic 300-post synthetic bulk corpus + keyword→results mapping + expected_verdict(text)
│   │   ├── posts.json              # the corpus (see "Fixture LinkedIn corpus")
│   │   └── templates/              # CURRENT-LinkedIn-shaped DOM: obfuscated classes, no global-nav (Phase 1.1)
│   ├── services/                   # slice D: the only DB-writing domain layer
│   │   ├── __init__.py
│   │   ├── views.py                # typed view models returned to routes (see Contracts)
│   │   ├── keywords.py  offerings.py  session.py  runs.py
│   │   ├── keyword_library.py      # Phase 1.4: SUGGESTED_KEYWORDS (60) — pure data
│   │   ├── profiles.py  seen_posts.py   # Phase 1.4: profile upsert; sightings, triage persistence, rescue, skipped list, purge
│   │   ├── dedupe.py               # normalize_text, content_hash (pure) + upsert_post(db, …) (legacy path; the Phase 1.4 job uses seen_posts)
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
    ├── conftest.py                 # scaffold: env isolation, tmp SQLite + alembic upgrade, resets; Phase 1.1 (R6): dead-port pin + 8765 guard + cooldowns 0 + channel chromium
    ├── support/                    # fake_llm.py (B), live_server.py (I)
    ├── fixtures/
    │   └── linkedin_pages/         # Phase 1.1 (R2): authored, realistic saved LinkedIn HTML + manifest.json (no personal data)
    │                               #   Phase 1.2: + search_results_sdui.html (SANITIZED from one real capture; synthetic data only)
    ├── unit/                       # pure; no network; agent/, scraper/, services/; test_test_isolation.py (R6)
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
- Money is stored as integer micro-USD (`cost_usd_micros`). It is displayed with the `usd` filter (≥4 decimals below $0.01, e.g. `$0.0012`). The header cost badge and run costs always show 4 decimals.
- Datetimes are timezone-aware UTC in the DB (`DateTime(timezone=True)`) and are shown in `SALES0_TIMEZONE` via `localdt` and `ago`.

**SQLite engine setup** (`db/engine.py`): `connect_args={"check_same_thread": False}`. On every connect it sets `PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000`. It uses `sessionmaker(expire_on_commit=False)`, and `session_scope()` commits on success and rolls back on exception. The background thread opens its own sessions and never shares one with a request.

**Migrations (binding from Phase 1.1)**
- **Never batch-rebuild a table that other tables reference by foreign key** while SQLite runs with `foreign_keys=ON`. Examples: `keywords`, `scrape_runs`, `enquiries`, `offerings`, `offering_groups`, `users`; check `data.md` for every `FK <table>`. A batch rebuild copies the table, DROPs the original and renames the copy. With enforcement on, the DROP fires the children's `ON DELETE CASCADE` / `SET NULL` actions, which silently deletes or nulls child rows, or it fails with a constraint error.
- **Add columns in place** with plain `op.add_column(...)`, nullable or with a `server_default`. Do not use `batch_alter_table` to add columns. Enum-like new columns enforce their allowed values in the model/service layer, or by a CHECK added only on a table that nothing references.
- Batch mode stays allowed for leaf tables (no inbound FKs), e.g. `status_history`, `linkedin_session`. A migration that truly must rebuild a referenced table does it in four steps: run `PRAGMA foreign_keys=OFF` outside the transaction, rebuild, run `PRAGMA foreign_key_check` (it must return no rows), then turn enforcement back on. That migration carries a one-line justification in its docstring.
- Every new migration gets a test that upgrades a POPULATED previous-head DB (runs, run keywords, enquiries, sources, hits, decisions, llm_calls) and asserts that every row count and FK value survives.
- Gates and tests never run migrations against `data/sales0.db`. They use a temp DB file.

> **Assumed:** this is a rule for future migrations. 0001–0002 are not rewritten. Phase 1.1 adds no migration: the head stays `0002_phase1_discovery`. Phase 1.2 adds no migration either (link kind and fingerprint are derived; `data.md` → Phase 1.2). Phase 1.4 adds `0003_phase1_4_seen_posts`: two new tables, in-place `ADD COLUMN`s (nullable or with a `server_default`; a nullable FK column is added with plain `op.add_column`, and if SQLite rejects an inline FK the model keeps the FK and the migration adds the bare column), a data backfill, and NO rebuild of `scrape_runs`, `enquiries`, `keywords` or any other referenced table (so no new CHECK value: interrupted runs are `aborted` + null reason).

**Error shape**
- JSON endpoints (`/api/*` and any `Accept: application/json`) return `{"ok": true, "data": {...}}`, or `{"ok": false, "error": {"code": "snake_code", "message": "Human sentence with next step."}}` with the matching HTTP status.
- htmx endpoints raise `DomainError`. The handler returns HTTP 200 with an HTML toast fragment `<div class="toast toast--error" role="alert">message</div>` and headers `HX-Retarget: #toasts` and `HX-Reswap: beforeend`, so the page never breaks. A non-htmx full-page request with an error renders `error.html` (owned by E) with a link back.
- Unhandled exceptions are logged with `exc_info` and shown to the user as the generic message "Something went wrong — details are in the server log (request_id=…)". Stack traces never reach the user.
- `DomainError` codes are listed in `api.md`.

**Logging** (structlog JSON; one object per line)
- Every line has `timestamp` (ISO UTC), `level`, `event`, `logger`, plus bound context when present: `request_id`, `run_id`, `job`, `enquiry_id`.
- `http_request`: `method`, `path`, `status`, `latency_ms`, `htmx` (bool), `input_summary` (query keys + form field names, no values for password fields), `output_summary` (content-type + bytes).
- `llm_call`: `feature`, `model`, `prompt_version`, `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`, `status` (`ok|error`), `error_type`, `fewshot_count`, `input_summary` (first 80 chars of the enquiry text), `output_summary` (verdict + priority + offering_id).
- `scrape_keyword`: `keyword`, `posts_seen`, `posts_new`, `posts_dupe`, `errors`, `duration_ms`, `status`. Phase 1.1 adds `cards_found`, `parse_errors`, `field_misses` (dict of field → count), `drift` (bool), `no_results` (bool). Phase 1.2 adds `strategy` (`sdui|urn|none`), `permalink_attempts`, `permalinks_found`. The level is WARNING when `status=warning`.
- `scrape_run`: `status`, totals, `abort_reason`, `duration_ms`. Phase 1.1 adds `layout_warnings` and `browser` (`chrome|chromium`).
- `linkedin_session`: `action` (`connect|check|run_start`), `result`, `reason`. Phase 1.1 adds `browser` (`chrome|chromium`), `browser_version`, `headless` (bool), `cookie_present` (bool, never the value), `final_path` (URL path only, no query), `status_code`, and `soft_hints` (names of the matched soft DOM hints, possibly empty). When `soft_hints` is empty on a logged-in verdict, a separate WARNING `linkedin_dom_hints_missing` is logged. This is informational and never blocks.
- `browser_launch` (DEBUG): `channel`, `headless`, `profile` (dir name only), `user_agent_overridden` (bool).
- Cookie values are never logged at any level, never rendered, and never returned by an endpoint. Only presence booleans are allowed.
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
- **From Phase 1.1 (slice R6), `tests/conftest.py` also does the following:**
  - pins `SALES0_PORT` (Port isolation below);
  - sets `SALES0_SAFETY_COOLDOWN_MIN=0` and `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN=0` by default, so safety tests can run back to back (the cool-down's own tests set them explicitly);
  - sets `SALES0_LINKEDIN_BROWSER_CHANNEL=chromium` by default, so the suite is deterministic on any machine. Tests that exercise the real Chrome set `chrome` explicitly (Real-Chrome tests below);
  - drops `SALES0_LINKEDIN_HEADLESS` from `TEST_ENV`, since the setting no longer exists;
  - adds `("sales0.scraper.browser", "reset_browser_cache")` to the reset seams.
- **Port isolation** (`tests/conftest.py`, session-scoped autouse, Phase 1.1). 8765 is the default port, and the user may be running their own sales0 on it. A test that falls back to the default could talk to (and mutate) the user's real server and DB.
  - At session start, pick a free **dead** port: bind `127.0.0.1:0`, read the port, close the socket, so nothing listens there. Set `SALES0_PORT` to it in `os.environ` before any `Settings` is built. Every URL derived from settings (`settings.base_url`, the fixture `base_url`) then points at that dead port, unless a test sets its own server port (ephemeral, or 8766). An accidental connection fails fast with "connection refused".
  - Guard: patch `socket.socket.connect` / `connect_ex` (and so `create_connection`, httpx, and the Python side of Playwright's driver connection). Any connection to `127.0.0.1`, `localhost` or `::1` on port **8765** then raises `ConnectionRefusedError("tests must never connect to 8765 (the user's sales0)")`. The guard also records the violation in a module-level `guard_violations` list. An autouse per-test teardown fails the test if the list grew, which catches code that swallowed the error. `FORBIDDEN_PORT = 8765` is exported. Subprocesses (live servers) get `SALES0_PORT` explicitly from the harness, never the default.
  - `tests/unit/test_test_isolation.py` asserts six things: `get_settings().port != 8765`; `:8765` is not in `settings.base_url`; the default test port has no listener (`httpx.ConnectError`); the DB URL is under `tmp_path` and is never `data/sales0.db`; an explicit `SALES0_PORT=8799` is respected; and `socket.connect(("127.0.0.1", 8765))`, `connect_ex(("localhost", 8765))` and `httpx.get("http://127.0.0.1:8765/…")` are all refused and recorded (the test then clears its own deliberate violations). It also asserts that both cool-down settings are 0, that the channel is `chromium`, and that `scraper.browser.profile_dir(get_settings(), "chromium")` is under `tmp_path`.
- **Every-page render test** (`tests/integration/web/test_every_page_renders.py`, slice R5). The user once hit a 500 when a template referenced a shell/context key that its route did not provide. This test GETs every page route through the real app and the real `render_page`/`shell_context`. The routes are `/` (following the redirect), `/inbox?tab=review`, `/inbox?tab=accepted`, `/inbox?tab=discarded`, `/inbox?tab=discarded&q=zzz`, `/setup`, `/runs`, `/runs/{id}` (a seeded finished run), `/pipeline`, `/contacts`, `/competitors`, `/import`, `/outreach`, `/metrics` and `/whatsapp`. It also covers the error page (a full-page `GET /runs/999999` renders `error.html` with HTTP 404 and "That item no longer exists") and the fragments `/_shell/status-light`, `/_shell/banner`, `/setup/linkedin/status` and `/runs/{id}/progress`.
  - It runs that set once for each seeded state: never checked; `connected`; `not_connected` with a last_error; `needs_attention`; busy `connect`; busy `check`; an active run; an active safety cool-down; a finished run with `layout_warnings > 0`; and the browser fallback (a stubbed `resolve_browser` returning chromium with its note).
  - Each response must be 200 (404 for the error page) and contain its key text: "Review inbox"; "Connect LinkedIn" or "Connect again" on /setup; "Run now" on /runs; "Coming in Phase N" on stubs; the status-light label.
  - The route list is asserted against `app.routes`: every GET route that returns HTML and has no path parameter must be in the list, so a new page cannot skip the test.
- **Saved-page tests** (slice R2). `tests/fixtures/linkedin_pages/` holds authored, realistic LinkedIn HTML (`## LinkedIn browser` → Saved page samples). `tests/unit/scraper/test_classify_page.py` runs `classify_page` over every manifest entry's recorded signals, plus a table of URL/status/cookie cases (pure). `tests/integration/scraper/test_saved_pages.py` serves each file in headless Chromium through `page.route(<manifest url>, fulfill(status, body))`. It adds a `li_at` cookie for the samples whose manifest says `cookie: true`. It runs the real `SIGNALS_JS` + `EXTRACT_JS` + `classify_page` + `post_from_raw`, then asserts the manifest's expected verdict, post count, URNs, field values, `drift` and `field_misses`.
- **Real-Chrome tests** (`tests/integration/scraper/test_real_chrome.py`, slice R1, marker `chrome`). These set `SALES0_LINKEDIN_BROWSER_CHANNEL=chrome` and launch HEADLESS against the fixture server, on a profile under `tmp_path`, never `data/`. They assert six things in Chrome: `navigator.webdriver === false`; `navigator.userAgent` has no `HeadlessChrome` and contains `Chrome/<installed major>.`; `Intl.DateTimeFormat().resolvedOptions().timeZone == SALES0_TIMEZONE`; `navigator.language` equals the resolved locale; the fixture 2FA login detected via cookie + URL; and a full `search("ai workshop")` returns the 4 corpus posts. If Google Chrome is not installed, the test FAILS with "Google Chrome not installed — install it, or run this gate on the user's Mac". It is not skipped.
- Markers (declared in `pyproject.toml`): `llm` (real Haiku), `browser` (Playwright), `e2e`, and `chrome` (needs installed Google Chrome; Phase 1.1). Nothing is skipped by default.
- Fixture-mode scraper tests run the fixture router in a uvicorn thread on a free port (fixture `fixture_linkedin_server` in `tests/integration/scraper/conftest.py`, owned by C; Phase 1.1: R3).
- Pacing in tests: `SALES0_SCRAPE_DELAY_MIN_S=0.05`, `MAX_S=0.15`, `KEYWORD_PAUSE_MIN_S=0.05`, `MAX_S=0.1`, `MAX_RUNS_PER_DAY=50`.
- Real-LLM assertions check structure and the stable facts in the fixture expectation table, never prose.
- pytest-timeout: 120s per test, 600s for `e2e`.
- **E2E harness** (`tests/support/live_server.py`, slice I):
  1. Create a temp dir and run `alembic upgrade head` against `sqlite:///<tmp>/e2e.db`.
  2. Spawn `[sys.executable, "-m", "sales0"]`. Under `uv run pytest`, this is the same `.venv` interpreter as the documented run command. The env is: `SALES0_PORT=8766`, `SALES0_LINKEDIN_MODE=fixture`, `SALES0_DATABASE_URL` and `SALES0_DATA_DIR` set to the temp locations, the test pacing values, and the real key from `.env`. Phase 1.1 adds `SALES0_SAFETY_COOLDOWN_MIN=0`, `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN=0` and `SALES0_LINKEDIN_BROWSER_CHANNEL=chromium`; `extra_env` can override any of them. The harness refuses to start when `port == 8765`.
  3. Poll `/api/health` until 200 (30s max), capturing stdout lines for the log assertions.
  4. Provide `restart()`, which keeps the DB for the state-survival check.
  5. Terminate the server on teardown.

  The browser is pytest-playwright's headless Chromium `page`. The checkpoint, captcha and logout E2E uses its own server instance (also on 8766, started after the journey server has stopped).

**Startup hooks** (`web/app.py` lifespan):
- The scaffold version runs `configure_logging`, ensures `data/` exists, and calls `assert_at_head()`.
- In Phase 1, slice **D** (the only Phase-1 slice allowed to edit `web/app.py`) adds exactly one line, `services.runs.recover_interrupted_runs(db)`. This marks queued/running runs `failed` with "Interrupted by server restart".
- Since Phase 1, `/` (in `web/routes/stubs.py`) returns a 303 redirect to `/inbox`. The scaffold's `home.html` no longer exists.

## Configuration

Every variable below appears in `.env.example`, with comments, in this order (optional ones such as `SALES0_ANTHROPIC_WORKSPACE_ID` appear commented out or empty, with their purpose). All are read by `sales0.config.Settings` (prefix `SALES0_`).

| Variable | Default | Purpose |
|---|---|---|
| `SALES0_ANTHROPIC_API_KEY` | — (required) | Anthropic key (SecretStr). The user creates `.env`. |
| `SALES0_ANTHROPIC_WORKSPACE_ID` | (unset) | Optional. When set, sent as the `anthropic-workspace-id` header on every Anthropic request. REQUIRED for org-level keys that are not workspace-scoped (this user's key needs it). |
| `SALES0_LLM_MODEL` | `claude-haiku-5-5` | Model id. Haiku only by constraint. |
| `SALES0_LLM_MAX_OUTPUT_TOKENS` | `900` | Triage output cap |
| `SALES0_LLM_TIMEOUT_S` | `30` | Per-request timeout |
| `SALES0_LLM_MAX_RETRIES` | `3` | Retries on 429/5xx/timeouts (exponential backoff with jitter, base 1s) |
| `SALES0_LLM_PRICE_INPUT_USD_PER_MTOK` | `0.10` | Input price per 1M tokens (`claude-haiku-5-5`, prompts ≤100K tokens) |
| `SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK` | `0.50` | Output price per 1M tokens (`claude-haiku-5-5`, prompts ≤100K tokens) |
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
| `SALES0_LINKEDIN_BROWSER_CHANNEL` | `auto` | Phase 1.1. `auto`: use the installed Google Chrome when present, else the bundled Chromium with the note "Google Chrome not found — using the built-in browser". `chrome`: Google Chrome only (an error when it is missing). `chromium`: the bundled Chromium only (tests). Resolved by `scraper.browser.resolve_browser`. |
| `SALES0_LINKEDIN_CONNECT_TIMEOUT_S` | `300` | How long Connect waits for a login |
| `SALES0_SAFETY_COOLDOWN_MIN` | `60` | Phase 1.1. Minutes Run now stays refused (`safety_cooldown`) after a run aborted with `rate_limited` or `captcha`. 0 = off (tests). |
| `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN` | `15` | Phase 1.1. Same, after a run aborted with `checkpoint` or `logout`. 0 = off (tests). |
| `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` | `40` | Cap (Phase 1.4; was 10) |
| `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` | `80` | Cap on posts scanned per keyword across all passes (Phase 1.4; was 15) |
| `SALES0_SCRAPE_MAX_POSTS_PER_DAY` | `2000` | Cap across runs (posts scanned, local day; Phase 1.4; was 150) |
| `SALES0_SCRAPE_MAX_RUNS_PER_DAY` | `4` | Cap (runs started, local day; aborted runs count; Phase 1.4; was 3) |
| `SALES0_SCRAPE_DELAY_MIN_S` / `_MAX_S` | `3` / `9` | Random delay between page actions (scrolls, expands) |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S` / `_MAX_S` | `20` / `60` | Random pause between keywords |
| `SALES0_SCRAPE_MAX_SCROLLS` | `20` | Max scrolls per search pass (Phase 1.4; was 4) |
| `SALES0_SCRAPE_NO_GROWTH_SCROLLS` | `2` | Phase 1.4. Consecutive scrolls without new cards that end a pass ("no growth") |
| `SALES0_SCRAPE_SORT` | `latest` | Phase 1.4. `latest` (LinkedIn `sortBy="date_posted"`) or `relevance` |
| `SALES0_SCRAPE_DATE_FILTERS` | `past-week,past-month` | Phase 1.4. Comma list of `past-24h`/`past-week`/`past-month`; one search pass each, in order; the next pass runs only while the keyword's post cap is not reached. Replaces `SALES0_SCRAPE_DATE_FILTER`, which is still read as a one-item list when `…_FILTERS` is unset |
| `SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD` | `40` | Phase 1.4. Max "see more" clicks per keyword (only truncated cards; `pacer.short` delays). Beyond it a card is read truncated |
| `SALES0_TRIAGE_CONCURRENCY` | `4` | Phase 1.4. Concurrent Haiku triage calls (1–8) |
| `SALES0_SEEN_RAW_RETENTION_DAYS` | `0` | Days the raw text of a seen post is kept; 0 (default) keeps it forever, a positive value purges after that many days |
| `SALES0_SEED_SUGGESTED_KEYWORDS` | `true` | Phase 1.4. Auto-seed the 60 suggested keywords once at startup. Tests and the E2E harness set `false` |
| `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD` | `15` | Phase 1.2. Max "Copy link to post" attempts per keyword (posts without a URN). Phase 1.4: attempted only for AI-genuine posts, after triage. Integer ≥ 0; 0 disables permalink capture (cards then link to the author profile). In `.env.example` right after `SALES0_SCRAPE_MAX_SCROLLS`. |
| `SALES0_FEWSHOT_MAX_EXAMPLES` | `6` | Few-shot cap per triage call |
| `SALES0_FEWSHOT_TEXT_CHARS` | `600` | Truncation per example |
| `SALES0_TRIAGE_MAX_INPUT_CHARS` | `4000` | Truncation of the enquiry text sent to the LLM |

**Phase 1.1 config changes.**
- `SALES0_LINKEDIN_HEADLESS` is **removed** from `Settings` and `.env.example`. Connect is always headed, and every check and run is always headless. `extra="ignore"` tolerates a stale line in an old `.env`.
- New: `SALES0_LINKEDIN_BROWSER_CHANNEL`, `SALES0_SAFETY_COOLDOWN_MIN` and `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN`. In `.env.example` they go in the `# --- LinkedIn ---` block, right after `SALES0_LINKEDIN_MODE` / `SALES0_LINKEDIN_CONNECT_TIMEOUT_S`, each with a comment.
- Validators: `linkedin_browser_channel` goes through the enum-strip/lower validator and must be `auto|chrome|chromium`. Both cool-down values are integers ≥ 0.
- The Setup "Safety limits" rows drop "Browser window during runs" and add "Browser" (the resolved label), "Runs" ("hidden (headless)"), "Cool-down after rate limit / captcha" (`N min`) and "Cool-down after checkpoint / logout" (`N min`).
- Locale and timezone are not configurable in Phase 1.1. They are derived (`## LinkedIn browser` → Launch options).

Phase 2 adds `SALES0_APP_PASSWORD` (and the CRM variables in `## Phase 2` → Settings; the planned `SALES0_SESSION_SECRET` is dropped: the session cookie is signed with `SALES0_SECRET_KEY`). Phase 3 adds `SALES0_GOOGLE_CLIENT_SECRET_PATH`, `SALES0_GOOGLE_TOKEN_PATH` (the sender is the signed-in member; there is no test-recipient setting) and `SALES0_LINKEDIN_MAX_PROFILES_PER_DAY` (30). Phase 5 adds `SALES0_WHATSAPP_INGEST_TOKEN`. Each is appended to `.env.example` in its phase.

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

`AnthropicClient.structured` calls `messages.create(model=settings.llm_model, system=..., messages=..., tools=[{name, description, input_schema}], tool_choice={"type":"tool","name":tool_name}, max_tokens=...)`. Call-shape rule: no `temperature`, `top_p`, `top_k` or thinking/budget parameters are ever sent, by `structured` or `ping`. Haiku 5.5 has rejected sampling parameters since 2026-10-21, and triage relies on forced tool use plus guards rather than sampling settings. When `SALES0_ANTHROPIC_WORKSPACE_ID` is set, the client is built with `default_headers={"anthropic-workspace-id": <id>}`, so every request (including `ping`) carries it. The SDK's internal retries are disabled (`max_retries=0`); the adapter retries itself on `RateLimitError`, `APIStatusError` ≥500, `APIConnectionError` and `APITimeoutError`, up to `SALES0_LLM_MAX_RETRIES` with backoff. It maps 401/403 to `LLMAuthError("Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env")`, the HTTP 400 that says the key requires a workspace to `LLMAuthError("This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env")` (never retried), and 404 to `LLMModelNotFound("Model '<id>' not found — check SALES0_LLM_MODEL")`. Any other rejected request (another 4xx, e.g. HTTP 400 `invalid_request_error`) is not retried and surfaces to the user as "AI request rejected — see logs"; the Anthropic error detail goes only to the `llm_call` log line. It emits one `llm_call` log line per attempt-final outcome. It does NOT write to the DB; persistence of `llm_calls` is done by `services.triage` from the returned `LLMResponse`, which keeps the layer DB-free.

`python -m sales0.cli check-llm` calls `ping()` and prints `LLM OK model=<model> input_tokens=N output_tokens=M` with exit 0. On error it prints `LLM ERROR type=<auth|workspace-required|model-not-found|transient|bad-response> <mapped message>` to stderr with exit 1 (a missing key is still reported as missing). The type distinguishes a bad key, a key that needs `SALES0_ANTHROPIC_WORKSPACE_ID`, a wrong model slug, a temporary outage, and a rejected request or unusable response (`LLMBadResponse`), rather than a bare OK/MISSING.

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
LinkKind = Literal["post","profile","search","other"]   # Phase 1.2
@dataclass(frozen=True)
class ScrapedPost: source_ref: str                 # Phase 1.2 (was `urn: str`): "urn:li:activity|ugcPost|share:N" when known, else "ck:<KEY>"
                   url: str                        # Phase 1.2: the card link — post permalink | author profile URL | search URL
                   author_name: str | None; author_profile_url: str | None
                   author_headline: str | None; posted_relative: str | None; posted_date: date | None; text: str
                   link_kind: LinkKind = "post"    # Phase 1.2: == scraper.urls.link_kind_of(url)
                   search_url: str | None = None   # Phase 1.2: the keyword's search URL (provenance)
                   @property
                   def urn(self) -> str | None: ... # source_ref when it starts with "urn:li:", else None
@dataclass(frozen=True)
class KeywordScrapeResult: keyword: str; posts: list[ScrapedPost]; scrolls: int; parse_errors: int; drift_warning: bool
                           cards_found: int = 0                  # Phase 1.1: card elements discovered (before parsing)
                           no_results: bool = False              # Phase 1.1: the "no results" marker was present
                           field_misses: dict[str, int] = {}     # Phase 1.1 (field(default_factory=dict)): keys author_name,
                                                                 #   author_profile_url, author_headline, posted_relative → posts missing it
                                                                 #   Phase 1.2 adds key "text" (containers with a ref but no readable text)
                           strategy: Literal["sdui","urn","none"] = "none"   # Phase 1.2: which card strategy won on this page
                           permalink_attempts: int = 0           # Phase 1.2: "Copy link to post" attempts
                           permalinks_found: int = 0             # Phase 1.2: attempts that yielded a post URL
@dataclass(frozen=True)
class SessionCheckResult: logged_in: bool; reason: Literal["ok","no_cookie","redirected_login","checkpoint","captcha","rate_limited","error"]; url: str
                          soft_hints: tuple[str, ...] = ()       # Phase 1.1: matched soft DOM hints (logged only)
@dataclass(frozen=True)
class ConnectResult: connected: bool; reason: Literal["ok","timeout","window_closed","cancelled","error"]; detail: str | None
                     browser: Literal["chrome","chromium"] | None = None   # Phase 1.1

# scraper/errors.py
class AccountSafetyStop(Exception): kind: Literal["logout","checkpoint","captcha","rate_limited"]; url: str
class ScrapeError(Exception): ...   # AccountSafetyStop is NOT a ScrapeError subclass (callers catch them separately)
class SessionMissing(Exception): url: str   # Phase 1.1: no li_at in the profile before ANY LinkedIn request (not a safety stop, not a ScrapeError)
class BrowserUnavailable(ScrapeError): ...  # Phase 1.1: channel "chrome" requested but not installed, or the browser failed to launch

# scraper/browser.py  (Phase 1.1, slice R1)
@dataclass(frozen=True)
class BrowserChoice: channel: Literal["chrome","chromium"]; version: str | None   # e.g. "156.0.7311.42"; None if unknown
                     label: str            # "Google Chrome 156" | "Built-in browser (Chromium)"
                     note: str | None      # "Google Chrome not found — using the built-in browser" only on an auto fallback
def resolve_browser(settings: Settings) -> BrowserChoice      # cached per process; raises BrowserUnavailable for channel=chrome when absent
def reset_browser_cache() -> None
def profile_dir(settings: Settings, channel: str) -> Path      # table in "## LinkedIn browser" → Profiles
def user_agent_for(version: str, platform: str = sys.platform) -> str   # reduced Chrome UA, e.g. "...Chrome/156.0.0.0 Safari/537.36"
def system_locale() -> str                                   # "en-IN"-style BCP-47; fallback "en-US"
def launch_options(settings: Settings, choice: BrowserChoice, *, headless: bool) -> dict   # kwargs for launch_persistent_context (pure)
@contextmanager
def open_context(settings: Settings, *, headless: bool) -> Iterator[BrowserContext]   # resolve → profile → launch → always close
def first_page(context: BrowserContext) -> Page

# scraper/detection.py  (Phase 1.1, slice R2 — PURE except read_signals/session helpers)
PageKind = Literal["ok","logout","checkpoint","captcha","rate_limited","http_error"]
@dataclass(frozen=True)
class PageSignals:
    url: str; status: int | None                 # final URL; main-document status (None = unknown)
    has_session_cookie: bool                     # non-expired li_at applicable to base
    has_captcha: bool; has_login_form: bool; has_authwall: bool
    soft_hints: tuple[str, ...] = ()             # NEVER affects the verdict
@dataclass(frozen=True)
class PageVerdict: kind: PageKind; reason: str    # reason e.g. "path:/authwall", "status:429", "no_cookie", "login_form"
def classify_url(url: str) -> Literal["logout","checkpoint"] | None   # path-only rules (navigation latch, connect poll)
def classify_page(signals: PageSignals) -> PageVerdict                # the ONE classifier for live pages AND tests
def session_cookie_present(context: BrowserContext, base: str, now: float | None = None) -> bool
SIGNALS_JS: str                                   # () => {has_captcha, has_login_form, has_authwall, no_results, cards_found, soft_hints}
def read_signals(page: Page, response: Response | None, context: BrowserContext, base: str) -> tuple[PageSignals, dict]
#   (signals, extras) — extras = {"no_results": bool, "cards_found": int} from the same SIGNALS_JS evaluation
def same_site(url: str, base: str) -> bool; def is_rate_limit_response(url: str, status: int, base: str) -> bool   # unchanged

# scraper/session_check.py  (slice R1)
def verify_in_context(context: BrowserContext, base: str, timeout_ms: int, page: Page | None = None) -> SessionCheckResult
#   no navigation when the cookie is absent (reason "no_cookie"); else ONE goto {base}/feed/ → read_signals → classify_page
def check_session(settings: Settings) -> SessionCheckResult   # opens+closes its own HEADLESS context (Re-check)
# scraper/connect.py  (slice R1)
def connect_interactive(settings: Settings, on_tick: Callable[[int], None], should_cancel: Callable[[], bool], *,
                        poll_interval_s: float = 1.0, timeout_s: float | None = None) -> ConnectResult
#   HEADED in live mode (headless in fixture mode). Poll rule: "## LinkedIn browser" → Connect.
# scraper/search.py  (slice R2)
class LinkedInSearcher:
    def __init__(self, settings: Settings, pacer: Pacer | None = None): ...
    def __enter__(self) -> "LinkedInSearcher": ...   # open_context(headless=True); verify_in_context →
                                                     #   no_cookie → SessionMissing; logout/checkpoint/captcha/rate_limited → AccountSafetyStop;
                                                     #   error → ScrapeError
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
                  stored_status: Literal["not_connected","connected","needs_attention"],   # the DB value; `status` may be the derived "busy"
                  last_checked_at, last_connected_at, last_successful_run_at, last_error: str | None, mode: Literal["live","fixture"], connect_elapsed_s: int | None,
                  browser_label: str, browser_note: str | None,          # Phase 1.1: from scraper.browser.resolve_browser; on BrowserUnavailable:
                                                                          #   label "Google Chrome (not installed)", note = the error message
                  cooldown_until: datetime | None)                        # Phase 1.1: services.runs.safety_cooldown_until (UTC)
Eligibility(can_run: bool, reason: str | None, reason_code: str | None,   # reason_code = the DomainError code start_run would raise
            runs_today: int, runs_cap: int, posts_today: int, posts_cap: int)
RunKeywordView(keyword_text, status, posts_seen, posts_new, posts_dupe, errors, duration_ms, error_message)
RunView(id, status, trigger, mode, started_at, finished_at, duration_ms, keywords_planned, keywords_done, current_keyword, current_step,
        posts_seen, posts_new, posts_dupe, enquiries_triaged, triage_failed, errors_count, abort_reason, error_message, cost_usd_micros,
        layout_warnings: int = 0)   # Phase 1.1: count of this run's keyword rows with status "warning" (computed on read)
RunDetailView(run: RunView, keywords: list[RunKeywordView])
EnquiryCardView(id, review_status, stage, triage_status, triage_error,
                requirement_summary, ai_verdict, ai_reason, ai_poster_type, ai_priority, ai_offering_id, priority, offering_id, offering_name,
                company, contact_person, email, phone, city, mode, dates_mentioned, topic,
                author_name, author_headline, source_url, source_ref, raw_text, date_posted, date_added, keywords: list[str],
                discard_reason, is_override, decided_at, cost_usd_micros, tokens_total,
                link_kind: LinkKind | None = None)   # Phase 1.2: scraper.urls.link_kind_of(source_url); None when source_url is null
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
#   OfferingInput length violations raise offering_invalid. A name matching a DEACTIVATED offering reactivates it (with the new group/description), not offering_duplicate.
update_offering(db, offering_id: int, data: OfferingInput) -> OfferingView
delete_offering(db, offering_id: int) -> Literal["deleted","deactivated"]   # deactivated when any enquiry references it (FK RESTRICT, data.md)
# services/session.py
session_status(db) -> SessionStatusView
start_connect(db) -> SessionStatusView                     # DomainError job_busy
start_check(db) -> SessionStatusView                       # DomainError job_busy
# services/runs.py
run_eligibility(db, now: datetime | None = None) -> Eligibility
start_run(db, actor_user_id: int | None = None) -> RunView # DomainError run_in_progress|needs_attention|safety_cooldown|not_connected|job_busy|no_keywords|daily_run_cap|daily_post_cap (api.md order)
safety_cooldown_until(db, now: datetime | None = None) -> datetime | None   # Phase 1.1 (data flow step 6c)
keyword_outcome(result: KeywordScrapeResult) -> tuple[Literal["done","warning"], str | None]   # Phase 1.1, PURE (data flow step 3)
get_run(db, run_id: int) -> RunDetailView                  # DomainError not_found
list_runs(db, limit: int = 20) -> list[RunView]
active_run(db) -> RunView | None
recover_interrupted_runs(db) -> int                        # called once at startup; returns count marked failed
# services/inbox.py
list_enquiries(db, tab: Literal["review","accepted","discarded"], q: str | None, page: int = 1, page_size: int = 25) -> InboxPage
get_card(db, enquiry_id: int) -> EnquiryCardView
decide(db, enquiry_ids: list[int], decision: Literal["accept","discard"], discard_reason: str | None = None,
       actor_user_id: int | None = None) -> list[EnquiryCardView]       # DomainError not_found | decision_invalid
#   decision_invalid also when any target enquiry is still triage_status=pending.
update_fields(db, enquiry_id: int, priority: str | None = None, offering_id: int | None = <omitted sentinel>) -> EnquiryCardView
#   priority=None leaves it unchanged; offering_id omitted keeps it; offering_id=None sets "No match".
#   DomainError field_invalid for a priority outside high|medium|low or an inactive/unknown offering.
retry_triage(db, enquiry_id: int) -> EnquiryCardView       # synchronous single real call; DomainError triage_failed
# services/costs.py
cost_summary(db, now: datetime | None = None) -> CostSummary
# services/dedupe.py
normalize_text(text: str) -> str
content_hash(text: str) -> str
upsert_post(db, post: ScrapedPost, run_id: int, keyword_id: int, keyword_text: str) -> tuple[Literal["new","dupe_urn","dupe_content"], int]  # (kind, enquiry_id)
#   Phase 1.2: "dupe_urn" now means "same source ref" (URN or ck:); match order + link upgrade in "## LinkedIn SDUI search results" → Identity
near_duplicate(a_profile_url: str | None, a_text: str, b_profile_url: str | None, b_text: str) -> bool   # Phase 1.2, PURE
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

## LinkedIn browser (Phase 1.1)

This section is binding for slices R1 (browser + login) and R2 (detection + parsing). It replaces Phase 1's bundled-Chromium profile and its DOM-marker login detection.

### Browser resolution (`scraper/browser.resolve_browser`, cached; `reset_browser_cache` seam)

| `SALES0_LINKEDIN_BROWSER_CHANNEL` | Google Chrome installed | Result |
|---|---|---|
| `auto` (default) | yes | `chrome`, label "Google Chrome <major>", note None |
| `auto` | no | `chromium`, label "Built-in browser (Chromium)", note **"Google Chrome not found — using the built-in browser"** |
| `chrome` | yes | `chrome` |
| `chrome` | no | raises `BrowserUnavailable("Google Chrome is not installed — install it from https://www.google.com/chrome/ or set SALES0_LINKEDIN_BROWSER_CHANNEL=auto")` |
| `chromium` | any | `chromium`, note None |

- **"Installed"** means the binary at one of Playwright's standard channel locations exists:
  - macOS: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`, else `~/Applications/Google Chrome.app/…`. In the second case Playwright gets `executable_path` instead of `channel`, because Playwright only looks in `/Applications`.
  - Linux: `/opt/google/chrome/chrome`.
- **Chrome version:**
  - macOS: `CFBundleShortVersionString` from `Contents/Info.plist`, via `plistlib`.
  - Linux: `<binary> --version`, 5 s timeout.
  - Neither starts a browser. On this Mac the result is `156.x`.
- **Chromium version:** `browserVersion` of the `chromium` entry in Playwright's bundled `driver/package/browsers.json`. If that is unreadable, it is `None`.
- **Playwright launch:**
  - `chrome` → `channel="chrome"`. Its headless mode is Chrome's own new headless.
  - `chromium` → `channel="chromium"`. This opts into new headless for the bundled Chromium (not `chromium-headless-shell`). It requires `uv run playwright install chromium`, which installs both builds.

> **Assumed:** the Chromium version comes from Playwright's `browsers.json` so that no browser has to be launched just to build the headless UA. If it is unreadable, the headless launch keeps Chromium's own UA and logs WARNING `headless_ua_unmasked`. This is a best-effort fallback that matters only when Chrome is absent.

### Profiles (`scraper/browser.profile_dir(settings, channel)`, all under `settings.data_path`, gitignored)

| Mode | Channel | Directory |
|---|---|---|
| live | chrome | `linkedin-chrome-profile/` (new in Phase 1.1) |
| live | chromium | `linkedin-profile/` (Phase 1's Chromium profile, reused only by the fallback) |
| fixture | chrome | `linkedin-profile-fixture-chrome/` |
| fixture | chromium | `linkedin-profile-fixture-chromium/` |

A Chrome profile and a Chromium profile are never shared, because the two builds' profile formats and versions differ. The Phase-1 login (in `linkedin-profile/`) is therefore not reused by Chrome, and the user logs in once more. This is the "No `li_at` cookie" path of data flow step 2, which is not a safety stop. Phase 1's `linkedin-profile-fixture/` is unused; the README says it can be deleted.

**One context per profile, ever.** The `JobRunner` single-flight rule guarantees it inside sales0. `open_context` additionally checks the profile's `SingletonLock`. When a live process holds it (for example, the user left the sales0 Chrome window open from an aborted Connect), it raises `BrowserUnavailable("The sales0 browser window is still open — close it and try again")` instead of letting Playwright fail obscurely. The sales0 profile is separate from the user's everyday Chrome profiles. A different `user_data_dir` starts a separate Chrome instance, so it works while the user's own Chrome is open, and it never reads or changes their profiles.

### Launch options (`scraper/browser.launch_options`, pure, unit-tested)

The headed (Connect) and headless (Re-check, runs) launches use the **identical** argument set, except for the rows marked as differing. This matters because Playwright's default args `--use-mock-keychain` and `--password-store=basic` decide how Chrome encrypts the profile's cookies. Changing them between launches would make the Connect login unreadable to the headless runs, so they are kept as they are. The only default arg removed is `--enable-automation`.

| Option | Headed (Connect, live) | Headless (check, run; and fixture-mode Connect) |
|---|---|---|
| `user_data_dir` | `profile_dir(settings, choice.channel)` | same |
| `channel` / `executable_path` | from `BrowserChoice` | same |
| `headless` | `False` | `True` |
| `ignore_default_args` | `["--enable-automation"]` | same |
| `args` | `--disable-blink-features=AutomationControlled`, `--no-first-run`, `--no-default-browser-check`, `--window-size=1280,860` | the same, without `--window-size` |
| `chromium_sandbox` | `True` (sandbox on) | `True` |
| viewport | `no_viewport=True` (a normal window that the user can resize) | `viewport={"width": 1280, "height": 860}` |
| `user_agent` | not set (Chrome's natural UA) | `user_agent_for(choice.version)` = `Mozilla/5.0 (<platform>) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/<major>.0.0.0 Safari/537.36`. `<platform>` is `Macintosh; Intel Mac OS X 10_15_7` on macOS (Chrome's frozen value, also on Apple Silicon), `X11; Linux x86_64` on Linux, and `Windows NT 10.0; Win64; x64` on Windows. This is byte-for-byte the reduced UA that headed Chrome sends, and it never contains `HeadlessChrome`. |
| `locale` | `system_locale()` | same |
| `timezone_id` | `settings.timezone` (`SALES0_TIMEZONE`) | same |

- `system_locale()` (cached) tries three sources in order:
  1. On macOS, `defaults read -g AppleLocale` (2 s timeout). `en_IN` becomes `en-IN`, and any `@…` suffix is dropped.
  2. Otherwise `LC_ALL` / `LANG`. `en_IN.UTF-8` becomes `en-IN`.
  3. Otherwise `en-US`.
- `open_context` logs `browser_launch` (DEBUG) and maps every launch `PlaywrightError` to `BrowserUnavailable` with a next step, e.g. "Could not start Google Chrome (…) — quit any sales0 Chrome window and try again".

> **Assumed:** the "Chrome is being controlled by automated test software" infobar can still appear in the headed Connect window on some Chrome versions, even with `--enable-automation` removed. The Setup copy says so (`ui.md`). UA client hints (`Sec-CH-UA`) are not overridden. Headless new-mode Chrome reports Chrome's own brands, and overriding them would need CDP per page. This is accepted as best-effort.

### Logged-in detection (`scraper/detection.classify_page`, the ONE classifier for live pages and tests)

**Primary signals:**
- The **session cookie**: a non-expired `li_at` cookie applicable to `{base}`, from `context.cookies(base)`. In live mode that means domain `.linkedin.com`; in fixture mode, the fixture host. "Non-expired" means `expires == -1` (a session cookie) or `expires > now`, and the value is non-empty.
- The **final URL path** of the page.
- The **main-document HTTP status**.

DOM markers for logged-out pages count only as additional logged-out signals. DOM markers for logged-in pages are **soft hints**: they are logged and never required.

`classify_page(signals)` rules (first match wins):

| # | Condition | Verdict `kind` (`reason`) |
|---|---|---|
| 1 | `status` ∈ {429, 999} | `rate_limited` (`status:<n>`) |
| 2 | `has_captcha`, or `classify_url(url) == "captcha"` | `captcha` |
| 3 | `classify_url(url) == "checkpoint"` | `checkpoint` (`path:<path>`) |
| 4 | `classify_url(url) == "logout"` | `logout` (`path:<path>`) |
| 5 | `not has_session_cookie` | `logout` (`no_cookie`) |
| 6 | `has_login_form` or `has_authwall` | `logout` (`login_form` / `authwall`) |
| 7 | `status` ≥ 400 | `http_error` (`status:<n>`) |
| 8 | otherwise | `ok` |

`classify_url(url) -> "logout" | "checkpoint" | "captcha" | None` is path-only. The query is never inspected, so a search keyword like "login" never trips it. It returns None for a URL that is not `same_site` with base. Path segments are lower-cased, and the first matching rule wins:
- **captcha:** any segment contains `captcha` (e.g. `/uas/consumer-captcha-v2`).
- **logout (login/challenge-to-login paths):** the first segment is one of `login`, `login-submit`, `uas`, `authwall`, `signup`, `signin`, `reg`. Also any path that starts with `/checkpoint/lg/` (LinkedIn's login form, e.g. `/checkpoint/lg/login-submit`), `/checkpoint/rm/sign-in-another-account`, or `/m/login`.
- **checkpoint:** the first segment is `checkpoint` (every other `/checkpoint/*`, e.g. `/checkpoint/challenge/…`, `/checkpoint/challengesV2/…`, `/checkpoint/pk/…`).

DOM markers, all in `selectors.py`, read in ONE `page.evaluate(SIGNALS_JS, SIGNAL_ARGS)`:
- `LOGIN_FORM_MARKERS` (logged-out): `input#username`, `input[name="session_key"]`, `form[action*="login"]`.
- `AUTHWALL_MARKERS` (logged-out): `.authwall`, `[data-test-id*="authwall"]`, `#authwall`.
- `CAPTCHA_MARKERS`: `iframe[src*="captcha" i]`, `iframe[src*="recaptcha"]`, `iframe[src*="arkoselabs"]`, `iframe[src*="funcaptcha"]`, `iframe[title*="captcha" i]`, `#captcha-internal`, `form[action*="captcha"]`.
- `SOFT_LOGGED_IN_HINTS` (name → selector; reported as names, never used in a verdict): `self_profile_link` → `a[href*="/in/"]`, `global_nav_attr` → `[data-test-global-nav]`, `nav_aria` → `nav[aria-label]`, `main_landmark` → `main, [role="main"]`, `legacy_global_nav` → `#global-nav, header.global-nav`.

`LOGGED_IN_MARKER` (Phase 1) is **deleted**. No code may require a logged-in DOM marker.

Where the classifier runs:
- **Connect poll:** cookie + URL only; see Connect.
- **Re-check:** `check_session(settings)`, which is headless and opens and closes its own context, then calls `verify_in_context`.
- **Run start:** `LinkedInSearcher.__enter__` → `verify_in_context`.
- **Mid-run:** every safety checkpoint; see Mid-run safety.

`verify_in_context` works like this:
- If the cookie is absent, it returns `SessionCheckResult(False, "no_cookie", feed_url)` without navigating.
- Otherwise it does ONE `goto({base}/feed/, wait_until="domcontentloaded")`, then waits up to `timeout_ms` for `main, [role="main"], form, iframe` to be attached. Missing content does not fail the check.
- It then runs `read_signals` → `classify_page`.
- Mapping: `ok` → `("ok", logged_in=True)`; `logout` → `"redirected_login"`, or `"no_cookie"` when the reason is `no_cookie`; `checkpoint` / `captcha` / `rate_limited` map to themselves; `http_error` or a navigation error → `"error"`.
- `soft_hints` are copied into the result.

### Connect (`scraper/connect.connect_interactive`)

1. `resolve_browser`. Then `open_context(headless = settings.linkedin_mode == "fixture")`: headed in live mode.
2. Go to `{base}/login` in the first page. A navigation error is ignored, because the user may already be interacting.
3. Every `poll_interval_s` (1 s), pumping events with `page.wait_for_timeout`:
   1. All windows closed → `window_closed`.
   2. `should_cancel()` → `cancelled`.
   3. Elapsed ≥ timeout → `timeout`.
   4. Otherwise call `on_tick(elapsed)` and check the **cheap rule**: `session_cookie_present(context, base)` (no navigation) AND at least one open page P whose `P.url` is `same_site` with base and has `classify_url(P.url) is None`. This means the user has reached the feed or any logged-in page: after 2FA, and after LinkedIn's "remember this browser" step. While the user is on `/login`, `/checkpoint/…` (2FA, remember-browser) or `/authwall`, the rule is false and Connect keeps waiting. These pages are never stops during Connect.
   5. When the rule holds, **verify** once in that same window: `verify_in_context(context, base, timeout_ms, page=P)`, which navigates P to `/feed/`. `logged_in` → return `ConnectResult(True, "ok", None, browser=choice.channel)`, and the context closes, so the window disappears. `rate_limited` → return `("error", "LinkedIn is rate-limiting this browser — wait a few hours, then Connect again")`. Any other failed verdict → keep polling. P is not verified again until its URL changes, which means the user did something.
4. Expected latency: cookie set → next poll (≤ 1 s) → one `/feed/` load → closed. That is about 2 s on live LinkedIn and under 3 s in the fixture (success criterion). If the profile is already logged in, `/login` redirects to `/feed/`, and Connect succeeds on the first poll.
5. `BrowserUnavailable` → `("error", <its message>)`.

### Mid-run safety (`scraper/search.LinkedInSearcher`)

**Latches** are registered at `__enter__`, before the run-start check:
- A `context.on("response")` listener: any `same_site` response (document, XHR, fetch, including background `/voyager/api/…`) with status 429 or 999 latches `rate_limited` with its URL.
- A `page.on("framenavigated")` listener on the main frame: `classify_url(url)` not None latches that kind with its URL. This catches a redirect to `/authwall`, `/login` or `/checkpoint/…` the moment it happens.

**Checkpoints** run before each keyword, after each navigation, after each scroll, after each "see more" click, and on every poll inside the wait-for-more-cards loop (latch only). Each checkpoint does this:
1. If a latch is set → stop.
2. Otherwise `read_signals` (cookie + one `SIGNALS_JS` evaluation) → `classify_page`.
3. `ok` → continue. `http_error` on the search document → `ScrapeError`, which is a keyword `error`, and the run continues. Any other kind → stop.
4. **Stop** = close the context FIRST, then raise `AccountSafetyStop(kind, url)`.

A stop is never retried and never pushed through. `li_at` disappearing mid-run is caught at the next checkpoint by rule 5.

### LinkedIn accounts and the SessionStore (Phase 5, migration `p8_liaccts_b2e5f7a9`)

**Why.** The global `linkedin_session` singleton, the cool-down derived from "the latest aborted run" and the daily caps were all global. They are now per **LinkedIn account** (`linkedin_accounts`, `data.md`), so a second login (a teammate, or a server login by pasted cookie) has its own status, cool-down and caps, and one account being stopped never blocks another. No `tenant_id` column yet (adding `workspace_id` with default 1 later is a cheap migration).

**Data-access layer.** `services/linkedin_accounts.py` is the only place that queries `linkedin_accounts`: list / add / rename / disable / enable, status transitions (`mark_connected|mark_not_connected|mark_needs_attention|mark_successful_run|note_error`), pins (proxy, user agent, locale, timezone, viewport), the encrypted `session_state`, and `paste_cookie`. `services/session.py` keeps the old names and the Setup-card facade; each takes an optional `account_id`. **`default_account_id(db)`** = the first non-disabled account by id; every entry point that is not told which account uses it (Run now, the scheduler, Connect, Re-check, the digest's single-account views). `linkedin_session` is **legacy**: the p8 migration copied it into the default account and nothing reads or writes it any more (the table is kept).

**Per-account state.** `scrape_runs.linkedin_account_id` stamps every run (`start_run(db, actor, linkedin_account_id=None)`; an unstamped queued run is stamped with the default account when the job starts). `runs.safety_cooldown_until(db, now, account_id)`, `runs._day_counts(...)`, `runs.posts_seen_today(...)` and `run_eligibility(db, now, account_id)` filter on that account (rows with a NULL account count for the default account, so pre-p8 history keeps counting). The runs stay authoritative; `runs.sync_account_state` mirrors the derived `runs_today`, `posts_today` and `cooldown_until` onto the account row at the end of each run, and `mark_needs_attention` records `safety_state`, `last_stop_at`, `last_stop_reason`. A disabled account fails eligibility with `account_disabled`. Only one browser job runs at a time across all accounts (the single `JobRunner`), so `run_in_progress` / `job_busy` stay global.

**SessionStore (`scraper/session_store.py`).** The scraper never touches the database: it receives an `AccountHandle(spec, save_state)` built by `linkedin_accounts.handle_for/build_handle`, and `open_account_context(settings, handle, headless=)` returns the browser context from the right store. A `None` handle is the legacy default folder profile.
- `FolderSessionStore` (default; today's behaviour): a persistent Chrome/Chromium profile under the data dir. The default account keeps the pre-p8 directories (`PROFILE_DIRNAMES`, keyed by mode and channel); any other folder account uses `<profile_dirname>-<mode>-<channel>` (`profile_dirname` = `linkedin-account-<id>`).
- `DbSessionStore`: Playwright `storage_state` (cookies + localStorage) kept **encrypted** in `linkedin_accounts.session_state` with `SALES0_SECRET_KEY` (`services/team/crypto.encrypt`, the Fernet helper that also protects Gmail tokens). The context is `browser.new_context(storage_state=…, proxy, user_agent, locale, timezone_id, viewport)` on a non-persistent browser; when it closes the refreshed state is saved back (encrypted) through `handle.save_state`. A state that cannot be decrypted (key changed) is treated as no session.
- Pins apply to both stores; a proxy is passed to `launch_persistent_context` (folder) or `new_context` (db). The proxy URL (it may hold credentials) and the cookie are never logged or returned by a view.

**Entry points** that take `linkedin_account_id` (default = `default_account_id`): `discovery_job.execute` (from the run's stamped account), `connect_job.execute`, `check_job.execute`, `scheduled_discovery.start_scheduled_discovery`, `session_check.check_session(settings, account)`, `connect.connect_interactive(..., account=)`, `LinkedInSearcher(settings, account=)`. Custom test doubles (`connect_fn`, `check_fn`, `searcher_factory`) keep their old call signatures.

**Adding accounts (Setup → LinkedIn accounts).** (a) **Chrome window** (folder backend, local): Connect opens the account's own profile. (b) **Paste `li_at` cookie** (db backend, the server fallback when Chrome cannot open): the form shows the warnings (the cookie is a login, anyone holding it acts as you; LinkedIn ToS risk; datacentre IPs are flagged faster; stored encrypted, `SALES0_SECRET_KEY` required on a non-loopback host). `paste_cookie` stores `{cookies:[li_at], origins:[]}` encrypted, leaves the account `not_connected`, and the Re-check job validates it with ONE feed load (`/feed/`, the fixture in tests); a connected result sets `connected`, a rejected cookie sets `not_connected` and the stored state is erased, and an expired cookie on a connected account becomes `needs_attention` (logout). `SALES0_LINKEDIN_SESSION_BACKEND` is the backend of NEW accounts created by "Add account" (the default account is folder). A **streamed remote-browser login** (noVNC / a screencast of a server-side Chrome) is documented future work and not built.

**Fixture LinkedIn.** Any non-empty `li_at` is a valid session in the dev-only fixture, except values starting `invalid`, `expired` or `revoked`, which it rejects (to test the failure path). A state saved by the DB backend after `/login?direct=1` is a valid session in a fresh context.

### Post parsing (`scraper/selectors.py` + `scraper/parsing.py`)

`EXTRACT_JS` is evaluated with `page.evaluate(EXTRACT_JS, EXTRACT_ARGS)`. It discovers the cards itself, and `EXTRACT_ARGS` is a JSON dict of the selector lists below. It returns one raw dict per card: `urn`, `author_name`, `author_profile_url`, `author_headline`, `posted_relative`, `text`, plus `via` (which fallback matched each field, logged at DEBUG). Class-based selectors survive only as the LAST fallback of a field (`legacy_*`). Every field has ordered fallbacks, and the first non-empty value wins. The scope is `main` when present, else `document`.

- **Cards.**
  1. Elements matching `CARD_ATTR_SELECTORS`: `[data-urn^="urn:li:activity:"]`, `[data-urn^="urn:li:ugcPost:"]`, `[data-urn^="urn:li:share:"]`, `[data-id^="urn:li:activity:"]`, `[data-id^="urn:li:ugcPost:"]`, `[data-id^="urn:li:share:"]`. Outermost only: a match nested inside another match is a reshared original and belongs to its outer card.
  2. Then, always also: every `POST_LINK_SELECTORS` anchor (`a[href*="/feed/update/urn:li:activity:"]`, `a[href*="/feed/update/urn:li:ugcPost:"]`, `a[href*="/feed/update/urn:li:share:"]`, `a[href*="/posts/"]`) that is not inside a card already found. Its card is `anchor.closest(CARD_CONTAINERS)`, where `CARD_CONTAINERS` = `li, article, [role="article"], [data-view-name*="update" i], [data-view-name*="search-entity" i]`. If there is no container, the anchor is skipped. Cards are de-duplicated by element.
- **URN**, in order:
  1. The card's `data-urn` / `data-id` (a valid prefix).
  2. The first descendant carrying one.
  3. An href matching `/feed/update/(urn:li:(activity|ugcPost|share):\d+)`.
  4. An href matching `/posts/[^/]*-(activity|ugcPost|share)-(\d+)` → `urn:li:<type>:<n>`.

  None of these → `ParseError`, counted.
- **Author anchor:** the first `a[href*="/in/"], a[href*="/company/"]` in card DOM order, outside any nested reshared card.
- **author_name**, in order: `[aria-hidden="true"]` text inside the author anchor; `span[dir]` inside it; the anchor's `aria-label`, cleaned of `View `, `’s profile` / `'s profile`, `, graphic`; the first line of the anchor's `innerText`; legacy `.update-components-actor__title span[aria-hidden="true"]`.
- **author_profile_url**, in order: the author anchor's `href`; legacy `a.update-components-actor__meta-link`.
- **author_headline**, in order: the first line of the author anchor's (or its parent's) `innerText` that is not the name, not a connection degree (`• 1st|2nd|3rd+`), not "Follow", and not a relative time; legacy `.update-components-actor__description span[aria-hidden="true"]`.
- **posted_relative**, in order: a `time` element's text or `datetime`; the first element before the text block whose own text or `aria-label` matches `RELATIVE_TIME_RE`; legacy `.update-components-actor__sub-description span[aria-hidden="true"]`.
- **text:** the candidate with the LONGEST `innerText` among `TEXT_SELECTORS` = `[data-test-id*="commentary" i]`, `[dir="ltr"]`, `span[dir]`, legacy `.update-components-text`. Candidates inside the author anchor or a nested reshared card are excluded. A tie goes to the earlier selector. None → `ParseError` "no post text".
- **"See more"** (clicked by `search.py`, paced, then a checkpoint), in order:
  1. `button[aria-label*="see more" i]`, `button[aria-label*="show more" i]`, `[role="button"][aria-label*="see more" i]`.
  2. A `button` or `[role="button"]` whose trimmed text matches `^(…|\.\.\.)?\s*(see|show)?\s*more$`i.
  3. Legacy `button.feed-shared-inline-show-more-text__see-more-less-toggle`.

  Never an `a[href]`, and never an element whose `aria-label` matches `actions|options|menu|comment|reaction`.
- **No-results marker:** legacy `.search-reusable-search-no-results`, OR any `h1, h2, h3, p` in scope whose trimmed text starts with "No results found" or equals "No results" (case-insensitive).
- **`RELATIVE_TIME_RE`:** `^\s*(\d+)\s*(s|m|min|mins|h|hr|hrs|d|w|wk|mo|y|yr|yrs)\b`, or `(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago`. A leading "Edited •" and trailing "•" are stripped. `relative_time.parse_relative` accepts both forms.
- **`post_from_raw`** (pure) is unchanged in contract. Missing optional fields stay None. `search.py` counts them into `field_misses`.

**Layout drift.** After the content wait (`wait_for_function` until `cards_found > 0`, or `no_results`, or the timeout) and an `ok` safety checkpoint, a drift check runs. `drift_warning = (posts parsed == 0) and (cards_found > 0 or not no_results)`. In words: a logged-in results page that has result containers, or that lacks a "no results" marker, but yields 0 posts. When it is true, it logs WARNING `scraper_layout_drift` with `keyword`, `final_path`, `cards_found`, `parse_errors`. The keyword row becomes `warning`, and the run-level alert shows (data flow steps 3 and 7). Partial field failures are counted in `field_misses` and surfaced the same way. Nothing is silent.

**Layout samples (q21, `scraper/layout_sample.py`).** A warning says THAT the markup moved; the sample says HOW. When a
keyword's scan ends with drift, an unreadable card, or a watched field missing on at least half of the posts
(`needs_sample`, the same rule as `keyword_outcome`), the searcher copies the still-loaded page (no extra LinkedIn
request): up to 3 result containers (winning strategy's cards, else SDUI list items, else `<main>`'s children), with
scripts, styles, SVG, images, media, iframes, form fields and comments removed, only structural attributes kept
(`data-view-name`, `data-testid`, `role`, `componentkey`, `aria-*`, `href` without query/fragment, `datetime`, `class`
cut to 120 chars, ...), every text node cut to 300 chars and the sample to 120 KB, plus page path, title and per-selector
counts. Nothing from cookies or storage is read. The local job stores it on `scrape_run_keywords.layout_sample_json`;
the laptop worker sends it in `KeywordResultMeta.layout_sample` (a malformed sample is dropped, never the keyword).
The run page shows "page sample kept" on those rows and **Download page samples (N)**: one HTML file served as an
attachment only (`text/plain`, `Content-Security-Policy: sandbox`, `nosniff`); the app never renders it.
`SALES0_SCRAPE_LAYOUT_SAMPLES=false` turns capture off.

### Saved page samples (`tests/fixtures/linkedin_pages/`, slice R2)

These are authored, realistic HTML files modelled on CURRENT LinkedIn. They use obfuscated, random-looking class names (e.g. `class="t8kq2 _p3z9x"`), and no element has a `global-nav` id or class. A test greps every logged-in sample for `global-nav` and requires 0 hits. There is no personal data: fictional names, `.example` emails, `+91 90000 0000x` phones. `manifest.json` lists one entry per case: `{file, url, status, cookie, expect: {kind, reason?, posts?, urns?, drift?, no_results?, cards_found?, fields?, field_misses?, soft_hints_nonempty?}}`.

| File | Served at (manifest `url`) | cookie | Expected |
|---|---|---|---|
| `feed_logged_in.html` | `https://www.linkedin.com/feed/` | yes | `ok`; `soft_hints` non-empty; no `global-nav` |
| `search_results_content.html` | `…/search/results/content/?keywords=ai%20workshop&…` | yes | `ok`; 6 posts: 2 `data-urn` activity, 1 `data-urn` ugcPost, 1 `data-id` activity, 1 anchor-only `/feed/update/urn:li:activity:…`, and 1 anchor-only `/posts/<slug>-activity-<n>-xxxx`. It also includes one reshare (nested card, not double-counted), one truncated post with a "…see more" button, and one post with no headline (`field_misses == {"author_headline": 1}`). The manifest pins author_name, headline, posted_relative and a text snippet per URN. |
| `search_results_empty.html` | `…/search/results/content/?keywords=zzqx…` | yes | `ok`; 0 posts; `no_results=true`; `drift=false` (text "No results found", no legacy class) |
| `login.html` | `https://www.linkedin.com/login` | no | `logout` (`path:/login`) |
| `login.html` | `https://www.linkedin.com/feed/` | yes | `logout` (`login_form`). This proves the DOM logged-out marker works when the URL looks fine. |
| `authwall.html` | `https://www.linkedin.com/authwall?trk=…` | yes | `logout` (`path:/authwall`) |
| `checkpoint_challenge.html` | `https://www.linkedin.com/checkpoint/challenge/AgF…` | yes | `checkpoint` |
| `captcha_challenge.html` | `https://www.linkedin.com/checkpoint/challenge/AgG…` (arkoselabs iframe) | yes | `captcha` |
| `search_results_drifted.html` | `…/search/results/content/?keywords=ai%20workshop&…` | yes | `ok`; result containers (`ul[role="list"] > li` with text but no URN attributes and no post links); 0 posts; `drift=true` |

| `search_results_sdui.html` (Phase 1.2) | `…/search/results/content/?keywords=ai%20workshop&…` | yes | `ok`; `strategy=sdui`; exactly 6 posts; refs `ck:<synthetic KEY>` ×6 in DOM order; `drift=false`; every `link_kind=profile` (no permalink capture on a static sample). Pinned per ref: author_name, author_profile_url, author_headline, posted_relative, `text_contains`. Built and sanitised per `## LinkedIn SDUI search results` → Saved SDUI sample. |

The table also contains three status-only manifest rows that need no file: status 429 → `rate_limited`, status 999 → `rate_limited`, and status 500 on `/feed/` with a cookie → `http_error`.

Phase 1.2 manifest schema additions (all optional, read by `test_saved_pages.py`): `expect.strategy`, `expect.refs` (source refs in DOM order; `urns` stays for the legacy samples and means the same), `expect.link_kinds` (ref → kind), and `fields` keyed by ref. A top-level `synthetic` block on the SDUI case lists the allowed names, `/in/` + `/company/` slugs and componentkey KEYs (used by the sanitisation test).

### `python -m sales0.cli check-browser` (slice R1; real local smoke, part of the gate)

1. `resolve_browser(get_settings())`.
2. In a **throwaway** `tempfile.TemporaryDirectory()` profile, never `data/` and never the sales0 profile, launch `launch_persistent_context` HEADLESS with `launch_options(...)`.
3. Open **`about:blank` only**. It never navigates anywhere else and never contacts LinkedIn.
4. Read `Browser.getVersion` over a CDP session (`product`, e.g. `Chrome/156.0.7311.42`), `navigator.userAgent` and `navigator.webdriver`. Close the context and delete the temp dir.

The whole command runs under a hard 60 s timeout. It is a watchdog thread; when it fires, it kills the browser process group and exits.
- **Success** (stdout, exit 0), exactly one line: `browser OK channel=<chrome|chromium> version=<version>`. On an auto fallback, the same line is followed by ` (Google Chrome not found — using the built-in browser)`.
- **Failure** (stderr, exit 1): `browser ERROR type=<chrome-not-found|launch-failed|timeout|automation-tell> <message>`. `automation-tell` = `navigator.webdriver` is true, or the UA contains `HeadlessChrome`.

Logs go to stderr, so stdout carries only the result line, as with `check-llm`. On this Mac the gate expects `browser OK channel=chrome version=156.`.

## LinkedIn SDUI search results (Phase 1.2)

Binding for slices S2 (parse), S3 (permalink + search), S4 (fixture), S5 (services). It extends `## LinkedIn browser` → Post parsing; everything there not contradicted here stands (field fallbacks, "see more", no-results marker, drift rule, safety checkpoints).

### DOM contract (all in `scraper/selectors.py`; never a class name)

| Constant | Value | Use |
|---|---|---|
| `SDUI_POST` | `[data-view-name="feed-full-update"]` | One post (6 on the capture). Outermost only. |
| `SDUI_LISTITEM` | `[role="listitem"][componentkey]` | The post's wrapper; also the drift container count. |
| `SDUI_LISTITEM_KEY_RE` | `^expanded(.+?)FeedType_[A-Za-z_]+$` | KEY from the wrapper's `componentkey` (`expanded<KEY>FeedType_FLAGSHIP_SEARCH`). |
| `SDUI_KEY_ATTR` | `componentkey` | Inner `div[componentkey="<KEY>"]` (preferred source of KEY). |
| `SDUI_ACTOR` | `[data-view-name="feed-actor"]` | Author block (2 per post: avatar link + name/meta). |
| `SDUI_TEXT` | `[data-view-name="feed-commentary"]`, `[data-testid="expandable-text-box"]` | Post text, in order. |
| `SDUI_EXPAND` | `[data-testid="expandable-text-button"]` | "…more"; becomes the FIRST entry of `SEE_MORE_ARIA_SELECTORS`' chain (clicked by the existing paced `_expand`). |
| `SDUI_TEXT_EXCLUDE` | `[data-view-name="feed-update-image"]`, `[data-view-name="feed-article"]`, `[data-view-name="feed-document-container"]` | Text candidates inside these are ignored (article titles, document captions). |
| `SDUI_CONTROL_MENU` | `[data-view-name="feed-control-menu"]` | The post's "…" menu (trigger + items). |
| `COPY_LINK_TEXT` | `copy link to post` | Exact menu item text (trimmed, case-insensitive). |
| `SDUI_SCROLL_CONTAINER` | `[data-testid="lazy-column"]` | Infinite-scroll column; the mouse is moved over it before each wheel. |
| `SDUI_SCREEN` | `[data-sdui-screen*="SearchResultsContent"]` | Informational; logged as `sdui_screen` in `scraper_search_done`, never a verdict. |

### Card discovery (ONE ordered strategy, in `CARDS_PRELUDE_JS`)

1. **`sdui`**: every `SDUI_POST` in scope, outermost only. If ≥ 1 is found, this strategy wins for the page.
2. **`urn`**: otherwise the Phase 1.1 discovery unchanged (URN attributes, then post links).
3. **`none`**: neither found. `cards_found` is then the number of `SDUI_LISTITEM` elements in scope (so "listitems present but no posts" is drift). Otherwise `cards_found` = posts found by the winning strategy.

`__cards(sel)` returns `{strategy, cards}`; `COUNT_CARDS_JS`, `SIGNALS_JS` (`cards_found`) and `CONTENT_READY_JS` use the same function, so "cards found" still means one thing everywhere. The strategy is logged per keyword (`scrape_keyword.strategy`).

### SDUI field extraction (raw dict per post)

`EXTRACT_JS` returns, per card, `{strategy, urn, component_key, post_href, author_name, author_profile_url, author_headline, posted_relative, text, has_control_menu, via}` (contract S2 → S3; the URN strategy fills `component_key=null`, `has_control_menu=false`).

- **component_key**: the first descendant-or-self `[componentkey]` whose value does NOT match `SDUI_LISTITEM_KEY_RE` (the inner key), else the KEY captured from the closest ancestor `SDUI_LISTITEM`. Trimmed; must be non-empty, contain no whitespace, and be ≤ 197 characters (so `ck:<KEY>` fits `source_ref` String(200)); otherwise it is treated as absent.
- **urn**: any `urn:li:(activity|ugcPost|share):\d+` found in an attribute or href inside the post (best effort; absent on the capture).
- **Actor lines**: the `innerText` lines of every `SDUI_ACTOR` in the post (DOM order), excluding actors inside `SDUI_TEXT` or `SDUI_TEXT_EXCLUDE`; consecutive duplicate lines collapsed.
  - **author anchor**: the first `a[href*="/in/"], a[href*="/company/"]` inside those actors → `author_profile_url`.
  - **author_name**: the first actor line that is not skipped by the Phase 1.1 headline-skip rules (degree `• 3rd+`, Follow, Premium/Verified, `View … profile`, a relative time).
  - **author_headline**: the next such line that does not contain the name.
  - **posted_relative**: the first actor line (or `aria-label`) whose head passes `isRel` (accepts `now`, `24m`, `3h`, `2d`, `1w`, `3mo`, `1yr`, the long form, and `• Edited` suffixes).
- **text**: the first `SDUI_TEXT` match with non-empty text outside `SDUI_TEXT_EXCLUDE`; its `innerText` minus a trailing occurrence of the `SDUI_EXPAND` element's text (`…more`) and a trailing `…`.
- **has_control_menu**: the post contains `SDUI_CONTROL_MENU`.
- `via` records `strategy=sdui` and which rule filled each field (DEBUG `scraper_parse_via`, as today).

`post_from_raw(raw, base, today, search_url=None)` (pure) and `parse_cards(raws, base, today, cap=None, search_url=None)` (pure; `ParsedCards` gains `strategy`):
- **source_ref** = the URN when `urn` is valid, else `urn_from_href(post_href)`, else `urn_from_href(raw["copied_url"])` (set by permalink capture), else `"ck:" + component_key`. None of these → `ParseError` (counted in `parse_errors`).
- **Empty text with a ref** → not a post; counted as `field_misses["text"]` (Phase 1.2: no longer a parse error, for both strategies).
- **url / link_kind**, first that applies: a URN → `permalink(base, urn)`, `post`; else `canonical_post_url(copied_url, base)` returns a URL (same-site `/posts/…` or `/feed/update/…`, query/fragment dropped) → that URL, `post`; `author_profile_url` → `profile`; `search_url` → `search`; else `ParseError`.
- `posted_date = parse_relative(normalize_relative(posted_relative), today)` (`relative_time`; the estimate is a date, stored in `enquiries.date_posted`).
- `parse_cards` de-duplicates by `source_ref` within the page (a re-rendered post is one post).

`scraper/urls.py` adds PURE helpers shared by the scraper and services (one place): `canonical_post_url` (Permalink capture step 4), and:
- `link_kind_of(url) -> LinkKind | None`: None for empty; path (fixture prefix stripped) containing `/feed/update/` or starting `/posts/` → `post`; starting `/in/` or `/company/` → `profile`; starting `/search/` → `search`; else `other`.
- `profile_slug(url) -> str | None`: `"in/<slug>"` or `"company/<slug>"` from the path, URL-decoded and lower-cased; None otherwise.

### Identity and dedupe (`services/dedupe.upsert_post`, no new column)

> **Assumed:** `componentkey` is stable across loads of the same search, but this is NOT verified (one capture). Identity therefore never relies on it alone.

Match order (first hit wins):
1. **Same source ref**: an `enquiry_sources.source_ref == post.source_ref` → `dupe_urn` (the kind name is kept; it now means "same ref", URN or `ck:`).
2. **Same content**: `enquiries.content_hash == content_hash(post.text)` (Phase 1 normalisation, unchanged) → `dupe_content`, plus a new source row. This is what merges the same post seen under two keywords or two runs when its key changed, and P7 ≈ P2.
3. **Same-author fingerprint** (new; covers a re-rendered post whose text was captured truncated once and expanded once, so the full hashes differ): candidates = enquiries whose `author_profile_url` contains `/<profile_slug(post.author_profile_url)>` (bound parameter), filtered in Python by `near_duplicate(...)` → `dupe_content`, plus a new source row. `near_duplicate`: both `profile_slug`s non-null and equal; `a = normalize_text(strip_ellipsis(text))[:300]` for each (`strip_ellipsis` drops a trailing `…` / `...` and a trailing "…more"); with `n = min(len(a), len(b))`, `n ≥ 100` and `a[:n] == b[:n]`.
4. Otherwise **new** (`content_hash` of the full text, as today; the empty-text guard uses `source_ref`).

Rules on every duplicate:
- **Alias**: if `post.urn` is set and no source row has it, insert a source row for the URN (a later run that finds the URN but a different key then matches at step 1).
- **Link upgrade**: if `post.link_kind == "post"` and `link_kind_of(enquiry.source_url) != "post"`, set `enquiries.source_url = post.url`.
- `raw_text`, author fields and `content_hash` of an existing enquiry are never rewritten.

Stored values: `enquiries.source_url = post.url` (permalink, else author profile, else search URL). `enquiry_sources.source_url` = the permalink when `link_kind == "post"`, else `post.search_url` (where it was seen).

> **Assumed:** the existing full-text hash already covers "same post, re-rendered" whenever the text is read fully, so the fingerprint is computed on read from existing columns (`author_profile_url`, `raw_text`) rather than stored. Thresholds (300 chars, 100-char minimum overlap) are chosen so that a LinkedIn "…more" truncation (~200 chars) of a long post still matches its expanded form.

### Permalink capture (`scraper/permalink.py`, called by `search.py`)

Runs per keyword AFTER "see more" expansion and AFTER `EXTRACT_JS`, so it can never change what was parsed. Eligible posts: the first `cap_posts` raws (DOM order) that have no URN, a `component_key` and `has_control_menu`. At most `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD` attempts (0 = skip entirely; no hook installed, no clicks).

1. **Hook (once per results page, `page.evaluate(CLIPBOARD_HOOK_JS)`)**: idempotent; sets `window.__sales0_copied = []` and replaces `navigator.clipboard` via `Object.defineProperty(navigator, "clipboard", {configurable: true, value: {...}})` with an object whose `writeText(t)` pushes `String(t)` and resolves, and whose `write()` resolves without effect. **It never calls the original**, so the system clipboard is never touched. It works even where `navigator.clipboard` is undefined.
2. Per eligible post (locator: `[componentkey="<KEY>"]` → its `SDUI_POST`; fallback: the n-th `SDUI_POST`):
   1. `pacer.delay()` (the normal action delay), then click the menu trigger: the first `button` / `[role="button"]` inside `SDUI_CONTROL_MENU` (or the element itself if it is one). Checkpoint.
   2. `pacer.short()` (new: uniform in `[delay_min_s/4, delay_max_s/4]`), then wait ≤ 2 s for a visible element whose trimmed text equals `COPY_LINK_TEXT`: first inside `SDUI_CONTROL_MENU`, else inside a visible document-level `[role="menu"]`. Click the innermost such element. **No other menu item is ever clicked.** Checkpoint.
   3. Poll `TAKE_COPIED_JS` (returns and clears the list) for ≤ 1.5 s. `pacer.short()`, press `Escape`. Checkpoint.
   4. A safety stop at any checkpoint propagates (`AccountSafetyStop`, browser closed first). If the main-frame URL path is no longer the search path, capture stops for this keyword (the posts are already extracted).
3. Outcome per post: `copied_url` (the last captured string) or a miss (`no_menu_item`, `no_copy`, `click_error`), logged DEBUG `scraper_permalink` with `keyword`, `index`, `outcome` (never the post text). **3 consecutive misses end capture for that keyword.**
4. `scraper/urls.canonical_post_url(copied, base) -> (urn | None, url | None)` (pure; in `urls.py` because `post_from_raw` uses it): non-same-site → `(None, None)`; URN via `urn_from_href` → `(urn, permalink(base, urn))`; same-site `/posts/` or `/feed/update/` path → `(None, url without query/fragment)`; else `(None, None)`. Tracking query strings (`utm_*`, `rcm`) are therefore never stored.

`capture_permalinks(page, raws, *, cap, pacer, checkpoint) -> PermalinkStats(attempts, found)` sets `raw["copied_url"]` on each hit (it never touches other keys); `search.py` then calls `parse_cards(..., search_url=url)`, and fills `strategy`, `permalink_attempts`, `permalinks_found` on `KeywordScrapeResult`. Before each scroll wheel it moves the mouse to the centre of `SDUI_SCROLL_CONTAINER` when present (paced; the existing wheel and growth wait are otherwise unchanged).

> **Assumed:** live LinkedIn's "Copy link to post" calls `navigator.clipboard.writeText` at click time (the capture showed the item, not its handler). If it does not, every attempt misses, 3 misses end capture, and cards fall back to the author profile with no other effect. Live cost: ~1 normal + 2 short delays per post (≈ 9 s at the defaults, ≈ 2–3 min for 15 posts).

### Drift and field misses (extends Layout drift; existing rules kept)

- `drift_warning` keeps its formula; with SDUI `cards_found` counts listitems when no post was found, so a page with `SDUI_LISTITEM`s but 0 `SDUI_POST` parsed → drift.
- `services.runs.keyword_outcome`: `FIELD_LABELS` gains `"text": "post text"` (listed first). Its half rule for `text` uses the denominator `len(posts) + field_misses["text"]` (≥ 2); the other fields keep `len(posts)`. Example: "Parsed 4 posts, but couldn't read post text on 3 — LinkedIn layout may have changed".

### Saved SDUI sample (`tests/fixtures/linkedin_pages/search_results_sdui.html`)

Built ONCE from the raw capture at `/private/tmp/claude-501/-Users-sai-Workspace-Code-sales0/b074bc78-7197-4abe-843f-5eea90d4cf2c/scratchpad/capture/search.html`. **The raw file contains real people's public posts and is never copied into the repo, never committed, never quoted in a test, log or commit message.** Any helper script used to sanitise it lives in the session scratchpad, not the repo.

- **Keep** the exact element nesting and every `data-view-name`, `data-testid`, `role`, `componentkey` (shape), `data-sdui-screen`, `aria-*`, `dir`, `hidden`, `type` and the obfuscated `class` tokens of the 6 posts and their wrappers (the lazy column, the listitems, the control menus with "Copy link to post", media containers, social buttons). Unrelated page chrome may be cut down to `main`/landmark skeletons.
- **Replace** every name, `/in/` and `/company/` slug, headline, post text, alt text, `aria-label` containing a name, image `src`/`srcset` (→ `data:` placeholder or `https://example.invalid/img/<n>.png`), and every `componentkey` KEY (→ synthetic KEYs of the same length and character class) with synthetic values. Post texts are synthetic training-enquiry texts with `.example` emails and `+91 90000 0000x` phones. Relative times keep the capture's mix (`24m`, `3h`, `2d`, `1w`, one `• Edited`).
- **Strip** every `<script>`, `<noscript>`, `<iframe>`, `<link>` to LinkedIn hosts, inline event handlers, and every attribute outside the allowlist `{class, id, role, componentkey, href, src, alt, dir, hidden, type, tabindex, aria-*, data-view-name, data-testid, data-sdui-screen}`. hrefs other than `/in/`, `/company/` and `/search/` become `#`.
- **Sanitisation test** (`tests/unit/scraper/test_sdui_sample_sanitized.py`, pure, reads the file as text + `html.parser`):
  - every `/in/<slug>` and `/company/<slug>` is in the manifest `synthetic.slugs` list;
  - every pinned `author_name` in the manifest is in `synthetic.names`, and every `feed-actor` text line that is not a headline, degree, time or "Follow" is a synthetic name;
  - every `aria-label` and `alt` value is in `synthetic.labels` (the generic UI labels, e.g. "Like", "Open control menu for post by {name}", with `{name}` expanded over `synthetic.names`);
  - every `componentkey` value is `<KEY>` or `expanded<KEY>FeedType_FLAGSHIP_SEARCH` with KEY in `synthetic.keys`;
  - no `<script`, `<iframe`, `licdn.com`, `urn:li:member`, `urn:li:fsd_profile`, `ACoA` (member id prefix), `trackingId`, `trk=`, or `@` outside `.example` addresses;
  - no attribute outside the allowlist; exactly 6 `[data-view-name="feed-full-update"]`; at least one `[data-view-name="feed-control-menu"]` containing "Copy link to post"; file < 300 KB.

## Fixture LinkedIn corpus

**Purpose.** Fixture mode lets the SAME `LinkedInSearcher` and Playwright code run offline, for tests and demos. `scraper/urls.base_url(settings)` returns `https://www.linkedin.com` in live mode and `http://{host}:{port}/__fixtures/linkedin` in fixture mode. Every path below is relative to that base and is identical in shape to LinkedIn's.

**Fixture routes** (slice C, `linkedin_fixture/routes.py`; mounted only when mode=fixture; Phase 1.1 changes owned by R3):
- `GET /login` (Phase 1.1: a 2FA flow, so Connect's cookie + URL rule is exercised):
  - With a valid `li_at` → 302 to `/feed/` (already logged in).
  - Otherwise it renders a login page: `input#username`, `form[action*="login-submit"]`, obfuscated classes. A script on that page navigates after 300 ms to `/checkpoint/challenge/verify` (the "user submitted the form" step).
  - `GET /login?direct=1` keeps Phase 1's behaviour: it sets the cookie and 302s to `/feed/`. Test setup helpers use it.
- `GET /checkpoint/challenge/verify` (Phase 1.1): a 2FA code page. No cookie is set yet. After 600 ms its script goes to `/checkpoint/challenge/verify-submit`.
- `GET /checkpoint/challenge/verify-submit` (Phase 1.1): sets cookie `li_at=fixture-session` (path `/`, httponly, `max_age` 1 year) and 302s to `/checkpoint/challenge/remember-browser`.
- `GET /checkpoint/challenge/remember-browser` (Phase 1.1): a "Remember this browser?" page. The cookie is already set, but the path is a checkpoint, so Connect must keep waiting. After 600 ms its script goes to `/feed/`.
- `GET /feed/`: with `li_at`, it renders a feed page in **current LinkedIn markup** (Phase 1.1: obfuscated class names, `main` landmark, `nav[aria-label="Primary"]`, a self-profile `a[href*="/in/"]`, and **no** `global-nav` id or class anywhere). Without the cookie it 302s to `/authwall`.
- `GET /authwall`: a guest/login-wall page with `.authwall` and `form[action*="login"]`, and no `global-nav`.
- `GET /search/results/content/?keywords=K&...`: without `li_at` → 302 to `/authwall`. When K (lower-cased, trimmed) is `checkpoint-test` → 302 to `/checkpoint/challenge/`. **`logout-test` (Phase 1.1)** → 302 to `/authwall`. This simulates an expired session mid-run: the cookie is still present, and the path classifies as `logout`. When K is `captcha-test` → renders a page containing `iframe[src*="captcha"]`. When K is `ratelimit-test` → HTTP 429. When K is `ratelimit-scroll-test` → the results page renders normally, but a background XHR/fetch it issues returns HTTP 429. When K is `checkpoint-scroll-test` → the results page lazy-loads more cards on the first scroll, and the page load triggered by the second scroll redirects to the checkpoint, to exercise the per-scroll checkpoint. When K is `lazy-scroll-test` → the `ai workshop` posts are served lazily: one more card is revealed ~700ms after each scroll (longer than a fixed short post-scroll wait), to exercise the wait-for-growth logic. When K is `layout-drift-test` → a logged-in results page with neither parseable result cards nor the no-results marker, so the searcher sets `drift_warning=True`. Trigger keywords are defined in `linkedin_fixture/corpus.py` (`LOGOUT_KEYWORD = "logout-test"` is added in Phase 1.1). Otherwise it renders every post whose `keywords` contains K, in corpus order. Zero matches renders a "No results found" heading. Phase 1.1: there is no `.search-reusable-search-no-results` class; the text marker alone must be detected. `layout-drift-test` renders `main > ul[role="list"] > li` items with text but no URN attributes and no post links.
- `GET /checkpoint/challenge/`: a security-check page.

**Fixture markup (Phase 1.1, binding: it proves detection and parsing do not depend on class names).** Every fixture template uses obfuscated, deterministic class names, e.g. `class="k3v9q _m1x7"`. None of the old `update-components-*`, `feed-shared-*`, `global-nav*` or `search-reusable-*` classes appear. A unit test (R3) greps the rendered pages for them. The corpus posts rotate through the card shapes that `## LinkedIn browser` → Post parsing must handle, by corpus position:

| Posts | Card shape |
|---|---|
| P1, P2, P3 | `div[data-urn="urn:li:activity:…"]` |
| P5, P6, P7 | `li > div[data-id="urn:li:activity:…"]` |
| P8, P9 | `li` with no URN attribute, URN only in `a[href*="/feed/update/urn:li:activity:…/"]` |
| P10, P11 | `article` with no URN attribute, URN only in `a[href*="/posts/<slug>-activity-<id>-AbCd/"]` |

- **Author block:** `a[href*="/in/<profile_slug>/"]` containing `span[aria-hidden="true"]` (the name), then a line with the headline, then a `span` with `2d •`-style relative time. There are no actor classes.
- **Text:** `div[dir="ltr"]` (or `span[dir="ltr"]`).
- **"See more":** posts longer than `SEE_MORE_AT` render a `button[aria-label="…see more, visually reveals content which is already detected by screen readers"]` that reveals the tail.
- **Counts:** the corpus, URNs and expected counts are unchanged, so Phase 1's run expectations (seen 9 / new 7 / dupes 2; then 11 / 2 / 9) still hold, now through the fallback paths.
- **Permalink:** built as `{base}/feed/update/{urn}/`.
- **After each scroll,** the searcher waits up to about 2.5 s (1.2 s in fixture mode) for `cards_found` to grow before it decides the page is exhausted.

**Fixture markup (Phase 1.2, binding; supersedes the card-shape table above for every keyword except `legacy-urn-test`).** Results pages mirror the SDUI capture (`## LinkedIn SDUI search results`), still with obfuscated deterministic classes and still with no legacy classes / `global-nav`:

```html
<main><div data-sdui-screen="com.linkedin.sdui.flagshipnav.search.SearchResultsContent">
 <div data-testid="lazy-column">
  <div role="listitem" componentkey="expanded{KEY}FeedType_FLAGSHIP_SEARCH">
   <div componentkey="{KEY}">
    <div data-view-name="feed-full-update">
     <h2><span class="{visually-hidden token}">Feed post</span></h2>
     <div data-view-name="feed-actor"><a href="{base}/in/{slug}/" aria-label="View {name}’s profile"><img alt="" src="data:,"></a></div>
     <div data-view-name="feed-actor"><a href="{base}/in/{slug}/"><span>{name}</span></a><span>• 3rd+</span>
       <p>{headline}</p><p><span>{posted_relative} • </span></p><button type="button">Follow</button></div>
     {control menu — see copy_link}
     <div data-view-name="feed-commentary"><span data-testid="expandable-text-box">{text_head}<span hidden>{text_tail}</span></span>
       <button type="button" data-testid="expandable-text-button">…more</button></div>   <!-- only when text_tail -->
     <div><button aria-label="React Like">Like</button><button>Comment</button><button>Repost</button><button>Send</button></div>
    </div></div></div>
 </div></div></main>
```

- `/company/{slug}/` for company authors (P11). No URN, no `data-urn`/`data-id`, no `/feed/update/` or `/posts/` href appears anywhere in the DOM (a unit test greps the rendered pages for `urn:li:`, `/feed/update/`, `/posts/` and requires 0 hits).
- **KEY** = `posts.json` `component_key` (synthetic, opaque, e.g. `sxQ3v9LmP1aZ0k`). Exception: P1's KEY is `component_key + "-" + <keyword slug>`, so it differs between its two keywords (simulates an unstable key; P1 must still merge by content).
- **Control menu** by `posts.json` `copy_link`:
  - `urn` (P2, P5, P8, P10): `<div data-view-name="feed-control-menu"><button type="button" aria-label="Open control menu for post by {name}" aria-expanded="false">…</button><div role="menu" hidden>` with `role="menuitem"` items "Save", "Copy link to post", "Not interested", "Unfollow {name}", "Report post"`</div></div>`. The trigger unhides the menu; "Copy link to post" calls `navigator.clipboard.writeText("{base}/posts/{posts_slug}?utm_source=share&utm_medium=member_desktop&rcm=ACoAAFixture")`, hides the menu and shows a toast "Link copied to clipboard.".
  - `noop` (P1, P6, P9): the same menu, but "Copy link to post" only hides the menu (nothing is written).
  - `none` (P3, P7, P11): no `feed-control-menu` in the post.
  - Every menu-item click is appended to `window.__fixture_menu_clicks` (item text) and every trigger click increments `window.__fixture_menu_opens`, so tests can assert that only "Copy link to post" is ever clicked and that the per-keyword cap holds.
- `FixturePost` gains `component_key`, `copy_link`, `key_for(keyword) -> str` and `possible_refs() -> tuple[str, ...]` (the URN when `copy_link == "urn"`, plus `ck:` + every `key_for(k)` over its keywords), used by e2e to locate cards.
- **`legacy-urn-test`** (new trigger keyword, `LEGACY_KEYWORD`): renders ALL corpus posts in their Phase 1.1 `CARD_SHAPES` (data-urn / data-id / update-link / posts-link), proving the URN fallback strategy: `strategy=urn`, 10 posts, refs = their URNs, 0 permalink attempts.
- **`permalink-miss-test`** (new trigger keyword, `PERMALINK_MISS_KEYWORD`): renders P1, P6, P9, P2 in SDUI markup with every menu forced to `noop` → exactly 3 menu opens (3-consecutive-miss stop), 4 posts, all `ck:` refs.
- **`layout-drift-test`** now renders 3 `SDUI_LISTITEM`s (`componentkey="expanded…FeedType_FLAGSHIP_SEARCH"`) with text but no `feed-full-update` → `strategy=none`, `cards_found=3`, `drift=true`.
- Lazy keywords (`lazy-scroll-test`, `checkpoint-scroll-test`, `ratelimit-scroll-test`) render SDUI posts; revealed cards are appended as new listitems to the lazy column.
- "No results found" page unchanged.

**Run expectations (Phase 1.2) still hold without any DOM URN:** run 1 — P1 (`ck:` key A, new), P2 (URN from copy link, new), P3 (`ck:`), P1 again under `ai workshop` (`ck:` key B → content hash → `dupe_content`), P5 (URN), P6 (`ck:`), P7 (`ck:` → content hash with P2 → `dupe_content`), P8 (URN), P9 (`ck:`): seen 9 / new 7 / dupes 2 / triaged 7; P1 has 2 source rows (`ck:` A, B) and both chips; P2 has 2 source rows (its URN + `ck:` P7). Run 2 — P10 (URN) and P11 (`ck:`) new, all 9 others match by ref: 11 / 2 / 9 / 2.

**Safety checkpoints:** `## LinkedIn browser` → Mid-run safety.

> **Assumed:** the selectors model LinkedIn's DOM as of October 2026 as best known. Live LinkedIn changes markup often, so drift is made visible rather than prevented: a keyword `warning` plus the run-level alert. Fixing selectors means changing `selectors.py` (and the fixture templates or saved samples, if the shape is new).

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

P1 appears under two keywords with the same URN (URN duplicate). P7 is the same text as P2 under a new URN (content duplicate). Phase 1.2: the URNs stay the corpus identity, but the SDUI DOM never shows them; P1 then merges by content (its `ck:` key differs per keyword) and P7 by content (Fixture markup (Phase 1.2) → Run expectations).

**Deterministic run expectations** (keywords added in this order: `corporate ai training`, `ai workshop`, `ai consulting`):
- Run 1: `posts_seen=9` (3+4+2), `posts_new=7` (P1,P2,P3,P5,P6,P8,P9), `posts_dupe=2` (P1 again, P7), `enquiries_triaged=7`. Enquiry P1 has keyword chips `corporate ai training` and `ai workshop`. Enquiry P2 has `corporate ai training` and `ai workshop` (via P7), and two `enquiry_sources` rows.
- Then add `claude code training` and run again. It runs first, because it has never run. Run 2: `posts_seen=11`, `posts_new=2` (P10, P11), `posts_dupe=9`, `enquiries_triaged=2`.

## Security & Safety

- Secrets live only in `.env`, read through `SecretStr`. Logs record `api_key_present` only. `.env` and `data/` are gitignored before they are first created.
- LinkedIn safety (see `capabilities/linkedin-discovery.md`): caps, human-like pacing, a stop on any safety page, no retries after a stop, and no automatic reconnect.
- CSRF posture (Phase 1): a middleware checks every unsafe request (POST/PUT/PATCH/DELETE). If an `Origin` header is present it must match an allowed origin; otherwise a present `Referer` must. The allowed origins are: the configured `SALES0_HOST:SALES0_PORT`; the loopback aliases `localhost`, `127.0.0.1` and `[::1]` on the configured or the served port; and the request's own `Host`, but only when that Host is itself loopback or the configured host. The request's Host is never trusted on its own, which defends against DNS rebinding. A rejected htmx request gets the toast envelope (HTTP 200, per the htmx error convention above). Any other rejected request gets HTTP 403 with code `csrf_rejected` (`api.md`). A request with neither header is allowed (non-browser clients such as curl and tests). In fixture mode, the `/__fixtures/` paths are exempt. Phase 2's shared login adds session-bound CSRF tokens on top of this check.
- Fixture routes are never mounted in live mode (a test asserts `/__fixtures/linkedin/feed/` returns 404 in live mode).
- All LLM output is untrusted. It is schema-validated and guarded (`agent.md`) and rendered with Jinja autoescape. Raw post text is escaped and never rendered as HTML.

## Portability to a VPS (later)

The same command runs on any Linux host after `uv sync && uv run playwright install --with-deps chromium`. With `SALES0_LINKEDIN_BROWSER_CHANNEL=auto`, sales0 uses Google Chrome when `google-chrome-stable` is installed (`/opt/google/chrome/chrome`), and otherwise the bundled Chromium with the Setup note. The live headed **Connect** needs a display. On a VPS, either run it under `xvfb-run` and reach it through a VNC/noVNC tunnel, or log in on the laptop and copy the profile directory for the channel in use (`data/linkedin-chrome-profile/` for Chrome). Copying works only between the same browser channel. This is documented in the README in Phase 2, together with the shared login. A Postgres switch is a URL change (see Stack).


## Thorough discovery (Phase 1.4)

Binding for slices A–G (`roadmap.md` → Phase 1.4). Each slice codes against the signatures below; they are the only coupling.

### Funnel and honesty

```
scanned (cards parsed, repeats included)
  └─ already seen (seen_posts hit: source_ref | fingerprint | content_hash | same-author prefix)  → no LLM, no new row
  └─ new (never seen) → registered in seen_posts → triaged once (Haiku)
        ├─ genuine  → enquiry created (To review) → permalink captured → profile.relevant_count+1
        ├─ discard  → stays in seen_posts only (Skipped by AI tab; raw text kept forever by default; opt-in purge)
        └─ failed   → seen_posts.triage_status=failed (retry from Skipped tab); no enquiry
```
A normal run must scan ≥ 200 posts with the defaults, and the summary states scanned vs relevant honestly (never "found 200 enquiries").

### Slice A — schema and config
`db/models/seen_posts.py::SeenPost` and `db/models/profiles.py::LinkedinProfile` map `data.md` → Phase 1.4 exactly (column names = field names; `enums.py` gains `SEEN_TRIAGE_STATUSES`, `PROFILE_KINDS`). Existing models gain the listed columns. `Settings` gains (names binding): `scrape_no_growth_scrolls: int=2 (ge 1)`, `scrape_sort: Literal["latest","relevance"]="latest"`, `scrape_date_filters: tuple[str,...]=("past-week","past-month")` (parsed from the comma list; each in `DATE_FILTERS`; the old `scrape_date_filter` is kept as a read-only property returning the first), `scrape_expand_max_per_keyword: int=40 (ge 0)`, `triage_concurrency: int=4 (1..8)`, `seen_raw_retention_days: int=14 (ge 0)`, `seed_suggested_keywords: bool=True`; the changed defaults of `architecture.md` → Configuration. `.env.example` lists them. Slice A also owns `services/keyword_library.py` (the 60 phrases of `search-setup.md`, `key` = slug of the text; a unit test asserts 60 unique keys/texts, 15 per group, 2–100 chars each). `tests/conftest.py` sets `SALES0_SEED_SUGGESTED_KEYWORDS=false`, `SALES0_TRIAGE_CONCURRENCY=4` and, as before, tiny pacing. The migration follows `data.md` → Backfill and the Migrations rules, and `tests/integration/db/test_migration_0003.py` upgrades a POPULATED 0002 DB (P1 accepted, P2/P3 AI-discard pending, a human-discarded AI-discard, a failed-triage one, one with two sources) and asserts: row counts and FK values survive, moved/kept sets exactly as `data.md`, `llm_calls` kept with `seen_post_id`, profile counters, and that the migration's frozen fingerprint equals `services.seen_posts.fingerprint` for the same input.

### Slice B — services (all take `db` first; flush, never commit; clock via `now`)
```python
# services/profiles.py
def touch_profile(db, *, profile_url: str | None, name: str | None, headline: str | None, now=None) -> int | None
#   upsert by profile_slug(profile_url); +1 posts_seen; last_seen_at=now; fills display_name/headline when non-null; None if the URL has no /in|/company slug
def mark_relevant(db, profile_id: int | None, n: int = 1) -> None        # relevant_count += n
def get_profile(db, slug: str) -> ProfileView | None
# services/seen_posts.py
def fingerprint(author_slug: str | None, text: str) -> str               # sha256((author_slug or "") + "\n" + normalize_text(text))
def snippet_of(text: str) -> str                                         # one line, <= 240 chars
@dataclass(frozen=True)
class Sighting: kind: Literal["new","seen"]; seen_post_id: int; profile_id: int | None; verdict: Literal["genuine","discard"] | None
                triage_status: Literal["pending","done","failed"]; enquiry_id: int | None
                needs_permalink: bool        # seen + has an enquiry whose source_url is not a post link
def register_sighting(db, post: ScrapedPost, run_id: int, keyword_id: int | None, keyword_text: str, *, now=None) -> Sighting
#   match order: source_ref → fingerprint → content_hash → same-profile text_prefix near-duplicate (>=100 chars overlap).
#   Always: touch_profile; last_seen_at/times_seen. "seen" + enquiry: keyword hit, unseen source ref, link upgrade (as dedupe._merge). "new": insert (triage_status pending, raw_text, raw_purge_after). Never calls the LLM.
def triage_inputs(db, seen_post_ids: list[int]) -> list[tuple[int, TriageInput]]      # TriageInput(subject="seen_post", enquiry_id=<seen_post_id>)
@dataclass(frozen=True)
class Persisted: seen_post_id: int; verdict: Literal["genuine","discard"] | None; enquiry_id: int | None; cost_usd_micros: int; triage_status: str
def persist_triage(db, seen_post_id: int, outcome: TriageOutcome | None, *, error: str | None = None, run_id: int | None, keyword_id: int | None, now=None) -> Persisted
#   writes llm_calls rows (feature triage, seen_post_id, run_id); sets verdict/reason/poster_type/ai_result/triage_status.
#   genuine → create the Enquiry (all ai_* + editable fields as triage_enquiry does; content_hash; EnquirySource rows; keyword hit; author_profile_id; seen_post_id; posted_relative; source_url = post_url or profile URL or search URL; triage_status done; review_status pending_review), set seen_posts.enquiry_id, raw_text=NULL, raw_purge_after=NULL, update llm_calls.enquiry_id, mark_relevant, status_history rows.
#   discard → keep raw_text. outcome None / failed → triage_status failed + triage_error.
def attach_permalink(db, seen_post_id: int, url: str, *, urn: str | None = None, now=None) -> bool  # seen_posts.post_url; enquiry.source_url (+ URN source row) when the enquiry link is not already a post link
def retry_triage(db, seen_post_id: int, llm=None) -> SkippedPostView        # sync one real call; DomainError not_found | raw_purged | triage_failed
def rescue(db, seen_post_id: int, *, accept: bool = False, actor_user_id: int | None = None) -> EnquiryCardView
#   DomainError not_found | raw_purged ("post text no longer stored") | already_rescued. Creates the enquiry from seen_posts.ai_result (ai_verdict stays "discard"), review_status pending_review; accept=True also records decision accept (is_override=True) → accepted/prospect_new (via inbox.decide). Sets rescued_at, enquiry_id, mark_relevant.
def list_skipped(db, *, q: str | None = None, state: Literal["discard","failed"] = "discard", page: int = 1, page_size: int = 25) -> SkippedPage
#   state discard: verdict discard AND enquiry_id IS NULL; failed: triage_status failed. q: case-insensitive substring over snippet, reason, author_name, raw_text. Newest last_seen first.
def purge_expired(db, *, now=None) -> int           # raw_text=NULL, raw_purged_at=now where raw_purge_after<=now AND raw_text IS NOT NULL; keeps fingerprint/text_prefix/snippet
# services/keyword_library.py  (OWNED BY SLICE A — pure data, written first; B, C and the fixture import it)
@dataclass(frozen=True) class SuggestedKeyword: key: str; text: str; group: str
SUGGESTED_KEYWORDS: tuple[SuggestedKeyword, ...]     # the 60 of search-setup.md; group = an offering name
# services/keywords.py (additions)
@dataclass(frozen=True) class SeedResult: added: int; already_present: int
def seed_suggested(db) -> SeedResult                 # idempotent, never overwrites (search-setup.md)
def seed_suggested_once(db) -> SeedResult | None     # honours app_meta keywords_seeded_v1 and settings.seed_suggested_keywords
def update_keyword(db, keyword_id: int, text: str | None = None, group_name: str | None | _Unset = UNSET) -> KeywordView   # keyword_invalid | keyword_duplicate | not_found
def record_yield(db, keyword_id: int | None, scanned: int, relevant: int) -> None   # Core UPDATE: scanned_total/relevant_total += , last_scanned/last_relevant = ; deleted keyword → 0 rows
# services/runs.py (additions/changes)
def plan_keywords(db, limit: int) -> list[tuple[int, str]]    # resume-first (data.md), then never-run, then least-recently-run, then id; active only
def recover_interrupted_runs(db) -> int           # now aborted + null reason + keyword rows skipped (data.md)
# run_eligibility / posts_seen_today: "posts" = scanned (runs.posts_seen). Caps from settings (2000/day, 4 runs/day).
# services/inbox.py: InboxPage.counts gains "skipped" (discard, no enquiry) and "skipped_failed"; list_enquiries unchanged for the 3 tabs.
```
Views (frozen Pydantic, `services/views.py`): `KeywordView` gains `group_name: str | None, suggested: bool, scanned_total: int, relevant_total: int, last_scanned: int, last_relevant: int`; `RunView` gains `posts_relevant, posts_skipped_ai, permalinks_found: int`, `relevance_pct: float | None` (= relevant ÷ posts_new × 100; None when posts_new is 0); `RunKeywordView` gains `posts_relevant, posts_skipped_ai, permalinks_found, scrolls, cost_usd_micros`; `EnquiryCardView` gains `posted_relative: str | None`, `author_profile_url: str | None`; `SkippedPostView(id, snippet, reason, poster_type, author_name, author_headline, profile_url, keyword_text, post_url, link_kind, posted_relative, first_seen_at, last_seen_at, times_seen, triage_status, triage_error, raw_text: str | None, can_rescue: bool)`; `SkippedPage(items, total, page, page_size, state, q, counts: dict[str,int])`.

### Slice C — scraper depth and fixture
```python
# scraper/urls.py
def search_url(base: str, keyword: str, date_filter: str, sort: Literal["latest","relevance"] = "latest") -> str   # sortBy="date_posted" | "relevance"
# scraper/types.py: KeywordScrapeResult gains  passes: int = 1; expanded: int = 0; stop_reason: Literal["cap","no_growth","max_scrolls","no_results","drift"] = "cap"
# scraper/search.py
class LinkedInSearcher:
    def scan(self, keyword: str, max_posts: int) -> KeywordScrapeResult
    #   passes = settings.scrape_date_filters × settings.scrape_sort. Per pass: goto, checkpoint, wait; scroll loop (delay, hover, wheel, settle-wait, checkpoint) until
    #   cards >= cap OR scrolls >= scrape_max_scrolls OR scrape_no_growth_scrolls consecutive scrolls without growth; expand truncated cards (<= scrape_expand_max_per_keyword, pacer.short, checkpoint each);
    #   EXTRACT + parse_cards; merge across passes by source_ref (first sighting wins); stop when posts >= cap, else next pass. NEVER opens a post menu. Keeps the last page loaded for capture_links.
    def capture_links(self, refs: Sequence[str]) -> LinkCaptureResult
    #   refs = ScrapedPost.source_ref values from the LAST scan() (page still loaded). Locates each card (see below), runs the Permalink-capture steps for at most scrape_permalink_max_per_keyword refs, 3 consecutive misses end it.
    #   A ref whose card is no longer in the DOM is skipped ("no_card") and counts neither as an attempt nor a miss. Raises AccountSafetyStop (browser closed first).
    def search(self, keyword: str, max_posts: int) -> KeywordScrapeResult     # kept: scan() then capture_links(all eligible); Phase 1.2/1.3 callers and tests unchanged
@dataclass(frozen=True) class LinkCaptureResult: links: dict[str, str]; attempts: int; found: int      # links: source_ref → canonical permalink URL (hits only)
```
**Listitem-tier permalink capture.** For the `listitem` card tier (`has_control_menu` was false in Phase 1.3) `EXTRACT_JS` now also returns `has_control_menu=true` when the card contains `LISTITEM_MENU` (`button[aria-label^="Open control menu for post by"]`), plus `card_index` (position among that tier's cards in DOM order) and `menu_label` (the button's `aria-label`). Capture locates the card by `component_key` when present, else by `card_index` verified against `menu_label` (a mismatch = "no_card"), clicks the `LISTITEM_MENU` button as the trigger, then finds the "Copy link to post" item in the opened menu or a visible document-level `[role="menu"]` (else any visible innermost element whose trimmed text equals `COPY_LINK_TEXT`). Everything else (hook, pacing, one item only, checkpoints, `Escape`) is the Phase 1.2 design. `permalink.eligible_indices` is replaced by `eligible_refs(raws, refs, cap)`.

**Date/sort evidence of depth.** `scan()` logs one `scraper_pass` DEBUG line per pass (`date_filter`, `sort`, `scrolls`, `cards`, `stop`). `KeywordScrapeResult.stop_reason`: `cap`, `no_growth` (last pass ended without reaching the cap), `max_scrolls`, `no_results`, `drift`.

**Fixture bulk corpus (`linkedin_fixture/bulk.py`).** `generate_bulk(n=300, seed=1404) -> list[FixturePost]`, deterministic, ids `B001`–`B300`, synthetic authors (`bulk-author-NNN`, names "Bulk Author NNN", `.example` emails, `+91 90000 0xxxx` phones), `expected` ∈ genuine/discard by `kind`: buyer (10%, genuine), agency_recruiting (20%), trainer_ad (15%), event_promo (20%), job_post (10%), thought_leadership (25%) (all discard). `results_for(keyword) -> list[FixturePost] | None`: None unless the lower-cased keyword is a `SUGGESTED_KEYWORDS` text other than the four legacy test keywords (`corporate ai training`, `ai workshop`, `ai consulting`, `claude code training`, which keep returning exactly the P-set, so Phase 1/1.2 counts are unchanged); else 90 distinct posts `bulk[(h + 7*i) % 300]`, `h = int(sha1(keyword).hexdigest(), 16) % 300`, with `age_days = i*30//90` (newest first for sort=latest; a stable shuffle for relevance). `datePosted="past-week"` keeps `age_days <= 7` (≈24 posts), `past-month` all 90. `expected_verdict(text) -> "genuine" | "discard" | None` finds the bulk post by normalised text (used by the fake-LLM oracle in tests). Markup variant per keyword: even `h` → SDUI (`feed-full-update`), odd `h` → the marker-free `listitem` variant (with the `Open control menu for post by` button and a menu portal containing "Copy link to post"). "Copy link to post" writes a permalink via `navigator.clipboard.writeText` for ≈80% of posts and is a no-op for the rest (never 3 in a row, except under `permalink-miss-test`). **Infinite scroll**: the results page renders the first 10 cards plus a sentinel; a scroll listener (and an IntersectionObserver) fetches `GET /search/results/content/more/?keywords=…&datePosted=…&sortBy=…&start=N` (10 cards as the same markup, ≈150 ms latency) and appends them to the lazy column; an empty fragment ends the list. The fixture honours `datePosted` and `sortBy` and records them (`window.__fixture_query`) for assertions. Trigger keywords and routes of Phase 1.1–1.3 are unchanged.

### Slice D — triage agent
`TriageInput` gains `subject: Literal["enquiry","seen_post"] = "enquiry"` (for `seen_post`, `enquiry_id` carries the seen_post id and `log_context` uses the key `seen_post_id`). `PROMPT_VERSION = "triage-v2"`. `prompts/triage_system.md` is rewritten per `agent.md` → Prompt v2. New:
```python
# agent/batch.py
@dataclass(frozen=True) class BatchItem: key: int; inp: TriageInput
@dataclass(frozen=True) class BatchResult: key: int; outcome: TriageOutcome | None; fatal: Exception | None
def triage_concurrently(items: Sequence[BatchItem], deps: TriageDeps, *, concurrency: int = 4,
                        should_stop: Callable[[], bool] = lambda: False) -> Iterator[BatchResult]
#   ThreadPoolExecutor(max_workers=concurrency) around run_triage; yields in completion order. The first LLMAuthError/LLMModelNotFound yields BatchResult(fatal=exc),
#   submits nothing further (items never started yield nothing: the caller leaves them pending). should_stop is polled before each submission. No DB access inside the pool.
```
Test support: `tests/support/fake_llm.py::FakeLLMClient.with_oracle(label: Callable[[str], Literal["genuine","discard"] | None])` answers `record_triage` from the enquiry text found in the prompt (a valid, guarded-safe result; thread-safe; counts `calls`). Eval: `tests/integration/agent/eval_set.py` (`EVAL_POSTS`, ≥ 40 labelled synthetic posts: 16 genuine and 24 discard across agency / marketplace / trainer-ad / event / job / thought-leadership / individual-trainer-seeker / non-English / emoji-heavy / no-contact genuine) and `test_triage_eval.py` (real Haiku, `triage_concurrently`, concurrency 4): verdict accuracy ≥ 90 %, every returned email/phone literally present in the post (0 hallucinated), reason ≤ 160 chars.

### Slice E — discovery job (`jobs/discovery_job.py`)
Job flow (replaces data flow steps 2 selection, 3, 4, 5):
1. Run start: `seen_posts.purge_expired(db)` (log `seen_posts_purged`); `plan_keywords(db, settings.scrape_max_keywords_per_run)`; open the searcher; login check as before.
2. Per keyword (`max_posts = max(1, min(scrape_max_posts_per_keyword, scrape_max_posts_per_day − scanned today))`):
   1. `result = searcher.scan(keyword, max_posts)`.
   2. For each post: `register_sighting` → counters (`posts_seen` +1 always; `new`/`dupe`); commit every 10 posts for live progress. IntegrityError on a deleted keyword → the existing retry-with-null rule.
   3. Triage the keyword's new posts: `triage_inputs` → `triage_concurrently(concurrency=settings.triage_concurrency)`; for each `BatchResult` on the job thread `persist_triage` (+ commit). Fatal LLM error → as step 6a (remaining items stay `pending`, run `failed`). Counters: `enquiries_triaged`, `posts_relevant`, `posts_skipped_ai`, `triage_failed`, per-keyword `cost_usd_micros`.
   4. Permalinks (only now, page still loaded): refs = this keyword's newly-genuine posts + `needs_permalink` sightings → `searcher.capture_links(refs)` → `attach_permalink` for each hit. A safety stop here keeps everything already persisted.
   5. `record_yield`, keyword row `done|warning` via `keyword_outcome`, `keywords_done += 1`, `_mark_keyword_run`, commit, `scrape_keyword` log, paced pause.
3. The daily scanned-post cap is checked after each keyword (as before). Safety stops, `SessionMissing`, open errors and fatal LLM errors keep their Phase 1.1 handling.
4. Finalise: `current_step` = "Run finished — scanned 312 · new 140 · relevant 9 (6%) · skipped by AI 131 · already seen 172" (also on `aborted`/cap, prefixed with the reason).
5. Resumability: every keyword commits independently and `seen_posts` makes a re-scan free; a restart marks the run `aborted` and the next run's `plan_keywords` continues with the un-run keywords (data.md).
`web/app.py` startup gets two lines: `keywords.seed_suggested_once(db)` and the existing `recover_interrupted_runs`.

### Slice F — web
Routes (`api.md`): `POST /setup/keywords/suggested`, `POST /setup/keywords/{id}/edit`, `GET /inbox?tab=skipped&state=discard|failed&q=&page=`, `POST /inbox/skipped/{id}/rescue`, `POST /inbox/skipped/{id}/override`, `POST /inbox/skipped/{id}/retry`. Templates per `ui.md`.

### Logging additions
`scrape_keyword`: `posts_relevant`, `posts_skipped_ai`, `scrolls`, `passes`, `expanded`, `stop_reason`, `cost_usd`. `scrape_run`: `posts_relevant`, `posts_skipped_ai`, `permalinks_found`, `relevance_pct`, `purged`. New: `seen_posts_purged` (`rows`), `keywords_seeded` (`added`, `already_present`), `triage_batch` (`items`, `concurrency`, `duration_ms`).

### Tests (conventions)
- Fixture server ports stay ephemeral (integration) / 8766 (E2E) / 8767 (boot); never 8765.
- Integration tests that run a full multi-keyword discovery use `@pytest.mark.timeout(480)` and the fake-LLM oracle; real Haiku is used by the eval (slice D), the existing triage suites and the E2E.
- Gate commands for unit, integration and E2E use disjoint temp DBs, profiles and ports, so they run concurrently (`roadmap.md` → Phase 1.4 Gate).

## LinkedIn results variants (Phase 1.3)
The SDUI results page ships in several markup variants. Card discovery order: `sdui` (`data-view-name="feed-full-update"`), `urn`, then `listitem` (`role=listitem` containing an expandable text box / `Open control menu for post by <Name>` button / `feed-commentary`). `listitem` also wins when the earlier tiers produce no post text. Its raws have `has_control_menu=false` (no permalink capture; the card links to the author profile). **Phase 1.4 supersedes this:** `listitem` raws carry `has_control_menu`, `card_index` and `menu_label`, and permalink capture works on this tier (`## Thorough discovery (Phase 1.4)` → Slice C). Selectors live in `scraper/selectors.py` (`LISTITEM_*`); sample: `tests/integration/scraper/test_listitem_variant.py`.

---

# Phase 2 — CRM memory, read-only Google Sheet sync, shared login

> This section is the **binding contract between the Phase 2 slices** (`roadmap.md` → Phase 2). Every name, path and signature below can be coded against before the owning slice lands. Data: `data.md` → Phase 2 entities. Behaviour: the capability files. HTTP: `api.md`. Screens: `ui.md`.
> **Assumed (all Phase 2 decisions are also listed in `NOTES.md` → Phase 2 spec):** the shared login reuses `SALES0_SECRET_KEY` (`services/team/crypto.sign`, purpose `session`) instead of the planned `SALES0_SESSION_SECRET`; Origin/Referer CSRF stays and no per-form token is added; the CRM is event-driven off Phase 3/5 events so Phase 1/3/5 code only gains one-line `publish` calls.

## Phase 2 layout delta (new files only; existing files are listed under "Shared-file edits")
```
migrations/versions/p6_crm_5b2e7d14.py              S1
src/sales0/db/models/
    enums_crm.py  crm.py  contacts.py  timeline.py  registries.py  sheetdata.py     S1   (+ exports in models/__init__.py)
src/sales0/services/
    crm/__init__.py          install()                                        S1
    crm/actor.py  stages.py  views.py  timeline.py  prospects.py  leads.py  clients.py
    crm/followups.py  flags.py  contacts.py  companies.py  registries.py  pipeline.py  hooks.py       S1
    crm/digest_sections.py   register()                                       S5
    sheetsync/sql_repository.py  mapping.py  metrics.py  status_map.py        S2
    importer/__init__.py  csv_import.py  paste.py                             S5
src/sales0/jobs/sheet_sync_job.py                                             S2 (edit)
src/sales0/web/auth.py  routes/auth.py  templates/auth/login.html             S3
src/sales0/web/routes/pipeline.py  contacts.py  registries.py  importer.py    S4
src/sales0/web/routes/sync.py  templates/sync/page.html  templates/sync/_status.html   S2
src/sales0/web/templates/pipeline/**  contacts/**  registries/**  importer/**           S4
tests/unit/crm/**  tests/integration/crm/**  tests/integration/db/test_migration_p6_crm.py    S1
tests/unit/sheetsync/** (extend)  tests/integration/sheetsync/**  tests/fixtures/sheets/**   S2
tests/unit/test_auth_session.py  tests/integration/web/test_auth.py                        S3
tests/integration/web/test_pipeline_routes.py  test_contacts_routes.py  test_registries_routes.py  test_import_routes.py   S4
tests/integration/importer/**  tests/integration/crm/test_digest_sections.py              S5
tests/e2e/test_phase2_journey.py  tests/integration/db/test_migration_realistic.py  README.md   S6
```

## Settings added in step 0 (`config.py`, `.env.example` `# --- CRM / login ---` block)
| Variable (`SALES0_` prefix) | Field | Default | Purpose |
|---|---|---|---|
| `APP_PASSWORD` | `app_password: SecretStr \| None` | unset | Shared password. Unset = open (loopback only, as in Phase 1). Empty string counts as unset. `check_startup` refuses a non-loopback `SALES0_HOST` while it is unset ("Set SALES0_APP_PASSWORD before binding to a public host"). |
| `SESSION_DAYS` | `session_days: int` (1-90) | `14` | Login cookie lifetime |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_S` | `login_max_attempts`, `login_lockout_s` | `5` / `60` | Failed logins per minute per client IP before a lockout |
| `FOLLOWUP_DAYS_1` / `_2` / `_FINAL` | `followup_days_1/2/final` | `3` / `7` / `14` | Reminder offsets in calendar days from the contact date (`data.md` → follow_ups) |
| `STALE_LEAD_DAYS` | `stale_lead_days` | `14` | A lead with no timeline event for this long is "stale" (digest) |
| `REPEAT_POSTER_MIN` / `_DAYS` | `repeat_poster_min`, `repeat_poster_days` | `3` / `60` | Flag threshold |
| `CRM_PAGE_SIZE` | `crm_page_size` | `50` | List page size (contacts, pipeline list) |
The sheet variables already exist (`SALES0_SHEET_SYNC_MINUTES`, `_CLIENT_SOURCING_ID`, `_METRICS_ID`, `_TABS_JSON`, `_FETCH_*`).
`session_cookie_secure` is derived (true when `SALES0_PUBLIC_BASE_URL` is `https://`).

## Migrations — the FK recipe for a referenced table (binding for `p6_crm_5b2e7d14`)
Adding a foreign key to an existing column in SQLite means rebuilding the table. `outreach_messages` is referenced by `outreach_events` (`CASCADE`), `outreach_replies` (`CASCADE`) and itself, so a plain batch rebuild under `foreign_keys=ON` would cascade-delete its children. The migration therefore runs the rebuild in `with op.get_context().autocommit_block():` — `PRAGMA foreign_keys=OFF`; `batch_alter_table("outreach_messages", recreate="always", reflect_args=[Column("follow_up_of_id", Integer, ForeignKey("outreach_messages.id", ondelete="SET NULL"))])` with `create_foreign_key(... ondelete="SET NULL")` x3; `PRAGMA foreign_key_check` (any row → `raise RuntimeError`); `PRAGMA foreign_keys=ON` (in a `finally`). The docstring carries the one-line justification required by "Migrations (binding)". The migration test (S1) and the realistic-DB test (S6) assert that `outreach_events` / `outreach_replies` / `outreach_messages` row counts are identical before and after. (Note for the maintainer: `p5_replies_c3d81f20` used a plain batch on the same table; it is harmless on an empty table and is not changed.)

## Events (`services/events.py`; step 0 adds the dataclasses and the two publish calls)
```python
@dataclass(frozen=True)
class MessageSent:            # published by services/outreach/messages.py::_execute, after state='sent', before db.commit()
    outreach_message_id: int
    member_id: int
    to_addrs: tuple[str, ...]
    sent_at: datetime

@dataclass(frozen=True)
class EnquiryDecided:         # published by services/inbox.py::decide(), once per enquiry, after the row is updated and flushed
    enquiry_id: int
    decision: str                       # "accept" | "discard"
    previous_review_status: str
    actor_user_id: int | None = None
    actor_member_id: int | None = None  # decide() gains the optional keyword actor_member_id: int | None = None
```
`ReplyReceived` already exists (`kind` human|auto_reply|bounce; `intent` interested|not_now|unsubscribe|question|other|None; `outreach_message_id`, `reply_id`, `from_addr`). Handlers run synchronously in the publisher's session inside a savepoint (`events.publish`); a failing CRM handler is logged and never breaks sending, replying or deciding. `services/crm/__init__.py::install()` subscribes `hooks.on_message_sent`, `hooks.on_reply_received`, `hooks.on_enquiry_decided` (idempotent; `events.reset()` in tests removes them, so CRM tests call `install()` in a fixture).

## Shared-file edits (each file has exactly ONE owner; others never touch it)
| File | Owner | Edit |
|---|---|---|
| `config.py`, `.env.example` | S1 step 0 | the settings above + `check_startup` message |
| `web/app.py` | S1 step 0 | wiring (below) |
| `services/events.py`, `services/inbox.py` (`decide`), `services/outreach/messages.py` (`_execute`) | S1 step 0 | dataclasses + three `publish` calls |
| `services/profiles.py` | S1 | `ensure_profile` |
| `services/sheetsync/engine.py`, `repository.py`, `registry.py`, `state.py`, `__init__.py`, `jobs/sheet_sync_job.py`, `cli.py` | S2 | see S2 contract |
| `web/middleware.py` (`allowed_origins`), `web/templates/base.html` (Log out control) | S3 | see S3 contract |
| `web/context.py` (NAV, shell), `web/routes/stubs.py`, `web/routes/outreach.py` (compose `prospect_id`), `web/routes/inbox.py` (pass `actor_member_id`), `tests/integration/web/test_shell.py`, `test_every_page_renders.py`, `web/static/css/app.css` | S4 | see S4 contract |
| `services/digest.py` | nobody (sections register through `digest.register_section`) | |
| `tests/conftest.py` | S1 step 0 | `SALES0_APP_PASSWORD` is cleared from the environment per test; nothing else |

### Step 0 — the wiring skeleton (the FIRST commit of S1, done before the other slices start coding against the tree; ~15 min)
Creates (a) the settings, (b) `events.py` additions and the three publish calls, (c) **placeholder modules** that make `app.py` complete: `services/crm/__init__.py` (`install()` for real once hooks exist, no-op until then), `services/sheetsync/sql_repository.py` (`def install() -> None: pass`), `services/crm/digest_sections.py` (`def register() -> None: pass`), `web/auth.py` (`def install(app, settings) -> None: pass`), and **empty routers** `web/routes/{pipeline,contacts,registries,importer,sync,auth}.py` (`router = APIRouter()`), (d) `services/crm/views.py` with the Pydantic view classes named below (fields final), (e) `app.py`:
```python
app.add_middleware(CsrfOriginMiddleware)          # innermost
web_auth.install(app, settings)                   # S3: AuthMiddleware (+ its router)  -- between CSRF and request-context
app.add_middleware(RequestContextMiddleware)      # outermost
... existing include_router calls ...
for r in (pipeline.router, contacts.router, registries.router, importer.router, sync.router): app.include_router(r)
# lifespan, after seed_admin:
crm.install(); sql_repository.install(); crm_digest_sections.register()
with session_scope() as db: crm.startup_backfill(db)      # once: app_meta crm_flags_backfilled_v1 -> flags.recompute_all
```
Until a slice replaces its placeholder the app behaves exactly as in Phase 1, so every Phase 1 test stays green. The slices **replace the placeholder files wholesale**.

## S1 contract — `services/crm/*` (signatures are final; all functions take `db: Session` first, `flush` but never `commit`; time via `now: datetime | None = None` → `now_or(now)`; failures raise `DomainError`)
```python
# crm/actor.py
@dataclass(frozen=True)
class Actor:
    label: str                      # "Priya", "system", "sheet-sync", "import", "team"
    member_id: int | None = None
    @staticmethod
    def of_member(m: TeamMember | None) -> "Actor": ...      # None -> TEAM
SYSTEM = Actor("system"); SHEET_SYNC = Actor("sheet-sync"); IMPORT = Actor("import"); TEAM = Actor("team")

# crm/stages.py  (pure)
PROSPECT_STAGES = ("new", "contacted", "follow_up_1", "follow_up_2", "final_follow_up")
LEAD_STAGES = ("meeting_scheduled", "proposal_sent", "won", "lost", "no_response")
STAGE_LABELS: dict[str, str]        # "follow_up_1" -> "Follow-up 1", "meeting_scheduled" -> "Meeting scheduled", ...
def stage_rank(stage: str) -> int; def is_terminal_lead_stage(stage: str) -> bool

# crm/timeline.py
def add_event(db, *, subject_type: str, subject_id: int, kind: str, body: str | None = None, data: dict | None = None,
              actor: Actor = SYSTEM, occurred_at: datetime | None = None,
              prospect_id: int | None = None, enquiry_id: int | None = None,
              company_id: int | None = None, contact_id: int | None = None) -> TimelineEvent
def add_note(db, *, subject_type: str, subject_id: int, body: str, actor: Actor, now=None) -> TimelineItemView   # body 1-4000 chars, kind="note"; scopes derived from the subject
def list_timeline(db, *, prospect_id=None, enquiry_id=None, company_id=None, contact_id=None,
                  subject: tuple[str, int] | None = None, limit: int = 200, before: datetime | None = None) -> list[TimelineItemView]
    # newest first; merges status_history rows of the linked enquiries (kind "status_change", actor from actor_user_id or "team");
    # exactly one scope argument is required (else ValueError)

# crm/prospects.py
def ensure_for_enquiry(db, enquiry_id: int, *, actor: Actor = TEAM, now=None) -> ProspectView
    # IDEMPOTENT. One prospect per linkedin_profile (enquiry.author_profile_id, else ensure_profile(author_profile_url));
    # no profile -> one prospect per enquiry. Sets enquiries.prospect_id/company_id/contact_id, linkedin_profiles.tracked=True (only when NULL),
    # upserts company + contact (+ contact_links), timeline `created` (new prospect) + `enquiry_linked`, then flags.on_enquiry_linked.
    # A prospect closed with reason "enquiry_discarded" is reopened.
def create_manual(db, *, name: str, company: str | None = None, email: str | None = None, phone: str | None = None,
                  linkedin_url: str | None = None, website: str | None = None, notes: str | None = None,
                  source: str = "manual", actor: Actor = TEAM, now=None) -> ProspectView     # adopts an existing prospect of the same profile/email/contact
def get_prospect(db, prospect_id: int) -> ProspectDetail                       # not_found 404
def list_prospects(db, *, stage: str | None = None, status: str = "open",     # open | closed | converted | all
                   q: str | None = None, company_id: int | None = None, source: str | None = None,
                   flag: str | None = None, due: str | None = None,           # due: "today" | "overdue"
                   page: int = 1, page_size: int | None = None) -> ProspectPage
def move_stage(db, prospect_id: int, stage: str, *, actor: Actor = TEAM, note: str | None = None, now=None) -> ProspectView
    # any move is allowed (back too), recorded as `stage_change`; sets stage_source="app"; entering `contacted` the first time sets contacted_at
    # and calls followups.schedule_for_contact; entering a later stage completes the reminders up to it; refuses a closed/converted prospect (`prospect_closed` 409)
def mark_contacted(db, prospect_id: int, *, when: datetime | None = None, actor: Actor = TEAM, channel: str = "manual", note: str | None = None, now=None) -> ProspectView
def record_response(db, prospect_id: int, *, actor: Actor = TEAM, source: str = "manual", reply_id: int | None = None, note: str | None = None, now=None) -> ProspectView
    # sets responded_at (first time), cancels open reminders, timeline `reply_received`
def close(db, prospect_id: int, reason: str, *, actor: Actor = TEAM, now=None) -> ProspectView      # status=closed; reason in ("no_response","lost","not_interested") also sets linkedin_profiles.tracked=False
def reopen(db, prospect_id: int, *, actor: Actor = TEAM, now=None) -> ProspectView
def set_tracked(db, prospect_id: int, tracked: bool, *, actor: Actor = TEAM, now=None) -> ProspectView
def update(db, prospect_id: int, *, name=UNSET, notes=UNSET, company=UNSET, actor: Actor = TEAM, now=None) -> ProspectView
def resolve_for_message(db, message_id: int) -> int | None                     # outreach_messages.prospect_id -> enquiries.prospect_id via enquiry_id -> prospect of a contact whose email is in to_addrs

# crm/leads.py
def convert(db, prospect_id: int, *, stage: str = "meeting_scheduled", meeting_at: datetime | None = None,
            opened_by: str = "manual", actor: Actor = TEAM, note: str | None = None, now=None) -> LeadView
    # DomainError: lead_needs_contact 409 ("This prospect has not been contacted yet - mark it Contacted first."),
    #              lead_needs_response 409 ("No response is recorded yet - press 'Response received' first."), lead_exists 409
    # prospect.status -> converted; cancels reminders; timeline `lead_created`
def move_stage(db, lead_id: int, stage: str, *, proposal_sent_on: date | None = None, meeting_at: datetime | None = None,
               value_estimate: int | None = None, outcome_note: str | None = None, actor: Actor = TEAM, now=None) -> LeadView
    # proposal_sent -> sets proposal_sent_on (default local today) AND enquiries.date_proposal_sent of the origin enquiry; timeline `proposal_sent`
    # won -> closed_at + clients.create_from_lead; lost/no_response -> closed_at + prospect `tracked=False`; moving back clears closed_at (and does not delete the client)
def get_lead(db, lead_id: int) -> LeadDetail
def list_leads(db, *, stage: str | None = None, open_only: bool = True, q: str | None = None, page: int = 1, page_size: int | None = None) -> LeadPage

# crm/clients.py
def create_from_lead(db, lead_id: int, *, actor: Actor = SYSTEM, now=None) -> ClientView      # idempotent per lead
def create_legacy(db, *, name: str, company: str | None = None, started_on: date | None = None, notes: str | None = None, actor: Actor = TEAM, now=None) -> ClientView
def get_client(db, client_id: int) -> ClientDetail; def update_client(db, client_id: int, *, name=UNSET, notes=UNSET, status=UNSET, actor: Actor = TEAM, now=None) -> ClientView
def list_clients(db, *, status: str | None = None, q: str | None = None, page: int = 1, page_size: int | None = None) -> ClientPage

# crm/followups.py
def schedule_for_contact(db, prospect_id: int, contacted_at: datetime, *, now=None) -> list[FollowUpView]     # the three reminders; no-op if the prospect has no_follow_up or already has open auto reminders
def list_due(db, *, today: date | None = None, member_id: int | None = None) -> DuePanel                       # DuePanel(due_today: list[FollowUpView], overdue: list[FollowUpView]); today default = local_today(settings.tz)
def due_count(db, *, today: date | None = None) -> int                                                        # due today + overdue
def complete(db, follow_up_id: int, *, actor: Actor = TEAM, note: str | None = None, now=None) -> FollowUpView  # moves the prospect to the reminder's stage
def snooze(db, follow_up_id: int, due_on: date, *, actor: Actor = TEAM, now=None) -> FollowUpView
def add_custom(db, *, prospect_id: int | None = None, lead_id: int | None = None, due_on: date, note: str | None = None, actor: Actor = TEAM, now=None) -> FollowUpView
def cancel_open(db, *, prospect_id: int | None = None, lead_id: int | None = None, reason: str, actor: Actor = SYSTEM, now=None) -> int
# Every function that changes reminders refreshes prospects.next_follow_up_on.

# crm/flags.py
def set_flag(db, entity_type: str, entity_id: int, kind: str, *, source: str = "auto", detail: dict | None = None, actor: Actor = SYSTEM, now=None) -> FlagView   # idempotent; updates detail
def clear_flag(db, entity_type: str, entity_id: int, kind: str, *, actor: Actor = TEAM, now=None) -> bool
def flags_for(db, entity_type: str, ids: Sequence[int]) -> dict[int, list[FlagView]]                           # active only
def on_enquiry_linked(db, enquiry_id: int, *, now=None) -> None        # recompute repeat_poster for its prospect/profile + detect_competitor_mentions
def recompute_repeat_poster(db, prospect_id: int, *, now=None) -> FlagView | None
    # count enquiries linked to the prospect (or sharing its linkedin_profile) whose date_added is within repeat_poster_days of `now`; >= repeat_poster_min -> flag {"count": n, "within_days": d}; below -> auto flag cleared
def detect_competitor_mentions(db, enquiry_id: int) -> list[int]
    # for every ACTIVE competitor: name or website host appears in raw_text/requirement_summary/company (casefold, word boundary) -> competitor_mentions(enquiry) + flag competitor_mention on the enquiry and its prospect; returns competitor ids
def recompute_all(db, *, now=None) -> dict[str, int]                   # {"repeat_poster": n, "competitor_mention": n}; run once at startup and after an import / sheet sync that touched enquiries
# no_follow_up: set_flag(db, "prospect", id, "no_follow_up", source="user"|"auto") ALSO cancels open reminders; clear_flag does not recreate them.

# crm/contacts.py
def upsert(db, *, full_name: str | None = None, email: str | None = None, phone: str | None = None, linkedin_url: str | None = None,
           title: str | None = None, company_id: int | None = None, origin: str = "manual", actor: Actor = SYSTEM, now=None) -> ContactView
    # match order: any email in contact_emails (casefold) -> linkedin_slug -> phone digits (only when a name also matches loosely, else new);
    # fills empty columns, never blanks one; adds new emails/phones to contact_emails/contact_phones; DomainError contact_invalid 400 if no name/email/linkedin
def get_contact(db, contact_id: int) -> ContactDetail
def list_contacts(db, *, q: str | None = None, tag: str | None = None, company_id: int | None = None, page: int = 1, page_size: int | None = None) -> ContactPage
    # q: casefold LIKE over search_text; tag: exact tag name; ordered by full_name, id; one query + one count; < 300 ms at 10k rows (tested)
def update_contact(db, contact_id: int, *, full_name=UNSET, title=UNSET, notes=UNSET, company=UNSET, actor: Actor = TEAM, now=None) -> ContactView
def add_tag(db, contact_id: int, name: str, category: str = "custom", *, actor: Actor = TEAM, now=None) -> ContactView   # tag_invalid 400 (1-40 chars)
def remove_tag(db, contact_id: int, tag_id: int, *, actor: Actor = TEAM, now=None) -> ContactView
def list_tags(db) -> list[TagView]                                      # with usage counts
def link(db, contact_id: int, entity_type: str, entity_id: int, role: str | None = None) -> None   # idempotent
def find_duplicates(db, *, limit: int = 50) -> list[DuplicateGroup]     # groups sharing an email, a linkedin_slug, a phone, or an identical casefolded full_name+company
def merge(db, keep_id: int, drop_id: int, *, actor: Actor = TEAM, now=None) -> ContactView
    # merge_self 400; moves emails/phones/tags/contact_links; repoints prospects.contact_id, enquiries.contact_id, timeline_events.contact_id; fills empty columns of keep; deletes drop; timeline `contact_merged`

# crm/companies.py
def get_or_create(db, name: str, *, website: str | None = None, city: str | None = None) -> CompanyView   # by name_key
def get_company(db, company_id: int) -> CompanyDetail                  # + prospects, enquiries, contacts, timeline
def list_companies(db, *, q: str | None = None, page: int = 1, page_size: int | None = None) -> CompanyPage

# crm/registries.py
def list_competitors(db, *, q: str | None = None) -> list[CompetitorView]; def get_competitor(db, id) -> CompetitorDetail   # + mentions (linked enquiries/clients)
def create_competitor(db, *, name: str, website=None, approx_clients=None, client_names=None, contact=None, email=None, notes=None, actor=TEAM, now=None) -> CompetitorView
def update_competitor(db, id: int, *, ... same optional fields ..., active=UNSET, actor=TEAM, now=None) -> CompetitorView
def delete_competitor(db, id: int, *, actor=TEAM, now=None) -> None     # sheet_owned 409 if it has a sheet source; competitor_invalid 400 for a blank name
def link_competitor(db, competitor_id: int, entity_type: str, entity_id: int, *, actor=TEAM, now=None) -> None   # manual mention (source="user")
def list_groups(db, *, active: bool | None = None) -> list[GroupView]
def create_group(db, *, name: str, platform: str = "whatsapp", link=None, details=None, notes=None, actor=TEAM, now=None) -> GroupView
def update_group(db, id: int, *, ..., active=UNSET, actor=TEAM, now=None) -> GroupView; def delete_group(db, id: int, *, actor=TEAM, now=None) -> None

# crm/pipeline.py
def summary(db, *, today: date | None = None) -> PipelineSummary
    # prospect counts per stage (open only), leads per stage (open) + won/lost/no_response last 90 days, client counts active/legacy, due_today, overdue
def board(db, *, kind: str = "prospects", q: str | None = None, flag: str | None = None, per_column: int = 50) -> Board    # columns in stage order, each with total + first `per_column` cards

# crm/hooks.py  (subscribers; each wraps its work so one failure cannot break the publisher)
def on_enquiry_decided(db, ev: EnquiryDecided) -> None
    # accept -> prospects.ensure_for_enquiry; discard -> if the enquiry has a prospect that is `new`/open, stage_source != "sheet" and no OTHER accepted enquiry is linked -> close(reason="enquiry_discarded")
def on_message_sent(db, ev: MessageSent) -> None
    # prospect = resolve_for_message (an accepted enquiry without a prospect gets one via ensure_for_enquiry); none -> return.
    # timeline `email_sent` (data: message_id; body: subject); contacts.upsert for each recipient + contact_links(prospect, role="recipient");
    # enquiries.date_email_sent = sent date when NULL; stage `new` -> mark_contacted(when=sent_at, channel="email");
    # a message with follow_up_of_id (or any send while the prospect is already contacted/follow-up) completes the EARLIEST open reminder and moves the prospect to its stage.
def on_reply_received(db, ev: ReplyReceived) -> None
    # prospect = resolve_for_message(ev.outreach_message_id); none -> return.
    # auto_reply / bounce: timeline note only (never counts as a response).
    # human: timeline `reply_received` (data: reply_id, intent) + record_response(source="reply");
    #   intent == "unsubscribe": flag no_follow_up (source auto, detail {"reason":"unsubscribe","reply_id":..}) + cancel reminders; NO lead;
    #   intent in ("interested","question","not_now") AND prospect.contacted_at is not None AND no open lead: leads.convert(stage="meeting_scheduled", opened_by="reply", actor=SYSTEM, note=<intent>);
    #   intent "other"/None: response recorded, no lead (a human decides).
def startup_backfill(db) -> None      # in crm/__init__.py: once per DB, guarded by app_meta key crm_flags_backfilled_v1
```
View classes (`services/crm/views.py`, frozen Pydantic, created in step 0 with these fields): `FlagView(kind, source, detail, created_at)`; `FollowUpView(id, prospect_id, lead_id, who, kind, kind_label, due_on, note, overdue: bool)`; `DuePanel(due_today, overdue)`; `TimelineItemView(id, kind, kind_label, body, data, actor, occurred_at, subject_type, subject_id, href: str | None)`; `ProspectView(id, name, kind, stage, stage_label, status, closed_reason, source, company_id, company_name, contact_id, contact_name, linkedin_profile_id, linkedin_url, tracked, contacted_at, contacted_estimated, responded_at, next_follow_up_on, flags, sheet_status, sheet_removed_at, sheet_tabs, open_lead_id, created_at)`; `ProspectDetail(ProspectView + notes, reason_url, reason_text, extra_link, enquiries: list[LinkedEnquiryView], messages: list[LinkedMessageView], replies: list[LinkedReplyView], contacts: list[ContactView], follow_ups, timeline, lead, client, sheet_sources: list[SheetSourceView])`; `LinkedEnquiryView(id, requirement_summary, priority, review_status, ai_verdict, date_added, source_url, company, sheet_status)`; `LinkedMessageView(id, subject, state, sent_at, reply_status, owner_name)`; `LinkedReplyView(id, message_id, received_at, kind, intent, snippet)`; `LeadView(id, prospect_id, prospect_name, company_name, stage, stage_label, opened_at, closed_at, meeting_at, proposal_sent_on, value_estimate, outcome_note, flags, days_since_activity)`; `LeadDetail(LeadView + timeline, follow_ups, client, enquiries, messages)`; `ClientView(id, name, company_id, status, source, started_on, lead_id, prospect_id)`; `ClientDetail(ClientView + notes, timeline, competitors)`; `ContactView(id, full_name, email, phone, linkedin_url, title, company_id, company_name, tags: list[TagView], origin)`; `ContactDetail(ContactView + emails, phones, notes, links: list[ContactLinkView(entity_type, entity_id, label, href, role)], timeline)`; `TagView(id, name, category, count)`; `DuplicateGroup(reason, contacts: list[ContactView])`; `CompanyView(id, name, website, city)`; `CompetitorView(id, name, website, approx_clients, client_names, contact, email, notes, active, sheet_synced: bool, mention_count)`; `GroupView(id, name, platform, link, details, keep_level, active, sheet_synced: bool)`; `PipelineSummary(prospects_by_stage, leads_by_stage, closed_recent, clients_active, clients_legacy, due_today, overdue)`; `Board(kind, columns: list[BoardColumn(stage, label, total, cards)])`; and the `*Page(items, total, page, page_size)` wrappers.

## S2 contract — SQL repository, mapping, metrics, CLI, scheduler, Sync page
```python
# services/sheetsync/sql_repository.py
class SqlSheetRepository:                  # implements SheetRepository (+ drain_errors); one short transaction per call (session_scope), never shares a session across calls
    def __init__(self, session_factory: Callable[[], Session] | None = None, *, clock=utcnow): ...
    def get_by_key(self, entity: str, key: str) -> StoredRecord | None       # built from sheet_sources rows with that (entity, key); crm = {} (opaque)
    def upsert(self, record: SheetRecord, sheet_source: SheetSource) -> None  # = mapping.apply_record + write/refresh the sheet_sources row; catches Exception -> rollback, remember (row_number, "apply-failed:<ExceptionName>"), do NOT store the new row hash
    def refresh_source(self, entity, key, sheet_source) -> None               # row_number only
    def mark_removed(self, sheet_id, gid, ids) -> int                         # delete that tab's sources; targets with no live source -> sheet_removed_at + timeline `sheet_removed` (actor sheet-sync); never deletes a row
    def list_keys_for_tab(self, sheet_id, gid) -> set[RecordId]
    def drain_errors(self) -> list[RowIssue]                                  # returned once, cleared; engine appends them to tab.errors
def install(session_factory=None) -> None  # register_repository_factory(lambda: SqlSheetRepository(...)); idempotent

# services/sheetsync/mapping.py  (the ONLY place that knows the sheet -> CRM column mapping of data.md)
@dataclass
class ApplyResult: action: str  # created | updated | restored | unchanged;  changed_fields: tuple[str, ...];  target_type: str;  target_id: int
def apply_record(db: Session, record: SheetRecord, *, origin: str, source: SheetSource | None, actor: Actor, now: datetime,
                 previous_fields: dict[str, Any] | None = None) -> ApplyResult
    # origin "sheet" (sync: source given, actor SHEET_SYNC, stage_source="sheet")
    # origin "import" (CSV import: source=None, no sheet_sources row, actor IMPORT; stage rules identical; a record whose key already belongs to a sheet-fed row is NOT written)
# services/sheetsync/status_map.py (pure): def map_enquiry_status(sheet_status, *, date_email_sent, date_proposal_sent) -> StatusOutcome(prospect_stage|None, prospect_status, closed_reason, lead_stage|None)
#                                          def map_person_status(sheet_status) -> StatusOutcome        # exactly the tables in data.md
# services/sheetsync/metrics.py: def apply_metric(db, record: SheetMetricRow, *, now) -> ApplyResult
```
Engine changes (S2): after every non-dry-run `upsert`, `repo.drain_errors()` (if the repository has it) is appended to `tab.errors`; a dry-run additionally asks the repository nothing new. `SheetSyncJob.available()`/`NO_REPOSITORY` in `jobs/sheet_sync_job.py` become "ready" once `install()` ran (the "Waiting for the Phase 2 CRM tables" note is deleted; the Schedule page then shows the job as ready, still opt-in). A test seam `jobs.sheet_sync_job.set_source_factory(factory | None)` lets tests give the runner a `StaticSource` (no network). CLI: `sync-sheets [--dry-run] [--tab NAME] [--memory]` — default repository = `SqlSheetRepository` (the DB must be at head, else exit 3 with "database is not migrated: run alembic upgrade head"); `--memory` forces the in-memory repository (used by the live dry-run gate: no DB needed); the output stays counts only. New command `crm-counts`: one line per table with its row count (`prospects=212 leads=9 ...`), counts only, used by the live-temp-DB gate. The runner's status survives restarts through the existing `SqlStateStore` (singleton `sheet_sync_state`).
**Sync page** (`web/routes/sync.py`): `GET /sync`, `POST /sync/run` ("Sync now": `get_scheduler().defs["sheet_sync"].runner(ctx).trigger_now()`), `GET /sync/status` (fragment, polls every 2 s while running), `GET /api/sync/status` (JSON of `SqlStateStore.load()` + runner status).

## S3 contract — shared login (`web/auth.py`, `web/routes/auth.py`)
```python
# web/auth.py
COOKIE = "sales0_session"
OPEN_PATHS = ("/login", "/logout", "/api/health", "/static/")      # plus "/__fixtures/" ONLY when settings.linkedin_mode == "fixture"
def password_fingerprint(settings) -> str                            # HMAC(master_secret, "pw:" + password)[:12]; changing the password invalidates every session
def issue_session(response: Response, settings, now=None) -> None   # value = crypto.sign(f"v1:{int(iat)}:{fp}", "session"); HttpOnly; SameSite=Lax; Path=/; Max-Age=session_days*86400; Secure iff settings.session_cookie_secure
def session_valid(request: Request, settings, now=None) -> bool
class LoginThrottle:                                                  # in-memory, per client IP (request.client.host; X-Forwarded-For is NOT trusted)
    def check(self, ip, now) -> int | None      # seconds of lockout left, or None
    def fail(self, ip, now) -> None; def reset(self, ip) -> None      # >= login_max_attempts failures within 60 s -> lockout login_lockout_s
class AuthMiddleware(BaseHTTPMiddleware): ...                         # sets request.state.auth_enabled (bool) on every request
def install(app: FastAPI, settings: Settings) -> None                # adds AuthMiddleware + includes routes.auth.router
```
Behaviour: see `capabilities/shared-login.md`. `web/middleware.py::allowed_origins` also accepts the origin of `settings.public_base_url` (so a VPS behind a domain passes the CSRF check). The header gets a "Log out" POST form (`#logout`) when `request.state.auth_enabled|default(false)`.

## S4 contract — UI routes
Routers: `web/routes/pipeline.py` (prefix none; paths in `api.md`), `contacts.py`, `registries.py`, `importer.py`. Routes never touch ORM objects: they call `services.crm.*` and `services.importer.*` and render `services.crm.views`. Acting member: `get_acting_member_optional` → `Actor.of_member(member)`. Every mutating route accepts htmx (returns the updated fragment + toast) and plain forms (303 + flash), exactly like `web/routes/outreach.py`. `context.py`: the four stub rows lose `phase=2` (NAV phase `None`), a `("Sync", "/sync", None)` row is added after Import, `shell_context` adds `due_count` (shown as a badge on Pipeline when > 0). "Draft email": `GET /outreach/compose?prospect_id=ID` pre-fills contact, email, company and requirement from the prospect's primary contact and newest enquiry, and `POST /outreach/draft` forwards the hidden `prospect_id` / `company_id` to `drafting.generate_draft` (which already accepts them); nothing else in the outreach flow changes (approval, sending, caps stay as built).

## S5 contract — digest sections and import services
```python
# services/crm/digest_sections.py
def register() -> None      # digest.register_section(DigestSection(key, title, order, build), replace=True) for the three sections below
#   follow_ups_due  (order 25) "Follow-ups due": items {"text": "Acme Training - Follow-up 1 due today (Priya Menon)", "href": "/pipeline/prospects/12"} (cap 10; overdue first, marked "overdue 3d"); stats {"due_today": n, "overdue": n}; empty_text "No follow-ups due."
#   stale_leads     (order 26) "Stale leads": open leads whose newest timeline event is older than settings.stale_lead_days; items {"text": "Acme - Proposal sent, quiet for 19 days", "href": "/pipeline/leads/4"}; stats {"stale": n, "open_leads": n}; empty_text "No stale leads."
#   pipeline_movement (order 27) "Pipeline movement": counts inside the digest window of new prospects, contacted, replies, leads opened, won; stats = the same counts
# services/importer/csv_import.py
def import_csv(db, *, filename: str, kind: str, data: bytes, actor: Actor, now=None) -> ImportBatchView
    # kind in the import_batches.kind values; decodes utf-8-sig/latin-1; parses with sheetsync.parsers.parse_tab (TabSpec(name, kind, "import", "0")) so every sheet quirk is handled the same way;
    # applies each record with sheetsync.mapping.apply_record(origin="import", source=None, actor=IMPORT); a record whose natural key already belongs to a sheet-fed row is "merged" (sheet wins, nothing overwritten);
    # kinds "clients" (name, company, started_on, notes) and "contacts" (name, email, phone, linkedin, company, tags) use small local parsers; idempotent (second import: new 0 / unchanged n); max 5 MB, 20 000 rows
# services/importer/paste.py
def paste_post(db, llm, *, text: str, link: str | None, author: str | None, author_profile_url: str | None, actor: Actor, now=None) -> PasteResult(enquiry_id, outcome)   # outcome: created | duplicate
    # normalises + dedupes with services.dedupe (same content_hash as discovery), creates an enquiry (origin="manual", review_status pending_review), triages it synchronously with the real triage service (one Haiku call), touches the author profile when a URL is given; paste_invalid 400 under 40 chars
```
`import_batches` rows are written by `import_csv`/`paste_post` (kind `paste`).

## Test plan (Phase 2)
| Level | What | Owner |
|---|---|---|
| unit | stages/ranks; status mapping tables (every row of data.md, case/whitespace variants, unknown status → rule 9); follow-up date maths with a frozen clock; session cookie sign/verify/expiry/password-change; throttle; contact matching + merge helpers; every new parser/mapping edge | S1, S2, S3 |
| integration (real SQLite) | **migration** on a fresh DB and on a **copy of a realistic DB** (S6 builds it at p5 head with ≥ 200 enquiries incl. accepted ones sharing an author, outreach messages + events + replies; asserts every old table's row count unchanged, `PRAGMA foreign_key_check` empty, downgrade -1 / upgrade round-trip, one head); services (accept idempotency across two enquiries from one profile; hooks: sent → Contacted + 3 reminders; reply interested/question/not_now → Lead only when contacted; unsubscribe → `no_follow_up` + reminders cancelled + no lead; auto_reply/bounce → no lead; Due today with a frozen clock; flags repeat_poster at 3 within 60 days, none at 2; competitor mention; contacts search < 300 ms at 10 000 rows; merge; tag filter); **sheet sync** with `httpx.MockTransport`/`StaticSource` over synthetic CSV fixtures `tests/fixtures/sheets/*.csv` (every quirk of `spec/capabilities/sheet-sync.md`): created counts per tab, sync twice = 0 changes, a sheet edit changes only sheet-owned columns, a CRM note/flag/stage/timeline survives a re-sync, a removed row → `sheet_removed_at` and restored on return, the same person in Outreach + Prospects = one prospect with two sources, a prospect adopted by LinkedIn slug when the app already has the profile, each status row of the mapping table, `apply-failed` row reported and retried, invariant `rows == added + updated + unchanged + error rows` on every tab, `sheet_metrics` weeks incl. the duplicate "Week 43"; the existing no-write guard test stays and a new test asserts that no code path under `src/sales0` can send a non-GET request to `docs.google.com` / `sheets.googleapis.com` (fetch guard + source scan, extended to the new modules); web routes (TestClient, real templates) for pipeline, contacts, registries, import, sync, login (password set: 303 `/login`, htmx 401 + `HX-Redirect`, JSON 401, wrong password no cookie, 6th attempt locked, `/api/health` open, logout, `next` is same-origin only) | S1-S5 |
| e2e journey (live server, headless Chromium, real Haiku; port 8792) | enable the password → log in (redirect when logged out) → discovery run (fixture mode) → accept an enquiry → prospect appears in Pipeline (New) → **Draft email** from the prospect → approve → send with the in-process `FakeGmailClient` → prospect is **Contacted**, three reminders exist → add a note → move the clock / set a follow-up for today → shows under **Due today** → fake human reply (`interested`) through the real reply poller → **Lead · Meeting scheduled** → Proposal sent (`date_proposal_sent` set) → Won → **Client** → timeline lists every step with timestamps; repeat poster flag after 3 fixture posts from one author; Sync page: "Sync now" with a static synthetic source shows per-tab counts and the synced prospects/competitors/groups, 0 changes on the second run | S6 |
| boot gate | server on 8767 with `SALES0_APP_PASSWORD` set: `/inbox` → 303 to `/login`, `/api/health` → 200; without the password `/inbox` → 200 | S3 test + S6 gate |
| live (read-only, counts only, never in `pytest`) | `sync-sheets --dry-run --memory` against the two REAL sheets: per-tab row counts and parse-error counts, Active Enquiries rows > 0; then `sync-sheets` into a TEMP DB (`SALES0_DATABASE_URL`/`SALES0_DATA_DIR` in the scratchpad) twice (second run: 0 added/updated/removed) and `crm-counts` | S6 |
All tests use temp DBs/dirs and ports ≥ 8766 (never 8765), never open linkedin.com, never write to Google.
