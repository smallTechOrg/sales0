# NOTES — harness-friction journal

Run start: 2026-10-27 (zero-shot-harness 0.2.0, project-builder orchestrating)
Branch: feature/sales0-20261027-1816-v0.1 (base: main)

## Design
- spec-writer took ~17 min / 51 tool calls for a 26-file spec (21 capability files incl. phases 2–5). Thorough, but heavy for a first invocation; most later-phase capability files won't be read until those phases.
- spec-writer flagged Haiku pricing ($1/$5 per MTok) as Assumed — needs user confirmation.
- `.env` appeared untracked before any .gitignore existed (user created it while design ran). Risk window: a careless `git add -A` before the scaffold would stage it. Mitigated by explicit staging.

## Scaffold
- Real-key validation FAILED with a non-obvious cause: the user's key is an org-level (non-workspace-scoped) Anthropic key → HTTP 400 "must include anthropic-workspace-id header". Intake had no step to validate the key *before* design (17 min spent before discovering it). Suggest: intake runs a one-shot key ping as soon as .env appears.
- Generator added optional SALES0_ANTHROPIC_WORKSPACE_ID (sent as anthropic-workspace-id header) — a spec deviation, sensible.
- Proceeding per orchestrator brief: all non-LLM gates run; real-LLM gates marked PENDING-KEY. Non-LLM suite: 35 passed.
- Key resolved mid-build: user added SALES0_ANTHROPIC_WORKSPACE_ID; root's course-correction asked for workspace-header support that the scaffold generator had already added — orchestrator messages and generator autonomy overlapped (no harm, but duplicate effort risk).

