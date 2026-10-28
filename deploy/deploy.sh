#!/usr/bin/env bash
# sales0 hosted deploy: Cloud Run service `sales0` + database role/secrets on the EXISTING Cloud SQL instance.
#
#   deploy/deploy.sh                 DRY RUN (default): prints every command, executes nothing, touches no cloud resource
#   deploy/deploy.sh --apply         runs them (the orchestrator does this after the resource list is approved)
#   deploy/deploy.sh --step secrets  only one step: secrets | db-user | deploy | verify  (repeatable)
#   deploy/deploy.sh rollback [REVISION]   print (or with --apply run) the traffic switch back to a revision
#
# Idempotent: every command is guarded ("create only if missing"), so re-running never rotates a secret or
# re-creates a role. Secret VALUES are generated on the fly and piped into Secret Manager; they are never printed.
# Creates ONLY: role sales0_app on ai-agent-db, secrets sales0-*, Cloud Run service sales0. No Cloud Scheduler, no new
# Cloud SQL instance. The database `sales0` itself is created by the app at first boot (see docs/deploy.md).
set -euo pipefail

PROJECT="${PROJECT:-ai-agent-boilerplate0}"
PROJECT_NUMBER="${PROJECT_NUMBER:-870371939888}"
REGION="${REGION:-us-central1}"
INSTANCE="${INSTANCE:-ai-agent-db}"
DB_HOST="${DB_HOST:-10.110.112.7}"                       # ai-agent-db private IP (Direct VPC egress, like the sibling services)
SA="${SA:-870371939888-compute@developer.gserviceaccount.com}"
SERVICE="${SERVICE:-sales0}"
DB_NAME="${DB_NAME:-sales0}"
DB_USER="${DB_USER:-sales0_app}"
PUBLIC_URL="${PUBLIC_URL:-https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app}"   # deterministic Cloud Run URL form
GOOGLE_CLIENT_SECRET_FILE="${GOOGLE_CLIENT_SECRET_FILE:-data/google_client_secret.json}"

