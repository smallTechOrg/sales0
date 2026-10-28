# sales0

AI sales and marketing agent for smallTech (AI corporate workshops, AI training, consulting).
sales0 finds buyer enquiries on LinkedIn, lets a person review them, tracks the relationship
in a small CRM, and drafts and sends outreach email from each person's own Gmail.

## What it does today

| Area | What is built |
|---|---|
| **LinkedIn discovery + triage** | Searches LinkedIn posts with the team's own logged-in session (slowly, with caps and safety stops), merges duplicates, and asks Claude Haiku once per new post whether it is a genuine buyer enquiry (reason, summary, priority, offering, contact details). |
| **Review inbox** | Only relevant posts are shown. Accept, discard (with a reason) or override; every decision is fed back into later triage as examples. |
| **CRM pipeline** | Accepted enquiries become Prospects, then Leads, then Clients, with Contacts, Competitors & Groups, Import (paste a post or CSV), per-record timeline and a "Due today" follow-up list. |
| **Google Sheet sync** | One-way, read-only import of the team's two Google Sheets into the CRM. sales0 never writes to Google. |
| **Outreach email** | Company research, a drafted email, human approval, send through **each person's own Gmail** (OAuth), deck attachment, follow-ups at +3 / +7 / +14 days. |
| **Reply tracking** | Read-only polling of each member's Gmail detects replies to sent outreach, classifies them and moves the prospect (for example to Lead). |
| **Digest, suggestions, costs** | Daily digest, proactive suggestions, AI spend view with an optional monthly cap. |
| **Scheduler** | Background jobs (nightly discovery window, daily digest, purge, sheet sync, profile watch) switched on/off on the **Schedule** page; sheet sync and profile watch are opt-in (disabled by default). |
| **Accounts and login** | Per-user email + password accounts with admin/member roles (see [Accounts and login](#accounts-and-login)), multiple LinkedIn accounts with per-account safety state. |
| **Prospect watch** | Track a LinkedIn profile (button on an inbox card or the **Tracked** page) and the watch job visits its recent-activity page for new posts, triages them like any other post and creates enquiries linked to the profile. Runs on the same LinkedIn account caps, pacing and cool-down as discovery; manual (**Run watch now**), scheduled (`profile_watch`, disabled until enabled on Schedule) or appended to discovery (`SALES0_WATCH_APPEND_TO_DISCOVERY`). A hit adds a note (and, for a prospect we had given up on, a follow-up) on the prospect's timeline. |
| **Metrics** | **Metrics** page: weekly and secondary tables computed from the CRM merged with the Sheet's figures (greater of app and sheet per cell, with sheet-first / app-first toggles), funnel, keyword and group yield, AI cost per relevant enquiry, manual weekly figures and target overrides, CSV export of every table. |
| **WhatsApp** | **Deferred by decision, not built.** The page is a labelled stub; no WhatsApp automation exists or is planned for now. |

Status of what is *planned only* (not in the code) is listed under [Roadmap](#roadmap-and-what-is-not-supported).
The full plan is in `spec/roadmap.md`.

## Quickstart (local, single user)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), an Anthropic API key, and Google
Chrome (or Playwright's Chromium) for LinkedIn.

```bash
# working directory: the repo root
cp .env.example .env                   # then set SALES0_ANTHROPIC_API_KEY (and SALES0_ANTHROPIC_WORKSPACE_ID if your key needs it)
uv sync
uv run playwright install chromium     # only needed when Google Chrome is not installed
uv run alembic upgrade head            # create / upgrade the database (the server refuses to start on an old schema)
uv run python -m sales0                # serves http://127.0.0.1:8765/
```

You land on the Review inbox. Health check: `http://127.0.0.1:8765/api/health` (reports the
database revision and which settings are present, never their values). Logs are JSON lines on
stdout, one per HTTP request and one per AI call (tokens and USD cost); cookies are never logged.
Stop with Ctrl+C. Next: connect LinkedIn (below), and optionally Gmail ([Gmail OAuth](#gmail-oauth-per-person)).

Check an install without the web UI:

```bash
uv run python -m sales0.cli check-llm        # one real Haiku call
uv run python -m sales0.cli check-browser    # launches the configured browser headless on a throwaway profile
```

## Accounts and login

**Open mode (default, local).** With no users and no `SALES0_APP_PASSWORD`, there is no login
and the server only starts on a loopback host (`127.0.0.1`). This is the right mode for one
person on one machine.

**Bootstrap password.** `SALES0_APP_PASSWORD` is a *bootstrap* secret, not a team password: it
is the first admin's initial password. Log in with `SALES0_ADMIN_EMAIL` (default
`admin@smalltech.in`) and that password; sales0 creates that admin account on the first login.
It only counts while no admin has a password of their own: once one does, the variable is
ignored and everyone signs in with their own email and password. Starting on a non-loopback
`SALES0_HOST` is refused unless the password is set. Login turns on as soon as the bootstrap
password is set or any member has a password; otherwise it is open mode (the Users page shows an "Open mode" warning).

```bash
SALES0_APP_PASSWORD=choose-a-long-random-password     # in .env, then restart
```

**Users page (admins).** An admin adds people (name, email, role `admin` or `member`). sales0
shows a **one-time set-password link** (valid `SALES0_PASSWORD_RESET_TTL_H` hours, shown once,
nothing is emailed: send it yourself) for that person; they choose their own password (minimum `SALES0_PASSWORD_MIN_LENGTH` characters).
Passwords are stored only as an argon2id hash. Members cannot open user management (403).
Each person can change their own password under **Account**; an admin can force a reset (new link).
Accept/discard/stage changes/sends are credited to the signed-in person on the timeline
("Accepted by Priya"); older records with no actor show "Team".

Sessions last `SALES0_SESSION_DAYS` (or `SALES0_SESSION_REMEMBER_DAYS` with "remember me");
repeated wrong passwords for one address + email are throttled (`SALES0_LOGIN_MAX_ATTEMPTS` /
`SALES0_LOGIN_LOCKOUT_S`). Behind a reverse proxy set `SALES0_TRUSTED_PROXIES` so the throttle
sees real client addresses ([Reverse proxy](#reverse-proxy-tls)).

## LinkedIn: connecting, running, safety

### Connecting LinkedIn (one time, local)

1. Open **Setup**. The LinkedIn card shows which browser sales0 will use, for example
   "Browser: Google Chrome 156".
2. Click **Connect LinkedIn**. Your installed **Google Chrome** opens a normal window on
   LinkedIn's login page. It uses a **dedicated sales0 profile** stored in
   `data/linkedin-chrome-profile/`. This is **separate from your everyday Chrome profile**:
   sales0 never reads or changes your own Chrome profiles, bookmarks, passwords or cookies,
   and it works while your normal Chrome is open.
3. Log in to the team LinkedIn account in that window and finish LinkedIn's 2FA and any
   "remember this browser" prompt. You do this **once**; later checks and runs reuse the
   login. sales0 never sees, types or stores your password.
4. Chrome may show a bar saying **"Chrome is being controlled by automated test software"**.
   That is sales0's window, and it is safe to continue.
5. A few seconds after your LinkedIn feed appears, the header light turns green ("LinkedIn
   connected") and the window closes by itself.
6. **Re-check** confirms the login is still valid. It runs invisibly, with no window.

**Runs are invisible.** Run now (and Re-check) use the same sales0 Chrome profile
**headless**: no browser window appears. Only Connect ever shows a window.

**No Google Chrome?** sales0 falls back to Playwright's built-in Chromium (installed by
`uv run playwright install chromium`) and the card says "Google Chrome not found — using the
built-in browser". That fallback uses its own profile, `data/linkedin-profile/`. Set
`SALES0_LINKEDIN_BROWSER_CHANNEL=chrome` if you would rather get an error than the fallback.

### Using it (live LinkedIn)

1. **Setup**: the four default offerings are listed and can be edited. **Keywords** lists 60
   buyer-intent phrases ("looking for AI trainer", "corporate AI training", "Claude Code
   trainer", "hiring AI consultant" ...) under the four offerings; edit, switch off or remove
   any. **Add suggested keywords** adds missing ones and never changes what you have ("Added 0 · 60
   already present"). Your own topic keywords stay allowed, but a topic ("agentic AI training")
   mostly finds thought leadership, so prefer buyer phrases. Each keyword shows its yield
   ("Scanned 412 · Relevant 6"); switch off keywords that never yield.
2. **Connect LinkedIn** once (above).
3. **Runs → Run now** returns at once and starts a **long background run**. The panel shows
   live counters (Scanned · New to us · Already seen · Relevant · Skipped by AI · Post links)
   and "Keyword 7/40". See "Long runs" below.
4. **Inbox** shows **only relevant posts** (what the AI judged a genuine buyer, or what you
   rescued). Each card shows the summary, the AI verdict with a one-line reason, priority,
   offering, extracted details, a link (**"View post ↗"** or **"Author profile (post link
   unavailable) ↗"**, see below), the full text and the AI cost. **Accept** moves a card to
   Accepted as "Prospect · New". **Discard** asks for a reason. Use the checkboxes for bulk
   accept or discard. Discarded enquiries are kept and can be searched.
5. **Inbox → Skipped by AI** lists the posts the AI rejected, with the reason, poster type,
   author and how often it was seen. Search by words, reason or author. **Rescue → enquiry**
   moves one to To review (the AI's verdict stays "Discard" so its mistake is visible);
   **Override: accept** rescues and accepts in one click. Both teach the AI. The full text is
   kept for 14 days (`SALES0_SEEN_RAW_RETENTION_DAYS`); after that the row says "text purged"
   and cannot be rescued. A "Triage failed" filter lists posts whose AI call failed, with **Retry triage**.
6. **Runs → a run** shows the per-keyword log and the cost. The header shows the AI spend
   for today and this month.

### Thorough discovery and long runs (Phase 1.4)

- **Depth:** per keyword sales0 scrolls the results (sorted latest, past week first, then past
  month) until it has 80 posts, or two scrolls in a row add nothing, or 20 scrolls are used.
  With the defaults a normal run **scans 200+ posts**.
- **Long runs:** a run takes **1-3 hours on purpose** (human pacing, deep scrolling). You can
  close the tab; the server keeps working. If you restart the server mid-run, the run reads
  "Interrupted by server restart" (no cool-down), and the next **Run now continues with the
  keywords it had not reached**. Re-scanning is free: nothing is triaged twice.
- **Nothing twice:** a post already seen (same LinkedIn id, same author and text, same text
  by anyone, or the same author and opening) is only counted as "Already seen": no AI call, no
  new row. Authors are remembered once each in `linkedin_profiles`.
- **Honest summary:** "scanned N · new N · relevant N (x%) · skipped by AI N · already seen N".
  A few percent relevant is normal; scanned posts are never called "enquiries".
- **Shorter runs:** set `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN=10` in `.env` (about 30 minutes),
  or tune `SALES0_TRIAGE_CONCURRENCY` and `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD`.

### Post links and LinkedIn's search layout (Phase 1.2)

LinkedIn's content search now uses a new page layout that no longer contains post ids or
post links. sales0 reads that layout (and still reads the older one), recognises the same
post again on later runs without a post id, and merges duplicates by their text and author.

After the AI has triaged a keyword's posts, and **only for the relevant ones**, sales0 opens each
post's own **"…"** menu (on every results layout) and presses **"Copy link to post"**, slowly,
like a person. It never presses any other menu item. sales0 intercepts that
copy inside its own browser page, so **your computer's clipboard is never read or changed**.
It does this for at most `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD` posts per keyword (default
15) and gives up on a keyword after 3 posts in a row yield no link. A relevant post still
without a link is retried on a later run.

Each Inbox card therefore links in one of two ways. Both are expected:

- **"View post ↗"** opens the post itself.
- **"Author profile (post link unavailable) ↗"** opens the author's LinkedIn profile, when
  the post link could not be found. The post is in their recent activity. A later run may
  find the link, and the card then shows "View post ↗".

Set `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD=0` in `.env` (and restart) to skip the menu
clicks entirely.

### LinkedIn safety: caps, stops, cool-down, layout warnings

sales0 only uses your own logged-in session and searches gently. You can change the caps in
`.env`. The Setup page ("Safety limits") shows the values in effect.

- **Caps:** at most `SALES0_SCRAPE_MAX_RUNS_PER_DAY` runs per local day (default 4; aborted
  runs count), `SALES0_SCRAPE_MAX_POSTS_PER_DAY` posts scanned per day (2000),
  `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` keywords per run (40) and
  `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` posts per keyword (80).
- **Pacing:** random 3–9 s delays between page actions and 20–60 s pauses between keywords.
- **Safety stops:** if LinkedIn logs the session out, or shows a checkpoint, captcha or rate
  limit (including a throttled background request), the run **stops at once** and the
  browser closes. Nothing is retried and sales0 never logs in by itself. The header light
  turns red, a "LinkedIn needs attention" banner appears on every page, and Run now is
  paused. Posts saved before the stop are kept and triaged. The keyword that was being
  searched is searched last next time. To recover, press **Connect LinkedIn** in Setup: the
  sales0 Chrome window opens on LinkedIn so you can resolve it there.
- **Cool-down:** even after the light is green again, Run now stays disabled for 60 minutes
  after a rate limit or captcha, and 15 minutes after a checkpoint or logout
  (`SALES0_SAFETY_COOLDOWN_MIN`, `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN`). The Runs page says
  "LinkedIn asked us to slow down — next run allowed after HH:MM". Re-check and Connect do
  not shorten it.
- **Layout warnings:** sales0 reads LinkedIn by its session cookie, URLs and stable page
  attributes (such as `data-view-name` and `data-testid`), not by styling classes. If a keyword's results still can't be read, the run
  shows a yellow "LinkedIn's page layout may have changed" alert and a "Layout warning"
  badge, and that keyword's Message in the run detail says what was missed. It is a warning,
  not a problem with your account; runs stay allowed. Please report the keyword.

### Offline demo (fixture mode)

The same app can serve a built-in fake LinkedIn, so you can try the whole flow without a
LinkedIn account. Every page shows a `FIXTURE MODE` badge. The AI triage still makes real
Haiku calls. Keep demo data out of your real inbox with a separate demo database:

```bash
# working directory: the repo root
SALES0_DATABASE_URL=sqlite:///data/demo.db uv run alembic upgrade head
SALES0_DATABASE_URL=sqlite:///data/demo.db SALES0_LINKEDIN_MODE=fixture uv run python -m sales0
```

Open http://127.0.0.1:8765/setup, add the keyword `ai workshop` (or the three keywords
`corporate ai training`, `ai workshop`, `ai consulting`) and press **Connect LinkedIn**. The
suggested 60 phrases are served by a generated bulk corpus of 300 synthetic posts (90 per
phrase, with infinite scroll), which is what a 200+ post demo run reads. In
fixture mode no window opens: a hidden browser walks the fake login, a fake 2FA code page
and a "remember this browser" page, then the light turns green. Then **Runs → Run now**.

Demo keywords show the safety features: `checkpoint-test`, `captcha-test`, `ratelimit-test`
and `logout-test` stop the run (with the banner and, unless set to 0, the cool-down), and
`layout-drift-test` produces the layout-drift alert. `legacy-urn-test` shows every demo post
in LinkedIn's older layout, and `permalink-miss-test` shows posts whose "Copy link to post"
yields nothing (their cards link to the author profile). The `.env` pacing still applies, so with
several keywords expect the 20–60 s pauses between them. Fixture mode keeps its own browser
profile (`data/linkedin-profile-fixture-chrome/`, or `…-chromium/` with the built-in browser),
never your real sales0 login.

## CRM and Google Sheet sync

```bash
# working directory: the repo root
uv run alembic upgrade head          # once after pulling
uv run python -m sales0              # the same run command as always
```

**What you get.** Accept an enquiry in the Inbox and it appears in **Pipeline** as a
*Prospect · New* (one prospect per LinkedIn profile: accepting a second enquiry from the same
person adds to the same prospect). Press **Draft email** on it, approve and send as before:
the prospect becomes **Contacted** and reminders are created at +3 / +7 / +14 days (the
*Due today* panel at the top of Pipeline lists what is due). When the person replies
"interested" (or you press **Response received**) it converts to a **Lead** (*Meeting
scheduled → Proposal sent → Won / Lost / No response*); **Won** creates a **Client**. Every
step is on the record's timeline, and you can add notes. **Contacts** is one shared address
book (search, tags, merge duplicates); **Competitors & Groups** are flagged when an enquiry
mentions a competitor; **Import** takes a single pasted post (triaged by Haiku like any
scraped post) or a CSV of data that is not in your Sheets.

**Google Sheet sync (read-only).** The team's two Sheets are synced into the CRM one way:
the Sheet is the source of truth for the fields it holds, and everything added in sales0
(notes, stages you move, tags, follow-ups, timeline) is kept. **sales0 never writes to Google.**
Settings (all optional) are in the [Configuration](#configuration) table (`SALES0_SHEET_*`). The Sheets must be readable by "anyone with the link".

Open **Sync** in the navigation: it shows the last sync (status, time, per-tab counts of rows /
added / updated / unchanged / removed / errors, counts only, never cell content) and has
**Sync now**, a dry-run preview and the switch for the automatic interval. Rows that disappear
from a Sheet are only hidden in the CRM, never deleted. The same engine runs from the shell:

```bash
uv run python -m sales0.cli sync-sheets --dry-run --memory   # per-tab counts, nothing stored, no database needed
uv run python -m sales0.cli sync-sheets                      # sync into the database (must be migrated)
uv run python -m sales0.cli sync-sheets --tab "Competitors"  # one tab only
uv run python -m sales0.cli crm-counts                       # one line of row counts per CRM table (counts only)
```

Run `sync-sheets` twice: the second run reports 0 added / updated / removed.

**Upgrading an existing database.** `uv run alembic upgrade head` keeps every row. Accepted
enquiries are turned into prospects (one per LinkedIn author) with a timeline, so the
Pipeline is not empty after the upgrade. Back up `data/sales0.db` (with its `-wal`/`-shm`)
first if you want a way back: `uv run alembic downgrade -1` also works.

## Prospect watch and Metrics

**Tracked** lists the profiles being watched (last check, checks, hits, pause/untrack) and has **Run watch now** with
live progress. Profiles become tracked from an inbox card (Track), from the Tracked page, or automatically when a
prospect is created; closing a prospect as lost / no response / not interested untracks it. A profile is visited at
most every `SALES0_WATCH_MIN_DAYS_BETWEEN_CHECKS` days, at most `SALES0_WATCH_MAX_PROFILES_PER_RUN` per run, with a
pause between visits. Only the profile's own recent posts are triaged. A watch run is a normal run on the run's
LinkedIn account: it shares that account's overlap protection, safety cool-down and daily post cap with discovery
(it does not use up the daily run allowance), and a safety stop (checkpoint, captcha, logout, 429) stops it.

**Metrics** shows the same weekly numbers the team keeps in its Sheet, computed from the CRM. The *LinkedIn profile
watch* row counts tracked profiles actually visited by the watch job that week (not manual track/untrack changes).
Edit manual weekly figures (for example blog posts) and per-source targets on the page; export any table as CSV.

## Configuration

Every setting is an environment variable with the `SALES0_` prefix, read from the process
environment and the repo-root `.env` (copy `.env.example`). Blank means "use the default".
This table is **generated from the code** (`uv run python -m sales0.cli env-docs`) and a unit test fails if it
drifts; to refresh it run `uv run python -m sales0.cli env-docs --write README.md`. Secrets show no value.

<!-- env-docs:start -->

| Variable | Type | Default | Meaning |
| --- | --- | --- | --- |
| `SALES0_ANTHROPIC_API_KEY` | secret | (secret) | Anthropic API key for triage, research, drafting and the digest summary. Required to start the server. |
| `SALES0_ANTHROPIC_WORKSPACE_ID` | str | unset | Only when the key is not scoped to a workspace: sent as the `anthropic-workspace-id` header. |
| `SALES0_LLM_MODEL` | str | `claude-haiku-5-5` | Model slug used for every LLM call. |
| `SALES0_LLM_MAX_OUTPUT_TOKENS` | int | `900` | Max output tokens per LLM call. |
| `SALES0_LLM_TIMEOUT_S` | float | `30.0` | Per-call LLM timeout in seconds. |
| `SALES0_LLM_MAX_RETRIES` | int | `3` | Retries on transient LLM errors. |
| `SALES0_LLM_PRICE_INPUT_USD_PER_MTOK` | float | `0.1` | Input price (USD per 1M tokens) used for the spend estimate. |
| `SALES0_LLM_PRICE_OUTPUT_USD_PER_MTOK` | float | `0.5` | Output price (USD per 1M tokens) used for the spend estimate. |
| `SALES0_HOST` | str | `127.0.0.1` | Bind address. A non-loopback host refuses to start without `SALES0_APP_PASSWORD` (or an existing user account). |
| `SALES0_PORT` | int | `8765` | Bind port. |
| `SALES0_DATA_DIR` | str | `data` | Runtime directory: SQLite DB, Chrome profile, secret key, uploads. Put it on a persistent volume when hosted. |
| `SALES0_DATABASE_URL` | str | `sqlite:///data/sales0.db` | SQLAlchemy URL. SQLite only today; relative file paths resolve against the repo root. Postgres is not supported yet. |
| `SALES0_TEST_DATABASE_URL` | str | unset | Test-suite only: database URL used by tests instead of a temp file. |
| `SALES0_GOOGLE_CLIENT_SECRET_PATH` | str | `data/google_client_secret.json` | Google OAuth client JSON (Desktop `installed` or Web `web` type) used for Gmail. |
| `SALES0_GOOGLE_TOKEN_PATH` | str | `data/google_token.json` | Token file written by the `google-auth` CLI command (single-user fallback; team members' tokens live encrypted in the DB). |
| `SALES0_SECRET_KEY` | secret | (secret) | Encrypts Gmail tokens and LinkedIn sessions at rest and signs cookies (>= 16 chars or a Fernet key). Unset: generated once into `<data_dir>/secret.key`. Set it explicitly on a server and back it up: losing it makes stored tokens unreadable. |
| `SALES0_ADMIN_EMAIL` | str | `admin@smalltech.in` | Email of the first admin team member created on an empty database. |
| `SALES0_ADMIN_NAME` | str | `Admin` | Display name of that first admin. |
| `SALES0_GMAIL_DAILY_SEND_CAP` | int | `40` | Max outreach emails sent per member per day (0 disables sending). |
| `SALES0_DECK_MAX_BYTES` | int | `10485760` | Max size of one uploaded deck PDF, in bytes. |
| `SALES0_PUBLIC_BASE_URL` | str | unset | Public origin when hosted (e.g. `https://sales.example.com`). Builds the Google OAuth redirect (`<url>/oauth/google/callback`, must match a registered redirect URI exactly) and turns on the Secure cookie flag when it starts with `https://`. |
| `SALES0_GOOGLE_AUTH_ENDPOINT` | str | `https://accounts.google.com/o/oauth2/v2/auth` | Google authorize endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_TOKEN_ENDPOINT` | str | `https://oauth2.googleapis.com/token` | Google token endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_USERINFO_ENDPOINT` | str | `https://openidconnect.googleapis.com/v1/userinfo` | Google userinfo endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_REVOKE_ENDPOINT` | str | `https://oauth2.googleapis.com/revoke` | Google revoke endpoint (override for tests/proxies). |
| `SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS` | str | empty | Test only: hosts the research fetcher may reach although private. Ignored unless `SALES0_LINKEDIN_MODE=fixture`. |
| `SALES0_TIMEZONE` | str | `Asia/Kolkata` | IANA timezone for the scheduler, digest and daily caps. |
| `SALES0_LOG_LEVEL` | DEBUG \| INFO \| WARNING \| ERROR \| CRITICAL | `INFO` | Log level. |
| `SALES0_LOG_FORMAT` | json \| console | `json` | `json` or `console`. |
| `SALES0_LOG_FILE` | str | unset | Also write logs to this file (optional). |
| `SALES0_LINKEDIN_MODE` | live \| fixture | `live` | `live` drives the real LinkedIn; `fixture` uses the local fake site (tests/demo). |
| `SALES0_LINKEDIN_BROWSER_CHANNEL` | auto \| chrome \| chromium | `auto` | `auto` (installed Chrome, else bundled Chromium), `chrome`, or `chromium`. |
| `SALES0_LINKEDIN_CONNECT_TIMEOUT_S` | int | `300` | How long the headed LinkedIn login window may stay open. |
| `SALES0_SAFETY_COOLDOWN_MIN` | int | `60` | Minutes a LinkedIn account stays paused after a rate-limit/captcha stop (0 = off). |
| `SALES0_SAFETY_COOLDOWN_CHECKPOINT_MIN` | int | `15` | Minutes paused after a checkpoint/logout stop. |
| `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN` | int | `40` | Max search phrases per discovery run. |
| `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` | int | `80` | Max posts read per phrase. |
| `SALES0_SCRAPE_MAX_POSTS_PER_DAY` | int | `2000` | Max posts read per day. |
| `SALES0_SCRAPE_MAX_RUNS_PER_DAY` | int | `4` | Max discovery runs per day. |
| `SALES0_SCRAPE_DELAY_MIN_S` | float | `3.0` | Min human-like delay between actions (seconds). |
| `SALES0_SCRAPE_DELAY_MAX_S` | float | `9.0` | Max delay between actions (seconds). |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S` | float | `20.0` | Min pause between phrases (seconds). |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S` | float | `60.0` | Max pause between phrases (seconds). |
| `SALES0_SCRAPE_MAX_SCROLLS` | int | `20` | Max scrolls per search pass. |
| `SALES0_SCRAPE_NO_GROWTH_SCROLLS` | int | `2` | Consecutive scrolls without new cards that end a pass. |
| `SALES0_SCRAPE_SORT` | latest \| relevance | `latest` | `latest` or `relevance` search ordering. |
| `SALES0_SCRAPE_DATE_FILTERS` | list | `past-week,past-month` | Comma list of date filters, one search pass each: `past-24h`, `past-week`, `past-month`. |
| `SALES0_SCRAPE_DATE_FILTER` | str | unset | Legacy single date filter; read only when `SALES0_SCRAPE_DATE_FILTERS` is unset. |
| `SALES0_SCRAPE_VARIANTS` | bool | `True` | Run extra per-phrase search variants in a daily rotation. |
| `SALES0_SCRAPE_MAX_PASSES_PER_KEYWORD` | int | `6` | Cap on search passes per phrase. |
| `SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD` | int | `40` | Max "see more" clicks per phrase. |
| `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD` | int | `15` | Max "Copy link to post" attempts per phrase (0 = off). |
| `SALES0_FEWSHOT_MAX_EXAMPLES` | int | `6` | Max reviewed examples shown to triage as few-shot context. |
| `SALES0_FEWSHOT_TEXT_CHARS` | int | `600` | Characters kept per few-shot example. |
| `SALES0_TRIAGE_MAX_INPUT_CHARS` | int | `4000` | Characters of a post sent to triage. |
| `SALES0_TRIAGE_CONCURRENCY` | int | `4` | Parallel triage calls. |
| `SALES0_SEEN_RAW_RETENTION_DAYS` | int | `14` | Days raw seen-post text is kept. |
| `SALES0_SEED_SUGGESTED_KEYWORDS` | bool | `True` | Seed the suggested search phrases on first run. |
| `SALES0_REPLY_POLL_MINUTES` | int | `10` | Minutes between Gmail reply checks (0 disables the poller). |
| `SALES0_REPLY_LOOKBACK_DAYS` | int | `60` | Only sent messages younger than this are polled for replies. |
| `SALES0_REPLY_MAX_THREADS_PER_POLL` | int | `300` | Max threads checked per member per cycle. |
| `SALES0_REPLY_OWN_ALIASES` | str | empty | Comma list of extra addresses that are "us" (Send-As aliases); never counted as replies. |
| `SALES0_SHEET_SYNC_MINUTES` | int | `30` | Minutes between Google Sheet syncs (0 disables). |
| `SALES0_SHEET_CLIENT_SOURCING_ID` | str | `1xUdfMXUKFRNd3RXTq-PAynlbFqyI5sfWUtEHDtlw3ag` | Spreadsheet id of the client-sourcing sheet. |
| `SALES0_SHEET_METRICS_ID` | str | `1tnwiBRMfp_QR0CmAkxZM_Chl-VAixmcWPxE1NCGED5w` | Spreadsheet id of the metrics sheet. |
| `SALES0_SHEET_TABS_JSON` | str | unset | Optional JSON list replacing the default tabs: `[{"name","kind","gid","sheet_id"?}]`. |
| `SALES0_SHEET_FETCH_TIMEOUT_S` | float | `20.0` | Sheet fetch timeout in seconds. |
| `SALES0_SHEET_FETCH_RETRIES` | int | `3` | Sheet fetch retries. |
| `SALES0_SHEET_FETCH_MAX_BYTES` | int | `20000000` | Max bytes read from a sheet export. |
| `SALES0_APP_PASSWORD` | secret | (secret) | Bootstrap password: creates the first admin account when no users exist (and is the shared login until then). Unset = open mode, loopback only. Not a per-user password. |
| `SALES0_SESSION_DAYS` | int | `14` | Login session lifetime in days. |
| `SALES0_LOGIN_MAX_ATTEMPTS` | int | `5` | Failed logins per client before lockout. |
| `SALES0_LOGIN_LOCKOUT_S` | int | `60` | Lockout duration in seconds. |
| `SALES0_SESSION_REMEMBER_DAYS` | int | `30` | Lifetime of a "remember me" login cookie, in days. |
| `SALES0_TRUSTED_PROXIES` | str | empty | Comma list of IPs/CIDRs of reverse proxies whose `X-Forwarded-For` is trusted for client IPs (login throttle). Empty = never trust it. |
| `SALES0_PASSWORD_RESET_TTL_H` | int | `24` | Hours an admin-issued set-password link stays valid. |
| `SALES0_PASSWORD_MIN_LENGTH` | int | `10` | Minimum length of a user password. |
| `SALES0_LINKEDIN_SESSION_BACKEND` | folder \| db | `folder` | Where NEW LinkedIn accounts keep their session: `folder` (Chrome profile under the data dir) or `db` (encrypted storage state). |
| `SALES0_FOLLOWUP_DAYS_1` | int | `3` | Days after the first email before follow-up 1 is due. |
| `SALES0_FOLLOWUP_DAYS_2` | int | `7` | Days before follow-up 2. |
| `SALES0_FOLLOWUP_DAYS_FINAL` | int | `14` | Days before the final follow-up. |
| `SALES0_STALE_LEAD_DAYS` | int | `14` | Days without activity before a lead counts as stale. |
| `SALES0_REPEAT_POSTER_MIN` | int | `3` | Posts needed to flag someone as a repeat poster. |
| `SALES0_REPEAT_POSTER_DAYS` | int | `60` | Window (days) for the repeat-poster count. |
| `SALES0_CRM_PAGE_SIZE` | int | `50` | Rows per page in CRM lists. |
| `SALES0_MONTHLY_SPEND_CAP_USD` | float | `0.0` | Monthly AI spend cap in USD (0 = no cap). Over the cap every LLM feature stops. |
| `SALES0_SPEND_CAP_WARN_PCT` | int | `80` | Percent of the cap at which a warning shows. |
| `SALES0_SCHEDULER_ENABLED` | bool | `True` | False = no background scheduler thread (pages still work). |
| `SALES0_SCHEDULER_POLL_S` | float | `5.0` | Scheduler tick in seconds. |
| `SALES0_SCHEDULER_WINDOW_START` | str | `01:00` | Nightly discovery window start (local `HH:MM`). |
| `SALES0_SCHEDULER_WINDOW_END` | str | `05:00` | Nightly discovery window end (local `HH:MM`, same day). |
| `SALES0_SCHEDULER_DISCOVERY_MAX_RUNS_PER_DAY` | int | `1` | Scheduled discovery runs per day (also bounded by `SALES0_SCRAPE_MAX_RUNS_PER_DAY`). |
| `SALES0_SCHEDULER_DIGEST_TIME` | str | `07:30` | Local time the daily digest is built. |
| `SALES0_SCHEDULER_PURGE_TIME` | str | `04:15` | Local time of the nightly purge. |
| `SALES0_SCHEDULER_WATCH_TIME` | str | `05:30` | Local time the `profile_watch` job (visit tracked profiles) runs; the job is disabled until enabled on /schedule. |
| `SALES0_WATCH_MAX_PROFILES_PER_RUN` | int | `25` | Prospect watch: max tracked profiles visited per watch run. |
| `SALES0_WATCH_MIN_DAYS_BETWEEN_CHECKS` | float | `3.0` | Prospect watch: minimum days before the same profile is visited again. |
| `SALES0_WATCH_MAX_POSTS_PER_PROFILE` | int | `15` | Prospect watch: max posts read per profile visit (also bounded by the daily post cap). |
| `SALES0_WATCH_PROFILE_PAUSE_MIN_S` | float | `20.0` | Prospect watch: minimum pause between two profile visits (seconds). |
| `SALES0_WATCH_PROFILE_PAUSE_MAX_S` | float | `60.0` | Prospect watch: maximum pause between two profile visits (seconds). |
| `SALES0_WATCH_APPEND_TO_DISCOVERY` | bool | `False` | True = also visit due tracked profiles at the end of each discovery run (same browser session and caps). |
| `SALES0_PURGE_RETENTION_DAYS` | int | `90` | Days of raw data kept before purge. |
| `SALES0_DIGEST_AI_SUMMARY` | bool | `True` | One LLM call adds a short summary to the digest. |

<!-- env-docs:end -->

## Deploying on a single server

sales0 is built as a single-process app (one uvicorn worker, a background scheduler thread,
SQLite). One server, one process, one persistent data directory. Do not run two instances on
the same data directory.

### Layout and persistence

- Put **`SALES0_DATA_DIR`** (an absolute path, for example `/var/lib/sales0`) on a persistent
  disk or volume. It holds the SQLite database (`sales0.db`, plus `-wal` and `-shm`), uploaded
  decks, the generated `secret.key` (when `SALES0_SECRET_KEY` is unset), Chrome profiles for
  LinkedIn accounts, and logs if you point `SALES0_LOG_FILE` there. Relative paths resolve
  against the repo root, so set absolute paths on a server.
- Set `SALES0_DATABASE_URL=sqlite:////var/lib/sales0/sales0.db` (four slashes = absolute path).
- Set `SALES0_SECRET_KEY` explicitly (for example `openssl rand -base64 48`) and keep a copy
  outside the server. It encrypts Gmail tokens and stored LinkedIn sessions; if it is lost
  those become unreadable and every member must reconnect.
- Apply schema changes before starting a new version: `uv run alembic upgrade head`.

### systemd

`/etc/systemd/system/sales0.service` (paths and user are examples):

```ini
[Unit]
Description=sales0
After=network-online.target
Wants=network-online.target

[Service]
User=sales0
Group=sales0
WorkingDirectory=/opt/sales0
EnvironmentFile=/etc/sales0.env
ExecStartPre=/usr/local/bin/uv run --frozen alembic upgrade head
ExecStart=/usr/local/bin/uv run --frozen python -m sales0
Restart=on-failure
RestartSec=5
# modest hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/var/lib/sales0 /opt/sales0

[Install]
WantedBy=multi-user.target
```

`/etc/sales0.env` (mode 600, owned by root or `sales0`) holds the same `KEY=value` lines as
`.env`: at minimum `SALES0_ANTHROPIC_API_KEY`, `SALES0_SECRET_KEY`, `SALES0_DATA_DIR`,
`SALES0_DATABASE_URL`, `SALES0_APP_PASSWORD`, `SALES0_PUBLIC_BASE_URL`, `SALES0_TRUSTED_PROXIES`.
Then `systemctl daemon-reload && systemctl enable --now sales0`, and `journalctl -u sales0 -f`.
Adjust `ReadWritePaths` if your data directory is elsewhere. This unit is a template that has not been
run against a real server here; check paths, the `uv` location and permissions on first use.

### Docker (sketch, untested)

There is no Dockerfile in the repo. A sketch, **untested**, to adapt:

```dockerfile
FROM python:3.12-slim
RUN pip install uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
RUN uv run playwright install --with-deps chromium
ENV SALES0_HOST=0.0.0.0 SALES0_DATA_DIR=/data SALES0_DATABASE_URL=sqlite:////data/sales0.db
VOLUME /data
EXPOSE 8765
CMD ["sh", "-c", "uv run alembic upgrade head && uv run python -m sales0"]
```

Run with `-v sales0-data:/data --env-file sales0.env -p 127.0.0.1:8765:8765` and put a TLS
reverse proxy in front. Binding `0.0.0.0` inside the container requires `SALES0_APP_PASSWORD`
(startup refuses otherwise). Keep `/data` on a named volume or host path, never in the image layer.

### Reverse proxy (TLS)

Terminate TLS at a proxy (Caddy, nginx, a cloud load balancer) and forward to sales0 on
`127.0.0.1:8765`. Set:

- `SALES0_PUBLIC_BASE_URL=https://sales.example.com`: used to build the Google OAuth redirect
  and, when it starts with `https://`, marks the session cookies `Secure`.
- `SALES0_TRUSTED_PROXIES=127.0.0.1` (the proxy's address, or a CIDR): only then is
  `X-Forwarded-For` trusted for the client address used by the login throttle. Leave it empty
  when sales0 faces clients directly, otherwise anyone could spoof their address.
- **Always set `SALES0_APP_PASSWORD` and create user accounts on a server**, even though sales0
  itself listens on loopback behind the proxy: a loopback bind with nothing configured is "open
  mode", and the proxy would expose it to the internet.

Caddy example:

```
sales.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

### Backups (SQLite)

The database runs in WAL mode, so copying `sales0.db` with `cp` while the app runs can produce a
broken backup (committed data may still sit in `sales0.db-wal`). Use SQLite's online backup,
which is consistent and safe while the server is running:

```bash
mkdir -p /var/backups/sales0
sqlite3 /var/lib/sales0/sales0.db ".backup '/var/backups/sales0/sales0-$(date +%F-%H%M).db'"
sqlite3 /var/backups/sales0/sales0-<stamp>.db "PRAGMA integrity_check;"     # expect: ok
```

The backup file is a single self-contained database; you do not copy `-wal`/`-shm` with it.
If the `sqlite3` CLI is not installed, the same thing with Python:

```bash
uv run python - <<'PY'
import sqlite3, datetime
src = sqlite3.connect("/var/lib/sales0/sales0.db")
dst = sqlite3.connect(f"/var/backups/sales0/sales0-{datetime.datetime.now():%F-%H%M}.db")
src.backup(dst); dst.close(); src.close()
PY
```

Also back up the rest of the data dir that is not in the database: `secret.key` (if you did not
set `SALES0_SECRET_KEY`), `decks/` and your `.env`/`/etc/sales0.env`. Cron (daily at 02:30, keep 14 days):

```cron
30 2 * * * sales0 sqlite3 /var/lib/sales0/sales0.db ".backup '/var/backups/sales0/sales0-$(date +\%F).db'" && find /var/backups/sales0 -name 'sales0-*.db' -mtime +14 -delete
```

Copy the backups off the machine (object storage, another host). Restore:

```bash
systemctl stop sales0
cd /var/lib/sales0
mv sales0.db sales0.db.broken; rm -f sales0.db-wal sales0.db-shm   # stale WAL files must not be mixed with a restored file
cp /var/backups/sales0/sales0-<stamp>.db sales0.db
chown sales0:sales0 sales0.db
sqlite3 sales0.db "PRAGMA integrity_check;"
uv run alembic upgrade head        # from /opt/sales0, in case the backup is from an older version
systemctl start sales0
```

Before upgrading the app or running a migration, take a backup first; `alembic downgrade -1` exists but a backup is the safe way back.

## Gmail OAuth (per person)

Each person who sends outreach connects **their own** Gmail on the **Team** page; mail always
goes out from their own address. The refresh token is stored encrypted in the database
(`SALES0_SECRET_KEY`). The app requests `gmail.modify` (drafts, send, read for reply tracking,
trash for test cleanup), `openid` and the account email. Reply tracking only reads.

One-time Google Cloud setup (details and error table in [docs/gmail-setup.md](docs/gmail-setup.md)):

1. Create a project, enable the **Gmail API**.
2. **OAuth consent screen**: for a Google Workspace domain choose **Internal** (no Google review,
   only users in your domain). For personal `@gmail.com` accounts choose External + **Testing**
   and add each person under *Test users* (Testing-mode refresh tokens expire after 7 days).
3. **Credentials -> Create OAuth client ID**, then download the JSON to
   `SALES0_GOOGLE_CLIENT_SECRET_PATH` (default `data/google_client_secret.json`):
   - **Local use:** type *Desktop app*. The JSON has an `installed` block. Google allows
     `http://127.0.0.1` / `localhost` on any port, so open sales0 on `http://127.0.0.1:<port>/team`
     and press *Connect my Gmail*.
   - **Hosted:** type **Web application**. The JSON has a `web` block. Add this exact value under
     *Authorized redirect URIs* (scheme, host, path, no trailing slash must all match):
     `<SALES0_PUBLIC_BASE_URL>/oauth/google/callback`, for example
     `https://sales.example.com/oauth/google/callback`. Set `SALES0_PUBLIC_BASE_URL` to the
     public origin and restart. A Desktop client cannot be used for a hosted deployment because
     Google only accepts loopback redirects for it.
4. Open **Team**, sign in as the person, press **Connect my Gmail**. The Team page shows the exact
   redirect URI to register when `SALES0_PUBLIC_BASE_URL` is set. If the redirect is not in the
   JSON's `redirect_uris`, or a Desktop client is used with a public URL, the Connect press shows
   a clear error naming the URI instead of sending you to a Google error page. The connecting
   Google account must match the member's email.

The single-user CLI path (`uv run python -m sales0.cli google-auth`) still works with a Desktop client.
`gmail.modify` is a *restricted* scope: Internal apps are exempt, but an External app used by
people outside one domain needs Google's OAuth verification (see the roadmap).

## LinkedIn on a server

LinkedIn is the fragile part of a hosted setup:

- A server has no screen, so the headed Chrome login window cannot be used. On the **Setup**
  page add a LinkedIn account by **pasting the `li_at` cookie** from a browser where you are
  logged in (browser dev tools -> Application -> Cookies -> `li_at`). The session is stored
  encrypted in the database (an account created from a pasted cookie uses the `db` backend; `SALES0_LINKEDIN_SESSION_BACKEND`
  picks the backend for other new accounts, and the default local account keeps using the Chrome profile folder). sales0 validates the cookie with one feed visit before marking the account connected. Treat the cookie
  like a password; it is shown nowhere after saving. When LinkedIn invalidates it, paste a new one.
- Install the browser on the server: `uv run playwright install --with-deps chromium`.
- **Datacentre IPs are suspicious to LinkedIn** and are more likely to trigger checkpoints and
  captchas than a home or office connection. The intended setup is a **fixed proxy per LinkedIn account**
  (a residential/ISP address that always belongs to that account, never rotating); each account can pin its
  own proxy URL, user agent, locale, timezone and viewport, and the browser applies them. **Planned:** there
  is no Setup form field for these pins yet (they are set through the service layer only), so today a hosted
  install runs from the server's own address.
- **Terms of service risk.** Automated access to LinkedIn is against LinkedIn's terms and can get the
  account restricted. sales0 keeps to your own logged-in session, human-like pacing, daily caps and
  stops at the first sign of trouble, but the risk is yours; do not use an account you cannot afford to lose.
- A streamed **remote-browser login** (log in through a browser window shown inside sales0) is *future
  work*, not built.

## Roadmap and what is not supported

Not supported today (do not rely on these):

- **PostgreSQL.** SQLite only. The database layer is SQLAlchemy and Alembic, so a Postgres move is
  possible, but migrations, locking and the single-process job runner are written and tested for SQLite. Future.
- **More than one process / worker** on the same data directory.
- **Multi-tenant (several companies in one install).** Planned, none of it exists yet:
  a tenant/workspace id on every table (a cheap migration: the names are already tenant-neutral and data goes
  through services), Postgres, object storage for decks and uploads instead of the local disk, a worker queue
  instead of the in-process scheduler thread, per-tenant proxies and Gmail/LinkedIn accounts, and **Google OAuth app
  verification** (including the restricted-scope security assessment for `gmail.modify`) for external domains.
- **WhatsApp** ingestion: deferred by decision. The WhatsApp row on **Metrics** reads "not enabled" and the page is a labelled stub.
- **Account picker for runs:** Run now, the scheduler and the watch use the default (first enabled) LinkedIn account; the services accept an account id but the UI has no picker yet. Only one browser job runs at a time across all accounts.
- **Watch limits:** no `posted` date for watch-found posts (the activity page renders ages differently), no permalink capture on watch visits.
- **Remote-browser LinkedIn login:** future (use the paste-`li_at` fallback).

## Security notes

- **Secret key.** `SALES0_SECRET_KEY` derives the encryption key for Gmail tokens and stored LinkedIn
  sessions and the key that signs cookies. Set it explicitly, keep it out of git, back it up. Rotating it invalidates
  stored tokens and sessions.
- **Cookies.** The login session cookie is HttpOnly, SameSite=Lax and carries the `Secure` flag when
  `SALES0_PUBLIC_BASE_URL` starts with `https://` (the session id is stored server-side only as a hash). The
  signed "acting as" and flash cookies are HttpOnly + SameSite=Lax but are not marked `Secure`. Serve hosted
  installs over HTTPS only.
- **CSRF.** Unsafe requests (POST etc.) must come from the app's own origin; the OAuth callback is protected by a
  single-use `state` bound to the signed-in person.
- **Login throttle.** Failed logins per client address and email are limited and then locked out for a while; the
  address comes from `X-Forwarded-For` only for proxies in `SALES0_TRUSTED_PROXIES`.
- **Passwords** are stored as argon2id hashes; set-password links are single-use and expire.
- **Encrypted at rest:** Gmail refresh tokens and LinkedIn session state in the database.
- **NOT protected:** the SQLite file itself is not encrypted (it holds enquiry text, contacts, drafted
  emails; use disk encryption and file permissions); logs and backups are plain files; `.env` / `/etc/sales0.env`
  contains the Anthropic key in clear text; anyone with shell access to the server can read everything; there is no
  audit trail beyond the CRM timeline; the research fetcher blocks private network addresses but runs on your server.
- **Personal data.** Enquiries and contacts are personal data. Fixtures in this repo are synthetic.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `sales0: SALES0_ANTHROPIC_API_KEY is missing` | Add the key to `.env` / the service environment file. |
| `Set SALES0_APP_PASSWORD before binding to a public host` | You set a non-loopback `SALES0_HOST`; set a password (or bind to `127.0.0.1` behind a proxy and still set one). |
| `Run: uv run alembic upgrade head` at start | The database is older than the code; run the migration (back up first). |
| Gmail "redirect_uri_mismatch" from Google | The redirect URI sales0 sent is not registered on the *Web application* client. Copy the exact URI from the Team page into Authorized redirect URIs. |
| "Desktop app client ... only allows loopback" on Connect | You set `SALES0_PUBLIC_BASE_URL` with a Desktop client JSON. Create a Web application client. |
| "Google returned no refresh token" | Remove sales0 at https://myaccount.google.com/permissions and connect again. |
| Gmail connection "needs reauth" after 7 days | Testing-mode consent screen; publish the app, or use an Internal app on Workspace. |
| Everyone gets locked out of login behind a proxy | `SALES0_TRUSTED_PROXIES` is unset, so all users share the proxy's address. Set it to the proxy IP. |
| LinkedIn light turns red / "needs attention" | A checkpoint, captcha, rate limit or logout stopped the run on purpose. Resolve it on LinkedIn, then Connect again (or paste a fresh `li_at`); the cool-down must also pass. |
| Stored Gmail/LinkedIn secrets stopped working after a restore | The `SALES0_SECRET_KEY` (or `data/secret.key`) differs from the one that encrypted them. Restore the right key or reconnect. |
| No scheduled run / digest | `SALES0_SCHEDULER_ENABLED`, the per-job switches on the **Schedule** page (opt-in), and the server timezone. |

## Tests

```bash
# working directory: the repo root
uv run pytest tests/unit -q          # pure unit tests: no network, no browser
uv run pytest tests/integration -q   # real SQLite files, real headless Chromium + headless Google Chrome, real Haiku (the pipeline tests use a fake-LLM oracle; ~10-15 min)
uv run pytest tests/e2e -q           # live servers on ports 8766 / 8792 (Phase 2 journey) + headless Chromium + real Haiku
uv run pytest tests -q               # everything
```

- Integration and E2E tests make **real** Anthropic calls with the key from `.env`. A full
  run costs a few US cents. If the key is missing, the session fails ("SALES0_ANTHROPIC_API_KEY
  missing in .env"); it is not skipped.
- The tests are safe to run **while your own sales0 is running on port 8765**: no test binds
  or connects to 8765 (a guard fails any test that tries), and none uses `data/sales0.db` or
  the profiles under `data/`. Each test uses a fresh temporary SQLite file migrated with
  Alembic and a temporary data dir.
- The `chrome`-marked integration tests need Google Chrome installed. They launch it
  headless on a temporary profile against the local fake LinkedIn only; without Chrome they
  fail with "Google Chrome not installed", they are not skipped.
- `tests/integration/pipeline/` runs the real job and a real headless browser over the fixture's
  bulk corpus: six keywords scan 200+ posts (scan → register → triage → capture links), a
  second run changes nothing, a mid-run restart is resumed, and old post text is purged. A
  fake LLM that answers from the corpus' known labels stands in for Haiku there.
- The E2E suite starts the real run command (`python -m sales0` from the project `.venv`) as
  a subprocess on port **8766** in fixture mode, with its own temp database and data dir (no
  startup keyword seeding, 6 keywords per run). Headless Chromium then walks setup → Connect
  LinkedIn (through the fake 2FA) → Re-check → Run now → inbox (relevant cards only) → accept →
  Skipped by AI (search, Rescue, Override), restarts the server and checks that the state
  survived, and runs again to see the AI learn from the decisions. A second journey adds the 60
  suggested keywords, presses Run now (returns in under 2 seconds with live counters), scans
  200+ posts with real Haiku, checks the inbox shows only genuine cards with "View post ↗",
  and a second run that finds nothing new. Further tests cover the logout/checkpoint/captcha
  safety stops, the layout-drift alert, the older LinkedIn layout still parsing, and the
  rate-limit cool-down surviving a Re-check and a restart. Port 8766 must be free; a full E2E
  run takes about 10 minutes.

- `tests/e2e/test_phase2_journey.py` (port **8792**, or the next free port) walks the whole CRM:
  login (redirect, wrong password, right password) -> accept two enquiries from one author in
  the Inbox (one prospect) -> note -> Draft email (research and drafting with real Haiku) ->
  approve and send through a `FakeGmailClient` (Contacted, three reminders) -> a follow-up due
  today -> a fake human reply classified by real Haiku -> Lead -> Proposal sent -> Won ->
  Client; then Sync now with a synthetic static sheet (second run: 0 changes), Contacts
  (search, tag, merge), Competitors & Groups and Import (paste + CSV). Nothing is sent to Gmail
  and Google Sheets are never contacted.
- `tests/integration/db/test_migration_realistic.py` upgrades synthetic populated databases
  (200+ enquiries, outreach messages with events and replies) and, when a copy of a real
  database exists at `$SALES0_REALDB_COPY`, that copy too, asserting no row is lost and
  `PRAGMA foreign_key_check` is clean. It never opens a real database in place.

## Layout

See `spec/architecture.md` → Layout. Source is in `src/sales0/` (`web/` pages and routes,
`services/` domain logic, `jobs/` background runner (resumable discovery), `scraper/` Playwright LinkedIn,
`agent/` + `prompts/` Haiku triage, `llm/` Anthropic adapter, `db/` models, and
`linkedin_fixture/` the fake LinkedIn). Migrations are in `migrations/` and tests in
`tests/`. Runtime data (database, browser profiles, logs) lives in the gitignored `data/`
directory: `sales0.db`, `linkedin-chrome-profile/` (the sales0 Chrome profile),
`linkedin-profile/` (the built-in-browser fallback) and the fixture profiles.
