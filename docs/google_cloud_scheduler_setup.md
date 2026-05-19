# Google Cloud Scheduler Setup

This setup moves the daily AAA gas prices sync from GitHub Actions to Google
Cloud Scheduler + Cloud Run Jobs.

## Architecture

- Cloud Scheduler runs at `4:30 AM` in `America/New_York`.
- Cloud Scheduler calls the Cloud Run Jobs API to execute a Cloud Run Job.
- The Cloud Run Job runs `scripts/cloud_run_aaa_sync.py`.
- The wrapper downloads the CSV cache from Cloud Storage, runs
  `scripts/aaa_gas_prices_to_sheets.py`, then uploads the refreshed national
  CSV cache.
- The same wrapper also runs `scripts/aaa_state_gas_prices_to_sheets.py` for
  all states. State CSV caches are stored separately in Cloud Storage and build
  forward from `2026-05-19`.
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
- national CSV cache:
  `outputs/aaa_gas_prices/aaa_national_gas_prices.csv`
- state CSV cache prefix:
  `outputs/aaa_gas_prices/states/`
- Cloud Run Job: `aaa-gas-prices-sync`
- Cloud Scheduler Job: `aaa-gas-prices-430am`
- runtime service account: `aaa-gas-prices-runner`
- scheduler service account: `aaa-gas-prices-scheduler`

The deploy script keeps the existing national CSV cache in Cloud Storage by
default. To intentionally replace the Cloud Storage cache with the repo CSV,
set:

```sh
export REFRESH_GCS_CACHE=1
```

before deploying.

By default, the deployed Cloud Run job updates:

- `AAA National Prices`
- `Daily Comparison`
- one tab per state, starting with rows from `2026-05-19`
- `State Comparison Since May 19`

The first all-state run creates or updates more than 50 tabs, so the state
sync pauses between sheet writes and retries temporary Google Sheets quota
errors. A manual first run can take several minutes.

To temporarily disable state tabs during a deploy, run:

```sh
export AAA_SYNC_STATES=0
```

before running `./scripts/deploy_gcp_cloud_run_scheduler.sh`.

To make the state tab writes slower or faster, set:

```sh
export AAA_STATE_SHEET_WRITE_SLEEP=5
```

before deploying. Keep this at `5` or higher if Google Sheets returns quota
errors.

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
Updated Google Sheet tab 'Iowa'.
Updated Google Sheet tab 'State Comparison Since May 19'.
Uploaded refreshed CSV cache to gs://...
```

Then check Google Sheets version history for a new edit from the service
account.

## Cutover

Keep GitHub Actions enabled until one Cloud Scheduler run succeeds at `4:30 AM`.
After that, disable the GitHub Actions schedule or keep only manual
`workflow_dispatch` as a fallback.
