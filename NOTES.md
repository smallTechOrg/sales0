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

## Phase 1.4 — thorough, idempotent discovery (2026-10-28)
- Built as 7 parallel slices (A schema, B services, C scraper+fixture, D triage, E job, F web, G e2e/pipeline); all landed in one tree with contract-first polling, no collisions.
- Assumed (spec): interrupted runs -> `aborted` with null reason; backfill moves only never-reviewed AI discards; extra author-independent content_hash match kept; Rescue keeps AI verdict "discard"; permalinks only for genuine posts; keyword seeding once at startup.
- Live mini-run (3 buyer-intent keywords, real session, temp DB): 59 scanned, 56 new, 0 relevant (all Haiku-discarded: job posts/recruiters/self-promo), run 2 immediately: 0 new, 0 new LLM calls. Real results per date-filter pass were often only 9-10 cards (LinkedIn's own result depth; stop=no_growth), one keyword reached the 40 cap. So >=200/run depends on 40 keywords, not per-keyword depth.
- Live permalink capture is NOT working yet: on the listitem tier, the "…" menu trigger is found and clicked after a deep scan but the menu never opens (0 menuitems), whereas a click right after a short scan opens it and shows menuitem "Copy link to post". Added scroll-to-centre + hover before the click; still failed live. Budget of ~6 live menu clicks used. Next: reproduce with page state right after a full scan (card_index/virtualisation/sticky header) — fallback profile link + time works meanwhile.
- Incident: my cleanup `pkill -f 'sales0$'` also killed the user's idle server on 8765 (command ends in "sales0"). Never pkill by name pattern; kill by PID.

## Phase 1.5 - calibration (recall-first triage, permalinks, search variants)
- Root cause of 0/94 relevant: triage rule 1 discarded agency/edtech "trainer requirement" posts, which the team's real sheet shows it pursues. Prompt rewritten (triage-v3) + `opportunity_type` (new column `ai_opportunity_type`, migration 0004).
- Assumed: the Discarded tab has only one row with `Full Enquiry` text, so discard precision is measured on that row plus the one-line Requirement Summaries of the discard-labelled rows (indicative only, n small). "No Response", "Rejected" and "not replying" notes are labelled genuine-but-lost; genuine-but-lost rows without full text are skipped.
- Assumed: the team treats concrete freelance-trainer requirements for a corporate programme as genuine (priority low), since the Active sheet is full of them; only single cheap hourly tutors, payroll jobs, self-promo, events, thought leadership, network-building without a concrete requirement and out-of-scope topics are discards. The Edureka "AI in Product Management" row (Discarded tab) is indistinguishable from Active rows, so it stays a known miss.
- Permalink finding (live, read-only session, 5 menu clicks): the Phase 1.4 centre+hover fix already opens the menu; the real bug was that LinkedIn's "Copy link to post" now copies a `lnkd.in/p/<code>` short link, which `canonical_post_url` rejected as off-site, so every "hit" was dropped (found=0). Fix: resolve the short link with one anonymous HEAD. Live: 2/2 captured as `/feed/update/urn:li:activity:N/`. Earlier note "menu never opens" was a mis-diagnosis of this.
- Limitation: variant passes load different result pages; only the LAST pass page stays loaded for `capture_links`, so genuine posts from earlier passes fall back to the profile link (their card is no longer in the DOM). Possible follow-up: capture_links per pass.
- Alembic head moved to `0004_opportunity_type`; tests now read the head via `head_revision()`.
