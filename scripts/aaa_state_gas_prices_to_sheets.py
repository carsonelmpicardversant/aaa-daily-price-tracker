#!/usr/bin/env python3
"""Scrape AAA state fuel prices and sync state history tabs to Google Sheets."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import aaa_gas_prices_to_sheets as aaa


STATE_COMPARISON_SHEET_NAME = "State Comparison Since Feb 28"
DEFAULT_BASELINE_DATE = dt.date(2026, 2, 28)
DEFAULT_STATE_CSV_DIR = Path("outputs/aaa_gas_prices/states")
WAYBACK_CDX_URL = "https://web.archive.org/cdx"

STATES: tuple[tuple[str, str], ...] = (
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
)

STATE_NAMES_BY_CODE = dict(STATES)
STATE_CODES_BY_NAME = {name.lower(): code for code, name in STATES}


@dataclass(frozen=True)
class StateResult:
    code: str
    name: str
    csv_path: Path
    ok_days: int
    expected_days: int
    latest_date: dt.date | None


def aaa_state_url(state_code: str) -> str:
    return f"https://gasprices.aaa.com/?state={urllib.parse.quote(state_code)}"


def state_csv_path(csv_dir: Path, state_code: str) -> Path:
    return csv_dir / f"{state_code.upper()}.csv"


def parse_date_arg(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_states_arg(value: str) -> list[tuple[str, str]]:
    normalized = value.strip()
    if not normalized or normalized.lower() == "all":
        return list(STATES)

    selected: list[tuple[str, str]] = []
    for raw_part in normalized.split(","):
        part = raw_part.strip()
        if not part:
            continue
        code = part.upper()
        if code not in STATE_NAMES_BY_CODE:
            code = STATE_CODES_BY_NAME.get(part.lower(), "")
        if code not in STATE_NAMES_BY_CODE:
            raise argparse.ArgumentTypeError(f"Unknown state: {part}")
        selected.append((code, STATE_NAMES_BY_CODE[code]))
    return selected


def fetch_state_live_records(
    state_code: str,
    start_date: dt.date,
    end_date: dt.date,
) -> list[aaa.PriceRecord]:
    scraped_at = aaa.utc_now()
    url = aaa_state_url(state_code)
    page = aaa.curl_get_text(url, timeout=45)
    return aaa.parse_aaa_page(
        page,
        source_kind="live",
        capture_timestamp=scraped_at,
        source_url=url,
        scraped_at_utc=scraped_at,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_wayback_captures_for_state(
    state_code: str,
    lookup_start: dt.date,
    lookup_end: dt.date,
) -> list[dict[str, str]]:
    params = {
        "url": aaa_state_url(state_code),
        "from": lookup_start.strftime("%Y%m%d"),
        "to": lookup_end.strftime("%Y%m%d"),
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
    }
    cdx_url = f"{WAYBACK_CDX_URL}?{urllib.parse.urlencode(params)}"
    payload = aaa.http_get_text(cdx_url, retries=4, timeout=90)
    data = json.loads(payload)
    if not data or len(data) == 1:
        return []

    header = data[0]
    captures = [dict(zip(header, row)) for row in data[1:]]
    return [
        capture
        for capture in captures
        if capture.get("mimetype", "").startswith("text/html")
    ]


def capture_date(timestamp: str) -> dt.date:
    return dt.datetime.strptime(timestamp[:8], "%Y%m%d").date()


def missing_dates_between(
    records: Iterable[aaa.PriceRecord],
    start_date: dt.date,
    end_date: dt.date,
) -> set[dt.date]:
    populated = {record.date for record in records}
    return {
        day
        for day in aaa.date_range(start_date, end_date)
        if day not in populated
    }


def target_capture_dates_for_missing(
    missing_dates: Iterable[dt.date],
    *,
    window_days: int,
) -> set[dt.date]:
    targets: set[dt.date] = set()
    for missing_date in missing_dates:
        likely_price_as_of_dates = {
            missing_date,
            missing_date + dt.timedelta(days=1),
            missing_date + dt.timedelta(days=7),
            aaa.add_months(missing_date, 1),
            aaa.add_months(missing_date, 12),
        }
        for price_as_of_date in likely_price_as_of_dates:
            for offset_days in range(-window_days, window_days + 1):
                targets.add(price_as_of_date + dt.timedelta(days=offset_days))
    return targets


def wayback_capture_url(state_code: str, timestamp: str) -> str:
    return (
        f"https://web.archive.org/web/{timestamp}id_/"
        f"{aaa_state_url(state_code)}"
    )


def fetch_state_wayback_records(
    state_code: str,
    start_date: dt.date,
    end_date: dt.date,
    *,
    lookup_start: dt.date,
    lookup_end: dt.date,
    sleep_seconds: float,
    capture_retries: int,
    capture_timeout: int,
    limit_captures: int | None,
    target_capture_dates: set[dt.date] | None,
    verbose: bool,
) -> list[aaa.PriceRecord]:
    captures = fetch_wayback_captures_for_state(state_code, lookup_start, lookup_end)
    if target_capture_dates is not None:
        captures = [
            capture
            for capture in captures
            if capture_date(capture["timestamp"]) in target_capture_dates
        ]
    if limit_captures:
        captures = captures[:limit_captures]
    if verbose:
        print(
            f"{state_code}: found {len(captures)} Wayback captures to inspect.",
            flush=True,
        )

    records: list[aaa.PriceRecord] = []
    for index, capture in enumerate(captures, start=1):
        timestamp = capture["timestamp"]
        url = wayback_capture_url(state_code, timestamp)
        try:
            page = aaa.http_get_text(
                url,
                retries=capture_retries,
                timeout=capture_timeout,
            )
            records.extend(
                aaa.parse_aaa_page(
                    page,
                    source_kind="wayback",
                    capture_timestamp=timestamp,
                    source_url=url,
                    scraped_at_utc=aaa.utc_now(),
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            if verbose:
                print(
                    f"{state_code}: parsed Wayback capture "
                    f"{index}/{len(captures)}: {timestamp}",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"Warning: {state_code} skipped Wayback capture "
                f"{timestamp}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return records


def process_state(
    state_code: str,
    state_name: str,
    *,
    csv_dir: Path,
    start_date: dt.date | None,
    end_date: dt.date,
    baseline_date: dt.date,
    backfill: bool,
    only_missing: bool,
    wayback_window_days: int,
    target_window_days: int,
    sleep_seconds: float,
    capture_retries: int,
    capture_timeout: int,
    limit_captures: int | None,
    verbose: bool,
) -> StateResult:
    csv_path = state_csv_path(csv_dir, state_code)
    all_records = aaa.load_existing_records(csv_path)
    if all_records:
        print(
            f"{state_code}: loaded {len(all_records)} cached records from {csv_path}.",
            flush=True,
        )

    effective_start_date = (
        start_date
        if start_date is not None
        else min((record.date for record in all_records), default=baseline_date)
    )
    if end_date < effective_start_date:
        raise ValueError(f"{state_code}: --end-date must be on or after --start-date.")

    if backfill:
        target_capture_dates = None
        if only_missing:
            missing_dates = missing_dates_between(
                all_records,
                effective_start_date,
                end_date,
            )
            target_capture_dates = target_capture_dates_for_missing(
                missing_dates,
                window_days=target_window_days,
            )
            if target_capture_dates:
                lookup_start = min(target_capture_dates)
                lookup_end = max(target_capture_dates)
            else:
                lookup_start = effective_start_date
                lookup_end = end_date
            print(
                f"{state_code}: targeting {len(missing_dates)} missing dates "
                f"with {len(target_capture_dates)} likely capture dates.",
                flush=True,
            )
        else:
            lookup_start = effective_start_date - dt.timedelta(days=wayback_window_days)
            lookup_end = end_date + dt.timedelta(days=wayback_window_days)
        print(
            f"{state_code}: backfilling from {effective_start_date} to {end_date} "
            f"using captures from {lookup_start} to {lookup_end}.",
            flush=True,
        )
        all_records.extend(
            fetch_state_wayback_records(
                state_code,
                effective_start_date,
                end_date,
                lookup_start=lookup_start,
                lookup_end=lookup_end,
                sleep_seconds=sleep_seconds,
                capture_retries=capture_retries,
                capture_timeout=capture_timeout,
                limit_captures=limit_captures,
                target_capture_dates=target_capture_dates,
                verbose=verbose,
            )
        )

    print(f"{state_code}: fetching latest AAA fuel prices.", flush=True)
    all_records.extend(
        fetch_state_live_records(state_code, effective_start_date, end_date)
    )

    merged = aaa.merge_records(all_records)
    csv_rows = aaa.records_to_sheet_rows(merged, effective_start_date, end_date)
    aaa.write_csv(csv_path, csv_rows)

    expected_days = (end_date - effective_start_date).days + 1
    ok_days = sum(1 for row in csv_rows[1:] if row[1] == "ok")
    latest_date = max((record.date for record in merged), default=None)
    print(
        f"{state_code}: wrote {csv_path} with "
        f"{ok_days}/{expected_days} populated days.",
        flush=True,
    )
    return StateResult(
        code=state_code,
        name=state_name,
        csv_path=csv_path,
        ok_days=ok_days,
        expected_days=expected_days,
        latest_date=latest_date,
    )


def format_price(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def format_change(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def format_percent(value: float | None) -> str:
    return "" if value is None else f"{value:.1%}"


def state_comparison_rows(
    states: Iterable[tuple[str, str]],
    csv_dir: Path,
    baseline_date: dt.date,
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        [
            "state",
            "state_code",
            "baseline_date",
            "baseline_regular",
            "latest_date",
            "latest_regular",
            "dollar_change",
            "percent_change",
            "baseline_status",
            "latest_status",
            "baseline_source_url",
            "latest_source_url",
        ]
    ]

    for state_code, state_name in states:
        records = aaa.load_existing_records(state_csv_path(csv_dir, state_code))
        by_date = {record.date: record for record in records}
        baseline_record = by_date.get(baseline_date)
        latest_record = max(records, key=lambda record: record.date) if records else None

        dollar_change: float | None = None
        percent_change: float | None = None
        if (
            baseline_record
            and latest_record
            and baseline_record.regular is not None
            and latest_record.regular is not None
        ):
            dollar_change = latest_record.regular - baseline_record.regular
            if baseline_record.regular:
                percent_change = dollar_change / baseline_record.regular

        rows.append(
            [
                state_name,
                state_code,
                baseline_date.isoformat(),
                format_price(baseline_record.regular if baseline_record else None),
                latest_record.date.isoformat() if latest_record else "",
                format_price(latest_record.regular if latest_record else None),
                format_change(dollar_change),
                format_percent(percent_change),
                "ok" if baseline_record else "missing",
                "ok" if latest_record else "missing",
                baseline_record.source_url if baseline_record else "",
                latest_record.source_url if latest_record else "",
            ]
        )
    return rows


def sync_state_tabs(
    client: aaa.GoogleApiClient,
    spreadsheet_id: str,
    results: Iterable[StateResult],
) -> None:
    for result in results:
        records = aaa.load_existing_records(result.csv_path)
        if not records:
            print(f"{result.code}: no records available for sheet sync.", flush=True)
            continue
        start_date = min(record.date for record in records)
        end_date = max(record.date for record in records)
        rows = aaa.records_to_google_sheet_rows(records, start_date, end_date)
        aaa.update_sheet(client, spreadsheet_id, result.name, rows)
        print(f"Updated Google Sheet tab '{result.name}'.", flush=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape AAA state gas prices and sync state tabs."
    )
    parser.add_argument(
        "--states",
        type=parse_states_arg,
        default=parse_states_arg("IA"),
        help=(
            "Comma-separated state codes/names, or 'all'. Defaults to IA while "
            "state support is being proven out."
        ),
    )
    parser.add_argument(
        "--baseline-date",
        type=parse_date_arg,
        default=DEFAULT_BASELINE_DATE,
        help="Comparison baseline date in YYYY-MM-DD format. Defaults to 2026-02-28.",
    )
    parser.add_argument(
        "--start-date",
        type=parse_date_arg,
        default=None,
        help="History start date. Defaults to earliest cached row or baseline date.",
    )
    parser.add_argument(
        "--end-date",
        type=parse_date_arg,
        default=dt.date.today(),
        help="History end date. Defaults to today.",
    )
    parser.add_argument(
        "--csv-dir",
        type=Path,
        default=DEFAULT_STATE_CSV_DIR,
        help="Directory for per-state CSV caches.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch Wayback captures before the live AAA page.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help=(
            "When backfilling, inspect captures likely to fill currently "
            "missing dates instead of scanning the whole date range."
        ),
    )
    parser.add_argument(
        "--wayback-window-days",
        type=int,
        default=2,
        help="Extra days around the requested date range to inspect in Wayback.",
    )
    parser.add_argument(
        "--target-window-days",
        type=int,
        default=2,
        help=(
            "Days around each likely source date to inspect when using "
            "--only-missing."
        ),
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.25,
        help="Seconds to wait between Wayback capture requests.",
    )
    parser.add_argument(
        "--capture-retries",
        type=int,
        default=1,
        help="Retries per Wayback capture.",
    )
    parser.add_argument(
        "--capture-timeout",
        type=int,
        default=25,
        help="Seconds to wait for each Wayback capture request.",
    )
    parser.add_argument(
        "--limit-captures",
        type=int,
        default=None,
        help="Debug option: inspect only the first N Wayback captures per state.",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=(
            Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
            if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            else None
        ),
        help="Google service account JSON path. Can also use GOOGLE_APPLICATION_CREDENTIALS.",
    )
    parser.add_argument(
        "--spreadsheet-id",
        default=os.environ.get("GOOGLE_SHEET_ID", ""),
        help="Existing Google Sheet ID.",
    )
    parser.add_argument(
        "--skip-google",
        action="store_true",
        help="Only write local CSVs; do not update Google Sheets.",
    )
    parser.add_argument(
        "--skip-state-tabs",
        action="store_true",
        help="Do not write individual state tabs.",
    )
    parser.add_argument(
        "--skip-comparison-tab",
        action="store_true",
        help="Do not write the all-state comparison tab.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print capture-level backfill progress.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.end_date < args.baseline_date and args.start_date is None:
        args.start_date = args.end_date

    results: list[StateResult] = []
    failures: list[str] = []
    for state_code, state_name in args.states:
        try:
            results.append(
                process_state(
                    state_code,
                    state_name,
                    csv_dir=args.csv_dir,
                    start_date=args.start_date,
                    end_date=args.end_date,
                    baseline_date=args.baseline_date,
                    backfill=args.backfill,
                    only_missing=args.only_missing,
                    wayback_window_days=args.wayback_window_days,
                    target_window_days=args.target_window_days,
                    sleep_seconds=args.sleep,
                    capture_retries=args.capture_retries,
                    capture_timeout=args.capture_timeout,
                    limit_captures=args.limit_captures,
                    verbose=args.verbose,
                )
            )
        except Exception as exc:
            failures.append(f"{state_code}: {exc}")
            print(f"Error: {state_code}: {exc}", file=sys.stderr, flush=True)

    if args.skip_google:
        print("Skipped Google Sheets sync because --skip-google was set.", flush=True)
        return 1 if failures else 0

    if args.credentials is None:
        print(
            "Error: provide --credentials or set GOOGLE_APPLICATION_CREDENTIALS.",
            file=sys.stderr,
        )
        return 2
    if not args.spreadsheet_id:
        print("Error: provide --spreadsheet-id or set GOOGLE_SHEET_ID.", file=sys.stderr)
        return 2

    client = aaa.GoogleApiClient(args.credentials, aaa.SCOPES)
    if not args.skip_state_tabs:
        sync_state_tabs(client, args.spreadsheet_id, results)
    if not args.skip_comparison_tab:
        rows = state_comparison_rows(args.states, args.csv_dir, args.baseline_date)
        aaa.update_sheet(client, args.spreadsheet_id, STATE_COMPARISON_SHEET_NAME, rows)
        print(
            f"Updated Google Sheet tab '{STATE_COMPARISON_SHEET_NAME}'.",
            flush=True,
        )

    if failures:
        print("Completed with state failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
