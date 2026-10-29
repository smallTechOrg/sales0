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
| GET | `/pipeline`, `/contacts`, `/competitors`, `/import`, `/sync`, `/login` | Phase 2 | Real pages (no longer stubs): see "Phase 2 routes" below |
| GET | `/outreach` | E | Stub "Coming in Phase 3" |
| GET | `/metrics` | E | Stub "Coming in Phase 4" |
| GET | `/whatsapp` | E | Stub "Coming in Phase 5" |
| GET | `/criteria` | feedback | Triage criteria + discard-reason breakdown (JSON: `{version, rules}`) |
| POST | `/criteria`, `/criteria/{id}`, `/criteria/{id}/active`, `/criteria/{id}/move` | feedback | Admin-only rule add / edit (text, kind) / enable-disable (`active`) / reorder (`direction=up\|down`); 303 + flash. Codes `criterion_invalid` 400, `criterion_exists` 409, `forbidden` 403. `/inbox/decide` also takes `custom_reason` and `save_rule` (admin) |

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
| GET | `/setup/linkedin/accounts` | — | `linkedin_accounts.list_accounts` | `#linkedin-accounts` (Phase 5; polls every 2 s while a job is busy). |
| POST | `/setup/linkedin/accounts` | form `label` | `add_account` | `#linkedin-accounts` |
| POST | `/setup/linkedin/accounts/{id}/rename` | form `label` | `rename_account` | `#linkedin-accounts` |
| POST | `/setup/linkedin/accounts/{id}/disable` · `/enable` | — | `disable_account` · `enable_account` | `#linkedin-accounts`. Disabling the last enabled account → `last_account` (409). |
| POST | `/setup/linkedin/accounts/{id}/connect` · `/check` | — | `start_connect(db, id)` · `start_check(db, id)` | `#linkedin-accounts`. Connect on a db-backend account → `account_uses_cookie` (409); on a disabled one → `account_disabled` (409). |
| POST | `/setup/linkedin/cookie` | form `cookie`, `label`, optional `account_id` | `paste_cookie` then `start_check` | `#linkedin-accounts`. The cookie is never echoed back. Errors: `cookie_invalid` (400), `secret_key_required` (400, non-loopback host without `SALES0_SECRET_KEY`), `account_backend_mismatch` (409, folder account). |
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

## Phase 2 routes

Conventions are the Phase 1 ones: forms are `application/x-www-form-urlencoded` (CSV upload: `multipart/form-data`), every mutating route accepts htmx (returns the updated fragment + toast; errors toasted) and plain forms (303 back with a flash), JSON callers (`Accept: application/json`) get the envelope. All routes except `/login`, `/logout`, `/api/health`, `/static/*` require a session when accounts are enabled (`capabilities/per-user-accounts.md`; open mode = no passwords anywhere). `Actor` = the logged-in member, or the member an admin chose to act as ("X as Y"). Owners: S4 = pipeline / contacts / competitors / import; S2 = sync; S3 = login.

**Pipeline** (`web/routes/pipeline.py`)
| Method | Path | Params / form fields | Effect | Returns |
|---|---|---|---|---|
| GET | `/pipeline` | `tab=prospects\|leads\|clients` (default prospects), `view=board\|list` (default board for prospects/leads), `stage`, `status=open\|closed\|converted\|all`, `q`, `flag=repeat_poster\|competitor_mention\|no_follow_up`, `source`, `due=today\|overdue`, `page` | `pipeline.summary/board/list_*` | page with the Due-today panel (`#due-today`) |
| GET | `/pipeline/prospects/{id}` · `/pipeline/leads/{id}` · `/pipeline/clients/{id}` | — | `get_prospect/get_lead/get_client` | detail page (timeline, notes, linked enquiries, contacts, outreach messages, replies, flags, follow-ups, sheet panel) |
| POST | `/pipeline/prospects` | `name`, `company`, `email`, `phone`, `linkedin_url`, `website`, `notes` | `create_manual` | 303 to the new prospect |
| POST | `/pipeline/prospects/{id}/stage` | `stage`, `note` | `move_stage` | prospect header + timeline fragments |
| POST | `/pipeline/prospects/{id}/response` | `note` | `record_response` ("Response received") | fragments |
| POST | `/pipeline/prospects/{id}/convert` | `stage` (default meeting_scheduled), `meeting_at` | `leads.convert` | `HX-Redirect` to the lead |
| POST | `/pipeline/prospects/{id}/close` · `/reopen` | `reason` | `close` / `reopen` | fragments |
| POST | `/pipeline/prospects/{id}/tracked` | `tracked=true\|false` | `set_tracked` | fragment |
| POST | `/pipeline/prospects/{id}/flag` | `kind=no_follow_up`, `on=true\|false` | `set_flag` / `clear_flag` | fragment |
| POST | `/pipeline/prospects/{id}/note` · `/leads/{id}/note` · `/clients/{id}/note` | `body` | `timeline.add_note` | `#timeline` fragment |
| POST | `/pipeline/prospects/{id}/followup` · `/leads/{id}/followup` | `due_on`, `note` | `followups.add_custom` | `#follow-ups` fragment |
| POST | `/pipeline/followups/{id}/done` · `/snooze` | `note` / `due_on` | `complete` / `snooze` | `#due-today` + `#follow-ups` (oob) |
| POST | `/pipeline/leads/{id}/stage` | `stage`, `proposal_sent_on`, `meeting_at`, `value_estimate`, `outcome_note` | `leads.move_stage` | lead header + timeline |
| POST | `/pipeline/clients` | `name`, `company`, `started_on`, `notes` | `create_legacy` | 303 to the client |
| POST | `/pipeline/clients/{id}` | `name`, `notes`, `status` | `update_client` | client header |
| GET | `/outreach/compose?prospect_id=ID` | (existing route, extended) | pre-fills from the prospect | compose page; `POST /outreach/draft` accepts hidden `prospect_id`, `company_id` |

