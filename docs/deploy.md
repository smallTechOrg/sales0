# Hosted deployment runbook (Cloud Run + Cloud SQL + laptop worker)

Audience: the person who deploys and runs sales0 for the team. Everything here is written for the existing Google
Cloud project `ai-agent-boilerplate0`, region `us-central1`, with the existing Cloud SQL instance `ai-agent-db`
(PostgreSQL 17, private IP `10.110.112.7`, `db-f1-micro`).

## 1. Architecture in one picture

```
 team browsers ──https──▶ Cloud Run service `sales0` (min 0 / max 1 instance, 15 min request timeout)
                              │  Direct VPC egress (private-ranges-only)
                              ▼
                   Cloud SQL `ai-agent-db` ── database `sales0`, role `sales0_app`  (shared instance, other DBs untouched)

 laptop ── `sales0 worker` ──https, bearer token──▶ /api/worker/*  (claim run → upload raw posts → complete)
   └─ logged-in Chrome profile, LinkedIn cookies: NEVER leave the laptop
```

* **No scheduler anywhere.** No in-app timer thread, no Cloud Scheduler, no tick endpoint. Cloud Run only gives the
  container CPU while a request is open, so everything that used to be scheduled is a **manual button** that does its
  work **inside the request** (the page waits, the button shows "Working..."):
  sheet sync (`/sync`), reply check (`/replies`), digest build (`/digest`), suggestions (`/suggestions`), purge and
  the others on `/schedule` ("Run now").
* A manual action that could outlive a request is chunked: the reply check stops starting new work after
  `SALES0_HOSTED_ACTION_BUDGET_S` (default 240 s) and shows "More conversations remain - click Check now again".
  Discovery is not a request at all: it is a queued row the worker drives with many short requests.
* The hosted app owns the database, de-duplication (`seen_posts`, `linkedin_profiles`), Haiku triage, counters, the
  daily caps and the safety cool-down. The worker owns only the browser.
* Local mode (`SALES0_MODE` unset) is unchanged: SQLite, in-process scheduler, in-process Chrome job.

## 2. What gets created (and nothing else)

`deploy/deploy.sh` (dry run by default) creates exactly:

| Resource | Name |
|---|---|
| Cloud SQL role (on the existing instance) | `sales0_app` |
| Database (created by the app itself at first boot, owned by `sales0_app`) | `sales0` |
| Secret Manager secrets | `sales0-db-password`, `sales0-secret-key`, `sales0-worker-token`, `sales0-app-password`, `sales0-anthropic-api-key`, `sales0-anthropic-workspace-id`, `sales0-google-client-secret` |
| IAM | `secretmanager.secretAccessor` for `870371939888-compute@developer.gserviceaccount.com` on those 7 secrets only |
| Cloud Run service | `sales0` (min 0, max 1, timeout 900 s, Direct VPC egress `default`/`default`, `private-ranges-only`) |
| Artifact Registry image | pushed by `gcloud run deploy --source` into the existing repo `cloud-run-source-deploy` |

It does not create a Cloud SQL instance, a Cloud Scheduler job, a VPC connector, or touch the other databases.

Why the database is created by the app: the instance is private-IP only, so nothing outside the VPC can run `CREATE
DATABASE`, and a database made with `gcloud sql databases create` is owned by `postgres` (then `sales0_app` could not
create tables in it). A role made with `gcloud sql users create` is a member of `cloudsqlsuperuser` (has `CREATEDB`),
so at first boot (`SALES0_AUTO_MIGRATE=true`) the app connects to the `postgres` database as `sales0_app`, runs
`CREATE DATABASE sales0` (making `sales0_app` the owner) and then `alembic upgrade head`, under a PostgreSQL advisory
lock so two overlapping instances cannot race.

## 3. First deploy

Prerequisites on the machine that runs the script: `gcloud` logged in with rights on the project, `openssl`, and the
two values that cannot be generated:

```bash
export SALES0_ANTHROPIC_API_KEY=...            # the claude-haiku-5-5 key (same as .env)
export SALES0_ANTHROPIC_WORKSPACE_ID=...       # same as .env
export GOOGLE_CLIENT_SECRET_FILE=path/to/google_client_secret.json   # a *Web application* OAuth client (section 6)
```

