# Common brief header for every overnight phase builder
You are project-builder for /zero-shot-build, continuing an existing build.
Repo: /Users/sai/Workspace/Code/sales0 (branch feature/sales0-20261027-1816-v0.1, PR #1, base main).
Harness support: /Users/sai/.claude/plugins/cache/zero-shot-harness/zero-shot-harness/0.2.0/support
Read first: spec/ (roadmap.md, architecture.md, agent.md, data.md, capabilities/*) and NOTES.md.
The user is ASLEEP for hours: never block on a question; decide, record `Assumed:` in NOTES.md, keep going.
Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never `git add -A`; never stage .env or data/; never merge the PR.
Do NOT bind port 8765 and NEVER touch /Users/sai/Workspace/Code/sales0-run (user's live copy: saved LinkedIn login + DB). Test servers on 8766+ with temp DBs/data dirs.
No automated test may open real linkedin.com. Real Haiku (claude-haiku-5-5 only, needs SALES0_ANTHROPIC_WORKSPACE_ID header; already in .env) is fine in tests.
SPEED: fan out code-generators per independent slice in parallel; run 3-4 scoped qa-auditors in parallel; run unit/integration/e2e gates concurrently.
Migrations: take the next free Alembic revision number (check migrations/versions); keep one linear chain; backfill existing data.
Sensitive data: real sheet rows contain personal data -> fixtures in the repo must be synthetic (same shape/quirks), never raw copies. Logs print counts, not personal data.
Per phase: spec update first (spec-writer), build, gate, commit `phase-N: ...`, push, update PR body, append NOTES.md (harness friction log). Return a SHORT handoff: sha, gate output tails, what is real vs stub vs PENDING (credentials/devices), risks. Do not leave servers running.

## HARD RULES learned overnight
- NEVER use `pkill -f`, `killall`, or `kill $(lsof -ti :PORT)` on anything but a PID you started yourself in this session (record the PID when you launch). A builder's `pkill -f 'sales0$'` killed the user's live app on :8765. Track your own PIDs; do not match by command name.
- Scratch files go in your scratchpad dir, never /tmp.
- Tests pinning the Alembic head must read it via head_revision().
