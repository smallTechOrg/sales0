# Capability: Hosted Deployment (Cloud Run + PostgreSQL + laptop worker)
**Deploy phase** · code: `config.py` (`SALES0_MODE`), `db/{engine,bootstrap,pgseq,locks}.py`, `jobs/discovery_job.py` (`ingest_batch`/`finish_keyword`/`finalise_run`), `services/worker_service.py`, `web/routes/worker_api.py`, `worker/{protocol,client,runner}.py`, `Dockerfile`, `deploy/deploy.sh` · migration `p10_worker_e6c2b9d4` · runbook `docs/deploy.md`

## What It Does
The same app runs as a shared team service on Cloud Run with its data in PostgreSQL (a database on an existing Cloud SQL instance) while LinkedIn is still searched from a laptop with the user's own logged-in Chrome. Local mode (`SALES0_MODE` unset) is unchanged.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| `SALES0_MODE=hosted`, `SALES0_WORKER_TOKEN`, `SALES0_SECRET_KEY`, https `SALES0_PUBLIC_BASE_URL`, a login | env | Cloud Run env + Secret Manager | Yes (boot refuses otherwise) |
| `SALES0_DB_HOST/PORT/NAME/USER/PASSWORD/SSLMODE` or `SALES0_DATABASE_URL` | env | Cloud Run | Yes (PostgreSQL) |
| `SALES0_AUTO_MIGRATE=true` | env | image | create the database if missing + `alembic upgrade head` at boot under an advisory lock |
| `SALES0_WORKER_SERVER_URL`, `SALES0_WORKER_TOKEN`, `SALES0_WORKER_*` | env | the laptop's `.env` | for `sales0 worker` |

## Outputs
Queued/claimed/finished `scrape_runs` rows, `workers` heartbeat rows, enquiries/seen posts exactly as in local mode, and the Setup/Runs/header "Worker online / Worker offline" state.

## Business Rules
- **No scheduler.** In hosted mode the scheduler thread, the reply poller thread and the sheet-sync interval are never started (`scheduler_enabled`, `reply_poll_minutes`, `sheet_sync_minutes` are forced off). Every job is a manual "Run now" that does its work inside the request (the sheet sync runs on the request thread; the reply check stops at `SALES0_HOSTED_ACTION_BUDGET_S` and says "click again"). Anything that could exceed ~5 minutes is chunked the same way.
- **Discovery = a queued row.** `POST /runs` only inserts a `queued` run (caps and cool-down checked; LinkedIn connection state is NOT, the worker owns the browser). `sales0 worker` claims it (`POST /api/worker/claim`): the server re-checks caps/cool-down, plans the keywords, marks it `running`. Per keyword the worker scans locally and uploads raw posts in batches (`.../keywords/{row}/posts`, per-keyword `seq`, idempotent), the server dedupes (`seen_posts`), triages with Haiku inside that request and answers with the permalinks it wants and whether to continue (fatal AI error / daily post cap / daily page-load cap). The worker captures permalinks (`.../links`), sends a final empty batch with the scan facts, and finally `complete`s with `finished|safety_stop|session_missing|open_failed|cap_hit|cancelled|crashed`; the server ends the run exactly as the local job does (status, `abort_reason`, cool-down, account state, skipped keywords).
- **Auth.** `/api/worker/*` is outside the login and takes `Authorization: Bearer $SALES0_WORKER_TOKEN` (constant-time compare). Without a configured token the routes are 404.
- **Resilience.** The client retries connection errors/timeouts/408/429/5xx with backoff 1..60 s for up to 30 minutes, then abandons the run quietly. A re-claim by the same worker name resumes its running run (the in-progress keyword is re-searched). A claimed run silent for `SALES0_WORKER_STALE_S` is ended "worker lost" (no cool-down) when the next claim or Runs page load sees it. Admins can Stop a running run (honoured at the next heartbeat) or cancel a queued one.
- **Not available hosted** (409 `hosted_mode`): Connect LinkedIn, Re-check, cookie paste, prospect-watch runs. LinkedIn cookies and the Chrome profile never leave the laptop.
- **Boot refusals** (`check_startup`): SQLite, missing secret key, non-https public URL, no worker token, open mode, fixture LinkedIn mode.
- **PostgreSQL.** Portable migrations, small pool (2 + 2), `SELECT ... FOR UPDATE` on the accept path, sequences moved past seeded ids (`db/pgseq.py`).

## Edge Cases
Two instances booting at once (advisory lock); a replayed upload batch; an out-of-order batch (409 `out_of_order`); an admin cancel while the worker scans; the hosted app redeployed mid-run; cool-down started by a run that was queued before it; oversized/odd post fields (clipped by the worker, rejected by the schema if forged).

## Acceptance
`tests/integration/worker/` (round trip, safety stop, cool-down at claim, idempotency, auth, resume, stale, cancel, outage retry, hosted UI), `tests/integration/db/test_pg_bootstrap.py`, `tests/unit/{test_hosted_config.py,worker/}`; the whole unit+integration suites also run on PostgreSQL with `SALES0_TEST_POSTGRES_URL`.
