# Capability: Prospect Research, Account Summary and Our Notes
**Feedback slice (branch `feature/sales0-research`, migration `q15_research_7d3a9c21`).** Supersedes the compose-only
research of `company-research.md` for prospects (compose still works and reuses it). Composition in `agent.md` →
Capability 2 (research) and Capability 2b (account summary).

## What It Does
On the prospect page (`/pipeline/prospects/{id}`), three cards next to the timeline:

1. **Research** - button "Research" (re-run: "Re-run research"). One press = one AI run = one new **version** of the
   prospect's research brief. Sources: (1) the company website (bounded ReAct loop, SSRF-guarded `SafeFetcher`), (2) our
   own data (enquiries / post text, author headlines, contacts + titles, sheet row, our notes) passed as quoted records,
   (3) the LinkedIn company page text fetched earlier by the laptop worker, (4) Anthropic server-side web search.
   Shows the current version (what they do, size, fit, AI-training needs, hooks), its **sources** (chips Website /
   LinkedIn / Web / Our data), who/when, cost, web-search count, an **Edit research** form (saving = a new version marked
   human-edited) and the **history** (every version, who/when, AI vs edited).
   A line "LinkedIn page: pending / being read / fetched <time> / failed: why / needs the laptop worker".
2. **Account summary** - button "Refresh summary": ONE Haiku call over everything we have (enquiries, contacts +
   headlines, latest research, our notes, emails + replies, follow-ups, timeline, sheet data, LinkedIn page text) that
   writes: who they are, what they want from us, where we stand, characteristics, open follow-ups, next best step.
   Editable (a human edit is a new version), versioned with history, badge "AI-generated" vs "Human-edited".
3. **Our notes** - dated, authored entries: add; edit / delete your OWN; the sheet's notes cell is shown beside them,
   read-only ("From the Google Sheet").

**Research now covers three things** (prompt `research-v3`): the **company**, the **enquiry** (`enquiry_insights`: what is
really asked, audience / timing / budget signals, fit, what to clarify) and the **people** behind it (`people`: name,
title, background, how to approach - the enquiry author first, then contacts; web search also looks up up to two of them,
professional information only). Each version records what it covered (`inputs.schema`, `enquiry_ids`, `contact_ids`,
`trigger`).

**Structured record, written by the research model (schema 4, `agent/research/synth.py`).** The cheap model still reads
the website (ReAct loop) and does the web search. A final call on `SALES0_RESEARCH_MODEL` (Sonnet 5.5, forced tool
`record_research`, prompt `research-synth-v1`) turns pages + web findings + LinkedIn page + our records into one typed record:
`profile` (industry, headquarters, size band, business model, customer segments), `enquiry` (need, audience, timing,
budget signal, urgency, fit, open questions), `facts` (section, claim, source, confidence - a URL source is kept only if it
was really in the input, else `our records`), `gaps` (what could not be established) and `completeness` (0-100, computed in
code). The flat fields drafting reads (`what_they_do`, `personalisation_hooks`, `enquiry_insights`, `people`, ...) are still
filled. If the call fails or is off (`SALES0_RESEARCH_SYNTHESIS=false`) the loop's own brief is kept as schema 3 and the
prospect stays pending for the bulk run. Every entry point (button, on accept, Pipeline bulk, Companies bulk) goes
through `research_prospect`, so all of them get this.

**Bulk from the Companies page**: tick companies > **Research selected** (`POST /companies/research`, JSON too). Each
company is researched through its primary prospect with a fresh version, in chunks of `SALES0_RESEARCH_BULK_CHUNK` per
click (hosted: also the action budget); the answer lists outcomes and a "Continue with N more" form. Companies with no
prospect, or whose newest research a person edited, are skipped and named.

**Automatic + bulk** (`services/research/auto.py`, `SALES0_AUTO_RESEARCH`, default on):
- *On accept*: after the Inbox commit, research for the accepted enquiry's prospect starts in a background thread (one
  version, actor `system`, `trigger: accepted`). A failure never affects the accept; the prospect stays pending.