**Contacts** (`web/routes/contacts.py`)
| Method | Path | Params / form fields | Effect |
|---|---|---|---|
| GET | `/contacts` | `q`, `tag`, `company_id`, `page` | `list_contacts` (`#contact-list`, htmx-searchable) |
| GET | `/contacts/{id}` · `/contacts/duplicates` · `/companies/{id}` | — | detail pages (companies: read-only timeline of its prospects and enquiries) |
| POST | `/contacts` | `full_name`, `email`, `phone`, `linkedin_url`, `title`, `company` | `upsert` |
| POST | `/contacts/{id}` | `full_name`, `title`, `notes`, `company` | `update_contact` |
| POST | `/contacts/{id}/tags` · `/contacts/{id}/tags/{tag_id}/delete` | `name`, `category` | `add_tag` / `remove_tag` |
| POST | `/contacts/merge` | `keep_id`, `drop_id` | `merge` (`hx-confirm`) |
| GET | `/companies` | `q`, `characteristic`, `page` | `list_companies` (`#company-results`, htmx-searchable; JSON) |
| POST | `/companies/{id}` · `/companies/{id}/merge` (admin) | `name`, `website`, `linkedin_url`, `city`, `notes` · `drop_id` | `update_company` / `merge` |
| POST | `/contacts/{id}/emails` · `/emails/{eid}` · `/emails/{eid}/delete` · `/emails/{eid}/primary` (same for `/phones`) | `email` / `phone`, `primary`, `next` | add / change / remove / make primary (303 back to `next`) |
| POST | `/pipeline/{prospects,leads,clients}/{id}/characteristics` | `characteristic_ids` (the full set) | `characteristics.set_for` (fragment / 303) |
| POST | `/pipeline/prospects/{id}/contacts` | `full_name`, `email`, `phone`, `title`, `linkedin_url` | `prospects.add_contact` |
| POST | `/competitors/characteristic` · `/competitors/characteristic/{id}` (admin) | `name` · `name`, `active` | registry create / rename / on-off |
| POST | `/inbox/decide` (extra fields) | `characteristic_ids`, `as_client` | `prospects.apply_accept_choices` after an accept |
| POST | `/tracked/add` | `profile_url`, `name`, `note`, `next` | `tracked_profiles.add_by_url` (303 + flash; `/tracked/{id}/track|untrack` also take `next`) |

**Competitors & Groups** (`web/routes/registries.py`)
| Method | Path | Form fields | Effect |
|---|---|---|---|
| GET | `/competitors` | `show=removed` (optional) | both registries on one page (`#competitor-list`, `#group-list`) |
| POST | `/competitors/competitor` · `/competitors/competitor/{id}` · `/competitors/competitor/{id}/delete` | `name`, `website`, `approx_clients`, `client_names`, `contact`, `email`, `notes`, `active` | create / update / delete (`sheet_owned` 409 for sheet-fed rows) |
| GET | `/competitors/competitor/{id}` | — | competitor detail with linked enquiries/clients |
| POST | `/competitors/competitor/{id}/link` | `entity_type=enquiry\|client`, `entity_id` | `link_competitor` |
| POST | `/competitors/group` · `/competitors/group/{id}` · `/competitors/group/{id}/delete` | `name`, `platform`, `link`, `details`, `notes`, `active` | create / update / delete |

