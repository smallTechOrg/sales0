# sales0 — Roadmap

## What

sales0 is an AI sales and marketing agent for smallTech. smallTech sells AI corporate workshops, technical AI training (for example "Build your first AI agent" or Claude Code training), AI consulting and build services, and products. Today the team finds buyers by hand-searching LinkedIn posts and tracking them in two Google Sheets: Active Enquiries and Discarded Enquiries. sales0 replaces those sheets. It scrapes requirement posts from LinkedIn on the team's own logged-in session, gently and with safety limits, and merges duplicates. One cheap AI call per enquiry decides genuine or discard, extracts the details and matches a smallTech offering. A human reviews everything in an inbox. Every decision feeds back into the AI. Later phases add a full memory of every company and person (pipeline, timeline, follow-ups), outreach email drafting with human approval, a metrics dashboard that replaces the weekly sheets, and WhatsApp group ingestion.

## Who

| User | Frequency | Primary need |
|---|---|---|
| Marketing person (1–3) | Daily | Triage the inbox of scraped enquiries fast: accept the genuine buyers, discard the rest |
| Sales person | Daily/weekly | Follow up accepted prospects and leads (Phase 2+) |
| Founder | Weekly | Weekly metrics against targets, cost view (Phase 4) |

Phases 1–4 use one shared login. Per-user accounts come in Phase 5. Every actor-bearing record has a nullable `actor_user_id` from Phase 1 onward, so attribution can be added without reshaping data.

## Success Criteria (product level)

- [ ] A marketing person can add keywords, connect LinkedIn once, press **Run now**, and within one run see AI-triaged enquiry cards (verdict, one-line reason, summary, priority, offering) in the Review inbox. No spreadsheet is involved.
- [ ] The same post found under two keywords, or reposted with identical text, appears as ONE enquiry.
- [ ] The scraper never pushes through a logout, captcha, checkpoint or rate-limit page. It stops the run, marks LinkedIn "Needs attention", and shows an alert banner.
- [ ] Every AI call records tokens and USD cost. Each enquiry card shows its cost, and the totals for today and this month are visible.
- [ ] Every accept, discard and override is stored. Later triage calls use these decisions as examples (the learning loop).
- [ ] Discarded enquiries are kept forever and can be searched.
- [ ] (Phase 2) Every company and person has a full timeline, and follow-ups due today are listed.
- [ ] (Phase 3) No email is ever sent without a human approving that exact message.
- [ ] (Phase 4) The Weekly and Secondary metrics sheets are produced automatically per ISO week.

## Out of Scope (all phases unless noted)

- Sending any message without human approval (never)
- Bypassing LinkedIn security (captcha solving, proxy rotation, account farms): never
- Scraping with any account other than the team's own logged-in session (never)
- Multi-tenant SaaS / billing (never)
- Mobile app (never; the web UI is responsive)
- Scheduled scrape jobs (Phase 4), WhatsApp (Phase 5), import of existing sheets (Phase 2), outreach email (Phase 3)
- LLM models other than the configured Haiku model (never, by user constraint)

## Phases of Development

The phase plan is: **Scaffold → Phase 1 Discovery+Triage+Inbox → Phase 2 CRM Memory → Phase 3 Research+Outreach → Phase 4 Metrics+Automation → Phase 5 WhatsApp+Accounts+Replies.**

Each phase ends at a human testing gate. All paths below are from `spec/architecture.md` → `## Layout`. Every command runs from the repo root `/Users/sai/Workspace/Code/sales0` (written as `./` below).

---

### Scaffold — Minimal Runnable Skeleton

**Goal:** the layout in `architecture.md` boots on the pinned toolchain, the migration tool is wired, the test harness is green, and the Anthropic key plus the Haiku model slug are proven alive with one real call.

**Single slice: `scaffold`.** It is not parallelised. It owns:

- `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `.env.example`, `README.md` (initial), `alembic.ini`
- `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_baseline.py` (creates the `app_meta` table only)
- `src/sales0/__init__.py`, `__main__.py`, `config.py` (ALL Phase-1 settings, see `architecture.md` → Configuration), `logging.py`, `errors.py`, `clock.py`, `cli.py`
- `src/sales0/db/__init__.py`, `db/engine.py`, `db/base.py`, `db/migrate_check.py`, `db/models/__init__.py` (placeholder exporting `AppMeta` only; Phase-1 slice A replaces it)
- `src/sales0/llm/**`: the complete LLM abstraction layer (`client.py`, `anthropic_adapter.py`, `pricing.py`, `errors.py`)
- `src/sales0/web/__init__.py`, `web/app.py`, `web/envelope.py`, `web/middleware.py`, `web/templating.py`, `web/routes/__init__.py`, `web/routes/health.py`
- Placeholder router modules, each exporting `router = APIRouter()` and already registered in `web/app.py`: `web/routes/setup.py`, `web/routes/runs.py`, `web/routes/inbox.py` (all three empty), `web/routes/stubs.py` (holds only `GET /` → `home.html`), `src/sales0/linkedin_fixture/__init__.py`, `src/sales0/linkedin_fixture/routes.py`. The fixture router is mounted only when `SALES0_LINKEDIN_MODE=fixture`.
- Placeholder `web/templates/base.html`, `web/templates/error.html` (used by the envelope's full-page error handler) and `web/templates/home.html` ("sales0 is running") and `web/static/css/app.css` (one rule) + `web/static/vendor/htmx.min.js` (vendored htmx 2.x)
- `tests/__init__.py`, `tests/conftest.py` (temp SQLite DB + migrations + settings reset), `tests/unit/test_smoke.py`, `tests/integration/test_llm_ping.py`, `tests/integration/test_boot.py`

**Gate (run in order; all must pass):**

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv sync
uv run playwright install chromium
uv run alembic upgrade head
uv run alembic current                      # must print: 0001_baseline (head)
uv run python -m sales0.cli check-llm       # must print: LLM OK model=claude-haiku-5-5 (exit 0; real call)
uv run python -m sales0                     # launch with the terminal tool's background mechanism
curl -fsS http://127.0.0.1:8765/api/health  # {"ok":true,"data":{"status":"ok","db_revision":"0001_baseline",...}}
uv run pytest tests -q                      # green; includes the real-key ping test
```

Also required: `.env.example` lists every variable in `architecture.md` → Configuration, and `.env` is gitignored. The first commit is pushed and a PR is open (root session).

**How the user tests it:** open http://127.0.0.1:8765/ and see "sales0 is running". Nothing else is expected yet.

---

### Phase 1 — LinkedIn Discovery → AI Triage → Review Inbox

**Goal:** the full primary journey works on the real LinkedIn session and the real Haiku. The user adds keywords, presses **Connect LinkedIn** (a one-time login in a visible browser window), and presses **Run now**. The run scrapes gently with live progress, merges duplicates, and triages each new enquiry with Haiku. The Review inbox shows the cards. The user accepts (→ Prospect · New), discards (kept and searchable) or overrides, one at a time or in bulk. The run log and the cost per enquiry are visible.

**Capabilities (4):** [search-setup](capabilities/search-setup.md), [linkedin-discovery](capabilities/linkedin-discovery.md), [ai-triage](capabilities/ai-triage.md), [review-inbox](capabilities/review-inbox.md).

**Real vs stubbed surfaces:**

| Surface | Phase 1 state |
|---|---|
| Inbox, Runs, Setup (LinkedIn connection, Keywords, Offerings), header status light, alert banner, cost badge | REAL |
| Nav: Pipeline (Prospects/Leads/Clients), Contacts, Competitors & Groups, Import | Stub page: "Coming in Phase 2" |
| Nav: Outreach | Stub page: "Coming in Phase 3" |
| Nav: Metrics | Stub page: "Coming in Phase 4" |
| Nav: WhatsApp groups | Stub page: "Coming in Phase 5" |

**Independent slices.** A–H fan out concurrently. Each one codes against the contracts in `architecture.md` → `## Contracts` and the schema in `data.md`. "Gate-after" means the slice's own integration tests need the listed slice to have landed. Its code does not wait.

| Slice | Owns (disjoint paths) | Build dependency | Gate-after |
|---|---|---|---|
| **A — Data model + migration** | `src/sales0/db/models/**` (replaces the scaffold placeholder `__init__.py`), `migrations/versions/0002_phase1_discovery.py`, `tests/integration/db/**` | Scaffold | — |
| **B — Triage agent** | `src/sales0/agent/**`, `src/sales0/prompts/**`, `tests/unit/agent/**`, `tests/integration/agent/**`, `tests/support/__init__.py`, `tests/support/fake_llm.py` | Scaffold (uses `sales0.llm`) | A (few-shot loader reads `triage_decisions`) |
| **C — Scraper + fixture LinkedIn** | `src/sales0/scraper/**`, `src/sales0/linkedin_fixture/**` (replaces the placeholder routes), `tests/unit/scraper/**`, `tests/integration/scraper/**` | Scaffold | — (fully independent) |
| **D — Services + discovery job** | `src/sales0/services/**`, `src/sales0/jobs/**`, one startup line in `src/sales0/web/app.py` (`recover_interrupted_runs`; no other app.py edits), `tests/unit/services/**`, `tests/integration/services/**`, `tests/integration/jobs/**` | Contracts of A, B, C | A, B, C |
| **E — UI shell + stubs** | `src/sales0/web/templates/base.html` and `src/sales0/web/templates/error.html` (both replace scaffold placeholders), `src/sales0/web/templates/home.html` (deleted; `/` redirects to `/inbox`), `src/sales0/web/templates/_shell/**`, `src/sales0/web/templates/stubs/**`, `src/sales0/web/static/css/app.css` (replaces placeholder), `src/sales0/web/static/js/app.js`, `src/sales0/web/context.py`, `src/sales0/web/routes/stubs.py`, `tests/integration/web/test_shell.py` | Contract of D (`session_status`, `cost_summary`) | D |
| **F — Setup page** | `src/sales0/web/routes/setup.py`, `src/sales0/web/templates/setup/**`, `tests/integration/web/test_setup_routes.py` | Contracts of D, E (shell blocks/classes) | D, E |
| **G — Runs page** | `src/sales0/web/routes/runs.py`, `src/sales0/web/templates/runs/**`, `tests/integration/web/test_runs_routes.py` | Contracts of D, E | D, E |
| **H — Inbox page** | `src/sales0/web/routes/inbox.py`, `src/sales0/web/templates/inbox/**`, `tests/integration/web/test_inbox_routes.py` | Contracts of D, E | D, E |
| **I — Live E2E + README** | `tests/e2e/**`, `tests/support/live_server.py`, `README.md` (Phase-1 update) | All of A–H | A–H |

**Gate (run in order; all must pass, real Haiku key from `.env`, SQLite production engine):**

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv sync
uv run alembic upgrade head
uv run alembic current                       # must print: 0002_phase1_discovery (head)
uv run pytest tests/unit -q
uv run pytest tests/integration -q           # real Haiku on the fixture corpus; real SQLite files
uv run pytest tests/e2e -q                   # live server (fixture mode, port 8766) + headless Chromium + real Haiku
uv run python -m sales0                      # launch with the terminal tool's background mechanism (live mode, port 8765)
curl -fsS http://127.0.0.1:8765/api/health   # db_revision "0002_phase1_discovery"
curl -fsS http://127.0.0.1:8765/inbox | grep -q 'Review inbox'
curl -fsS http://127.0.0.1:8765/static/css/app.css | grep -q '.card'
curl -fsS http://127.0.0.1:8765/static/js/app.js | grep -q 'htmx'
curl -fsS http://127.0.0.1:8765/pipeline | grep -q 'Coming in Phase 2'
```

The full suite in one line is `uv run pytest tests -q`.

Gate assertions that the tests must make (see each capability's Success Criteria and `architecture.md` → Fixture LinkedIn corpus):

1. Migration 0002 applies on a fresh SQLite file and seeds exactly the 4 default offerings.
2. Run 1 (fixture corpus, keywords `corporate ai training`, `ai workshop`, `ai consulting`) ends `succeeded` with `posts_seen=9, posts_new=7, posts_dupe=2, enquiries_triaged=7`.
3. The real Haiku verdicts and extraction on the fixture corpus match the expected-outcome table: P1 genuine with email `training@acmelogistics.example`; P2/P3/P9 discard; P6 email null; P8 offering "AI consulting & build services".
4. Run 2 after decisions: `posts_new=2`, and every triage `llm_calls` row of run 2 has `fewshot_count >= 1` (learning loop wired).
5. Keyword `checkpoint-test` → run `aborted` with `abort_reason=checkpoint`, session `needs_attention`, the banner is rendered, and no further keywords are searched.
6. E2E (headless Chromium against the live server) walks setup → connect → run (live progress) → inbox → accept → discard (bulk) → discarded search. It asserts real Haiku content on the cards and a server JSON log line with `"event":"llm_call"` and `"event":"http_request"`. After restarting the server, the accepted enquiry is still under the Accepted tab.

**How the user tests it (handoff seed):**

1. Start the app with `uv run python -m sales0` and open http://127.0.0.1:8765/. You land on the Review inbox, which shows its empty state.
2. Go to **Setup**. Four default offerings are listed and can be edited. Add 3–5 real keywords, for example `corporate AI training`, `GenAI workshop`, `AI training partner`.
3. Click **Connect LinkedIn**. A Chromium window opens. Log in to the team LinkedIn account and finish any 2FA. Within a few seconds of landing on the feed, the header light turns green ("LinkedIn connected") and the window closes.
4. Go to **Runs** and click **Run now**. Live progress shows the current keyword, posts seen, new posts and duplicates. A run takes several minutes on purpose, because of the human-like pacing.
5. When the run finishes, open the **Inbox**. Each card shows the summary, AI verdict with a one-line reason, priority, offering, link to the post, raw text (expand) and cost. Accept a genuine one; it moves to **Accepted** as "Prospect · New". Discard a few in bulk with a reason. Search the **Discarded** tab.
6. Open the run in **Runs** to see the per-keyword log and the cost.
7. Optional offline demo: run `SALES0_LINKEDIN_MODE=fixture uv run python -m sales0` and add the keyword `ai workshop` to try the flow without LinkedIn.
8. Stubs: Pipeline, Contacts, Competitors & Groups, Import, Outreach, Metrics and WhatsApp show "Coming in Phase N". This is expected, not a bug.

---

### Phase 2 — CRM Memory: Pipeline, Contacts, Timeline, Signals, Registries, Import, Shared Login

**Goal:** accepted enquiries become real Prospects that move New → Contacted → Follow-up 1 → Follow-up 2 → Final follow-up, convert to Leads (Meeting scheduled → Proposal sent → Won/Lost/No response) and then to Clients. Shared contacts link to everything. Every change is on a timeline. Follow-ups due today are listed. Repeat posters and competitor mentions are flagged. The existing Google Sheets can be imported, and single posts can be pasted in by hand. A shared-password login protects the app for VPS use.

**Capabilities (7):** [crm-pipeline](capabilities/crm-pipeline.md), [contacts](capabilities/contacts.md), [timeline-followups](capabilities/timeline-followups.md), [signals-flags](capabilities/signals-flags.md), [registries](capabilities/registries.md), [sheet-import](capabilities/sheet-import.md), [shared-login](capabilities/shared-login.md).

**Independent slices:**

| Slice | Owns | Build dependency | Gate-after |
|---|---|---|---|
| **2A — CRM data + migration** | `src/sales0/db/models/crm.py`, `db/models/contacts.py`, `db/models/timeline.py`, `db/models/registries.py`, edits to `db/models/__init__.py` (exports only), `migrations/versions/0003_phase2_crm.py` (incl. backfill of prospects from accepted enquiries), `tests/integration/db/test_phase2_migration.py` | Phase 1 | — |
| **2B — Pipeline + timeline services** | `src/sales0/services/crm/**`, `tests/integration/services/crm/**` | 2A contract | 2A |
| **2C — Signals (repeat poster, competitor mention)** | `src/sales0/services/signals.py`, `tests/integration/services/test_signals.py` | 2A contract | 2A |
| **2D — Import + manual paste** | `src/sales0/services/importer/**`, `tests/integration/services/importer/**`, `tests/fixtures/import/**` | 2A contract; reuses the Phase-1 triage agent | 2A |
| **2E — Shared login** | `src/sales0/web/auth.py`, `src/sales0/web/templates/auth/**`, `src/sales0/web/routes/auth.py`, `tests/integration/web/test_auth.py` | Phase 1 | — |
| **2F — Pipeline + Contacts UI** | `src/sales0/web/routes/pipeline.py`, `web/routes/contacts.py`, `web/templates/pipeline/**`, `web/templates/contacts/**`, `tests/integration/web/test_pipeline_routes.py` | 2B contract | 2B |
| **2G — Registries + Import UI** | `src/sales0/web/routes/registries.py`, `web/routes/importer.py`, `web/templates/registries/**`, `web/templates/importer/**`, `tests/integration/web/test_registries_routes.py` | 2C, 2D contracts | 2C, 2D |
| **2H — E2E + README** | `tests/e2e/test_phase2_journey.py`, `README.md` | All | All |

The routers for 2E–2G are registered by slice 2H in `web/app.py`. That is the only shared-file edit, and it is done last.

**Gate:**

```bash
# working directory: /Users/sai/Workspace/Code/sales0
uv run alembic upgrade head && uv run alembic current   # 0003_phase2_crm (head)
uv run pytest tests -q                                  # real Haiku + SQLite; includes Phase-1 suite (regression)
uv run python -m sales0                                 # background; then:
curl -fsS -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/inbox   # 303 to /login when SALES0_APP_PASSWORD is set
```

**How the user tests it:** log in with the shared password. Accept an enquiry, open it in Pipeline and move it to Contacted, add a note, and set a follow-up for today. It appears under "Due today" on the Pipeline page. Mark that a reply came in and convert it to a Lead, then to Proposal sent. The timeline shows every step with timestamps. Import the two exported sheets (CSV) and see the rows appear with duplicates merged. A repeat poster shows a "Repeat poster ×N" flag. Outreach, Metrics and WhatsApp are still stubs.

---

### Phase 3 — Research + Outreach: Profile Shortlisting, Company Research Agent, Approved Email Outreach

**Goal:** the sales person shortlists people from LinkedIn profiles (outreach prospects). The research agent looks up a company's website and LinkedIn page and writes a short brief. sales0 drafts a personalised email from the offering's template, attaches the matching deck, and sends it from the team's Gmail only after a human approves that exact message.

**Capabilities (3):** [profile-outreach-discovery](capabilities/profile-outreach-discovery.md), [company-research](capabilities/company-research.md), [email-outreach](capabilities/email-outreach.md).

**Slices:** 3A data + migration `0004_phase3_outreach` (`db/models/outreach.py`); 3B profile scraper (`src/sales0/scraper/profiles.py`, `linkedin_fixture/profiles/**`); 3C research ReAct agent (`src/sales0/agent/research/**`, `prompts/research_*.md`); 3D Gmail adapter + OAuth (`src/sales0/integrations/gmail/**`); 3E templates/decks/approval services (`src/sales0/services/outreach/**`); 3F Outreach UI (`web/routes/outreach.py`, `web/templates/outreach/**`); 3G E2E + README. Dependencies: 3B, 3C and 3D are independent. 3E depends on 3A and 3D. 3F depends on 3E. 3G depends on all.

**Gate:** `uv run alembic upgrade head && uv run alembic current` (shows `0004_phase3_outreach (head)`), then `uv run pytest tests -q`. The suite includes:

- a real Gmail `drafts.create` + `drafts.delete` against the team account, using `data/google_token.json` from a one-time `uv run python -m sales0.cli google-auth`;
- the real Haiku research loop against the fixture company site.

**How the user tests it:** run `uv run python -m sales0.cli google-auth` once. Then: research a prospect's company, draft an email for it, review it, attach a deck, approve it, and see it in Gmail Sent. A draft that is not approved is never sent.

---

### Phase 4 — Metrics + Automation: Weekly Dashboard, Cost View, Scheduling + Digest, Proactive Suggestions

**Goal:** the founder's Weekly and Secondary sheets are produced automatically per ISO week, with per-source figures against targets, funnel conversion and keyword yield. A cost view breaks spend down by feature. Scrape runs are scheduled within the safety caps. A daily digest and proactive suggestions (new keywords, old prospects to re-engage) appear.

**Capabilities (4):** [metrics-dashboard](capabilities/metrics-dashboard.md), [cost-view](capabilities/cost-view.md), [scheduling-digest](capabilities/scheduling-digest.md), [proactive-suggestions](capabilities/proactive-suggestions.md).

**Slices:** 4A metrics queries (`src/sales0/services/metrics/**`); 4B cost view (`src/sales0/services/costs_report.py`); 4C scheduler (`src/sales0/jobs/scheduler.py`); 4D digest + suggestions agent (`src/sales0/agent/suggest/**`, `prompts/suggest_*.md`); 4E dashboard UI (`web/routes/metrics.py`, `web/templates/metrics/**`); 4F E2E + README. Dependencies: 4A–4D are independent. 4E depends on 4A and 4B.

**Gate:** `uv run alembic upgrade head && uv run alembic current` (shows `0005_phase4_metrics (head)`), then `uv run pytest tests -q`. The metrics tests use a seeded fixture of ≥300 enquiries spread over 6 ISO weeks and assert the exact computed weekly counts.

**How the user tests it:** open Metrics and compare this week's numbers against what you did. Then enable the schedule and see the next run time. Read the digest.

---

### Phase 5 — WhatsApp Ingestion + Per-User Accounts + Reply Tracking

**Goal:** enquiries arrive from 10–30 WhatsApp groups through a linked-device bot and go through the same dedupe and triage. Each team member has their own login and is credited on the records they act on. Email replies are detected and move Leads forward.

**Capabilities (3):** [whatsapp-ingest](capabilities/whatsapp-ingest.md), [per-user-accounts](capabilities/per-user-accounts.md), [reply-tracking](capabilities/reply-tracking.md).

**Slices:** 5A WhatsApp bridge, a Node sidecar under `whatsapp-bridge/` that posts to `/api/ingest/whatsapp` (assumed in architecture); 5B ingest endpoint + group config (`src/sales0/services/ingest_whatsapp.py`, `web/routes/ingest.py`); 5C accounts (`src/sales0/services/users.py`, `web/routes/users.py`, migration `0006_phase5_users`); 5D Gmail reply poller (`src/sales0/jobs/reply_poller.py`); 5E E2E + README. 5A and 5B share the HTTP contract only.

**Gate:** `uv run alembic upgrade head && uv run alembic current` (shows `0006_phase5_users (head)`), then `uv run pytest tests -q` and `npm --prefix whatsapp-bridge test`.

**How the user tests it:** link WhatsApp by scanning the QR code, and messages from a selected group appear in the inbox. Each user logs in separately and sees their own name on the timeline. A reply to a sent email shows on the Lead.
