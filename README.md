# sales0

> **Taking over this repo?** Start with [docs/HANDOVER.md](docs/HANDOVER.md).

AI sales and marketing agent for smallTech (AI corporate workshops, AI training, consulting).
sales0 finds buyer enquiries on LinkedIn, lets a person review them, tracks the relationship
in a small CRM, and drafts and sends outreach email from each person's own Gmail.

## What it does today

| Area | What is built |
|---|---|
| **LinkedIn discovery + triage** | Searches LinkedIn posts with the team's own logged-in session (slowly, with caps and safety stops), merges duplicates, and asks Claude Haiku once per new post whether it is a genuine buyer enquiry (reason, summary, priority, offering, contact details). |
| **Review inbox** | Only relevant posts are shown. Accept, discard (with a reason) or override; every decision is fed back into later triage as examples. |
| **CRM pipeline** | A **company** is the main record: it is a Prospect, then a Lead (a person decides), then a Client (an enquiry marked Won), or it is closed (not interested, dormant, do not contact, lost client). Each company has people (with a role), enquiries (one specific requirement each, with its own status: Open, Contacted, Follow-ups, Meeting scheduled ... Won, Lost; setting Contacted or a Follow-up on an enquiry of a new prospect moves that prospect forward), outreach and follow-ups, a timeline and a "Due today" list; Competitors & Groups and Import (paste a post or CSV) complete it. |
| **Google Sheet sync** | One-way, read-only import of the team's two Google Sheets into the CRM. sales0 never writes to Google. |
| **Outreach email** | Company, enquiry and contact research (automatic when an enquiry is accepted, plus a bulk "Research active enquiries" button), a drafted email, human approval, send through **each person's own Gmail** (OAuth), deck attachment, follow-ups at +3 / +7 / +14 days. |
| **Reply tracking** | Read-only polling of each member's Gmail detects replies to sent outreach, classifies them, records the response and shows a "Suggested: Lead" hint on the company for a person to accept or dismiss. |
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
   kept forever by default (set `SALES0_SEEN_RAW_RETENTION_DAYS` above 0 to purge it after that many days; a
   purged row says "text purged" and cannot be rescued). A "Triage failed" filter lists posts whose AI call failed, with **Retry triage**.
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

- **Caps (per LinkedIn account, per local day):** at most `SALES0_SCRAPE_MAX_RUNS_PER_DAY` discovery
  runs (default 2; aborted runs count), `SALES0_SCRAPE_MAX_POSTS_PER_DAY` posts scanned (2000),
  `SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY` LinkedIn page loads (600: search passes, profile visits and
  permalink pages all count, scanned pages as well as kept posts), `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN`
  keywords per run (40), `SALES0_SCRAPE_MAX_PASSES_PER_KEYWORD` search passes per keyword (3) and
  `SALES0_SCRAPE_MAX_POSTS_PER_KEYWORD` posts per keyword (80). Prospect-watch runs have their own caps
  (`SALES0_WATCH_MAX_RUNS_PER_DAY` 2, `SALES0_WATCH_MAX_VISITS_PER_DAY` 30 profile visits) but share the
  post and page-load totals. When a run uses up the page-load or visit allowance it ends early (the run
  detail says which limit) and the next run is refused until midnight.
- **Error breaker:** `SALES0_SCRAPE_MAX_CONSECUTIVE_ERRORS` (3) page errors in a row (timeouts, failed
  loads) are treated as a rate limit: the run stops and the cool-down starts.
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
  not shorten it. A stop found by a Re-check starts the cool-down too, and during it **Re-check
  (and the validation of a pasted cookie) is refused** with the minutes left, because that extra
  feed load is what makes LinkedIn dig in; **Connect** stays available so you can resolve the warning.
- **Layout warnings:** sales0 reads LinkedIn by its session cookie, URLs and stable page
  attributes (such as `data-view-name` and `data-testid`), not by styling classes. If a keyword's results still can't be read, the run
  shows a yellow "LinkedIn's page layout may have changed" alert and a "Layout warning"
  badge, and that keyword's Message in the run detail says what was missed. It is a warning,
  not a problem with your account; runs stay allowed. Please report the keyword.

### Safety defaults (conservative)

| Setting | Default | Notes |
|---|---|---|
| `SALES0_SCRAPE_MAX_RUNS_PER_DAY` | 2 | was 4 |
| `SALES0_SCRAPE_MAX_PASSES_PER_KEYWORD` | 3 | was 6 |
| `SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY` | 600 | new, per account |
| `SALES0_SCRAPE_MAX_CONSECUTIVE_ERRORS` | 3 | new |
| `SALES0_WATCH_MAX_VISITS_PER_DAY` | 30 | new, per account |
| `SALES0_WATCH_MAX_RUNS_PER_DAY` | 2 | new, per account |
| `SALES0_WATCH_START_JITTER_MIN` | 30 | scheduled watch starts 0-30 min after its time, different each day |

The arithmetic: a full run is 40 keywords x 3 passes = 120 searches, so two runs use 240 of the 600 page
loads, leaving room for permalink pages and the watch (30 visits). A normal run still scans 200+ posts
(that needs about two result cards per pass). Raise the caps in `.env` at your own risk.