1. **Review**: `deploy/deploy.sh` (dry run: prints every command, executes nothing).
2. **Apply**: `deploy/deploy.sh --apply`. Steps can be run alone: `--step secrets`, `--step db-user`, `--step deploy`,
   `--step verify`. Every command is guarded ("create only if missing"), so re-running never rotates a secret or
   re-creates the role, and a failed run can simply be repeated.
3. The Cloud Run URL is deterministic: `https://sales0-870371939888.us-central1.run.app`. The script sets
   `SALES0_PUBLIC_BASE_URL` to it up front (Gmail redirect URI, secure cookies, allowed Host). If you use a custom
   domain, run with `PUBLIC_URL=https://your.domain deploy/deploy.sh --apply --step deploy`.
4. First boot takes a little longer (database creation + 19 migrations). `curl <url>/api/health` should answer
   `{"ok":true,"data":{"status":"ok"}}`.

If the first boot logs `database bootstrap failed`, see "Troubleshooting".

### Environment the service needs

Set by the image / `deploy.sh` (nothing to do by hand):

| Variable | Source | Purpose |
|---|---|---|
| `SALES0_MODE=hosted` | image | enables hosted behaviour and the boot-time refusals below |
| `SALES0_HOST=0.0.0.0`, `SALES0_PORT` (from `$PORT`) | image | bind address |
| `SALES0_DATA_DIR=/tmp/sales0` | image | scratch only (nothing persistent lives on disk) |
| `SALES0_AUTO_MIGRATE=true` | image | create database + `alembic upgrade head` at boot |
| `SALES0_DB_HOST=10.110.112.7`, `SALES0_DB_NAME=sales0`, `SALES0_DB_USER=sales0_app`, `SALES0_DB_SSLMODE=require` | env | PostgreSQL connection (pool 2 + 2 overflow, recycled every 5 min) |
| `SALES0_PUBLIC_BASE_URL` | env | https URL of the service |
| `SALES0_GOOGLE_CLIENT_SECRET_PATH=/secrets/google/google_client_secret.json` | env | where the secret is mounted |
| `SALES0_DB_PASSWORD` | secret `sales0-db-password` | role password |
| `SALES0_SECRET_KEY` | secret `sales0-secret-key` | encrypts Gmail tokens, signs cookies. **Back it up**; losing it makes stored Gmail tokens unreadable |
| `SALES0_WORKER_TOKEN` | secret `sales0-worker-token` | bearer token of the laptop worker |
| `SALES0_APP_PASSWORD` | secret `sales0-app-password` | the first admin's initial password (bootstrap only) |
| `SALES0_ANTHROPIC_API_KEY`, `SALES0_ANTHROPIC_WORKSPACE_ID` | secrets | Haiku (`claude-haiku-5-5` only) |
| Google OAuth client JSON | secret `sales0-google-client-secret` (file mount) | per-member Gmail OAuth |

Optional: `SALES0_ADMIN_EMAIL` / `SALES0_ADMIN_NAME` (first admin; default `admin@smalltech.in`),
`SALES0_TRUSTED_PROXIES` (see Risks), `SALES0_MONTHLY_SPEND_CAP_USD`, `SALES0_HOSTED_ACTION_BUDGET_S`,
`SALES0_WORKER_STALE_S`, `SALES0_DB_POOL_SIZE` / `SALES0_DB_MAX_OVERFLOW`. The full table is in the README.

The app **refuses to boot** in hosted mode (clear message, exit code 2) unless: the database is PostgreSQL, a secret key
is set, `SALES0_PUBLIC_BASE_URL` is https, a worker token is set, `SALES0_LINKEDIN_MODE` is `live`, and a login exists
(`SALES0_APP_PASSWORD` or an account that already has a password). Open mode is impossible.

## 4. Admin bootstrap and inviting teammates

