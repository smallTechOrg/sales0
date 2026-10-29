# sales0 handover (2026-10-30)

Read this first, then `README.md`, `spec/capabilities/domain-model.md` (the vocabulary), `NOTES.md` (decisions, `Assumed:` lines) and `tests/README.md`.

## What it is
AI sales agent for smallTech. LinkedIn post scraping (laptop worker) -> Haiku triage -> Inbox -> CRM -> Gmail outreach with human approval before every send. Haiku only (`claude-haiku-5-5`).

## The model (agreed with the owner, do not re-litigate)
- **Company** is the main record. **Prospect / Lead / Client are stages of one company**, not separate records. Prospect sub-status: new, contacted, follow_up_1, follow_up_2, final_follow_up. Lead: meeting_scheduled, proposal_sent. Client: active. Off-ramps: not_interested, dormant, do_not_contact (blocks all outreach; admin reopens), lost_client.
- **Enquiry** = one specific requirement (opportunity). Keep the word "Enquiry". Inbox review: pending_review -> accepted / discarded. After accept it has its OWN deal status (open, contacted, follow_up_1/2, final_follow_up, meeting_scheduled, proposal_sent, won, lost, no_response, on_hold), the same list for every enquiry.
- **Only link between them:** setting an enquiry to contacted / follow-up N moves its company's prospect sub-status forward (only while the company is a prospect, never backwards). meeting_scheduled / proposal_sent only show "Suggested: Lead" (a person decides). Won makes the company a client. The company never rewrites enquiry status.
- **Person** (contacts table) is its own record, belongs to many companies or none, has a role per company.
- Outreach and follow-ups belong to the company, sent to a chosen person. Characteristics (Training provider, Partner, Outreach client), priority and category are tags on the company/enquiry.
- Storage: `companies` has stage/sub_status/off_ramp (derived from the `prospects`/`leads`/`clients` ledger rows by `db/company_projection.py`; those tables are kept as the outreach/deal ledger, only the service layer writes them).
- Pipeline page tabs: Enquiries (default) | Prospects (New..Final follow-up, "Became lead" lane) | Leads | Clients.

## Where things run
- **Hosted app:** Cloud Run service `sales0` (project `ai-agent-boilerplate0`, us-central1), https://sales0-870371939888.us-central1.run.app, latest revision sales0-00008. DB `sales0` (role `sales0_app`) on Cloud SQL `ai-agent-db` (private IP). Secrets `sales0-*` in Secret Manager. Decks in GCS bucket `sales0-files` (`SALES0_ATTACHMENTS_BUCKET`). No scheduler: every job is a manual in-request button.
- **Deploy (manual, not GitHub Actions):** `bash deploy/deploy.sh --apply --step deploy` (dry run without `--apply`). `--set-env-vars` REPLACES all env vars, so new env vars must be added in `deploy/deploy.sh`. Migrations run on boot. Take a Cloud SQL backup first for risky migrations: `gcloud sql backups create --instance=ai-agent-db --project=ai-agent-boilerplate0`.
- **Alembic head:** `q17_company_model_c5e9a2d7` (single linear chain).
- **Scraping:** laptop only, `uv run sales0 worker` (needs `SALES0_WORKER_SERVER_URL` + `SALES0_WORKER_TOKEN`, logged-in Chrome profile). Never start it without asking the owner (LinkedIn account safety). `SALES0_SCRAPE_PACE_SCALE` is a test-only knob (rejected below 1.0 outside fixture mode).
- **Local UI work on real data:** `uv run sales0 pull-data --force` (read-only copy of the hosted DB into `data/pulled/sales0.db`, git-ignored, contains personal data), then `bash data/pulled/run.sh` -> http://127.0.0.1:8780, login `admin@local.test` (password printed once by pull-data). Scheduler, email and LinkedIn are off in that run.

## Tests and CI
- `scripts/test.sh [unit|integration|e2e|fast]` (xdist, per-worker template DB, ports from `tests/support/ports.py`). Approx: unit 10 s, integration 3-4 min on a quiet machine, e2e ~3 min. Run suites one at a time on the laptop.
- Real-Haiku tests replay from `tests/fixtures/llm_cassettes`; after changing a prompt re-record with `scripts/record_llm_cassettes.sh` (needs the real key from `.env`, never print it). CI runs with a fake key + replay.
- GitHub Actions (`.github/workflows/ci.yml`) runs lint (ruff E4/E7/E9/F), unit, 4 integration shards, e2e collection smoke on every push. It does NOT deploy.

## Owner's pending list / ideas (not started)
1. **Research for company and person together**, plus a way to **queue research for ALL current companies / prospects / enquiries / people at once** (owner said: do it later, not implemented). Existing research is per prospect (`services/research/`), button on the prospect/company page; each run is ~2 cents (web search is most of it), so a bulk queue needs a cost estimate + cap and chunking (hosted has no scheduler; Cloud Run requests are 900 s max).
2. Improve scraping to get more enquiries (the owner's main next focus after review).
3. Owner to do: re-upload decks (old files were lost with /tmp), change the admin password, test "Connect my Gmail" (Team page), invite teammates (Users page).
4. Optional: deploy via GitHub Actions (needs a Google service account; ask the owner before creating cloud resources).

## Not verified against the real thing
Real upload to the GCS bucket, the LinkedIn company-page reader (worker task), Gmail threading of follow-up drafts, quality of AI category / follow-up draft prompts, migration q17 on the full hosted dataset beyond the live check that all pages load (a backup `1793299032366` exists from just before q17).

## Rules the owner set
- Ask before creating cloud resources; reuse project `ai-agent-boilerplate0`. Human approves before every email. Keep LinkedIn account safe. Keep costs very low. No WhatsApp automation, no Chrome extension.
- Google Sheets are read-only sources (only the client-sourcing sheet syncs; its id is in `deploy/deploy.sh`). The hosted DB is the single source of truth; SQLite is for local dev/tests.
- Never read `.env` with a file tool; never `pkill`/`killall` (kill by PID); never delete `/Users/sai/Workspace/Code/sales0-run` (old local copy: LinkedIn Chrome profile, Google token, stale DB) without asking.
- Never `git stash pop` blindly: an old unrelated stash ("phase-1.1 chrome-extension attempt (rejected by user)") exists in this repo.