- *Bulk*: Pipeline > Enquiries card **Research active enquiries (N)** researches every pending prospect, one at a time,
  committing after each; stops at the spend cap, after two failures in a row, or (hosted) at
  `SALES0_HOSTED_ACTION_BUDGET_S` with "N more pending - click again". JSON: `POST /pipeline/research/pending`.
- *Active* = a prospect with an accepted enquiry that is not won / lost / no response / on hold, prospect not closed,
  company not off-ramped. *Pending* is derived (no queue table): no usable research, research from before v3, or an
  active enquiry / contact the newest research did not cover. A newest version a person edited is never replaced
  automatically (use "Re-run research" on the company).
- Hosted (Cloud Run bills CPU per request) the on-accept thread may pause between requests; the bulk button finishes it.

Manual runs still work as before. Hosted mode has no scheduler: research and the summary run INSIDE the request with an htmx
spinner (`hx-indicator`, button disabled). Research is bounded by `SALES0_RESEARCH_TOTAL_TIME_S` (60 s website budget)
plus one web-search request (typically 5-10 s); the summary is one call (2-4 s). Cloud Run's 900 s timeout is never
approached.

## Inputs
| Input | Source | Required |
|---|---|---|
| Company name, website, LinkedIn company page | the prospect's company (Edit details) | name |
| Our records | `services/research/context.build` (CRM tables; no new scraping) | - |
| LinkedIn company page text | `worker_tasks` (kind `company_page`, status `done`) | - |
| Web search | Anthropic `web_search_20250305`, `max_uses = SALES0_RESEARCH_WEB_SEARCH_MAX_USES` (3) | - |

## Outputs
| Output | Destination |
|---|---|
| Research version (brief JSON, sources, inputs used, cost, web searches) | `prospect_docs` (doc_type `research`) + a `company_briefs` row (back-compat) + timeline event `research` |
| Summary version (plain text + structured fields) | `prospect_docs` (doc_type `summary`) |
| Note entries | `prospect_notes` |
| Company-page task | `worker_tasks` |
| Cost rows | `llm_calls` feature `research` (prompt versions `research-web-v2`, `research-v3`) / `summary` (`summary-v1`), `web_search_requests` |

## Business Rules
- **Versions.** Every AI run and every human edit appends a version (`version` 1, 2, ... per prospect + doc type,
  unique). Nothing is overwritten. The highest version is current. A failed AI run is stored too (the page says why)
  but drafting skips failed versions (`latest_usable`).
- **Drafting** (`services/outreach/drafting.generate_draft`, prompt `draft-v2`): when the compose form carries a
  `prospect_id`, the brief is the research version shown on Compose (`research_doc_id`), else the newest usable one
  (a human edit included); the newest usable account summary is added as background (`<account_summary>`, never
  quoted). Compose's own "Research company" button, for a prospect, runs the same research (no LinkedIn queueing) and
  saves the next version; without a prospect it behaves as before (`company_briefs` only).
- **Web search** is ONE extra Haiku request with the server tool and a `record_web_findings` tool (`tool_choice=auto`;
  a forced tool would skip the search). A finding is kept only when its URL was really returned by the search
  (anti-hallucination). The system prompt is sent uncached on that request (otherwise the API writes a 1.25x cache entry
  over the search results). `SALES0_RESEARCH_WEB_SEARCH=false` turns it off. Cost = tokens + `$10 / 1,000 searches`
  (`SALES0_LLM_PRICE_WEB_SEARCH_USD_PER_1K`), from `usage.server_tool_use.web_search_requests`.
- **Prompt injection.** Website pages (`<page_data>`), the LinkedIn page and web findings (`<external_data>`) are
  untrusted data; `<our_records>` are trusted CRM facts but any third-party text quoted inside them is data too. All are
  `inert`-escaped (no delimiter breakout). Only the system prompt defines behaviour.