1. Read the first admin password once: `gcloud secrets versions access latest --secret sales0-app-password --project ai-agent-boilerplate0`.
2. Open the service URL, log in as `SALES0_ADMIN_EMAIL` (default `admin@smalltech.in`) with that password, and change it
   on the account page. (`SALES0_APP_PASSWORD` is only the *initial* password of an admin that has none; changing it
   later has no effect.)
3. **Users** page: add each teammate, press **Set-password link**, send them the link (valid 24 h). They choose their
   own password. Roles: admin / member.
4. Add keywords and offerings on **Setup** (or use "Add suggested keywords").

## 5. Running the worker (laptop)

The worker needs the sales0 checkout, Google Chrome and a one-time LinkedIn login. In the laptop's `.env` (never the
hosted one):

```
SALES0_WORKER_SERVER_URL=https://sales0-870371939888.us-central1.run.app
SALES0_WORKER_TOKEN=<gcloud secrets versions access latest --secret sales0-worker-token --project ai-agent-boilerplate0>
# optional: SALES0_WORKER_NAME=sai-macbook  SALES0_DATA_DIR=<where the LinkedIn Chrome profile should live>
```

```bash
uv run sales0 worker --connect   # once: a Chrome window opens on LinkedIn; log in and finish 2FA
uv run sales0 worker             # leave running; Ctrl-C to stop
```

* Then, in the hosted app, **Runs -> Queue a run**. The worker claims it within `SALES0_WORKER_POLL_S` (15 s), searches
  keyword by keyword with the usual human pacing, uploads each keyword's posts in batches, copies post links where the
  server asks, and completes the run. The hosted app triages every batch with Haiku while the worker waits, so
  counters and the review inbox fill in as the run proceeds. **Stop run** on the Runs page ends it at the worker's
  next heartbeat; **Cancel queued run** drops one nobody picked up.
* The Setup page shows the worker (name, version, last heartbeat, queued runs). When a run is queued and no worker has
  polled for 90 s the header and the Runs page say **"Worker offline: start `sales0 worker` on the laptop"**.
* **Survives outages.** Calls retry with backoff (1, 2, 4 ... 60 s) for up to 30 min; uploads carry a per-keyword
  sequence number, so a retry never double-counts. If the worker is killed mid-run, starting it again **resumes** the
  same run (it re-searches the keyword that was in progress; duplicates are recognised by fingerprint). A run whose
  worker stays silent for `SALES0_WORKER_STALE_S` (180 s) is ended as "worker lost" the next time anyone looks.
* **Safety stops are shared.** A rate-limit/captcha/checkpoint/logout is reported by the worker; the hosted app records
  it (the whole team sees the cool-down, "Next run allowed after HH:MM") and refuses to hand out another run until it
  ends. Daily run/post/page-load caps are enforced on the server both when a run is queued and again when it is
  claimed. To resolve a checkpoint/captcha: `uv run sales0 worker --connect` on the laptop, then queue again.
* One worker serves the one default LinkedIn account. Do not point two laptops at the same hosted app and the same
  LinkedIn login.

Hosted mode has no Connect LinkedIn / Re-check buttons, no cookie paste and no prospect-watch runs (they need a
browser on the server; the server has none by design): those actions answer "not available on the hosted app".

### 5a. One-off: move existing local data to the hosted database (`push-data`)

The hosted database is private-IP only, so the laptop pushes its SQLite file THROUGH the hosted app
(`POST /api/worker/import/*`, same bearer token, hosted mode only). Use the same two env vars as the worker.

```bash
uv run sales0 push-data --source ~/path/to/sales0-run/data/sales0.db --dry-run   # counts only, sends nothing
uv run sales0 push-data --source ~/path/to/sales0-run/data/sales0.db             # the real push
```

* The source is **never modified**: it (and its `-wal`/`-shm`) is copied to a temp dir and, if behind, only the copy is
  migrated to the current schema.
* Rows keep their ids and go parent-first (keywords, profiles, runs, seen posts, enquiries, hits, sources, LLM calls,
  status history, offerings, competitors, digests, CRM tables). It is an **upsert by primary key, so re-running is
  safe** (nothing is duplicated); the last step moves PostgreSQL id sequences past `MAX(id)` and prints
  `local -> hosted` row counts per table.