**Import** (`web/routes/importer.py`)
| Method | Path | Form fields | Effect |
|---|---|---|---|
| GET | `/import` | — | two cards: Paste a post, Import a CSV; recent batches |
| POST | `/import/csv` | file `file`, `kind` | `import_csv` → 303 `/import/batches/{id}` |
| GET | `/import/batches/{id}` | — | batch summary with per-row errors |
| POST | `/import/paste` | `text`, `link`, `author`, `author_profile_url` | `paste_post` → 303 `/inbox?tab=review` (toast with the verdict) |

**Sync** (`web/routes/sync.py`)
| Method | Path | Effect |
|---|---|---|
| GET | `/sync` | status panel + per-tab table + Sync now |
| POST | `/sync/run` | `trigger_now()`; htmx returns the status panel (polls); `409 job_busy`-style toast "A sync is already running" |
| GET | `/sync/status` | `#sync-status` fragment (`hx-trigger="every 2s"` only while running) |

**Login and accounts** (`web/routes/auth.py`, `web/routes/users.py`; Phase 5 - `capabilities/per-user-accounts.md`)
| Method | Path | Form | Effect |
|---|---|---|---|
| GET | `/login` | `next` (query) | login page (email, password, remember me); 303 `/` in open mode; 303 to `next` when already logged in |
| POST | `/login` | `email`, `password`, `remember` (checkbox), `next` | success: `Set-Cookie` (rotating, HttpOnly, SameSite=Lax) + 303 to `next` or `/inbox`; wrong/unknown/inactive: 401 "Wrong email or password."; locked: 429 + `Retry-After` |
| POST | `/logout` | - | deletes the server-side session, clears the cookie, 303 `/login` |
| GET | `/set-password/{token}` | - | open path. Valid link: the form; used/expired/unknown: 410 page "This link has expired or was already used" |
| POST | `/set-password/{token}` | `password`, `confirm` | min length (`password_weak` 400), match; sets the hash, burns the token, revokes the member's sessions, 303 `/login` |
| GET | `/account` | - | My account (name, role, change-password form) |
| POST | `/account/password` | `current`, `password`, `confirm` | change own password (`password_wrong` 400); other sessions are signed out; 303 `/account` with a flash |
| GET | `/users` | - | **admin only** (403 otherwise; in open mode the acting member must be admin). Members table, add form |
| POST | `/users` | `name`, `email`, `role` | add a member (no password) and show their one-time set-password link ONCE (`#set-password-url`); `member_exists` 409, `member_invalid` 400 |
| POST | `/users/{id}/reset` | - | force reset: new one-time link shown once; older links and the member's sessions are invalidated, `must_reset` set when they had a password |
| POST | `/users/{id}/role` | `role` | change role; `last_admin` 409 for the only active admin |
| POST | `/users/{id}/active` | `active` = `0`/`1` | deactivate (revokes sessions) / reactivate; `last_admin` / `self_lockout` 409 |
| POST | `/team/acting` | `member_id`, `next` | admin (or open mode) only: act as another member; others 403 |
The Team page's `POST /team/members` and `/team/members/{id}/active` also require the REAL user to be an admin. Open paths: `/login`, `/logout`, `/set-password/*`, `/api/health`, `/static/*` (+ `/__fixtures/*` in fixture mode).

## JSON

