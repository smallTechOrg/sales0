# sales0 — HTTP Contract (Phase 1 + 1.1)

Single origin: `http://127.0.0.1:8765`. Pages are server-rendered HTML. Partials are HTML fragments for htmx: a request is treated as htmx when it carries the `HX-Request: true` header. JSON endpoints live under `/api/` and use the envelope from `architecture.md` → Conventions. Forms are `application/x-www-form-urlencoded`. Every mutating form route accepts htmx and returns a fragment. Without htmx it redirects 303 back to the page.

## Pages

| Method | Path | Owner slice | Returns |
|---|---|---|---|
| GET | `/` | E | 303 → `/inbox` |
| GET | `/inbox?tab=review\|accepted\|discarded&q=&page=` | H | Review inbox page (default tab `review`) |
| GET | `/runs` | G | Run now panel + active run progress + history |
| GET | `/runs/{run_id}` | G | Run detail (per-keyword log, totals, cost) |
| GET | `/setup` | F | LinkedIn connection card, Keywords card, Offerings card, Safety caps (read-only) |
| GET | `/pipeline`, `/contacts`, `/competitors`, `/import` | E | Stub page "Coming in Phase 2" + one-line description |
| GET | `/outreach` | E | Stub "Coming in Phase 3" |
| GET | `/metrics` | E | Stub "Coming in Phase 4" |
| GET | `/whatsapp` | E | Stub "Coming in Phase 5" |

## Partials / actions

| Method | Path | Form fields | Effect | Fragment returned |
|---|---|---|---|---|
| POST | `/setup/keywords` | `text` | `add_keyword` | `#keyword-list` (re-rendered) + ok toast |
| POST | `/setup/keywords/{id}/delete` | — | `remove_keyword` (`hx-confirm`) | `#keyword-list` |
| POST | `/setup/keywords/{id}/toggle` | `active=true\|false` | `set_keyword_active` | keyword row |
| POST | `/setup/offerings` | `name`, `group_name`, `description` | `create_offering` | `#offering-list` |
| GET | `/setup/offerings/{id}/edit` | — | — | offering row in edit mode |
| POST | `/setup/offerings/{id}` | `name`, `group_name`, `description` | `update_offering` | `#offering-list` |
| POST | `/setup/offerings/{id}/delete` | — | `delete_offering` (`hx-confirm`; toast says "deleted" or "deactivated — still used by N enquiries") | `#offering-list` |
| POST | `/setup/linkedin/connect` | — | `start_connect` | `#linkedin-card` (busy state, polling) |
| POST | `/setup/linkedin/connect/cancel` | — | `runner.cancel_connect()` | `#linkedin-card` |
| POST | `/setup/linkedin/check` | — | `start_check` | `#linkedin-card` |
| GET | `/setup/linkedin/status` | — | — | `#linkedin-card`. Includes `hx-trigger="every 1s"` only while busy. Also sends `HX-Trigger: session-changed` when the status changed, so the header light refreshes. |
| GET | `/_shell/status-light` | — | — | `#status-light` fragment (E). Refreshed on the `session-changed` event and every 5s while busy. |
| GET | `/_shell/banner` | — | — | `#alert-banner` fragment (E) |
| POST | `/runs` | — | `start_run` | `#run-panel` (progress, polling) |
| GET | `/runs/{id}/progress` | — | — | `#run-panel`. `hx-trigger="every 1s"` while queued or running. The final render shows the outcome + "Open inbox (N new)". |
| POST | `/inbox/decide` | `enquiry_ids` (repeated), `decision=accept\|discard`, `discard_reason` (opt), `tab`, `q`, `page` | `decide` | `#inbox-list` for the current tab + tab counts (`hx-swap-oob` on `#inbox-tabs`) + toast "Accepted 2" / "Discarded 3" |
| POST | `/inbox/{id}/fields` | `priority`, `offering_id` (each optional; an absent `offering_id` keeps it, `""`/`none` means "No match"; neither field → `field_invalid`) | `update_fields` (semantics in `architecture.md` → Contracts) | that card `#enquiry-{id}` |
| POST | `/inbox/{id}/retry` | — | `retry_triage` | that card |
| POST | `/setup/keywords/suggested` | — | `seed_suggested` (Phase 1.4) | `#keyword-list` + toast "Added N · M already present" |
| POST | `/setup/keywords/{id}/edit` | `text`, `group_name` (opt) | `update_keyword` | keyword row |
| GET | `/inbox?tab=skipped&state=discard\|failed&q=&page=` | — | `list_skipped` | Skipped-by-AI tab (full page, or `#inbox-list` for htmx) |
| POST | `/inbox/skipped/{id}/rescue` | — | `seen_posts.rescue(accept=False)` | row removed + toast "Moved to To review"; tab counts oob |
| POST | `/inbox/skipped/{id}/override` | — | `seen_posts.rescue(accept=True)` | row removed + toast "Accepted as Prospect · New (overrode AI)" |
| POST | `/inbox/skipped/{id}/retry` | — | `seen_posts.retry_triage` | that row |

