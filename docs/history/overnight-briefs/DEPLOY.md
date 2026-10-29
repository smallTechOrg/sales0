# DEPLOY brief: shared hosted app (Cloud Run + Cloud SQL) with a local scraper worker

Read COMMON.md first (HARD RULES apply). Start from the merged build branch HEAD (after integrator E).

## Binding decisions (from the user)
- Hosted app on **Cloud Run**, database = a new DB `sales0` on the EXISTING Cloud SQL Postgres 17 instance `ai-agent-db` (project `ai-agent-boilerplate0`, us-central1, private IP). Do not create a new instance. Look at how the existing services (e.g. glitch-api) connect (VPC / connector) and copy that pattern.
- **No scheduler at all.** No in-app timer thread on the hosted side, no Cloud Scheduler, no tick endpoint. Everything a scheduler did is a MANUAL action in the UI (sheet sync, reply poll, digest build, suggestions, purge). Cloud Run scales to zero (min instances 0).
- Because Cloud Run only gives CPU during a request: every manual action must do its work INSIDE the request (synchronously, with a visible spinner / htmx indicator), not in a background thread that outlives the response. Anything that may exceed ~5 min must be chunked (do N items per click, show "more remaining, click again"). Set the Cloud Run request timeout to 15 min.
- **Scraping stays on the user's laptop** (logged-in real Chrome). The laptop runs a *worker* process: `sales0 worker` polls the hosted app for queued runs, scrapes locally, uploads raw posts. The hosted app owns the DB, dedupe/idempotency (seen_posts, linkedin_profiles), Haiku triage, storage, and run status. "Start run" in the hosted dashboard just queues a run row. LinkedIn cookies / Chrome profile never leave the laptop.
- Worker API: authenticated with a bearer token (`SALES0_WORKER_TOKEN`, stored hashed or compared constant-time, secret in Secret Manager), under `/api/worker/*`: claim next queued run (with keywords, caps, today's usage counters), heartbeat, upload raw post batches (idempotent on fingerprint), report completion/abort reason (cool-down is recorded server-side so the team sees it). Worker must survive hosted-side downtime (retry with backoff, resume).
- Team login: existing per-user accounts + set-password invite links, ON in hosted mode (`SALES0_APP_PASSWORD`/accounts required; refuse to boot hosted mode in open mode). Gmail: per-member web OAuth, redirect URI = the Cloud Run URL (`SALES0_PUBLIC_BASE_URL`).
- Haiku only. Secrets via Secret Manager env refs, never in the image or repo.

## Work slices
1. **Postgres port**: SQLAlchemy engine config from `SALES0_DATABASE_URL` (sqlite stays default for local dev/tests; postgres for hosted). Fix SQLite-isms (PRAGMA, `BEGIN IMMEDIATE` in db/locks.py and the accept path -> `SELECT ... FOR UPDATE` / advisory locks, strftime/julianday/datetime() use in services + templates, partial indexes, batch-alter migrations, CHECK/ENUM, boolean/JSON/datetime types). Alembic must run on both. Run the unit+integration suites against a local Postgres (docker or a temp pg) in addition to SQLite if available; otherwise state clearly what was not verified. Remove the single-process data-dir flock in hosted mode.
2. **Hosted mode switch** (`SALES0_MODE=hosted|local`): disables the in-app scheduler thread and the local-browser job runners; keeps manual "Run now" buttons for hosted-capable jobs, made request-bound/chunked. Setup/Runs UI shows worker status (last heartbeat, version) and a clear "Worker offline: start `sales0 worker` on the laptop" hint when queued runs are not claimed.
3. **Worker API + `sales0 worker` client** (see above). Local mode keeps working exactly as today (single-machine, no worker) so nothing regresses. Tests with a fake LinkedIn fixture for the worker round trip.
4. **Container + deploy**: Dockerfile (python 3.12-slim, uv, no Playwright/Chrome in the image), `deploy/` with a `deploy.sh` that is idempotent and PRINTS the gcloud commands (dry-run by default; `--apply` required) for: db + user creation on ai-agent-db, secrets, Cloud Run deploy with Cloud SQL/VPC pattern, min-instances 0, max-instances 1, timeout 900. `docs/deploy.md` runbook: first deploy, admin bootstrap, inviting teammates, Gmail OAuth redirect URI, running the worker, rollback, backups (Cloud SQL backups), cost.
5. **Docs**: README "Hosted" section; NOTES; env docs updated.

## Hard limits
- DO NOT create or modify any cloud resource. No `gcloud ... create/deploy/set-iam/secrets create`. Read-only gcloud (list/describe) is fine. The user must approve the resource list first; the orchestrator runs `--apply`.
- DO NOT touch /Users/sai/Workspace/Code/sales0-run or port 8765 (user's live app).
- Run test suites one at a time, with `nice`, no concurrency (the user's laptop is struggling).
- Commit in small commits on a new branch `feature/sales0-deploy` from the merged build HEAD; push; no PR.

## Facts found (read-only gcloud), project ai-agent-boilerplate0, region us-central1
- Existing Cloud Run services reach Cloud SQL via Direct VPC egress, NOT the Cloud SQL connector: annotations `run.googleapis.com/network-interfaces: [{"network":"default","subnetwork":"default"}]` and `run.googleapis.com/vpc-access-egress: private-ranges-only` (gcloud: `--network=default --subnet=default --vpc-egress=private-ranges-only`). Connect straight to private IP 10.110.112.7:5432 (instance ai-agent-db, Postgres 17, db-f1-micro: keep the pool small, e.g. pool_size=2, max_overflow=2).
- Service account used by siblings: 870371939888-compute@developer.gserviceaccount.com (grant it secretAccessor on the new secrets only).
- APIs enabled: run, sqladmin, secretmanager, artifactregistry, cloudbuild. Artifact Registry repo `cloud-run-source-deploy` exists (use `gcloud run deploy --source` or build to it).
- Existing DBs on the instance: never touch them; create only `sales0` + role `sales0_app` (generate password into Secret Manager as `sales0-db-password`, name all secrets `sales0-*`).
- Orchestrator (not the builder) runs the `--apply`; the user approved creating exactly these: DB+role on ai-agent-db, `sales0-*` secrets, Cloud Run service `sales0` (min 0, max 1, timeout 900). No Cloud Scheduler.
