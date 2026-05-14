# State Gas Prices Tracker

The state tracker extends the AAA national tracker with full-history state tabs
and a cross-state comparison tab.

## Sheet Structure

The intended Google Sheet layout is:

- `AAA National Prices`
- `Daily Comparison`
- `State Comparison Since Feb 28`
- one tab per state, such as `Iowa`, `California`, and `Texas`

Each state tab mirrors the national tab:

```text
date | status | regular | mid_grade | premium | diesel | e85
```

The per-state CSV cache keeps the richer source columns under:

```text
outputs/aaa_gas_prices/states/
```

For example:

```text
outputs/aaa_gas_prices/states/IA.csv
```

## Iowa Proof Run

Start with Iowa before running all states:

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states IA \
  --backfill \
  --start-date 2026-02-28 \
  --end-date 2026-05-14 \
  --credentials credentials/aaa-gas-prices-service-account.json \
  --spreadsheet-id 1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY
```

That will:

- inspect Wayback snapshots around the date range
- fetch the current AAA Iowa page
- write `outputs/aaa_gas_prices/states/IA.csv`
- update the `Iowa` tab
- update `State Comparison Since Feb 28`

If the first backfill leaves gaps, rerun a targeted missing-date pass:

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states IA \
  --backfill \
  --only-missing \
  --start-date 2026-02-28 \
  --end-date 2026-05-14 \
  --target-window-days 3 \
  --cdx-chunk-days 3 \
  --sleep 1 \
  --report-missing \
  --credentials credentials/aaa-gas-prices-service-account.json \
  --spreadsheet-id 1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY
```

The targeted pass searches captures that could expose each missing date through
AAA's `Current Avg.`, `Yesterday Avg.`, `Week Ago Avg.`, `Month Ago Avg.`, or
`Year Ago Avg.` rows.

The targeted pass is intentionally more aggressive than the initial backfill:

- it queries compact Wayback ranges around each likely source date instead of
  one large date span
- it does not collapse captures by digest, which avoids losing same-day
  snapshots
- it also checks 28-32 day offsets because archived AAA month comparisons can
  be more useful when exact calendar-month captures are missing
- it skips future capture dates by default, so it does not ask Wayback for
  month/year comparison snapshots that cannot exist yet
- if one Wayback CDX lookup returns a bad response, it warns and keeps going
- `--report-missing` prints the remaining gaps after the pass finishes

If the targeted pass still leaves gaps, try the broader Wayback URL search:

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states IA \
  --backfill \
  --only-missing \
  --broad-wayback-url-search \
  --start-date 2026-02-28 \
  --end-date 2026-05-14 \
  --max-capture-date 2026-05-14 \
  --target-window-days 3 \
  --cdx-chunk-days 1 \
  --sleep 1 \
  --report-missing \
  --credentials credentials/aaa-gas-prices-service-account.json \
  --spreadsheet-id 1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY
```

The broad search checks archived `gasprices.aaa.com` URLs and filters for the
state query parameter. It is slower, but it can find snapshots that were stored
under a slightly different URL shape. Use `--cdx-chunk-days 1` for this pass
because broad wildcard Wayback searches can time out when the date range is
too large.

## State Coverage Report

Before creating every state tab in Google Sheets, run a coverage report. This
uses the same state CSV/backfill logic, but it does not update Google Sheets.

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states all \
  --backfill \
  --start-date 2026-02-28 \
  --end-date 2026-05-14 \
  --sleep 0.5 \
  --coverage-report
```

The report is written to:

```text
outputs/aaa_gas_prices/state_coverage_report.csv
```

Use the report to compare each state against Iowa before deciding whether to
create all tabs. Key fields are `coverage_pct`, `missing_days`,
`baseline_status`, and `missing_dates`.

## All-State Run

After Iowa is verified:

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states all \
  --backfill \
  --start-date 2026-02-28 \
  --end-date 2026-05-14 \
  --credentials credentials/aaa-gas-prices-service-account.json \
  --spreadsheet-id 1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY
```

The all-state backfill can take a while because it checks Wayback snapshots for
each state. Once the state CSVs exist, normal daily updates can run without
`--backfill`.

## Daily State Update

For a daily current-state update:

```sh
python3 scripts/aaa_state_gas_prices_to_sheets.py \
  --states all \
  --credentials credentials/aaa-gas-prices-service-account.json \
  --spreadsheet-id 1TqhBPhIdWGJAgcmaB4Lfk9CYEFAPSLgpIbFx4v47sWY
```

This fetches each live state page, merges the current records into each state's
CSV history, updates state tabs, and refreshes the comparison tab.
