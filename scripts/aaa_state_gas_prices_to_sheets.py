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
DEFAULT_COVERAGE_REPORT_PATH = Path("outputs/aaa_gas_prices/state_coverage_report.csv")
DEFAULT_SHEET_WRITE_SLEEP_SECONDS = 5.0
WAYBACK_CDX_URL = "https://web.archive.org/cdx"
LEADING_SHEET_NAMES = (
    aaa.DATA_SHEET_NAME,
    aaa.COMPARISON_SHEET_NAME,
)

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
    start_date: dt.date
    end_date: dt.date
    ok_days: int
    expected_days: int
    latest_date: dt.date | None
    missing_dates: tuple[dt.date, ...]


def aaa_state_url(state_code: str) -> str:
    return f"https://gasprices.aaa.com/?state={urllib.parse.quote(state_code)}"


def state_matches_original_url(original_url: str, state_code: str) -> bool:
    parsed = urllib.parse.urlparse(original_url)
    query = urllib.parse.parse_qs(parsed.query)
    state_values = query.get("state", [])
    if any(value.upper() == state_code.upper() for value in state_values):
        return True
    return f"state={state_code.lower()}" in original_url.lower()


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
    *,
    broad_url_search: bool,
    collapse_digest: bool,
) -> list[dict[str, str]]:
    lookup_url = "gasprices.aaa.com/*" if broad_url_search else aaa_state_url(state_code)
    params = {
        "url": lookup_url,
        "from": lookup_start.strftime("%Y%m%d"),
        "to": lookup_end.strftime("%Y%m%d"),
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
    }
    if collapse_digest:
        params["collapse"] = "digest"
    cdx_url = f"{WAYBACK_CDX_URL}?{urllib.parse.urlencode(params)}"
    payload = aaa.http_get_text(cdx_url, retries=4, timeout=90)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        preview = aaa.normalize_space(payload[:200])
        print(
            f"Warning: {state_code} skipped Wayback CDX range "
            f"{lookup_start} to {lookup_end}: invalid JSON response "
            f"({exc}). Preview: {preview!r}",
            file=sys.stderr,
            flush=True,
        )
        return []
    if not data or len(data) == 1:
        return []

    header = data[0]
    captures = [dict(zip(header, row)) for row in data[1:]]
    html_captures = [
        capture
        for capture in captures
        if capture.get("mimetype", "").startswith("text/html")
    ]
    if broad_url_search:
        html_captures = [
            capture
            for capture in html_captures
            if state_matches_original_url(capture.get("original", ""), state_code)
        ]
    return html_captures