* **Not pushed:** team members and their sessions/password tokens/Gmail tokens (the hosted app has its own accounts;
  actor references such as "who added this keyword" are re-mapped by e-mail, or left empty), the LinkedIn account row
  and legacy session (cool-down/counter state of that machine), scheduled jobs, workers, app_meta, sheet source/sync
  state. Seeded reference rows (offerings, competitors) are overwritten by your local versions.
* **Keep the raw text:** posts keep `raw_text` only until `raw_purge_after` (`SALES0_SEEN_RAW_RETENTION_DAYS`, default
  14), then the purge job nulls it. To keep it effectively forever set `SALES0_SEEN_RAW_RETENTION_DAYS=36500` on the
  hosted service (`gcloud run services update sales0 --update-env-vars SALES0_SEEN_RAW_RETENTION_DAYS=36500 ...`)
  **before** pushing, and in the laptop `.env`. Pushed posts whose purge date is earlier than `first_seen +` that
  retention are extended to it, so nothing is purged right after the import.

## 6. Gmail OAuth (per member)

Gmail needs a **Web application** OAuth client (the Desktop type only allows loopback redirects). In the Google Cloud
console for the project that owns the OAuth consent screen:

1. Credentials -> Create credentials -> OAuth client ID -> **Web application**.
2. Authorised redirect URI, exactly: `https://sales0-870371939888.us-central1.run.app/oauth/google/callback`
   (it must equal `SALES0_PUBLIC_BASE_URL` + `/oauth/google/callback`; no trailing slash).
3. Download the JSON to `data/google_client_secret.json` (gitignored) and make it the secret:
   `deploy/deploy.sh --apply --step secrets` (creates `sales0-google-client-secret` if missing). To replace it later:
   `gcloud secrets versions add sales0-google-client-secret --data-file=... --project ai-agent-boilerplate0`, then
   redeploy (`--step deploy`) so the instance restarts.
4. Each member connects their own Gmail on the **Team** page. While the consent screen is "External / Testing", add
   them as test users; refresh tokens then expire after 7 days (see `docs/gmail-setup.md`).

The OAuth "state" is kept in the instance's memory; with max-instances 1 and a 15-minute idle window this is fine for a
consent that takes a minute. If the instance scaled to zero in between, the callback says the sign-in expired: press
Connect again.

## 7. Rollback

* **Code**: `deploy/deploy.sh rollback` lists the last revisions; `deploy/deploy.sh rollback sales0-00012-abc --apply`
  sends 100 % of traffic to that revision.
* **Schema**: migrations only add (this release adds the `workers` table and columns, nothing is dropped). Rolling the
  code back to the revision before this feature leaves the new tables/columns in place, which older code ignores. If a
  *later* release has a destructive migration, restore from a backup instead (below) - do not `alembic downgrade`
  against production without a backup.
* **A bad run**: it only ever adds enquiries to the review inbox; discard them there. Secrets are not rotated by
  re-running the script; to rotate one: `gcloud secrets versions add <name> --data-file=-`, then redeploy.

## 8. Backups

The database lives on the shared Cloud SQL instance, which has automated daily backups (start time 14:00 UTC,
7 retained, point-in-time recovery **off**). Check/trigger from a machine with access:

```bash
gcloud sql backups list --instance ai-agent-db --project ai-agent-boilerplate0
gcloud sql backups create --instance ai-agent-db --project ai-agent-boilerplate0   # before risky changes
```

A backup covers every database on the instance. To restore only `sales0` without touching the neighbours, export it:
`gcloud sql export sql ai-agent-db gs://<bucket>/sales0.sql --database sales0` (needs a bucket the instance's service
account can write) and import into a scratch database. Also keep `SALES0_SECRET_KEY` somewhere safe: the data is
useless for Gmail without it.

## 9. Cost

Rough, us-central1, a handful of users; check the billing console for real numbers.

* **Cloud Run**: min 0 instances, 1 vCPU / 1 GiB, billed per request-second. A few hours of use a day is cents per month;
  an open Runs tab polls every 3 s (keeps one instance warm only while the tab is open).