## JSON

| Method | Path | Response `data` |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","db_revision":"<rev>","linkedin_mode":"live\|fixture","model":"<SALES0_LLM_MODEL>","api_key_present":true}` |
| GET | `/api/runs/{id}` | `RunDetailView` serialised (used by E2E for exact counter assertions). Served by `web/routes/runs.py` (G); unknown id → `not_found` 404. |
| GET | `/api/session` | `SessionStatusView` serialised. Served by `web/routes/setup.py` (F). Phase 1.1 adds `browser_label`, `browser_note` and `cooldown_until`. It never contains a cookie value. |

## Fixture-only routes

These are mounted at `/__fixtures/linkedin` only when `SALES0_LINKEDIN_MODE=fixture`. They are listed in `architecture.md` → Fixture LinkedIn corpus. In live mode every `/__fixtures/*` path returns 404.

## DomainError codes

| Code | HTTP (JSON) | Message (user-facing) |
|---|---|---|
| `keyword_invalid` | 400 | "Keyword must be 2–100 characters." |
| `keyword_duplicate` | 409 | "'<text>' is already in your keywords." |
| `offering_invalid` | 400 | "Offering name (1–80) and group (1–60) are required." |
| `offering_duplicate` | 409 | "An offering named '<name>' already exists." |
| `csrf_rejected` | 403 | "Request blocked: it came from another site." Raised by the same-origin middleware for a cross-origin unsafe request (`architecture.md` → Security & Safety). |
| `not_found` | 404 | "That item no longer exists — refresh the page." |
| `job_busy` | 409 | "LinkedIn is busy (<action> in progress) — wait for it to finish." |
| `run_in_progress` | 409 | "A run is already in progress." |
| `not_connected` | 409 | "Connect LinkedIn first (Setup → Connect LinkedIn)." |
| `needs_attention` | 409 | "LinkedIn needs attention: <last_error>. Press Connect LinkedIn in Setup — the sales0 Chrome window opens on LinkedIn so you can resolve it." (Phase 1.1 wording) |
| `safety_cooldown` | 429 | Phase 1.1. "LinkedIn asked us to slow down — next run allowed after HH:MM". `HH:MM` is `safety_cooldown_until` in `SALES0_TIMEZONE`, 24-hour; when that falls on a later local day it reads "HH:MM tomorrow". |
| `no_keywords` | 409 | "Add at least one active keyword in Setup." |
| `daily_run_cap` | 429 | "Daily run limit reached (<n>/<cap>). Runs reset at midnight (<tz>)." |
| `daily_post_cap` | 429 | "Daily post limit reached (<n>/<cap>) — protects your LinkedIn account." |
| `decision_invalid` | 400 | "Choose Accept or Discard for at least one enquiry." Also raised when a target enquiry is still pending triage. |
| `field_invalid` | 400 | Raised by `update_fields` for a priority outside high/medium/low or an inactive/unknown offering, and by `POST /inbox/{id}/fields` when neither field is sent ("Choose a priority or an offering to change."). |
| `triage_failed` | 502 | "AI triage failed: <error>. Try again in a minute." |
| `raw_purged` | 410 | Phase 1.4. "The post text is no longer stored (kept 14 days) — it can't be rescued. Find it again in a later run." |
| `already_rescued` | 409 | Phase 1.4. "This post is already in your inbox." |
| `llm_auth` | 502 | "Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env.", or for a key that is not workspace-scoped: "This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env." |

**`start_run` / eligibility order** (Phase 1.1; the first failing check wins; `Eligibility.reason_code` is the code): `run_in_progress` → `needs_attention` → `safety_cooldown` → `not_connected` → `job_busy` → `no_keywords` → `daily_run_cap` → `daily_post_cap`. Phase 1.4: `daily_post_cap` counts posts **scanned** today (cap 2000) and `daily_run_cap` caps at 4; `POST /runs` returns immediately (the run is `queued`; the work happens on the job thread).

**Routes are unchanged in Phase 1.1.** `/setup/linkedin/connect`, `/connect/cancel`, `/check` and `/status` keep their paths and fragments. Only the card copy changes (`ui.md`). The CLI gains `python -m sales0.cli check-browser` (`architecture.md` → LinkedIn browser).