## Phase 1
- Fan-out: A, B, C, D, E–H (one agent for the 4 web slices to keep shell/pages consistent — deviation from the roadmap's 4 separate web slices; parallel web slices sharing base.html/CSS contracts felt too collision-prone), then I (E2E+README). Wall-clock dominated by D (~18 min) and E–H (~23 min).
- "Gate-after" polling: D and E–H were told to poll for upstream slices; worked because A/B/C landed within ~10 min. Fragile if an upstream slice blocks.
- Stale scaffold tests (test_boot.py asserting 0001 revision + "sales0 is running") broke as soon as Phase 1 landed; no slice owned them → needed an extra fix slice. Spec slice table should assign scaffold-test upkeep to a Phase-1 slice.
- Per-slice audits caught real issues tests didn't: keyword-deleted-mid-run crash (D), background-XHR 429 not detected (C — account safety), likely-wrong LinkedIn logged-in selector (spec-originated), no CSRF (any webpage could trigger a LinkedIn run). Worth the cost.
- Test-ordering trap: pytest-playwright's session `page` fixture breaks in-process sync_playwright tests if ordered first; reorder hook placed in tests/e2e/conftest.py only applied when e2e collected → `pytest tests/integration` alone failed though full suite passed. Phase auditor caught it by running gate steps literally. Lesson: run every gate line separately.
- Spec amended 3x after code (workspace id, contract additions, CSRF/selectors/safety, wording). Spec-writer as single design authority handled amendments quickly (~1–2 min each).
- Real LinkedIn scraping cannot be gated automatically; fixture mode covers the parser/safety paths; live selectors remain a judgement risk (flagged to user).
- Final Phase 1 gate (run by orchestrator after last fix): unit 186, integration 277 (real Haiku), e2e 2 (live server + headless Chromium + real Haiku), boot/curl checks all OK.

## Phase 1.1 (after user gate)
- User gate found two real problems the automated gates could not: (1) bundled-Chromium fresh profile looks like a separate incognito window; (2) the logged-in check keyed on `#global-nav` class markers that current LinkedIn doesn't ship → user logged in (incl. 2FA) and sales0 never noticed. Lesson: never gate "logged in" on DOM classes of a third-party site; fixture markup mirrored our own selectors, so tests proved nothing about the real DOM. Fixture now uses obfuscated classes deliberately.
- Direction churn: Chrome-extension redesign (full spec + 9 slices built, ~1.5h) was rejected by the user ("should be easily usable"); stashed as stash@{0}. Next direction (decrypt Chrome cookie store) was declined by the spec-writer partway; escalated to root rather than re-prompting around it. Final: Playwright channel="chrome" persistent sales0 profile + cookie/URL-based detection. Lesson: for UX-shaped feedback, confirm the approach with the user (via root) before fanning out a large build.
- Root served the user's server from the same working tree while generators edited it → user hit a 500 from half-edited templates. Mitigation: every-page render test; recommend a separate worktree for builds when a live server runs from the repo.
- A test reached the user's live 8765 server during the extension work → test harness now pins a dead port and refuses loopback 8765 from any thread.
- Kept from the rejected attempt: port guard, safety cool-down, pure classify_page, fixture logout-test.
- Final gate (orchestrator-run): unit 420, integration 439 (real Haiku + real headless Chrome 156 against fixture), e2e 6, check-browser OK, boot on 8767 OK.

## Phase 1.2 (after user gate)
- Real LinkedIn content search now ships SDUI markup: no URNs, no post links, obfuscated classes. Phase 1.1's "stable attribute" selectors (data-urn) were still a guess — only a real capture settled it. Lesson: for third-party DOM, get ONE real capture early (root did, safely) and build a sanitised sample before writing selectors.
- Privacy: the real capture holds people's public posts. Sanitised sample + leak check by S1; an independent auditor still found one real componentkey UUID reused as a "leak" example inside a test. Independent re-check was worth it.
- Pricing defaults corrected to $0.10/$0.50 per MTok (spec-writer had guessed $1/$5 as Assumed). effort="low" probed: accepted with forced tool use but saves ~1% — not used.
- Process change requested by user: split audits into 3–4 parallel scoped auditors; run unit/integration/e2e gates concurrently.
- Gate (orchestrator-run): unit 630, integration 498 (real Haiku), e2e 6, check-browser OK.

## Phase 1.3 — marker-free results variant (2026-10-28)
- **Symptom:** "layout may have changed" on every live run although Connect worked and posts were visible. Run 1: 0 cards; run 2: 6 cards found, 0 parsed.
- **Cause:** LinkedIn serves the SDUI results page in more than one markup variant per load. One has no `data-view-name` at all, so the Phase 1.2 reader (keyed on `feed-full-update`) found nothing. A single capture had looked representative and wasn't.
- **Fix:** third card tier (`listitem`): `role=listitem` holding an expandable text box or an "Open control menu for post by <Name>" button; name from that label, headline/time from text nodes before the post text. It is also used when the other tiers yield no post text. No permalink capture on this tier (profile link instead).
- **Lesson:** reproduce through the app's own run path (not a one-shot page load) before declaring a parser fixed, and key parsers on visible structure (roles, aria-labels, test ids), never on a single vendor attribute. Verified live: 9/9 posts, no drift.

## Phase 3 research agent
Assumed:
- `llm_calls.feature` CHECK must allow `research` (currently `("ping","triage")` in migration 0002 / `LLM_FEATURES`); research rows fail to insert until the models/migration slice widens it. Verified green with it widened.
- `run_research` takes offering NAMES (list[str]); `ResearchLimits` is a plain dataclass (`from_env()` reads `SALES0_RESEARCH_*`); the integrator may wire Settings later.
- The brief is not persisted here; callers store `result.brief_dict`. The caller commits (rows are flushed).
- Registrable-site matching is a heuristic (last two labels, three for common `co.xx` suffixes), no public-suffix list; redirects are vetted per hop against the same policy.
- DNS is resolved by the checker separately from the connect (DNS-rebinding window accepted for a human-triggered tool).
- Max 6 steps (not the spec's 4 tool calls), max 5 pages, 200 KB/page, 60 s total, 3 redirects; the last step only offers `finish`.
- Caps/limits and the SSRF test seam is `SafeFetcher(allow_private_hosts=[...])` (empty = strict); only tests use it.

## Phase 3 outreach (worktree sales0-outreach)
- Assumed: slice A adds the `cryptography` dependency (Fernet) to pyproject; the cookie signing uses stdlib hmac (no itsdangerous).
- Assumed: all outreach tables (team, gmail_connections, templates, decks, outreach_messages/events, company_briefs) ship in ONE migration `p3_outreach_a7c41e9d` (down_revision `0002_phase1_discovery`). CRM id columns have no FKs; the integrator adds them at merge.
- Assumed: `SALES0_SECRET_KEY` is any string (>= 16 chars recommended); the Fernet key and the cookie-signing key are derived from it with HMAC-SHA256 (purpose-separated). Unset in dev -> generated once into `<data_dir>/secret.key` (0600) with a loud warning log.
- Assumed: "Acting as" is a signed cookie (`sales0_acting`), not authentication: the app has no login, anyone reaching it can pick any member. Default = first active admin. Inactive members cannot be selected.
- Assumed: the OAuth redirect URI is the request's own loopback origin + `/oauth/google/callback` (Google accepts any loopback port for Desktop clients). Non-loopback hosts are refused unless `SALES0_PUBLIC_BASE_URL` is set. The Team page therefore must be opened on 127.0.0.1/localhost to connect Gmail.
- Assumed: pending OAuth states (PKCE verifier, member binding, 10-minute expiry, single use) live in process memory; a restart mid-consent means "start again". The callback GET is CSRF-safe via that state (CsrfOriginMiddleware only checks unsafe methods).
- Assumed: a token whose Google email differs from the member's email is never stored; the connection row is set to `disconnected` with `last_error` explaining the mismatch. Granting fewer scopes than draft+trash stores the token as `scope_insufficient`.
- Assumed: Google endpoints (auth/token/userinfo/revoke) are settings (`SALES0_GOOGLE_*_ENDPOINT`) and the OAuth HTTP client comes from `app.state.oauth_http_factory` (tests inject `httpx.MockTransport`).
- Assumed: the "Team" link lives in the header next to the acting selector, not in the primary NAV (keeps the NAV order test unchanged); slice C owns the Outreach nav entry.
- Assumed: the last active admin cannot be deactivated. Only admins add members / change active; a member can disconnect only their own Gmail (admins anyone's).
- Assumed: `google-auth --member EMAIL` runs the usual consent flow into a temp file, stores the token encrypted for that member (member must exist; the configured admin is seeded first) and deletes the temp file; without `--member` behaviour is unchanged.
- Assumed (slice C1): `llm_calls.feature` CHECK widened to `ping, triage, research, drafting, suggest` in `LLM_FEATURES` and, via a SQLite batch recreate of `ck_llm_calls_feature`, inside `p3_outreach_a7c41e9d` (downgrade deletes research/drafting/suggest rows, then narrows). Research/drafting `llm_calls` rows drop an `enquiry_id` that does not exist in `enquiries` (FK); `company_briefs`/`outreach_messages` keep the caller's id.
- Assumed: only the message's OWNER may edit, approve, reject, send or retry it (403 `not_owner`, also for admins); admins may view every message (`get_message`, `list_outbox(everyone=True)`); a non-admin cannot read others' messages.
- Assumed: message states `draft -> edited -> approved -> sending -> sent | failed`, plus `rejected`. Editing an approved or failed message returns it to `edited` and clears approval and the Gmail draft id; edits are forbidden in sending/sent/rejected and while a failed send's outcome is unknown. Approve is blocked by `{x}`/`{{x}}`/`[Your Name]`-style gaps, no/invalid recipients, empty subject/body.
- Assumed: the approval hash is `DraftContent.sha256` of the message exactly as `gmail.prepare` builds it, INCLUDING the deck PDF bytes (read from `<data_dir>/decks/<sha>.pdf`, verified against `decks.sha256`). `send` rebuilds it locally and refuses (failed, approval cleared) if it differs before even creating the Gmail draft; the adapter then re-checks the live Gmail draft.
- Assumed: `send` accepts `approved` and also `failed` with a KNOWN outcome (nothing was sent; the deliberate click is the retry). A failed send whose outcome is unknown (transient/network/unclassified error during `send_draft`) needs `retry_send(confirm_not_sent=True)`; `retry_decided_by` stores the confirming member's email. Definite 4xx (auth, scope, not found, API error, mismatch) are "not sent". Nothing retries automatically.
- Assumed: `send`'s claim is a compare-and-swap UPDATE committed before Gmail is called; services in `messages.py` commit their own transitions (caller passes a session it owns). A crash while `sending` leaves the message `sending` (no auto-recovery; the UI should show it and an admin check Gmail's Sent folder).
- Assumed: the Gmail account must equal BOTH the acting member's email and `message.from_addr` (case-insensitive) or the send is refused (`sender_mismatch`) with the message left approved. The member's connection must be `connected` in the DB (`gmail_not_connected` otherwise).
- Assumed: the daily cap (`SALES0_GMAIL_DAILY_SEND_CAP`, per member, local `SALES0_TIMEZONE` day, counts `sent` by `sent_at` + `sending` by `updated_at`) is checked before the claim and again after it (race guard, reverts the claim). `0` disables sending.
- Assumed: drafting makes one Haiku call, plus one regeneration when the code check finds a placeholder, > 200 words, a price/discount not present in the inputs, or (soft) no company name / no brief hook / no sender name; anything still wrong is kept and described in `error` ("Draft needs attention: ..."). The sender signature is appended if the model omitted the member's name. The template is a skeleton: known variables are pre-filled, the model adapts it.
- Assumed: decks are deduplicated globally by sha256 (re-upload returns the existing row, re-activates it, and re-maps its offering if one is passed); deletion is soft and the file stays so approved messages still send. Limit `SALES0_DECK_MAX_BYTES` (default 10 MB). Offering lookup: newest active deck/template for the offering, else newest unmapped one.
- Assumed: no real Gmail in tests (FakeGmailClient via `set_client_factory`); `tests/unit/outreach/test_no_autosend.py` statically guards that `send_draft` is only called from `services/outreach/messages.py::_dispatch` (outside the gmail package) and that jobs/agent/scraper never import the sender.

## Phase 3 outreach UI + e2e (slice C2)
- Assumed: Outreach is a real page set under `/outreach` (outbox, `/compose`, `/messages/{id}`, `/templates`, `/decks`); the NAV entry has no "Phase 3" badge and the stub is gone. Pages need an acting member (409 `no_team_member` otherwise).
- Assumed: message actions (edit/approve/reject/send/retry, template and deck CRUD) are plain form POSTs that redirect back with a flash; a `DomainError` becomes a red flash for browsers, but htmx and `Accept: application/json` callers get the envelope (toast / JSON with the service's code and status, e.g. 409 `not_approved`, 403 `not_owner`). Research and Generate draft are htmx (indicator + disabled buttons; errors toasted, form kept; draft answers `HX-Redirect`).
- Assumed: the Send button exists only for `approved` + acting owner + connection `connected` + cap not reached; "Retry send" (known failure) posts to the same send route; an unknown-outcome failure shows only the confirmation checkbox form. Approve is disabled while placeholders remain. The hash prefix on Approve comes from `messages._build_content` (private helper reused so the label equals what approval binds).
- Assumed: a failed message whose approval was cleared (hash mismatch) cannot be approved from `failed`; the page tells the owner to save any edit, which returns it to `edited`.
- Assumed: "Stuck sending" = `sending` for more than 5 minutes (warning only; no automatic recovery).
- Assumed: LLM client for web routes = `get_llm()` dependency in `web/routes/outreach.py`, which returns `sales0.llm.get_llm_client()` (the same process singleton triage uses); tests use `set_llm_client` or `app.dependency_overrides[get_llm]`.
- Assumed: `SALES0_RESEARCH_ALLOW_PRIVATE_HOSTS` (1/true = loopback, or a comma list) is exposed as `Settings.research_private_hosts`, which is `[]` unless `linkedin_mode == "fixture"`; the research route passes it to `SafeFetcher(allow_private_hosts=...)`.
- Assumed: the outreach e2e runs the app IN-PROCESS under uvicorn (free port >= 8766) rather than via `LiveServer`, so `connections.set_client_factory` can inject a `FakeGmailClient` (a subprocess cannot share it) without any production env override for the Gmail API base. OAuth is real against `tests/support/fake_google.py`; research and drafting use real Haiku against `tests/support/research_site.py`.
- Stubbed (labelled "Coming later" in the UI): linking a message to a CRM pipeline prospect, reply tracking. Not implemented: pagination of the outbox (latest 200), deck preview beyond opening the PDF.
