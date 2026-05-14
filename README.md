# AAA Gas Prices Scraper

Local scraper for AAA national average fuel prices with Google Sheets sync.

Main files:

- `scripts/aaa_gas_prices_to_sheets.py` - scraper, CSV cache writer, and Google Sheets sync.
- `scripts/cloud_run_aaa_sync.py` - Cloud Run entrypoint with Cloud Storage CSV cache sync.
- `docs/aaa_google_sheets_setup.md` - Google API setup, first backfill, and macOS/GitHub schedules.
- `docs/google_cloud_scheduler_setup.md` - Cloud Run Job and Cloud Scheduler setup.
- `config/aaa_gas_prices.env.example` - environment variable template.

Quick local CSV check:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py --skip-google
```

First Google Sheets backfill after credentials are set:

```sh
python3 scripts/aaa_gas_prices_to_sheets.py --backfill --start-date 2026-01-01
```
