#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project ID.}"
GOOGLE_SHEET_ID="${GOOGLE_SHEET_ID:?Set GOOGLE_SHEET_ID to the target spreadsheet ID.}"

REGION="${REGION:-us-central1}"
JOB_NAME="${JOB_NAME:-aaa-gas-prices-sync}"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-aaa-gas-prices-430am}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-aaa-gas-prices-runner}"
SCHEDULER_SA_NAME="${SCHEDULER_SA_NAME:-aaa-gas-prices-scheduler}"
ARTIFACT_REPO="${ARTIFACT_REPO:-aaa-gas-prices}"
BUCKET="${AAA_GCS_BUCKET:-${PROJECT_ID}-aaa-gas-prices-cache}"
CSV_OBJECT="${AAA_GCS_OBJECT:-outputs/aaa_gas_prices/aaa_national_gas_prices.csv}"
SHEETS_SECRET_NAME="${SHEETS_SECRET_NAME:-aaa-gas-prices-service-account-json}"
SHEETS_CREDENTIALS_FILE="${SHEETS_CREDENTIALS_FILE:-credentials/aaa-gas-prices-service-account.json}"
SECRET_MOUNT_PATH="/secrets/google/aaa-gas-prices-service-account.json"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="${SCHEDULER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/${JOB_NAME}:latest"
RUN_JOB_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB_NAME}:run"

gcloud config set project "${PROJECT_ID}"

gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  storage.googleapis.com

if ! gcloud iam service-accounts describe "${RUNTIME_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" \
    --display-name="AAA gas prices Cloud Run runner"
fi

if ! gcloud iam service-accounts describe "${SCHEDULER_SA}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SCHEDULER_SA_NAME}" \
    --display-name="AAA gas prices scheduler invoker"
fi

if ! gcloud artifacts repositories describe "${ARTIFACT_REPO}" \
  --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${ARTIFACT_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="AAA gas prices container images"
fi

if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
fi

gcloud storage cp "${CSV_OBJECT}" "gs://${BUCKET}/${CSV_OBJECT}"

if ! gcloud secrets describe "${SHEETS_SECRET_NAME}" >/dev/null 2>&1; then
  if [ ! -f "${SHEETS_CREDENTIALS_FILE}" ]; then
    echo "Missing ${SHEETS_CREDENTIALS_FILE}."
    echo "Create Secret Manager secret ${SHEETS_SECRET_NAME} manually, or upload the JSON file and rerun."
    exit 1
  fi
  gcloud secrets create "${SHEETS_SECRET_NAME}" \
    --replication-policy=automatic \
    --data-file="${SHEETS_CREDENTIALS_FILE}"
elif [ -f "${SHEETS_CREDENTIALS_FILE}" ] && [ "${UPDATE_SHEETS_SECRET:-0}" = "1" ]; then
  gcloud secrets versions add "${SHEETS_SECRET_NAME}" \
    --data-file="${SHEETS_CREDENTIALS_FILE}"
fi

gcloud secrets add-iam-policy-binding "${SHEETS_SECRET_NAME}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/storage.objectAdmin" \
  --quiet

gcloud builds submit --tag "${IMAGE}" .

gcloud run jobs deploy "${JOB_NAME}" \
  --region="${REGION}" \
  --image="${IMAGE}" \
  --service-account="${RUNTIME_SA}" \
  --tasks=1 \
  --max-retries=1 \
  --task-timeout=600s \
  --cpu=1 \
  --memory=512Mi \
  --set-env-vars="GOOGLE_SHEET_ID=${GOOGLE_SHEET_ID},AAA_GCS_BUCKET=${BUCKET},AAA_GCS_OBJECT=${CSV_OBJECT},AAA_CSV_PATH=${CSV_OBJECT},GOOGLE_APPLICATION_CREDENTIALS=${SECRET_MOUNT_PATH}" \
  --set-secrets="${SECRET_MOUNT_PATH}=${SHEETS_SECRET_NAME}:latest"

gcloud run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker" \
  --quiet

if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" \
  --location="${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --schedule="30 4 * * *" \
    --time-zone="America/New_York" \
    --uri="${RUN_JOB_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SCHEDULER_SA}" \
    --headers="Content-Type=application/json" \
    --message-body="{}" \
    --attempt-deadline=300s
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --schedule="30 4 * * *" \
    --time-zone="America/New_York" \
    --uri="${RUN_JOB_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SCHEDULER_SA}" \
    --headers="Content-Type=application/json" \
    --message-body="{}" \
    --attempt-deadline=300s
fi

echo
echo "Cloud Run Job: ${JOB_NAME}"
echo "Cloud Scheduler job: ${SCHEDULER_JOB_NAME}"
echo "Schedule: 4:30 AM America/New_York"
echo "CSV cache: gs://${BUCKET}/${CSV_OBJECT}"
echo
echo "Manual test:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION} --wait"