* **Cloud SQL**: nothing new - a database and a role on the instance that already runs (the cost is the existing
  `db-f1-micro`). Its connection limit (~25) is shared with the other services: sales0 keeps at most 4.
* **Secret Manager**: 7 secrets - well inside the free tier.
* **Cloud Build / Artifact Registry**: a few cents per deploy (image pushed to the existing repo).
* **Anthropic Haiku**: unchanged from local use; `SALES0_MONTHLY_SPEND_CAP_USD` still applies, and the **Costs** page shows it.

## 10. Operating notes and troubleshooting

| Symptom | Cause / fix |
|---|---|
| Boot exits with `Refusing to boot SALES0_MODE=hosted: ...` | The message lists what is missing; fix the env/secret and redeploy |
| `database bootstrap failed (OperationalError ...)` | The role cannot connect (password secret changed after the role was created: `gcloud sql users set-password sales0_app --instance ai-agent-db --password "$(gcloud secrets versions access latest --secret sales0-db-password)"`) or the VPC egress flags are missing |
| `permission denied to create database` | `sales0_app` lacks `CREATEDB` (not created via `gcloud sql users create`). Fix: have a `postgres` user run `CREATE DATABASE sales0 OWNER sales0_app;` from inside the VPC |
| Header says "Worker offline" | Start `uv run sales0 worker` on the laptop; check `SALES0_WORKER_SERVER_URL` / token (a wrong token prints "the hosted app refused the token") |
| A run shows "worker lost" | The laptop slept or lost network for > 3 min. Queue again; keywords not reached are planned first |
| "Time budget used - click again" on Replies | Normal for a big mailbox: press Check now again |
| Login throttle locks everyone | See Risks (`SALES0_TRUSTED_PROXIES`) |
| Slow first page after idle | Cloud Run cold start (min instances 0 by decision) |

Logs: `gcloud run services logs read sales0 --region us-central1 --project ai-agent-boilerplate0` (JSON lines; counts
only, no personal data).

## 11. Risks and known limits

* **Cold start + in-memory state.** Gmail OAuth state and the login throttle live in the instance's memory. Max
  instances is 1 so they are consistent; a scale-to-zero in the middle of an OAuth consent forces a retry.
* **Client IP / login throttle.** Behind Cloud Run every request arrives from the front end, so unless
  `SALES0_TRUSTED_PROXIES` lists that peer, all users share one throttle bucket (5 failures lock logins for 60 s for
  everyone). After the first deploy read the peer address from a request log and set `SALES0_TRUSTED_PROXIES` to
  exactly that range; do not set `0.0.0.0/0` (it would let clients spoof their IP).
* **Public invoker.** The service is deployed with `--allow-unauthenticated` (the app has its own login and the worker has
  its own bearer token; Cloud Run IAM cannot sit in front of both a browser and a script without extra tooling). If an
  organisation policy forbids `allUsers` as invoker the deploy step fails with a clear error; the alternatives (IAP, or an
  ID-token header in the worker) are not built.
* **Worker token brute force.** `/api/worker/*` has no rate limit; the token is 36 random bytes, which is not guessable.
  Rotate it by adding a secret version and redeploying; update the laptop's `.env`.
* **Shared db-f1-micro.** Every service on the instance shares its small connection and memory budget. sales0 uses a
  pool of 2 (+2 overflow) per process and at most one process.
* **One worker, one LinkedIn account.** LinkedIn's terms and detection risk are unchanged (README, "LinkedIn on a
  server"); running from a home/office connection is the safer arrangement, which is why scraping stays on the laptop.
* **Prospect watch is local-only** (no worker protocol for it yet).
* **Unverified without cloud access**: the Dockerfile has not been built in an environment with a Docker daemon, and
  the Cloud SQL `CREATEDB` bootstrap, Direct VPC reachability and `sslmode=require` could only be checked against a
  local PostgreSQL 17, not the real instance. The first `--apply` is therefore also the first real test; the steps are
  idempotent so a failed attempt can be repeated.
