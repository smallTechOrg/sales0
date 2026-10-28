# Capability: Phase 4 ops (costs, scheduler, digest, suggestions)
**Phase 4, the parts that do not need the CRM tables** (metrics/funnel and follow-up sections arrive with Phase 2).

## Cost view and spend cap (`/costs`, `services/costs_report.py`)
- Reads `llm_calls` only. Daily (30 local days, zero days included), monthly (6), this-month per feature and per model, cost per triaged enquiry (triage cost / distinct enquiries with an ok triage call) and per outreach draft (drafting cost / drafts created this month).
- `SALES0_MONTHLY_SPEND_CAP_USD` (0 = off). `>= SALES0_SPEND_CAP_WARN_PCT` (80) % -> warning banner on every page; `>= 100` % -> hard stop: `ensure_within_spend_cap` raises `SpendCapReached` (429 `spend_cap_reached`) BEFORE every LLM call (triage, research, drafting, suggestions, digest summary). There is no essential LLM call. A run keeps the posts it saved and stops triaging (a fatal triage error for the run: the run is marked failed with the cap message, remaining keywords skipped, enquiries stay `pending`; Retry triage after raising the cap or next month). The scheduler refuses to start a nightly run while the cap is exceeded.

## Scheduler (`jobs/scheduler.py`, `/schedule`)
- In-process thread (poll `SALES0_SCHEDULER_POLL_S`), started/stopped in the app lifespan, off with `SALES0_SCHEDULER_ENABLED=false`. State in `scheduled_jobs` (enabled, next_run_at, last result, `state_json`), so choices, planned slots and "digest built today" survive restarts.
- Jobs: `discovery_run` (default OFF; window 01:00-05:00 local, up to min(`SALES0_SCHEDULER_DISCOVERY_MAX_RUNS_PER_DAY`, `SALES0_SCRAPE_MAX_RUNS_PER_DAY`) jittered slots, last 30 min of the window unused; started only through `jobs/scheduled_discovery.py::start_scheduled_discovery()` which wraps the existing `services.runs.start_run`, so it never overlaps a manual run and honours daily caps, cool-downs, a LinkedIn session that needs attention and the spend cap; every refusal is stored as `skipped` with the reason, transient ones (run in progress, cool-down) retry inside the window), `daily_digest` (default on, 07:30), `sheet_sync` (default off; ticks the existing `SheetSyncRunner`; shows "waiting for Phase 2" until a CRM repository is registered), `purge` (default on, 04:15; retention hooks via `register_purge_hook`).
- Missed-run policy: no catch-up storms (see the module docstring). Failed daily jobs back off 15 minutes and give up for the day after 4 failures.

## Daily digest (`services/digest.py`, `/digest`)
Sections come from a `DigestSection` registry (Phase 2 registers follow-ups due, stale leads, tracked-profile hits). Stored in `digests` (one per local date). Optional 5-line Haiku summary (llm feature `suggest`), number-verified against the data. Never emails: Copy to clipboard, or "Create Gmail draft to myself" (draft only, acting member's own connection).

## Proactive suggestions (`agent/suggest`, `/suggestions`)
New keyword phrases grounded in genuine enquiry text and dead-keyword pauses computed in code from yield (scanned >= 30, relevant = 0); accept adds/pauses the keyword, dismiss is remembered; idempotent through `suggestions.dedupe_key`. Real Haiku, feature `suggest`.
