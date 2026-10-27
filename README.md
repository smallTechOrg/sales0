# sales0

AI sales and marketing agent for smallTech. sales0 searches LinkedIn for posts where a
company is looking for AI training or AI consulting, using the team's own logged-in
LinkedIn session. It merges duplicate posts, asks Claude Haiku once per new post whether it
is a genuine buyer enquiry (with a one-line reason, summary, priority, matching offering and
extracted contact details), and puts every result in a **Review inbox**. A person accepts,
discards or overrides each card. Every decision is stored and fed back into later triage
calls as examples (the learning loop). No spreadsheet is involved.

**Status: Phase 1 (Discovery → AI triage → Review inbox).** Inbox, Runs and Setup are real.
Pipeline, Contacts, Competitors & Groups, Import (Phase 2), Outreach (Phase 3), Metrics
(Phase 4) and WhatsApp (Phase 5) are labelled stub pages that say "Coming in Phase N".
That is expected, not a bug. The full plan is in `spec/roadmap.md`.

## Prerequisites

- macOS or Linux
- Python 3.12 (pinned in `.python-version`; asdf works)
- [uv](https://docs.astral.sh/uv/) (creates the project-local `.venv`; every command below goes through `uv run`)
- An Anthropic API key. Only the configured Haiku model is used.

## Setup

```bash
# working directory: /Users/sai/Workspace/Code/sales0
cp .env.example .env                   # then edit .env (see below); .env is gitignored, never commit it
uv sync                                # creates .venv, installs app + dev dependencies from uv.lock
uv run playwright install chromium     # the browser used for LinkedIn and for the E2E tests
uv run alembic upgrade head            # creates/migrates data/sales0.db
uv run alembic current                 # prints: 0002_phase1_discovery (head)
uv run python -m sales0.cli check-llm  # one real Haiku call; prints: LLM OK model=claude-haiku-5-5 ...
```

### `.env`

All settings use the `SALES0_` prefix. `.env.example` lists every variable with comments and
defaults. The ones you normally set:

| Variable | Needed | What it is |
|---|---|---|
| `SALES0_ANTHROPIC_API_KEY` | required | Your Anthropic API key. Startup fails if it is missing. |
| `SALES0_ANTHROPIC_WORKSPACE_ID` | optional | The Anthropic workspace id (`wrkspc_...`). It is needed only if your key is an org-level key that is **not** workspace-scoped. Anthropic then rejects calls with "must include anthropic-workspace-id header", and sales0 tells you to set this. Leave it blank for a workspace-scoped key. |
| `SALES0_LINKEDIN_MODE` | optional | `live` (default) uses real LinkedIn. `fixture` uses the built-in fake LinkedIn for offline demos and tests. |
| `SALES0_HOST` / `SALES0_PORT` | optional | Bind address, default `127.0.0.1:8765`. Phase 1 refuses to bind a non-loopback host. |
| `SALES0_LOG_FILE` | optional | Also write the JSON logs to this file, e.g. `data/logs/sales0.jsonl`. |
| `SALES0_SCRAPE_*` | optional | The LinkedIn safety caps and pacing (see below). |

## Run

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv run python -m sales0
```

Open http://127.0.0.1:8765/. You land on the Review inbox. Health check:
http://127.0.0.1:8765/api/health (`db_revision` is `0002_phase1_discovery`). Logs are JSON
lines on stdout, one per HTTP request (`"event": "http_request"`) and one per AI call
(`"event": "llm_call"`, with tokens and USD cost). Stop the server with Ctrl+C.

If the database is not migrated, startup stops with "Run: uv run alembic upgrade head".

### Using it (live LinkedIn)

1. **Setup**: the four default offerings are listed and can be edited. Add 3–5 keywords
   buyers use, for example `corporate AI training`, `GenAI workshop`, `AI training partner`.
2. **Connect LinkedIn** (Setup): a Chromium window opens with a dedicated profile under
   `data/linkedin-profile/`. Log in to the team LinkedIn account there and finish any 2FA.
   Within a few seconds of reaching the feed, the window closes and the header light turns
   green ("LinkedIn connected"). sales0 never sees or stores your password. Later runs reuse
   this profile. **Re-check** confirms the login is still valid.
3. **Runs → Run now**: live progress shows the current keyword, posts seen, new, duplicates
   and triaged. A run takes several minutes on purpose, because the pacing is human-like.
4. **Inbox**: each card shows the summary, the AI verdict with a one-line reason, priority,
   offering, extracted details, a link to the post, the full text and the AI cost. **Accept**
   moves a card to Accepted as "Prospect · New". **Discard** asks for a reason. Use the
   checkboxes for bulk accept or discard. Discarded enquiries are kept and can be searched.
5. **Runs → a run** shows the per-keyword log and the cost. The header shows the AI spend
   for today and this month.

### LinkedIn safety caps

sales0 only uses your own logged-in session and searches gently. You can change the caps in
`.env`. The Setup page shows the values in effect.

- At most `SALES0_SCRAPE_MAX_RUNS_PER_DAY` runs per local day (default 3; aborted runs
  count), `SALES0_SCRAPE_MAX_POSTS_PER_DAY` posts per day (150),
  `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` keywords per run (10) and
  `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` posts per keyword (15).
- Random 3–9 s delays between page actions and 20–60 s pauses between keywords.
- If LinkedIn shows a login wall, checkpoint, captcha or rate-limit page, the run **stops
  at once**. Nothing is retried and sales0 never logs in again by itself. The header light
  turns red, a "LinkedIn needs attention" banner appears on every page, and Run now is
  paused. To recover, resolve the check in a normal browser, wait a while, then press
  **Connect LinkedIn** again in Setup.

### Offline demo (fixture mode)

The same app can serve a built-in fake LinkedIn, so you can try the whole flow without a
LinkedIn account. Every page shows a `FIXTURE MODE` badge. The AI triage still makes real
Haiku calls.

```bash
# working directory: /Users/sai/Workspace/Code/sales0
SALES0_LINKEDIN_MODE=fixture uv run python -m sales0
```

Open http://127.0.0.1:8765/setup, add the keyword `ai workshop` (or the three keywords
`corporate ai training`, `ai workshop`, `ai consulting`), press **Connect LinkedIn** (the
fake login connects in the background, with no window), then **Runs → Run now**. The demo
keyword `checkpoint-test` shows what a safety stop looks like. The `.env` pacing still
applies, so with several keywords expect the 20–60 s pauses between them.

Fixture mode keeps its own browser profile (`data/linkedin-profile-fixture/`), but by
default it shares `data/sales0.db` with live mode. To keep demo data out of your real inbox,
point it at a separate demo database:

```bash
# working directory: /Users/sai/Workspace/Code/sales0
SALES0_DATABASE_URL=sqlite:///data/demo.db uv run alembic upgrade head
SALES0_DATABASE_URL=sqlite:///data/demo.db SALES0_LINKEDIN_MODE=fixture uv run python -m sales0
```

## Tests

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv run pytest tests/unit -q          # pure unit tests: no network, no browser
uv run pytest tests/integration -q   # real SQLite files, real headless Chromium, real Haiku
uv run pytest tests/e2e -q           # live server + headless Chromium + real Haiku
uv run pytest tests -q               # everything
```

- Integration and E2E tests make **real** Anthropic calls with the key from `.env`. A full
  run costs a few US cents. If the key is missing, the session fails ("SALES0_ANTHROPIC_API_KEY
  missing in .env"); it is not skipped.
- Tests never touch `data/sales0.db`. Each test uses a fresh temporary SQLite file migrated
  with Alembic.
- The E2E suite starts the real run command (`python -m sales0` from the project `.venv`) as
  a subprocess on port **8766** in fixture mode, with its own temp database and data dir.
  Headless Chromium then walks setup → Connect LinkedIn → Run now → inbox → accept → bulk
  discard → discarded search, restarts the server and checks that the state survived. It
  also covers a second run that learns from the decisions, and the checkpoint/captcha
  safety stops. Port 8766 must be free.

## Layout

See `spec/architecture.md` → Layout. Source is in `src/sales0/` (`web/` pages and routes,
`services/` domain logic, `jobs/` background runner, `scraper/` Playwright LinkedIn,
`agent/` + `prompts/` Haiku triage, `llm/` Anthropic adapter, `db/` models, and
`linkedin_fixture/` the fake LinkedIn). Migrations are in `migrations/` and tests in
`tests/`. Runtime data (database, browser profiles, logs) lives in the gitignored `data/`
directory.