### Robots.txt, terms of service and what sales0 is

sales0 does not crawl. It drives **your own logged-in browser session** (Chrome) at human pace, only
the pages you could open yourself (search results, profiles you watch), one at a time, with caps and
stop-on-warning. It does not fetch `robots.txt`, and it does not use LinkedIn's API. Automated access
to LinkedIn is against LinkedIn's User Agreement even with your own account: LinkedIn may restrict
or close the account. That risk is yours; the caps, pacing and cool-downs reduce it but cannot remove it.

### Third-party personal data sales0 stores

Scanning posts stores other people's personal data on your disk: for every post sales0 keeps the
author's name, headline and profile URL, the post text and link, and the AI's verdict (table
`seen_posts`, and `linkedin_profiles` / enquiries for relevant ones). The **raw post text** of a scanned
post is kept forever by default (scraping is the expensive step, so everything is stored once: text, parsed date,
"see more" flag, link kind, search URL and a `raw_card` JSON with the card's text, reaction / comment / repost
counts, links, hashtags and media counts). Set `SALES0_SEEN_RAW_RETENTION_DAYS` above 0 to have the daily
**purge** job drop the text of already-triaged posts after that many days.
Names, profile URLs and the enquiries you keep stay until you delete them (contacts and enquiries can
be deleted from their pages). Logs print counts, not names or text. Treat the data directory and any
backup as personal data (see Security notes) and honour deletion requests by deleting the rows.

### First day (local)

1. `cp .env.example .env`, add the Anthropic key. Set `SALES0_ADMIN_NAME`, `SALES0_ADMIN_EMAIL` and
   `SALES0_APP_PASSWORD` **before the first start**: they only seed the first admin (drafts are signed with that name)
   and are ignored on later starts.