- **Prompt caching.** The research user turn is split: a stable head (task, records, other sources, offerings) with a
  cache breakpoint, then the per-step tail (pages so far, budget, notes, links) - multi-step runs read the head from
  the cache (measured: 2.3k tokens read on step 2). One-shot runs (no website) send no breakpoint.
- **LinkedIn company page via the laptop worker.** Research queues at most one `company_page` task per prospect (not
  when one is pending) when the company has a `linkedin.com/company/<slug>` URL and `SALES0_WORKER_TOKEN` is set.
  The task is handed only to a worker whose claim lists `capabilities: ["company_page"]`; old workers never see it
  (the claim answer omits the `task` key for them - their strict parser would reject it) and a new worker falls back to
  the old claim body when an old hosted app answers 422. The claim gives a SLUG, never a URL: the worker builds
  `<its LinkedIn base>/company/<slug>/` itself. Hand-out gates (same machinery as runs): no scrape run in progress (the
  task waits), account not disabled, no safety cool-down, daily page-load cap not reached, and
  `SALES0_COMPANY_PAGE_MAX_PER_DAY` (10) - otherwise the task is failed with the reason. The worker opens the logged-in
  profile with a page-load budget of 1, loads ONE page with the search checkpoints (before / after the navigation and
  after one gentle paced scroll; latched 429 / checkpoint / captcha / logout -> safety stop), reads the visible text
  (<= 20k chars) and uploads it. A safety stop marks the account `needs_attention` + `last_stop_at` (the usual
  cool-down then blocks every run and task); not logged in marks it `not_connected`. Its page load counts in
  `runs.page_loads_today` (the daily counter shared with discovery and the watch). A claimed task silent for
  `SALES0_COMPANY_PAGE_STALE_S` (600 s) is failed ("worker lost"). The fetched text is used by the NEXT research /
  summary press.
- **Notes.** Only the author edits / deletes a note (admins included, like outreach messages); a note without an author
  (migrated) may be edited by admins. `prospects.notes` (the old single field) was copied into a first note entry by
  the migration and is now legacy (kept, no longer written; `prospects.update(notes=...)` and the new-prospect form add a
  note entry instead). The sheet never writes `prospect_notes` (its notes cell stays in `prospects.sheet_notes`).
- **Permissions.** Any team member may run research / refresh the summary / edit either (they are shared account
  knowledge, attributed by name; an admin acting as a member is credited as that member). Spend cap
  (`ensure_within_spend_cap`) is checked before any call.

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Company website | `SafeFetcher` GET, <= 5 pages, 200 KB, 60 s total | observation to the agent; brief may be partial |
| Anthropic (Haiku 5.5) | web step (1 request, <= 3 searches) + research loop (<= 6 steps) / summary (1) | web step failure = research continues without it; fatal auth/model -> 502 toast, error row kept |
| LinkedIn (laptop worker) | ONE company page, logged-in profile, search safety checks | task failed with reason; safety stop -> account cool-down |

## Measured cost (real Haiku, example.com, 2026-10-29)
Research run with web search: web step 2 searches + ~21k input tokens = $0.0224; research loop 2 steps = $0.0003;
total **$0.0227** (~2.3 cents; ~90% is the per-search fee). Without web search: ~$0.0003-0.0006. Summary: **$0.0003**.

## Success Criteria
- [x] Research from the prospect page stores version N with sources from website / web / our data, and the timeline shows it.
- [x] Editing research or the summary creates a human-edited version; drafting uses the newest usable version.
- [x] Notes: add / edit / delete own; others get 403; migrated notes visible; sheet sync cannot clobber them.
- [x] A capable worker claims the company-page task, uploads the text, page loads are counted; old workers are untouched.
- [x] A safety stop on the company page pauses the account (cool-down) and later tasks are refused.
- [x] The real reader works against the fixture LinkedIn (headless Chromium), never linkedin.com in tests.
