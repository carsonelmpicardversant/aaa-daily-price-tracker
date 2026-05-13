# AAA Gas Prices to Google Sheets

This project scrapes the AAA national average table from
`https://gasprices.aaa.com/` and writes one row per calendar date to Google
Sheets.

The live AAA page only exposes the latest table. For historical backfill, the
script reads archived copies of the same AAA page from the Internet Archive
Wayback Machine, then uses AAA's table rows for `Current Avg.`, `Yesterday
Avg.`, `Week Ago Avg.`, `Month Ago Avg.`, and `Year Ago Avg.` to fill as many
dates as AAA makes available in those captures.

## Google API Setup

1. Open the [Google Cloud Console](https://console.cloud.google.com/).

2. Create or select a project.

3. Enable these APIs for the project:
   - [Google Sheets API](https://developers.google.com/sheets/api/guides/concepts)
   - [Google Drive API](https://developers.google.com/drive/api/guides/manage-sharing), only needed if the script creates a spreadsheet and shares it with you.

4. Create a service account:
   - Go to `IAM & Admin` -> `Service Accounts`.
   - Click `Create service account`.
   - Give it a name like `aaa-gas-prices-sync`.
   - You do not need to grant project roles for this local Sheets sync.

5. Create a JSON key:
   - Open the service account.
   - Go to `Keys`.
   - Click `Add key` -> `Create new key`.
   - Choose `JSON`.
   - Save the file locally, for example:

```sh
mkdir -p credentials
mv ~/Downloads/YOUR_KEY_FILE.json credentials/aaa-gas-prices-service-account.json
```

6. Choose how the spreadsheet should be created.

Recommended option: create the sheet yourself.

```text
1. Create a blank Google Sheet in your Google Drive.
2. Copy the spreadsheet ID from the URL:
   https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
3. Share the sheet with the service account's email address.
   The email is the `client_email` value inside the JSON key.
4. Give it Editor access.
```

Alternative option: let the script create the sheet.

```text
Set GOOGLE_SHARE_WITH_EMAIL to your Google account email. The script will create
a spreadsheet owned by the service account, then share it with you.
```

## First Run With Historical Backfill

From this project directory:

```sh
cd "/Users/207000019@bwt3.com/Documents/New project 2"

export GOOGLE_APPLICATION_CREDENTIALS="$PWD/credentials/aaa-gas-prices-service-account.json"

# Use this if you created and shared a blank Sheet yourself.
export GOOGLE_SHEET_ID="PASTE_SPREADSHEET_ID_HERE"

# Or use this if you want the script to create and share a new Sheet.
# export GOOGLE_SHARE_WITH_EMAIL="you@example.com"

python3 scripts/aaa_gas_prices_to_sheets.py --backfill --start-date 2026-01-01
```

The script writes:

- Google Sheets tab: `AAA National Prices`
- Local CSV cache: `outputs/aaa_gas_prices/aaa_national_gas_prices.csv`

Rows with `status` equal to `missing` mean no usable AAA live or archived row
was found for that date during the run. Re-running backfill later can fill more
dates if Wayback has additional captures available.

## Daily Run

After the first backfill, run this daily:

```sh
cd "/Users/207000019@bwt3.com/Documents/New project 2"
export GOOGLE_APPLICATION_CREDENTIALS="$PWD/credentials/aaa-gas-prices-service-account.json"
export GOOGLE_SHEET_ID="PASTE_SPREADSHEET_ID_HERE"

python3 scripts/aaa_gas_prices_to_sheets.py
```

The daily run loads the local CSV cache, fetches the latest AAA page, merges new
rows, and rewrites the Google Sheet.

## macOS Daily Schedule

Create `~/Library/LaunchAgents/com.local.aaa-gas-prices.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.local.aaa-gas-prices</string>

  <key>WorkingDirectory</key>
  <string>/Users/207000019@bwt3.com/Documents/New project 2</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/207000019@bwt3.com/Documents/New project 2/scripts/aaa_gas_prices_to_sheets.py</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>GOOGLE_APPLICATION_CREDENTIALS</key>
    <string>/Users/207000019@bwt3.com/Documents/New project 2/credentials/aaa-gas-prices-service-account.json</string>
    <key>GOOGLE_SHEET_ID</key>
    <string>PASTE_SPREADSHEET_ID_HERE</string>
  </dict>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>15</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>/tmp/aaa-gas-prices.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/aaa-gas-prices.err.log</string>
</dict>
</plist>
```

Load and test it:

```sh
launchctl load ~/Library/LaunchAgents/com.local.aaa-gas-prices.plist
launchctl start com.local.aaa-gas-prices
tail -f /tmp/aaa-gas-prices.out.log /tmp/aaa-gas-prices.err.log
```

To stop the schedule:

```sh
launchctl unload ~/Library/LaunchAgents/com.local.aaa-gas-prices.plist
```

## GitHub Actions Schedule

If you want the sync to run even when your Mac is off, use a scheduled GitHub
Actions workflow. Add these repository secrets:

- `AAA_GAS_PRICES_SERVICE_ACCOUNT_JSON`: the full service-account JSON key
- `AAA_GAS_PRICES_SPREADSHEET_ID`: the target spreadsheet ID

The workflow writes the JSON secret to
`credentials/aaa-gas-prices-service-account.json` at runtime and then runs:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py
```

The workflow in
[`/.github/workflows/aaa-gas-prices-sync.yml`](/Users/207000019@bwt3.com/Documents/New%20project%202/.github/workflows/aaa-gas-prices-sync.yml)
runs at `08:30 UTC` and `09:30 UTC`. The workflow uses the scheduled cron value
and the current New York UTC offset to run only the entry that maps to `4:30 AM
Eastern` for the current season. Manual `workflow_dispatch` runs also sync
immediately, so you can test the job anytime without waiting for the scheduled
window. GitHub may start scheduled workflows a little late during periods of
Actions load, but the job will still sync when the scheduled entry is the
correct one for the current Eastern offset.

## Useful Local Checks

Write only the CSV, without touching Google Sheets:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py --skip-google
```

Retry historical backfill later:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py --backfill --start-date 2026-01-01
```

Use verbose backfill logging:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py --backfill --verbose
```