| Method | Path | Response `data` |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","db_revision":"<rev>","linkedin_mode":"live\|fixture","model":"<SALES0_LLM_MODEL>","api_key_present":true}` |
| GET | `/api/runs/{id}` | `RunDetailView` serialised (used by E2E for exact counter assertions). Served by `web/routes/runs.py` (G); unknown id → `not_found` 404. |
| GET | `/api/session` | `SessionStatusView` serialised. Served by `web/routes/setup.py` (F). Phase 1.1 adds `browser_label`, `browser_note` and `cooldown_until`. It never contains a cookie value. |
| GET | `/api/pipeline/summary` | Phase 2. `PipelineSummary` (counts per stage, due_today, overdue, clients). Served by `web/routes/pipeline.py`. |
| GET | `/api/prospects/{id}` | Phase 2. `ProspectDetail` serialised (E2E assertions). Unknown id → `not_found` 404. |
| GET | `/api/sync/status` | Phase 2. `{"enabled", "interval_min", "running", "next_run_at", "last_started_at", "last_finished_at", "last_status", "last_error", "tabs": [{"tab","kind","rows","added","updated","unchanged","removed","errors","warnings","fetch_error"}]}`. Counts only, never cell content. Served by `web/routes/sync.py`. |
| POST | `/login`, `/logout`, `/set-password/{token}` | HTML, see above. A JSON caller without a session gets `401 {"ok": false, "error": {"code": "unauthorized", ...}}` from every other route. |

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
| `account_disabled` | 409 | Phase 5. "LinkedIn account '<label>' is disabled - enable it in Setup." (eligibility, after `run_in_progress`). |
| `safety_cooldown` | 429 | Phase 1.1. "LinkedIn asked us to slow down — next run allowed after HH:MM". `HH:MM` is `safety_cooldown_until` in `SALES0_TIMEZONE`, 24-hour; when that falls on a later local day it reads "HH:MM tomorrow". |
| `no_keywords` | 409 | "Add at least one active keyword in Setup." |
| `daily_run_cap` | 429 | "Daily run limit reached (<n>/<cap>). Runs reset at midnight (<tz>)." |
| `daily_post_cap` | 429 | "Daily post limit reached (<n>/<cap>) — protects your LinkedIn account." |
| `decision_invalid` | 400 | "Choose Accept or Discard for at least one enquiry." Also raised when a target enquiry is still pending triage. |
| `field_invalid` | 400 | Raised by `update_fields` for a priority outside high/medium/low or an inactive/unknown offering, and by `POST /inbox/{id}/fields` when neither field is sent ("Choose a priority or an offering to change."). |
| `triage_failed` | 502 | "AI triage failed: <error>. Try again in a minute." |
| `raw_purged` | 410 | Phase 1.4. "The post text is no longer stored (kept 14 days) — it can't be rescued. Find it again in a later run." |
| `already_rescued` | 409 | Phase 1.4. "This post is already in your inbox." |
| `unauthorized` | 401 | Phase 2. "Please log in." |
| `login_failed` | 401 | "Wrong email or password." |
| `link_invalid` | 400/410 | "This link has expired or was already used - ask an admin for a new one." |
| `password_weak` | 400 | "Use at least N characters for the password." |
| `password_wrong` | 400 | "Your current password is not right." |
| `last_admin` | 409 | "You cannot deactivate/demote the only active admin." |
| `self_lockout` | 409 | "You cannot deactivate your own account - ask another admin." |
| `forbidden` | 403 | "Only an admin can do that." |
| `login_locked` | 429 | "Too many attempts - try again in N seconds." |
| `prospect_closed` | 409 | "This prospect is closed - reopen it first." |
| `stage_invalid` | 400 | "Choose a valid stage." |
| `lead_needs_contact` | 409 | "This prospect has not been contacted yet - mark it Contacted first." |
| `lead_needs_response` | 409 | "No response is recorded yet - press 'Response received' first." |
| `lead_exists` | 409 | "This prospect already has an open lead." |
| `followup_invalid` | 400 | "Choose a valid date for the follow-up." |
| `contact_invalid` | 400 | "A contact needs a name, an email or a LinkedIn URL." |
| `tag_invalid` | 400 | "A tag is 1-40 characters." |
| `merge_self` | 400 | "Choose two different contacts to merge." |
| `competitor_invalid` | 400 | "A competitor needs a name." |
| `sheet_owned` | 409 | "This row comes from the Google Sheet - remove it there (or deactivate it here)." |
| `import_invalid` | 400 | "Choose a CSV file and what it contains." (also: file too large 413 `import_too_large`) |
| `paste_invalid` | 400 | "Paste at least 40 characters of the post." |
| `sync_running` | 409 | "A sync is already running." |
| `llm_auth` | 502 | "Anthropic rejected the key — check SALES0_ANTHROPIC_API_KEY in .env.", or for a key that is not workspace-scoped: "This Anthropic key is not workspace-scoped — set SALES0_ANTHROPIC_WORKSPACE_ID in .env." |

**Phase 5: eligibility is per LinkedIn account** (`run_eligibility(db, now, account_id)`, `start_run(db, actor, linkedin_account_id)`, default = the first enabled account): `account_disabled` follows `run_in_progress`; `needs_attention`, `safety_cooldown`, `not_connected`, `daily_run_cap` and `daily_post_cap` all read that account's status, aborted runs and counters. `GET /api/session` describes the default account.

**`start_run` / eligibility order** (Phase 1.1; the first failing check wins; `Eligibility.reason_code` is the code): `run_in_progress` → `needs_attention` → `safety_cooldown` → `not_connected` → `job_busy` → `no_keywords` → `daily_run_cap` → `daily_post_cap`. Phase 1.4: `daily_post_cap` counts posts **scanned** today (cap 2000) and `daily_run_cap` caps at 4; `POST /runs` returns immediately (the run is `queued`; the work happens on the job thread).

**Routes are unchanged in Phase 1.1.** `/setup/linkedin/connect`, `/connect/cancel`, `/check` and `/status` keep their paths and fragments. Only the card copy changes (`ui.md`). The CLI gains `python -m sales0.cli check-browser` (`architecture.md` → LinkedIn browser).