APPLY=0
STEPS=()
ACTION="deploy"
ROLLBACK_REV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --step) shift; STEPS+=("$1") ;;
    rollback) ACTION="rollback"; if [ $# -gt 1 ] && [[ "$2" != --* ]]; then shift; ROLLBACK_REV="$1"; fi ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
[ ${#STEPS[@]} -eq 0 ] && STEPS=(secrets db-user deploy verify)

if [ "$APPLY" -eq 1 ]; then echo "# MODE: APPLY (executing)"; else echo "# MODE: DRY RUN (nothing is executed; pass --apply to run)"; fi
echo "# project=$PROJECT region=$REGION instance=$INSTANCE service=$SERVICE db=$DB_NAME role=$DB_USER"

run() {  # print the command exactly as it will run; execute it only with --apply
  echo "\$ $*"
  if [ "$APPLY" -eq 1 ]; then eval "$*"; fi
}
note() { echo "# $*"; }

secret_from_cmd() {  # secret_from_cmd NAME "value-generating command"  -> create-if-missing, never rotate
  local name="$1" gen="$2"
  # An empty/failed generator must never create an empty secret, hence VALUE=... && [ -n ] && create.
  run "gcloud secrets describe $name --project $PROJECT >/dev/null 2>&1 || { VALUE=\"\$($gen)\" && [ -n \"\$VALUE\" ] && printf %s \"\$VALUE\" | gcloud secrets create $name --project $PROJECT --replication-policy=automatic --data-file=-; }"
}
secret_access() {  # grant the runtime service account read access to ONE new secret
  run "gcloud secrets add-iam-policy-binding $1 --project $PROJECT --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor >/dev/null"
}

step_secrets() {
  note "step: secrets (create-if-missing; values never printed)"
  secret_from_cmd sales0-db-password   "printf '%s-Aa1' \"\$(openssl rand -base64 24 | tr -d '/+=\\n')\""
  secret_from_cmd sales0-secret-key    "openssl rand -base64 48 | tr -d '\\n'"
  secret_from_cmd sales0-worker-token  "openssl rand -base64 36 | tr -d '\\n'"
  secret_from_cmd sales0-app-password  "openssl rand -base64 18 | tr -d '/+=\\n'"
  note "needs env SALES0_ANTHROPIC_API_KEY (and SALES0_ANTHROPIC_WORKSPACE_ID if the key is not workspace-scoped) when the secret does not exist yet"
  secret_from_cmd sales0-anthropic-api-key "printf %s \"\${SALES0_ANTHROPIC_API_KEY:?set SALES0_ANTHROPIC_API_KEY}\""
  secret_from_cmd sales0-anthropic-workspace-id "printf %s \"\${SALES0_ANTHROPIC_WORKSPACE_ID:?set SALES0_ANTHROPIC_WORKSPACE_ID}\""
  note "Google OAuth web client JSON (redirect URI: $PUBLIC_URL/oauth/google/callback), from \$GOOGLE_CLIENT_SECRET_FILE=$GOOGLE_CLIENT_SECRET_FILE"
  run "gcloud secrets describe sales0-google-client-secret --project $PROJECT >/dev/null 2>&1 || gcloud secrets create sales0-google-client-secret --project $PROJECT --replication-policy=automatic --data-file=$GOOGLE_CLIENT_SECRET_FILE"
  for s in sales0-db-password sales0-secret-key sales0-worker-token sales0-app-password sales0-anthropic-api-key \
           sales0-anthropic-workspace-id sales0-google-client-secret; do
    secret_access "$s"
  done
}

step_db_user() {
  note "step: db-user (role only; the app creates database '$DB_NAME' itself on first boot and so owns it)"
  run "gcloud sql users list --instance $INSTANCE --project $PROJECT --format='value(name)' | grep -qx $DB_USER || gcloud sql users create $DB_USER --instance $INSTANCE --project $PROJECT --password \"\$(gcloud secrets versions access latest --secret sales0-db-password --project $PROJECT)\""
}

step_deploy() {
  note "step: deploy (Cloud Build from ./Dockerfile into Artifact Registry repo cloud-run-source-deploy, then Cloud Run)"
  note "Direct VPC egress to the private Cloud SQL IP, same as glitch-api: --network/--subnet default, private-ranges-only"
  run "gcloud run deploy $SERVICE --project $PROJECT --region $REGION --source . \\
    --service-account $SA \\
    --allow-unauthenticated --port 8080 \\
    --min-instances 0 --max-instances 1 --timeout 900 --concurrency 10 --cpu 1 --memory 1Gi --cpu-boost \\
    --network default --subnet default --vpc-egress private-ranges-only \\
    --set-env-vars SALES0_MODE=hosted,SALES0_DB_HOST=$DB_HOST,SALES0_DB_NAME=$DB_NAME,SALES0_DB_USER=$DB_USER,SALES0_DB_SSLMODE=require,SALES0_PUBLIC_BASE_URL=$PUBLIC_URL,SALES0_GOOGLE_CLIENT_SECRET_PATH=/secrets/google/google_client_secret.json \\
    --set-secrets SALES0_DB_PASSWORD=sales0-db-password:latest,SALES0_SECRET_KEY=sales0-secret-key:latest,SALES0_WORKER_TOKEN=sales0-worker-token:latest,SALES0_APP_PASSWORD=sales0-app-password:latest,SALES0_ANTHROPIC_API_KEY=sales0-anthropic-api-key:latest,SALES0_ANTHROPIC_WORKSPACE_ID=sales0-anthropic-workspace-id:latest,/secrets/google/google_client_secret.json=sales0-google-client-secret:latest"
}

step_verify() {
  note "step: verify (read-only)"
  run "gcloud run services describe $SERVICE --project $PROJECT --region $REGION --format='value(status.url)'"
  run "curl -fsS $PUBLIC_URL/api/health"
  note "register $PUBLIC_URL/oauth/google/callback as an authorised redirect URI on the Google OAuth web client"
  note "first admin password: gcloud secrets versions access latest --secret sales0-app-password --project $PROJECT"
  note "laptop worker: SALES0_WORKER_SERVER_URL=$PUBLIC_URL SALES0_WORKER_TOKEN=\$(gcloud secrets versions access latest --secret sales0-worker-token --project $PROJECT) uv run sales0 worker"
}

step_rollback() {
  if [ -z "$ROLLBACK_REV" ]; then
    note "rollback: list revisions, then re-run with 'rollback <REVISION>'"
    run "gcloud run revisions list --service $SERVICE --project $PROJECT --region $REGION --limit 5"
  else
    note "rollback: send 100% of traffic to $ROLLBACK_REV (the database is NOT rolled back; see docs/deploy.md)"
    run "gcloud run services update-traffic $SERVICE --project $PROJECT --region $REGION --to-revisions $ROLLBACK_REV=100"
  fi
}

if [ "$ACTION" = "rollback" ]; then step_rollback; exit 0; fi
for s in "${STEPS[@]}"; do
  case "$s" in
    secrets) step_secrets ;;
    db-user) step_db_user ;;
    deploy) step_deploy ;;
    verify) step_verify ;;
    *) echo "unknown step: $s" >&2; exit 2 ;;
  esac
done
note "done"
