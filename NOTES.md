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