2. `uv sync`, `uv run alembic upgrade head`, `uv run python -m sales0`, then log in with that email and password.
3. **Setup → Connect LinkedIn**: log in once in the Chrome window that opens (finish 2FA).
4. Trim the keyword list on Setup. Set `SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN=10` for a first run of about 30 minutes.
5. **Runs → Run now**.
6. **Inbox**: review, accept, discard or rescue.
7. **Pipeline → Draft email** on a prospect (company research is optional).
8. **Team → Connect my Gmail** ([Gmail OAuth](#gmail-oauth-per-person)), then Approve and Send (the prospect becomes
   Contacted and reminders are created).
9. **Sync → Sync now / Preview** (only if you set the sheet ids, see below) and check **Replies**.
10. **Metrics**, **Costs** and **Schedule** show how it is going.

### One sales0 per data directory (enforced)

sales0 takes an exclusive lock on `<data_dir>/.sales0.lock` at startup and refuses to boot when another
sales0 process already holds it ("Another sales0 process is already using the data directory ..."). Two
processes would abort each other's live runs at startup and double the LinkedIn page loads on one
account. Stop the other one first (or give this one its own `SALES0_DATA_DIR`). The lock dies with the
process, so a crash leaves nothing to clean up; `SALES0_ALLOW_MULTI_PROCESS=1` skips it (unsupported).
The lock is advisory and local: do not put the data directory on a network share used by two machines.

### Offline demo (fixture mode)

The same app can serve a built-in fake LinkedIn, so you can try the whole flow without a
LinkedIn account. Every page shows a `FIXTURE MODE` badge. The AI triage still makes real
Haiku calls. Keep demo data out of your real inbox with a separate demo database:

```bash
# working directory: the repo root
SALES0_DATABASE_URL=sqlite:///data/demo.db uv run alembic upgrade head
SALES0_DATABASE_URL=sqlite:///data/demo.db SALES0_LINKEDIN_MODE=fixture \
  SALES0_SCRAPE_MAX_KEYWORDS_PER_RUN=2 SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S=1 SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S=2 \
  uv run python -m sales0
```

The two extra limits matter: the suggested keyword list has 60 active phrases, and a default run searches 40 of them
with 20–60 s pauses, which looks stuck in a demo. With these, **Run now** searches 2 phrases and finishes in a minute or two.

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

**What you get.** Accept an enquiry in the Inbox and its **company** appears in **Pipeline** as a
*Prospect · New* (a company we already deal with just gets the enquiry attached; its stage is never
lowered). Press **Draft email** on it, approve and send as before: the company becomes **Contacted**
and reminders are created at +3 / +7 / +14 days (the *Due today* panel at the top of Pipeline lists
what is due). When the person replies "interested" the company page shows *Suggested: Lead*; a
person presses **Mark as lead** (*Meeting scheduled → Proposal sent*). Marking one of its enquiries
**Won** (or pressing **Mark client**) makes the company a **Client**. Close a company as *Not
interested*, *Dormant*, *Do not contact* (blocks all outreach; only an admin reopens it) or *Lost
client*, and reopen it later. The Pipeline has four tabs, **Enquiries** (the default: accepted enquiries by status) |
**Prospects** (New to Final follow-up, plus a recent *Became lead* lane) | **Leads** (Meeting scheduled,
Proposal sent, plus a recent *Won* lane) | **Clients**, each with a count. Setting the enquiry that
brought a company in to Contacted / a Follow-up moves the company's prospect stage with it. Every step is on the company's timeline, and you can add
notes. **People** is one shared address book (search, tags, merge duplicates; a person can belong to
several companies, with a role at each); **Competitors & Groups** are flagged when an enquiry
mentions a competitor; **Import** takes a single pasted post (triaged by Haiku like any
scraped post) or a CSV of data that is not in your Sheets.

**Google Sheet sync (read-only).** The team's two Sheets are synced into the CRM one way:
the Sheet is the source of truth for the fields it holds, and everything added in sales0
(notes, stages you move, tags, follow-ups, timeline) is kept. **sales0 never writes to Google.**
Settings (all optional) are in the [Configuration](#configuration) table (`SALES0_SHEET_*`). The Sheets must be readable by "anyone with the link".
**Sheet sync is opt-in**: `SALES0_SHEET_CLIENT_SOURCING_ID` and `SALES0_SHEET_METRICS_ID` are empty by default and a sheet
with no id is simply not synced, so a fresh install or the demo never pulls anyone's real data. Put the spreadsheet ids
(the long id in the Sheet's URL) in `.env` to turn it on. On the hosted app, set them as environment variables of the service.

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
Pipeline is not empty after the upgrade. For a way back, take a consistent backup first with
SQLite's online backup, exactly as in [Backups (SQLite)](#backups-sqlite) (do not `cp` the live
file and its `-wal`/`-shm`); restoring that backup is the supported rollback. `uv run alembic
downgrade -1` is a structural convenience only: the `0003` and `0004` downgrades now keep child
rows (they rebuild tables with foreign keys switched off and check them afterwards), but a
downgrade never restores data the upgrade moved or backfilled (for example AI-discarded
enquiries that became *Skipped by AI* rows).

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

## CI

`.github/workflows/ci.yml` runs on every push and pull request: `ruff check`, unit tests,
the SQLite integration suite (no real LLM calls; `llm`-marked tests are deselected and a
placeholder API key satisfies the harness' key check), and an e2e collection smoke test.
Unit, integration and e2e run as separate processes. The real-Anthropic jobs (e2e journeys
and `llm`-marked integration tests) run only from a manual "Run workflow" dispatch with
`run_llm_tests` ticked, using the repository secrets `ANTHROPIC_API_KEY` and
`SALES0_ANTHROPIC_WORKSPACE_ID`. The PostgreSQL job (`postgres:17` service, harness variable
`SALES0_TEST_POSTGRES_URL`) is opt-in via the repository variable `RUN_PG_TESTS=true` until the
deploy branch's Postgres test harness is merged.

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
| `SALES0_MODE` | local \| hosted | `local` | `local` (default): today's single-machine app. `hosted`: Cloud Run + PostgreSQL; no scheduler/poller threads, no local-browser runs (a laptop `sales0 worker` scrapes), manual actions run inside the request. Refuses to boot without PostgreSQL, a secret key, an https public base URL, a worker token and a login (see docs/deploy.md). |
| `SALES0_WORKER_TOKEN` | secret | (secret) | Bearer token shared by the hosted app and the laptop worker (>= 32 chars; `openssl rand -base64 36`). Required in hosted mode and for `sales0 worker`; compared in constant time. |
| `SALES0_AUTO_MIGRATE` | bool | `False` | Create the database if it is missing and run `alembic upgrade head` at boot, under a PostgreSQL advisory lock. For the single hosted instance; leave off locally. |
| `SALES0_HOSTED_ACTION_BUDGET_S` | int | `240` | Hosted: a manual action stops starting new work after this many seconds and shows 'more remaining, click again' (the Cloud Run request timeout is 900 s). |
| `SALES0_WORKER_STALE_S` | int | `180` | A run claimed by a worker that has sent no heartbeat for this many seconds is marked lost and can be re-queued. |
| `SALES0_WORKER_SERVER_URL` | str | unset | `sales0 worker` only: the hosted app's base URL (e.g. `https://sales0-xxxx.a.run.app`). |
| `SALES0_WORKER_POLL_S` | float | `15.0` | `sales0 worker` only: seconds between polls while idle. |
| `SALES0_WORKER_HEARTBEAT_S` | float | `30.0` | `sales0 worker` only: seconds between heartbeats while a run is in progress. |
| `SALES0_WORKER_UPLOAD_BATCH` | int | `25` | `sales0 worker` only: raw posts per upload request. |
| `SALES0_WORKER_NAME` | str | unset | `sales0 worker` only: label shown on the hosted Setup page (default: this machine's host name). |
| `SALES0_HOST` | str | `127.0.0.1` | Bind address. A non-loopback host refuses to start without `SALES0_APP_PASSWORD` (or an existing user account). |
| `SALES0_PORT` | int | `8765` | Bind port. |
| `SALES0_DATA_DIR` | str | `data` | Runtime directory: SQLite DB, Chrome profile, secret key, uploads. Put it on a persistent volume when hosted. |
| `SALES0_ALLOW_MULTI_PROCESS` | bool | `False` | Skip the one-process-per-data-directory lock (`<data_dir>/.sales0.lock`). Unsupported: a second process aborts the first one's live runs and doubles the LinkedIn page loads. Leave off. |
| `SALES0_DATABASE_URL` | str | `sqlite:///data/sales0.db` | SQLAlchemy URL. SQLite (default; relative file paths resolve against the repo root) or PostgreSQL (`postgresql+psycopg://user:pass@host/db`; `postgresql://` and `postgres://` are accepted). |
| `SALES0_TEST_DATABASE_URL` | str | unset | Test-suite only: database URL used by tests instead of a temp file. |
| `SALES0_DB_HOST` | str | unset | Hosted: PostgreSQL host (the Cloud SQL private IP). When set, the URL is built from the `SALES0_DB_*` parts and `SALES0_DATABASE_URL` is ignored. |
| `SALES0_DB_PORT` | int | `5432` | PostgreSQL port for the `SALES0_DB_*` parts. |
| `SALES0_DB_NAME` | str | `sales0` | PostgreSQL database name for the `SALES0_DB_*` parts. |
| `SALES0_DB_USER` | str | `sales0_app` | PostgreSQL role for the `SALES0_DB_*` parts. |
| `SALES0_DB_PASSWORD` | secret | (secret) | PostgreSQL password (from Secret Manager); never URL-escaped by hand. |
| `SALES0_DB_SSLMODE` | str | `prefer` | libpq sslmode for the `SALES0_DB_*` parts (`prefer`, `require`). |
| `SALES0_DB_POOL_SIZE` | int | `2` | PostgreSQL connection pool size (keep small on the shared db-f1-micro instance). |
| `SALES0_DB_MAX_OVERFLOW` | int | `2` | Extra PostgreSQL connections allowed above the pool size under load. |
| `SALES0_DB_POOL_RECYCLE_S` | int | `300` | Recycle PostgreSQL connections older than this many seconds. |
| `SALES0_GOOGLE_CLIENT_SECRET_PATH` | str | `data/google_client_secret.json` | Google OAuth client JSON (Desktop `installed` or Web `web` type) used for Gmail. |
| `SALES0_GOOGLE_TOKEN_PATH` | str | `data/google_token.json` | Token file written by the `google-auth` CLI command (single-user fallback; team members' tokens live encrypted in the DB). |
| `SALES0_SECRET_KEY` | secret | (secret) | Encrypts Gmail tokens and LinkedIn sessions at rest and signs cookies (>= 16 chars or a Fernet key). Unset: generated once into `<data_dir>/secret.key`. Set it explicitly on a server and back it up: losing it makes stored tokens unreadable. |
| `SALES0_ADMIN_EMAIL` | str | `admin@smalltech.in` | Email of the first admin team member created on an empty database. |
| `SALES0_ADMIN_NAME` | str | `Admin` | Display name of that first admin. |
| `SALES0_GMAIL_DAILY_SEND_CAP` | int | `40` | Max outreach emails sent per member per day (0 disables sending). |
| `SALES0_DECK_MAX_BYTES` | int | `10485760` | Max size of one uploaded deck PDF, in bytes (at most 10 MB). |
| `SALES0_ATTACHMENTS_BUCKET` | str | unset | Google Cloud Storage bucket for deck PDFs (outreach attachments), object `decks/<sha256>.pdf`, Application Default Credentials. Required when hosted (uploads and sends refuse without it); unset locally = files under `<data_dir>/decks/`. |
| `SALES0_PUBLIC_BASE_URL` | str | unset | Public origin when hosted (e.g. `https://sales.example.com`). Builds the Google OAuth redirect (`<url>/oauth/google/callback`, must match a registered redirect URI exactly) and (with `SALES0_COOKIE_SECURE=auto`) turns on the Secure cookie flag when it starts with `https://`; its host is added to the accepted Host names. |
| `SALES0_ALLOWED_HOSTS` | str | empty | Extra Host names (comma separated, any port) accepted once logins are enabled, in addition to loopback, `SALES0_HOST` and the host of `SALES0_PUBLIC_BASE_URL`. Any other Host header is refused with 400 (DNS-rebinding guard). In open mode (no logins) only loopback and `SALES0_HOST` are accepted. |
| `SALES0_COOKIE_SECURE` | auto \| true \| false | `auto` | Secure flag on every cookie: `auto` (default: when the request is https, or `X-Forwarded-Proto: https` arrives from a `SALES0_TRUSTED_PROXIES` peer, or `SALES0_PUBLIC_BASE_URL` is https), `true` or `false`. |
| `SALES0_ALLOW_TESTSERVER_HOST` | bool | `False` | TEST ONLY: also accept the Host `testserver` (Starlette TestClient). Never set it on a server. |
| `SALES0_GOOGLE_AUTH_ENDPOINT` | str | `https://accounts.google.com/o/oauth2/v2/auth` | Google authorize endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_TOKEN_ENDPOINT` | str | `https://oauth2.googleapis.com/token` | Google token endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_USERINFO_ENDPOINT` | str | `https://openidconnect.googleapis.com/v1/userinfo` | Google userinfo endpoint (override for tests/proxies). |
| `SALES0_GOOGLE_REVOKE_ENDPOINT` | str | `https://oauth2.googleapis.com/revoke` | Google revoke endpoint (override for tests/proxies). |
| `SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS` | str | empty | Test only: hosts the research fetcher may reach although private. Ignored unless `SALES0_LINKEDIN_MODE=fixture`. |
| `SALES0_RESEARCH_SYNTHESIS` | bool | `True` | The final research brief (typed company profile, enquiry analysis, evidence-backed facts, gaps, completeness) is written by `SALES0_RESEARCH_MODEL`; the website reading and web search stay on the default model. `false` = the single-model flow of before. |
| `SALES0_RESEARCH_MODEL` | str | `claude-sonnet-5-5` | Model for the final research brief only (Sonnet class). Priced with the two `SALES0_RESEARCH_PRICE_*` settings below. |
| `SALES0_RESEARCH_PRICE_INPUT_USD_PER_MTOK` | float | `3.0` | Input price (USD per 1M tokens) of the research model, used for the spend estimate. Check it against current pricing. |
| `SALES0_RESEARCH_PRICE_OUTPUT_USD_PER_MTOK` | float | `15.0` | Output price (USD per 1M tokens) of the research model, used for the spend estimate. Check it against current pricing. |
| `SALES0_KEYWORD_IDEAS_MODEL` | str | `claude-opus-5-5` | Model for the Keywords page's AI suggestions (Opus class): one call over winning posts, noise posts and keyword performance. |
| `SALES0_KEYWORD_IDEAS_PRICE_INPUT_USD_PER_MTOK` | float | `5.0` | Input price (USD per 1M tokens) of the keyword-ideas model, for the spend estimate. Check it against current pricing. |
| `SALES0_KEYWORD_IDEAS_PRICE_OUTPUT_USD_PER_MTOK` | float | `25.0` | Output price (USD per 1M tokens) of the keyword-ideas model, for the spend estimate. Check it against current pricing. |
| `SALES0_KEYWORD_IDEAS_MAX_TOKENS` | int | `4000` | Output cap of the keyword-ideas call. |
| `SALES0_RESEARCH_SYNTHESIS_MAX_TOKENS` | int | `4000` | Output cap of the final research-brief call. |
| `SALES0_RESEARCH_BULK_CHUNK` | int | `5` | Companies researched per click of "Research selected" on the Companies page (hosted: also bounded by the action budget). |
| `SALES0_RESEARCH_WEB_SEARCH` | bool | `True` | Prospect research also runs Anthropic's server-side web search (news / public mentions) in one extra Haiku request. `false` turns it off. |
| `SALES0_RESEARCH_WEB_SEARCH_MAX_USES` | int | `3` | Most web searches one research run may make (billed per search). |
| `SALES0_AUTO_RESEARCH` | bool | `True` | Accepting an enquiry in the Inbox starts research on its company, the enquiry and the people behind it in the background (about 2 cents with web search). `false` = research only runs from the buttons. Hosted: the work may pause while the instance has no open request; the Pipeline button finishes whatever is left. |
| `SALES0_RESEARCH_WEB_SEARCH_TOOL` | str | `web_search_20250305` | Anthropic web search tool type sent with the research call (`web_search_20250305` works on Haiku). |
| `SALES0_LLM_PRICE_WEB_SEARCH_USD_PER_1K` | float | `10.0` | Web search price (USD per 1,000 searches) used for the spend estimate. |
| `SALES0_COMPANY_PAGE_MAX_PER_DAY` | int | `10` | LinkedIn company-page visits the laptop worker may make per LinkedIn account per day (prospect research; each also counts as a page load). `0` = never queue them. |
| `SALES0_COMPANY_PAGE_STALE_S` | int | `600` | A claimed company-page task with no result after this many seconds is marked failed (worker lost). |
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
| `SALES0_SCRAPE_MAX_RUNS_PER_DAY` | int | `2` | Max discovery runs per LinkedIn account per day (default 2; was 4). Watch runs have their own cap. |
| `SALES0_SCRAPE_MAX_PAGELOADS_PER_DAY` | int | `600` | Max LinkedIn page loads (search passes, profile visits, permalink pages) per account per day (default 600). A run ends early when it is used up. |
| `SALES0_SCRAPE_MAX_CONSECUTIVE_ERRORS` | int | `3` | Consecutive page errors (timeouts, failed loads) that stop a run as rate-limited and start the cool-down (default 3). |
| `SALES0_SCRAPE_LAYOUT_SAMPLES` | bool | `True` | When a keyword ends with a layout warning, keep a cleaned copy of its result cards (structure and visible text only; no cookies, scripts or images) that the run page offers for download, so a LinkedIn markup change can be fixed from evidence. `false` = never keep one. |
| `SALES0_SCRAPE_DELAY_MIN_S` | float | `3.0` | Min human-like delay between actions (seconds). |
| `SALES0_SCRAPE_DELAY_MAX_S` | float | `9.0` | Max delay between actions (seconds). |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MIN_S` | float | `20.0` | Min pause between phrases (seconds). |
| `SALES0_SCRAPE_KEYWORD_PAUSE_MAX_S` | float | `60.0` | Max pause between phrases (seconds). |
| `SALES0_SCRAPE_MAX_SCROLLS` | int | `20` | Max scrolls per search pass. |
| `SALES0_SCRAPE_PACE_SCALE` | float | `1.0` | Test-speed multiplier on every scraper wait (default 1.0 = unchanged). Honoured only with SALES0_LINKEDIN_MODE=fixture (clamped 0.05-1.0); any value below 1.0 is rejected at startup in live mode. |
| `SALES0_SCRAPE_NO_GROWTH_SCROLLS` | int | `2` | Consecutive scrolls without new cards that end a pass. |
| `SALES0_SCRAPE_SORT` | latest \| relevance | `latest` | `latest` or `relevance` search ordering. |
| `SALES0_SCRAPE_DATE_FILTERS` | list | `past-week,past-month` | Comma list of date filters, one search pass each: `past-24h`, `past-week`, `past-month`. |
| `SALES0_SCRAPE_DATE_FILTER` | str | unset | Legacy single date filter; read only when `SALES0_SCRAPE_DATE_FILTERS` is unset. |
| `SALES0_SCRAPE_VARIANTS` | bool | `True` | Run extra per-phrase search variants in a daily rotation. |
| `SALES0_SCRAPE_MAX_PASSES_PER_KEYWORD` | int | `3` | Cap on search passes per phrase (default 3; was 6). |
| `SALES0_SCRAPE_EXPAND_MAX_PER_KEYWORD` | int | `40` | Max "see more" clicks per phrase. |
| `SALES0_SCRAPE_PERMALINK_MAX_PER_KEYWORD` | int | `15` | Max "Copy link to post" attempts per phrase (0 = off). |
| `SALES0_FEWSHOT_MAX_EXAMPLES` | int | `6` | Max reviewed examples shown to triage as few-shot context. |
| `SALES0_FEWSHOT_TEXT_CHARS` | int | `600` | Characters kept per few-shot example. |
| `SALES0_TRIAGE_MAX_INPUT_CHARS` | int | `4000` | Characters of a post sent to triage. |
| `SALES0_TRIAGE_CONCURRENCY` | int | `4` | Parallel triage calls. |
| `SALES0_SEEN_RAW_RETENTION_DAYS` | int | `0` | Days raw seen-post text is kept before the purge job drops it; 0 (default) keeps it forever. |
| `SALES0_SEED_SUGGESTED_KEYWORDS` | bool | `True` | Seed the suggested search phrases on first run. |
| `SALES0_REPLY_POLL_MINUTES` | int | `10` | Minutes between Gmail reply checks (0 disables the poller). |
| `SALES0_REPLY_LOOKBACK_DAYS` | int | `60` | Only sent messages younger than this are polled for replies. |
| `SALES0_REPLY_MAX_THREADS_PER_POLL` | int | `300` | Max threads checked per member per cycle. |
| `SALES0_REPLY_OWN_ALIASES` | str | empty | Comma list of extra addresses that are "us" (Send-As aliases); never counted as replies. |
| `SALES0_SHEET_SYNC_MINUTES` | int | `30` | Minutes between Google Sheet syncs (0 disables). |
| `SALES0_SHEET_CLIENT_SOURCING_ID` | str | empty | Spreadsheet id of the client-sourcing sheet. |
| `SALES0_SHEET_METRICS_ID` | str | empty | Spreadsheet id of the metrics sheet. |
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
| `SALES0_FOLLOWUP_DRAFT_BATCH` | int | `10` | AI calls per `Draft due follow-ups` click (hosted: click again for the rest) and per scheduled run. Drafts are never sent without a human click. |
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
| `SALES0_SCHEDULER_FOLLOWUP_DRAFTS_TIME` | str | `08:00` | Local time the opt-in `followup_drafts` job writes draft follow-up emails (disabled until enabled on /schedule; local mode only; it never sends). |
| `SALES0_WATCH_MAX_PROFILES_PER_RUN` | int | `25` | Prospect watch: max tracked profiles visited per watch run. |
| `SALES0_WATCH_MIN_DAYS_BETWEEN_CHECKS` | float | `3.0` | Prospect watch: minimum days before the same profile is visited again. |
| `SALES0_WATCH_MAX_POSTS_PER_PROFILE` | int | `15` | Prospect watch: max posts read per profile visit (also bounded by the daily post cap). |
| `SALES0_WATCH_MAX_VISITS_PER_DAY` | int | `30` | Prospect watch: max profile visits per LinkedIn account per day (default 30); visits also count in the page-load cap. |
| `SALES0_WATCH_MAX_RUNS_PER_DAY` | int | `2` | Prospect watch: max watch runs per LinkedIn account per day (default 2). |
| `SALES0_WATCH_START_JITTER_MIN` | int | `30` | Scheduled prospect watch starts `SALES0_SCHEDULER_WATCH_TIME` plus a per-day pseudo-random 0..N minute delay (default 30; 0 = exact time). |
| `SALES0_WATCH_PROFILE_PAUSE_MIN_S` | float | `20.0` | Prospect watch: minimum pause between two profile visits (seconds). |
| `SALES0_WATCH_PROFILE_PAUSE_MAX_S` | float | `60.0` | Prospect watch: maximum pause between two profile visits (seconds). |
| `SALES0_WATCH_APPEND_TO_DISCOVERY` | bool | `False` | True = also visit due tracked profiles at the end of each discovery run (same browser session and caps). |
| `SALES0_PURGE_RETENTION_DAYS` | int | `90` | Days of raw data kept before purge. |
| `SALES0_DIGEST_AI_SUMMARY` | bool | `True` | One LLM call adds a short summary to the digest. |

<!-- env-docs:end -->

## Deploying on a single server

sales0 is built as a single-process app (one uvicorn worker, a background scheduler thread,
SQLite). One server, one process, one persistent data directory. Do not run two instances on
the same data directory (now enforced: a second process refuses to boot, see "One sales0 per data
directory").

### Layout and persistence

- Put **`SALES0_DATA_DIR`** (an absolute path, for example `/var/lib/sales0`) on a persistent
  disk or volume. It holds the SQLite database (`sales0.db`, plus `-wal` and `-shm`), uploaded
  decks (`decks/<sha256>.pdf`; the hosted app keeps them in the Cloud Storage bucket `SALES0_ATTACHMENTS_BUCKET` = `sales0-files`
  instead, see `docs/deploy.md` section 5b), the generated `secret.key` (when `SALES0_SECRET_KEY` is unset), Chrome profiles for
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

- `SALES0_PUBLIC_BASE_URL=https://sales.example.com`: used to build the Google OAuth redirect;
  its host is accepted as a `Host` header, and when it starts with `https://` all cookies are
  marked `Secure`.
- `SALES0_TRUSTED_PROXIES=127.0.0.1` (the proxy's address, or a CIDR): **required behind a
  proxy.** Only then is `X-Forwarded-For` trusted for the client address used by the login
  throttle (otherwise every visitor shares the proxy's address = ONE throttle bucket, so one
  attacker locks everybody out) and `X-Forwarded-Proto` for the cookie `Secure` flag. Leave it
  empty when sales0 faces clients directly, otherwise anyone could spoof their address.
- `SALES0_ALLOWED_HOSTS=other.example.com,10.0.0.5`: any extra names users open sales0 by.
  A request whose `Host` is not loopback, `SALES0_HOST`, the public URL's host or in this list
  gets a 400 (DNS-rebinding guard); in open mode only loopback / `SALES0_HOST` pass.
- `SALES0_COOKIE_SECURE=auto|true|false`: `auto` follows https (see above); force `true` if your
  proxy does not send `X-Forwarded-Proto`.

See [docs/security.md](docs/security.md) for the hardening notes (headers/CSP, body limits, file modes).
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

Before upgrading the app or running a migration, take a backup first. Restoring the backup is the supported way back; `alembic downgrade -1` only reverses the schema (it preserves child rows but does not restore data the upgrade moved) and is not a substitute.

### Spend cap accuracy

The monthly cap is checked **before** each AI call against the cost already recorded; a call's
cost is only recorded when it finishes. There is no in-flight accounting, so calls that start
concurrently (parallel triage, a run plus a watch run, a drafting request) can each pass the
check and together overshoot the cap. The overshoot is bounded: roughly the number of
concurrent calls times the cost of one call (cents with Haiku). Treat the cap as a soft limit
and set it a little below the real budget.

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
   `SALES0_GOOGLE_CLIENT_SECRET_PATH` (default `data/google_client_secret.json`; a `data/...` path follows
   `SALES0_DATA_DIR`, an absolute path is used as is):
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

## Hosted (Cloud Run + Cloud SQL, scraping on a laptop)

Besides the single-machine setup above, sales0 can run as a shared team app on **Google Cloud Run** with its data in
**PostgreSQL** (a database on an existing Cloud SQL instance), while LinkedIn is still searched from a **laptop** with its own
logged-in Chrome. Set `SALES0_MODE=hosted` (the container image does). Full runbook: [docs/deploy.md](docs/deploy.md).

| | Local mode (default) | Hosted mode |
|---|---|---|
| Database | SQLite file | PostgreSQL (`SALES0_DB_*` parts or `SALES0_DATABASE_URL`) |
| Scheduler / reply-poll threads | on (opt-in jobs) | **never started**; every job is a manual **Run now** that works inside the request (the page waits) |
| LinkedIn discovery | in-process Chrome job | **Queue a run**; `sales0 worker` on the laptop claims it, scrapes, uploads raw posts; the hosted app dedupes, triages with Haiku and keeps the counters |
| LinkedIn cookies / Chrome profile | on the server's disk | never leave the laptop |
| Login | optional (open mode on loopback) | **required**; the app refuses to boot in open mode |
| Instances | one process per data dir | Cloud Run min 0 / max 1, request timeout 15 min |

The worker talks to `/api/worker/*` with a bearer token (`SALES0_WORKER_TOKEN`). It survives hosted downtime (retry with
backoff; a restarted worker resumes its run). Hosted mode has **no** Connect/Re-check buttons, cookie paste or prospect-watch
runs: those need a browser and stay on the laptop (`sales0 worker --connect`). A manual action that could outlive a request
(reply check) stops after `SALES0_HOSTED_ACTION_BUDGET_S` seconds and says "more remaining, click again".

**Single source of truth.** Once hosted, the Cloud SQL database is the one source of truth for the team: the dashboard at
https://sales0-870371939888.us-central1.run.app reads and writes it, and the laptop only scrapes. SQLite is for local
development and the automated tests. There is **no scheduler**: sheet sync, reply checks, the digest and the rest are buttons
on the page, and scraping runs when someone queues a run and `sales0 worker` is running on the laptop.

**Gmail on the hosted app** needs a **Web application** OAuth client with the redirect URI
`https://sales0-870371939888.us-central1.run.app/oauth/google/callback` (a Desktop client cannot be used there). The JSON is
the Secret Manager secret `sales0-google-client-secret`, mounted as a file; after adding a new secret version, the next new
instance picks it up.

Deploy: `deploy/deploy.sh` prints the `gcloud` commands (dry run); `deploy/deploy.sh --apply` runs them.

### Run a scraper worker (any teammate's laptop)

```bash
# clone the sales0 repository, then in its folder:
uv sync                                   # needs Python 3.12, uv and Google Chrome
export SALES0_WORKER_SERVER_URL=https://sales0-870371939888.us-central1.run.app
export SALES0_WORKER_TOKEN=...            # an admin reads it from Secret Manager (sales0-worker-token) and shares it securely
# optional: SALES0_DATA_DIR=<folder for this laptop's LinkedIn Chrome profile>; SALES0_WORKER_NAME=<your name>
uv run sales0 worker --connect            # once: Chrome opens on LinkedIn; log in and finish 2FA (your everyday Chrome is untouched)
uv run sales0 worker                      # leave running; then Runs -> Queue a run in the hosted app
```

Run **one worker at a time per LinkedIn account**: two laptops on the same account double its page loads. The usual
account-safety caps (runs, posts and page loads per day, cool-downs after a warning) are enforced by the hosted app, so the
whole team shares them. The token and the LinkedIn session stay on that laptop; keep them in a local `.env`, not a shared shell.

### Work on the UI against real data (`pull-data`)

The hosted database is unreachable from laptops, so `pull-data` copies it THROUGH the hosted app (read-only,
`/api/worker/export/*`, same worker token) into a fresh local SQLite file you can run the app on:

```bash
uv run sales0 pull-data --out data/pulled/sales0.db        # add --force to replace, --tables a,b for a subset
data/pulled/run.sh                                          # http://127.0.0.1:8780, log in as admin@local.test (password printed once)
```

The checkout must be at the same migration as the server (otherwise it refuses: update the repo / the server is behind).
Team members, passwords, sessions, Gmail tokens, LinkedIn account state and sheet configuration are never exported; deck
files stay in the bucket (the local Decks page shows "file missing"). `run.sh` turns the scheduler and pollers off, uses
the built-in fake LinkedIn and caps Gmail sending at 0, so the local app cannot send email or touch linkedin.com. The file
contains **personal data** (git-ignored under `data/`): keep it on your laptop. Details: docs/deploy.md.

## Roadmap and what is not supported

Not supported today (do not rely on these):

- **PostgreSQL for the local single-machine mode.** Local mode is SQLite. PostgreSQL is supported for the hosted deployment
  (see [Hosted](#hosted-cloud-run--cloud-sql-scraping-on-a-laptop)); the local job runner is written for SQLite.
- **More than one process / worker** on the same data directory (refused at boot by a lock file).
- **Multi-tenant (several companies in one install).** Planned, none of it exists yet:
  a tenant/workspace id on every table (a cheap migration: the names are already tenant-neutral and data goes
  through services), Postgres, object storage for uploads instead of the local disk (decks already use GCS when hosted), a worker queue
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
- **Personal data.** Enquiries and contacts are personal data, and so are the other people's posts sales0 scans
  (see "Third-party personal data sales0 stores" for what is kept and the retention). Fixtures in this repo are synthetic.

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
uv run pytest tests -q               # everything (the suites are also safe to run in one process)
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

- `tests/e2e/test_company_journey.py` walks the company model without an LLM: accept two enquiries
  (one company) -> Pipeline tabs (Enquiries default, Prospects, Leads, Clients) -> Mark as lead -> an enquiry marked Won
  -> Client -> Lost client under Clients (Show closed) -> reopen; the old `/leads`, `/clients` and
  `/prospects/{id}` URLs redirect.
- `tests/e2e/test_phase2_journey.py` (port **8792**, or the next free port) walks the whole CRM:
  login (redirect, wrong password, right password) -> accept two enquiries from one author in
  the Inbox (one company) -> note -> Draft email (research and drafting with real Haiku) ->
  approve and send through a `FakeGmailClient` (Contacted, three reminders) -> a follow-up due
  today -> a fake human reply classified by real Haiku -> "Suggested: Lead" -> Mark as lead ->
  Proposal sent -> an enquiry Won -> Client; then Sync now with a synthetic static sheet (second run: 0 changes), Contacts
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
