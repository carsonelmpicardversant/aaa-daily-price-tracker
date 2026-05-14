# Google Cloud Scheduler Setup

This setup moves the daily AAA gas prices sync from GitHub Actions to Google
Cloud Scheduler + Cloud Run Jobs.

## Architecture

- Cloud Scheduler runs at `4:30 AM` in `America/New_York`.
- Cloud Scheduler calls the Cloud Run Jobs API to execute a Cloud Run Job.
- The Cloud Run Job runs `scripts/cloud_run_aaa_sync.py`.
- The wrapper downloads the CSV cache from Cloud Storage, runs
  `scripts/aaa_gas_prices_to_sheets.py`, then uploads the refreshed CSV cache.
- Secret Manager stores the Google Sheets service-account JSON key.

Useful Google docs:

- [Cloud Scheduler time zones](https://docs.cloud.google.com/scheduler/docs/configuring/cron-job-schedules)
- [Cloud Run jobs on a schedule](https://cloud.google.com/run/docs/execute/jobs-on-schedule)
- [Cloud Run job secrets](https://docs.cloud.google.com/run/docs/configuring/jobs/secrets)
- [Cloud Scheduler HTTP auth](https://docs.cloud.google.com/scheduler/docs/http-target-auth)

## Prerequisites

You need:

- a Google Cloud project with billing enabled
- owner/editor access, or enough IAM to create Cloud Run, Scheduler, Storage,
  Secret Manager, Artifact Registry, service accounts, and IAM bindings
- the existing spreadsheet ID
- the existing service-account JSON key used for Google Sheets

This local machine does not currently have `gcloud` installed, so the easiest
path is to use Google Cloud Shell.

## Cloud Shell Deploy

In Google Cloud Shell, clone the GitHub repo:

```sh
git clone https://github.com/carsonelmpicardversant/aaa-daily-price-tracker.git
cd aaa-daily-price-tracker
```

Create the Secret Manager secret named
`aaa-gas-prices-service-account-json`. You can do this in either the Cloud
Console or Cloud Shell.

Console path:

```text
Security -> Secret Manager -> Create secret
Name: aaa-gas-prices-service-account-json
Secret value: paste the full service-account JSON key
```

Then run the deploy script:

```sh
export PROJECT_ID="YOUR_GOOGLE_CLOUD_PROJECT_ID"
export GOOGLE_SHEET_ID="1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY"

./scripts/deploy_gcp_cloud_run_scheduler.sh
```

The script creates or updates:

- Artifact Registry repository: `aaa-gas-prices`
- Cloud Storage bucket: `${PROJECT_ID}-aaa-gas-prices-cache`
- Cloud Run Job: `aaa-gas-prices-sync`
- Cloud Scheduler Job: `aaa-gas-prices-430am`
- runtime service account: `aaa-gas-prices-runner`
- scheduler service account: `aaa-gas-prices-scheduler`

## Manual Test

After deploy finishes, run:

```sh
gcloud run jobs execute aaa-gas-prices-sync \
  --region=us-central1 \
  --wait
```

Confirm the Cloud Run logs show:

```text
Updated Google Sheet tab 'AAA National Prices'.
Updated Google Sheet tab 'Daily Comparison'.
Uploaded refreshed CSV cache to gs://...
```

Then check Google Sheets version history for a new edit from the service
account.

## Cutover

Keep GitHub Actions enabled until one Cloud Scheduler run succeeds at `4:30 AM`.
After that, disable the GitHub Actions schedule or keep only manual
`workflow_dispatch` as a fallback.
