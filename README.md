# sales0

AI sales and marketing agent for smallTech. sales0 searches LinkedIn for posts where a
company is looking for AI training or AI consulting, using the team's own logged-in
LinkedIn session. It merges duplicate posts, asks Claude Haiku once per new post whether it
is a genuine buyer enquiry (with a one-line reason, summary, priority, matching offering and
extracted contact details), and puts **only the relevant ones** in a **Review inbox**. A
person accepts, discards or overrides each card. Every decision is stored and fed back into
later triage calls as examples (the learning loop). No spreadsheet is involved.

**Status: Phase 2 (CRM memory) on top of Phase 1.4 discovery.** Accepted enquiries become
prospects in a **Pipeline** (Prospects, Leads, Clients), with Contacts, Competitors & Groups,
Import, a per-record timeline, a "Due today" follow-up list, a read-only **Google Sheet sync**
and an optional shared-password login (see [Phase 2: CRM, sheet sync and login](#phase-2-crm-sheet-sync-and-login)).
Outreach email and reply tracking are real too; Metrics and WhatsApp are still labelled stub
pages that say "Coming in Phase N". That is expected, not a bug. The full plan is in
`spec/roadmap.md`.

`SALES0_LINKEDIN_HEADLESS` no longer exists (Connect always shows a window, checks and runs
never do). A leftover line in an old `.env` is ignored.

## Run

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv run python -m sales0
```

Open http://127.0.0.1:8765/. You land on the Review inbox. Health check:
http://127.0.0.1:8765/api/health (`db_revision` is `0003_phase1_4_seen_posts`). Logs are JSON
lines on stdout, one per HTTP request (`"event": "http_request"`) and one per AI call
(`"event": "llm_call"`, with tokens and USD cost). Session cookies are never logged. Stop
the server with Ctrl+C.

If the database is not migrated, startup stops with "Run: uv run alembic upgrade head".

### Connecting LinkedIn (one time)

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

**Upgrading from Phase 1:** your Phase-1 login lives in the built-in browser's profile
(`data/linkedin-profile/`). Google Chrome cannot reuse it, so **press Connect LinkedIn once
more** and log in in the Chrome window. The card may still say "Connected" from Phase 1; if
you press Run now before reconnecting, the run stops at once with "Not logged in to LinkedIn
in sales0's browser — press Connect LinkedIn in Setup". No LinkedIn page is opened and there
is no penalty. Afterwards, `data/linkedin-profile-fixture/` is unused and can be deleted, and
`data/linkedin-profile/` can be deleted too unless you might run without Google Chrome.

### Using it (live LinkedIn)

1. **Upgrading from Phase 1.3:** stop sales0 and run `uv run alembic upgrade head` once (the
   app refuses to start on an old database and says so). No re-login is needed. Posts the AI
   had already discarded and you never touched move to **Skipped by AI**; everything you
   decided stays where it was.
2. **Setup**: the four default offerings are listed and can be edited. **Keywords** lists 60
   buyer-intent phrases ("looking for AI trainer", "corporate AI training", "Claude Code
   trainer", "hiring AI consultant" ...) under the four offerings; edit, switch off or remove
   any. **Add suggested keywords** adds missing ones and never changes what you have ("Added 0 · 60
   already present"). Your own topic keywords stay allowed, but a topic ("agentic AI training")
   mostly finds thought leadership, so prefer buyer phrases. Each keyword shows its yield
   ("Scanned 412 · Relevant 6"); switch off keywords that never yield.
3. **Connect LinkedIn** once (above).
4. **Runs → Run now** returns at once and starts a **long background run**. The panel shows
   live counters (Scanned · New to us · Already seen · Relevant · Skipped by AI · Post links)
   and "Keyword 7/40". See "Long runs" below.
5. **Inbox** shows **only relevant posts** (what the AI judged a genuine buyer, or what you
   rescued). Each card shows the summary, the AI verdict with a one-line reason, priority,
   offering, extracted details, a link (**"View post ↗"** or **"Author profile (post link
   unavailable) ↗"**, see below), the full text and the AI cost. **Accept** moves a card to
   Accepted as "Prospect · New". **Discard** asks for a reason. Use the checkboxes for bulk
   accept or discard. Discarded enquiries are kept and can be searched.
6. **Inbox → Skipped by AI** lists the posts the AI rejected, with the reason, poster type,
   author and how often it was seen. Search by words, reason or author. **Rescue → enquiry**
   moves one to To review (the AI's verdict stays "Discard" so its mistake is visible);
   **Override: accept** rescues and accepts in one click. Both teach the AI. The full text is
   kept for 14 days (`SALES0_SEEN_RAW_RETENTION_DAYS`); after that the row says "text purged"
   and cannot be rescued. A "Triage failed" filter lists posts whose AI call failed, with **Retry triage**.
7. **Runs → a run** shows the per-keyword log and the cost. The header shows the AI spend
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
# working directory: /Users/sai/Workspace/Code/sales0
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

## Phase 2: CRM, sheet sync and login

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv run alembic upgrade head          # once after pulling: adds the CRM tables (one head: p6_crm_5b2e7d14)
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

**Shared login (`SALES0_APP_PASSWORD`).** Set it in `.env` and restart:

```bash
SALES0_APP_PASSWORD=choose-a-long-shared-password
```

Every page then redirects to `/login` (`/api/health` and `/static` stay open); the cookie lasts
`SALES0_SESSION_DAYS` (default 14), five wrong passwords a minute from one address locks the
login for a minute, and **Log out** is in the header. Unset or empty = no login, which is only
accepted on a loopback `SALES0_HOST`: sales0 refuses to start on a public host without a password.

**Google Sheet sync (read-only).** The team's two Sheets are synced into the CRM one way:
the Sheet is the source of truth for the fields it holds, and everything added in sales0
(notes, stages you move, tags, follow-ups, timeline) is kept. **sales0 never writes to Google.**
Settings (all optional, in `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `SALES0_SHEET_SYNC_MINUTES` | `30` | Interval of the automatic sync (`0` turns it off). It is opt-in on the Schedule/Sync page. |
| `SALES0_SHEET_CLIENT_SOURCING_ID` / `SALES0_SHEET_METRICS_ID` | the team's two Sheets | Which Sheets to read. They must be readable by "anyone with the link". |
| `SALES0_SHEET_TABS_JSON` | built-in tab list | Replace the tab list (name, kind, gid). |
| `SALES0_SHEET_FETCH_TIMEOUT_S` / `_RETRIES` / `_MAX_BYTES` | `20` / `3` / `20000000` | Fetch limits. |

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

## Tests

```bash
# working directory: /Users/sai/Workspace/Code/sales0
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
