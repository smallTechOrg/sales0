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