def merge_captures(captures: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    by_timestamp: dict[str, dict[str, str]] = {}
    for capture in captures:
        timestamp = capture.get("timestamp", "")
        if timestamp:
            by_timestamp[timestamp] = capture
    return [by_timestamp[key] for key in sorted(by_timestamp)]


def coalesce_dates_to_ranges(
    dates: Iterable[dt.date],
    *,
    max_range_days: int | None = None,
) -> list[tuple[dt.date, dt.date]]:
    sorted_dates = sorted(set(dates))
    if not sorted_dates:
        return []

    ranges: list[tuple[dt.date, dt.date]] = []
    range_start = sorted_dates[0]
    previous = sorted_dates[0]
    for current in sorted_dates[1:]:
        range_days = (current - range_start).days + 1
        if (
            current == previous + dt.timedelta(days=1)
            and (max_range_days is None or range_days <= max_range_days)
        ):
            previous = current
            continue
        ranges.append((range_start, previous))
        range_start = current
        previous = current
    ranges.append((range_start, previous))
    return ranges


def fetch_targeted_wayback_captures_for_state(
    state_code: str,
    target_capture_dates: set[dt.date],
    *,
    broad_url_search: bool,
    cdx_chunk_days: int,
    sleep_seconds: float,
    verbose: bool,
) -> list[dict[str, str]]:
    captures: list[dict[str, str]] = []
    ranges = coalesce_dates_to_ranges(
        target_capture_dates,
        max_range_days=cdx_chunk_days,
    )
    for index, (range_start, range_end) in enumerate(ranges, start=1):
        try:
            range_captures = fetch_wayback_captures_for_state(
                state_code,
                range_start,
                range_end,
                broad_url_search=False,
                collapse_digest=False,
            )
        except Exception as exc:
            print(
                f"Warning: {state_code} skipped Wayback CDX range "
                f"{range_start} to {range_end}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            range_captures = []
        captures.extend(range_captures)
        if verbose:
            print(
                f"{state_code}: CDX target range {index}/{len(ranges)} "
                f"{range_start} to {range_end}: {len(range_captures)} captures.",
                flush=True,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        if broad_url_search:
            try:
                broad_captures = fetch_wayback_captures_for_state(
                    state_code,
                    range_start,
                    range_end,
                    broad_url_search=True,
                    collapse_digest=False,
                )
            except Exception as exc:
                print(
                    f"Warning: {state_code} skipped broad Wayback CDX range "
                    f"{range_start} to {range_end}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                broad_captures = []
            captures.extend(broad_captures)
            if verbose:
                print(
                    f"{state_code}: broad CDX target range {index}/{len(ranges)} "
                    f"{range_start} to {range_end}: {len(broad_captures)} captures.",
                    flush=True,
                )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    return merge_captures(captures)


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
        likely_price_as_of_dates = [
            missing_date,
            missing_date + dt.timedelta(days=1),
            missing_date + dt.timedelta(days=7),
            aaa.add_months(missing_date, 1),
            *[
                missing_date + dt.timedelta(days=days)
                for days in range(28, 33)
            ],
            aaa.add_months(missing_date, 12),
        ]
        for price_as_of_date in likely_price_as_of_dates:
            for offset_days in range(-window_days, window_days + 1):
                targets.add(price_as_of_date + dt.timedelta(days=offset_days))
    return targets


def wayback_capture_url(
    state_code: str,
    timestamp: str,
    original_url: str | None,
) -> str:
    source_url = original_url or aaa_state_url(state_code)
    return f"https://web.archive.org/web/{timestamp}id_/{source_url}"


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
    broad_url_search: bool,
    cdx_chunk_days: int,
    verbose: bool,
) -> list[aaa.PriceRecord]:
    if target_capture_dates is not None:
        captures = fetch_targeted_wayback_captures_for_state(
            state_code,
            target_capture_dates,
            broad_url_search=broad_url_search,
            cdx_chunk_days=cdx_chunk_days,
            sleep_seconds=sleep_seconds,
            verbose=verbose,
        )
        captures = [
            capture
            for capture in captures
            if capture_date(capture["timestamp"]) in target_capture_dates
        ]
    else:
        captures = fetch_wayback_captures_for_state(
            state_code,
            lookup_start,
            lookup_end,
            broad_url_search=broad_url_search,
            collapse_digest=True,
        )
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
        url = wayback_capture_url(state_code, timestamp, capture.get("original"))
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
    max_capture_date: dt.date,
    broad_wayback_url_search: bool,
    cdx_chunk_days: int,
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
            target_capture_dates = {
                target_date
                for target_date in target_capture_dates
                if target_date <= max_capture_date
            }
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
            lookup_end = min(
                end_date + dt.timedelta(days=wayback_window_days),
                max_capture_date,
            )
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
                broad_url_search=broad_wayback_url_search,
                cdx_chunk_days=cdx_chunk_days,
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
    missing_dates = tuple(
        dt.date.fromisoformat(row[0])
        for row in csv_rows[1:]
        if row[1] == "missing"
    )
    ok_days = expected_days - len(missing_dates)
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
        start_date=effective_start_date,
        end_date=end_date,
        ok_days=ok_days,
        expected_days=expected_days,
        latest_date=latest_date,
        missing_dates=missing_dates,
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


def coverage_report_rows(
    results: Iterable[StateResult],
    baseline_date: dt.date,
) -> list[list[Any]]:
    rows: list[list[Any]] = [
        [
            "state",
            "state_code",
            "start_date",
            "end_date",
            "populated_days",
            "expected_days",
            "coverage_pct",
            "missing_days",
            "baseline_date",
            "baseline_status",
            "baseline_regular",
            "latest_date",
            "latest_regular",
            "dollar_change",
            "percent_change",
            "first_missing_date",
            "last_missing_date",
            "missing_dates",
            "csv_path",
        ]
    ]

    for result in results:
        records = aaa.load_existing_records(result.csv_path)
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

        coverage_pct = (
            result.ok_days / result.expected_days if result.expected_days else 0
        )
        missing_dates = [day.isoformat() for day in result.missing_dates]
        rows.append(
            [
                result.name,
                result.code,
                result.start_date.isoformat(),
                result.end_date.isoformat(),
                result.ok_days,
                result.expected_days,
                format_percent(coverage_pct),
                len(result.missing_dates),
                baseline_date.isoformat(),
                "ok" if baseline_record else "missing",
                format_price(baseline_record.regular if baseline_record else None),
                latest_record.date.isoformat() if latest_record else "",
                format_price(latest_record.regular if latest_record else None),
                format_change(dollar_change),
                format_percent(percent_change),
                missing_dates[0] if missing_dates else "",
                missing_dates[-1] if missing_dates else "",
                ";".join(missing_dates),
                str(result.csv_path),
            ]
        )
    return rows


def write_coverage_report(path: Path, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def sync_state_tabs(
    client: aaa.GoogleApiClient,
    spreadsheet_id: str,
    results: Iterable[StateResult],
    *,
    sheet_write_sleep: float,
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
        if sheet_write_sleep > 0:
            time.sleep(sheet_write_sleep)


def reorder_sheet_tabs(
    client: aaa.GoogleApiClient,
    spreadsheet_id: str,
    comparison_sheet_name: str,
) -> None:
    metadata = client.request_json(
        "GET",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
        params={"fields": "sheets.properties(sheetId,title,index)"},
    )
    existing = sorted(
        (sheet["properties"] for sheet in metadata.get("sheets", [])),
        key=lambda props: int(props.get("index", 0)),
    )
    if not existing:
        return

    existing_by_title = {props["title"]: props for props in existing}
    desired_names = [
        *LEADING_SHEET_NAMES,
        comparison_sheet_name,
        *(state_name for _, state_name in STATES),
    ]
    ordered_names = [name for name in desired_names if name in existing_by_title]
    ordered_set = set(ordered_names)
    ordered_names.extend(
        props["title"] for props in existing if props["title"] not in ordered_set
    )

    current_names = [props["title"] for props in existing]
    if current_names == ordered_names:
        print("Google Sheet tabs already in preferred order.", flush=True)
        return

    requests = []
    for index, name in enumerate(ordered_names):
        requests.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": int(existing_by_title[name]["sheetId"]),
                        "index": index,
                    },
                    "fields": "index",
                }
            }
        )

    client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        body={"requests": requests},
    )
    print("Reordered Google Sheet tabs.", flush=True)


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
        "--max-capture-date",
        type=parse_date_arg,
        default=dt.date.today(),
        help=(
            "Latest Wayback capture date to query. Defaults to today so "
            "future month/year comparison captures are not requested."
        ),
    )
    parser.add_argument(
        "--broad-wayback-url-search",
        action="store_true",
        help=(
            "Also search broader gasprices.aaa.com Wayback captures and "
            "filter for matching state URLs."
        ),
    )
    parser.add_argument(
        "--cdx-chunk-days",
        type=int,
        default=3,
        help=(
            "Maximum days per targeted Wayback CDX lookup. Use 1 for "
            "broad searches if Wayback returns 504s."
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
        "--coverage-report",
        action="store_true",
        help=(
            "Write a state coverage CSV and do not update Google Sheets. "
            "Useful before creating all state tabs."
        ),
    )
    parser.add_argument(
        "--coverage-report-csv",
        type=Path,
        default=DEFAULT_COVERAGE_REPORT_PATH,
        help=(
            "Coverage report CSV path. Defaults to "
            "outputs/aaa_gas_prices/state_coverage_report.csv."
        ),
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
        "--skip-tab-reorder",
        action="store_true",
        help="Do not reorder Google Sheet tabs after syncing.",
    )
    parser.add_argument(
        "--comparison-sheet-name",
        default=STATE_COMPARISON_SHEET_NAME,
        help=(
            "Google Sheet tab name for the state comparison tab. Defaults to "
            f"{STATE_COMPARISON_SHEET_NAME!r}."
        ),
    )
    parser.add_argument(
        "--sheet-write-sleep",
        type=float,
        default=float(
            os.environ.get(
                "AAA_STATE_SHEET_WRITE_SLEEP", DEFAULT_SHEET_WRITE_SLEEP_SECONDS
            )
        ),
        help=(
            "Seconds to pause after each Google Sheets state-tab write. Defaults "
            f"to {DEFAULT_SHEET_WRITE_SLEEP_SECONDS:g}, or AAA_STATE_SHEET_WRITE_SLEEP."
        ),
    )
    parser.add_argument(
        "--report-missing",
        action="store_true",
        help="Print remaining missing dates after each state is processed.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print capture-level backfill progress.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if args.cdx_chunk_days < 1:
        print("Error: --cdx-chunk-days must be at least 1.", file=sys.stderr)
        return 2
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
                    max_capture_date=args.max_capture_date,
                    broad_wayback_url_search=args.broad_wayback_url_search,
                    cdx_chunk_days=args.cdx_chunk_days,
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

    if args.report_missing:
        for result in results:
            if result.missing_dates:
                missing_preview = ", ".join(
                    day.isoformat() for day in result.missing_dates[:40]
                )
                if len(result.missing_dates) > 40:
                    missing_preview += ", ..."
                print(
                    f"{result.code}: {len(result.missing_dates)} missing dates: "
                    f"{missing_preview}",
                    flush=True,
                )
            else:
                print(f"{result.code}: no missing dates.", flush=True)

    if args.coverage_report:
        rows = coverage_report_rows(results, args.baseline_date)
        write_coverage_report(args.coverage_report_csv, rows)
        print(
            f"Wrote coverage report to {args.coverage_report_csv}.",
            flush=True,
        )
        print(
            "Skipped Google Sheets sync because --coverage-report was set.",
            flush=True,
        )
        return 1 if failures else 0

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
        sync_state_tabs(
            client,
            args.spreadsheet_id,
            results,
            sheet_write_sleep=args.sheet_write_sleep,
        )
    if not args.skip_comparison_tab:
        if args.sheet_write_sleep > 0 and not args.skip_state_tabs:
            time.sleep(args.sheet_write_sleep)
        rows = state_comparison_rows(args.states, args.csv_dir, args.baseline_date)
        aaa.update_sheet(client, args.spreadsheet_id, args.comparison_sheet_name, rows)
        print(
            f"Updated Google Sheet tab '{args.comparison_sheet_name}'.",
            flush=True,
        )
    if not args.skip_tab_reorder:
        if args.sheet_write_sleep > 0 and (
            not args.skip_state_tabs or not args.skip_comparison_tab
        ):
            time.sleep(args.sheet_write_sleep)
        reorder_sheet_tabs(client, args.spreadsheet_id, args.comparison_sheet_name)

    if failures:
        print("Completed with state failures:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
