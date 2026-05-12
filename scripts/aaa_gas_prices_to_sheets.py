#!/usr/bin/env python3
"""Scrape AAA national fuel prices and sync them to Google Sheets.

The scraper uses AAA's live public page for daily updates. For historical
backfill, it reads archived copies of that same AAA page from the Internet
Archive Wayback Machine because the live AAA page does not expose an arbitrary
date parameter.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import csv
import datetime as dt
import gzip
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


LIVE_AAA_URL = "https://gasprices.aaa.com/?state=US"
WAYBACK_ORIGINAL_URL = "https://gasprices.aaa.com/"
WAYBACK_CDX_LOOKUP_URL = "gasprices.aaa.com/"
WAYBACK_CDX_URL = "https://web.archive.org/cdx"
WAYBACK_CAPTURE_TEMPLATE = (
    "https://web.archive.org/web/{timestamp}id_/https://gasprices.aaa.com/"
)

DATA_SHEET_NAME = "AAA National Prices"
COMPARISON_SHEET_NAME = "Daily Comparison"
DEFAULT_TITLE = "AAA National Gas Prices"
SPECIAL_COMPARISON_DATE = dt.date(2026, 2, 27)
SPECIAL_COMPARISON_LABEL = "Fri, Feb 27 (day before war starts)"
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

HEADERS = [
    "date",
    "status",
    "regular",
    "mid_grade",
    "premium",
    "diesel",
    "e85",
    "source_kind",
    "source_row",
    "source_price_as_of",
    "capture_timestamp",
    "source_url",
    "scraped_at_utc",
]

SHEET_HEADERS = HEADERS[:7]

FUEL_COLUMNS = {
    "regular": "regular",
    "mid-grade": "mid_grade",
    "midgrade": "mid_grade",
    "mid": "mid_grade",
    "premium": "premium",
    "diesel": "diesel",
    "e85": "e85",
}

ROW_TO_OFFSET = {
    "current avg.": ("Current Avg.", "current"),
    "current avg": ("Current Avg.", "current"),
    "yesterday avg.": ("Yesterday Avg.", "yesterday"),
    "yesterday avg": ("Yesterday Avg.", "yesterday"),
    "week ago avg.": ("Week Ago Avg.", "week"),
    "week ago avg": ("Week Ago Avg.", "week"),
    "month ago avg.": ("Month Ago Avg.", "month"),
    "month ago avg": ("Month Ago Avg.", "month"),
    "year ago avg.": ("Year Ago Avg.", "year"),
    "year ago avg": ("Year Ago Avg.", "year"),
}

ROW_PRIORITY = {
    "Current Avg.": 0,
    "Yesterday Avg.": 1,
    "Week Ago Avg.": 2,
    "Month Ago Avg.": 3,
    "Year Ago Avg.": 4,
}


class ScrapeError(RuntimeError):
    """Raised when a page cannot be parsed into AAA price rows."""


@dataclass(frozen=True)
class PriceRecord:
    date: dt.date
    regular: float | None
    mid_grade: float | None
    premium: float | None
    diesel: float | None
    e85: float | None
    source_kind: str
    source_row: str
    source_price_as_of: dt.date
    capture_timestamp: str
    source_url: str
    scraped_at_utc: str

    def to_row(self) -> list[Any]:
        return [
            self.date.isoformat(),
            "ok",
            price_to_cell(self.regular),
            price_to_cell(self.mid_grade),
            price_to_cell(self.premium),
            price_to_cell(self.diesel),
            price_to_cell(self.e85),
            self.source_kind,
            self.source_row,
            self.source_price_as_of.isoformat(),
            self.capture_timestamp,
            self.source_url,
            self.scraped_at_utc,
        ]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_space(" ".join(self.parts))


class FirstTableParser(HTMLParser):
    """Extract rows from the first AAA price table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.finished = False
        self.depth = 0
        self.current_row: list[str] | None = None
        self.current_cell: list[str] | None = None
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.finished:
            return
        attrs_dict = {k: v or "" for k, v in attrs}
        if tag == "table":
            classes = attrs_dict.get("class", "")
            if self.in_table:
                self.depth += 1
            elif "table-mob" in classes.split():
                self.in_table = True
                self.depth = 1
            return

        if not self.in_table:
            return
        if tag == "tr":
            self.current_row = []
        elif tag in ("td", "th"):
            self.current_cell = []

    def handle_data(self, data: str) -> None:
        if self.in_table and self.current_cell is not None:
            self.current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.finished or not self.in_table:
            return
        if tag in ("td", "th") and self.current_cell is not None:
            cell = normalize_space(" ".join(self.current_cell))
            if self.current_row is not None:
                self.current_row.append(cell)
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            if any(self.current_row):
                self.rows.append(self.current_row)
            self.current_row = None
        elif tag == "table":
            self.depth -= 1
            if self.depth <= 0:
                self.in_table = False
                self.finished = True


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def price_to_cell(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def parse_price(value: str) -> float | None:
    cleaned = value.replace("$", "").replace(",", "").strip()
    if not cleaned or cleaned in {"-", "n/a", "N/A"}:
        return None
    return float(cleaned)


def parse_aaa_date(value: str) -> dt.date:
    parsed = dt.datetime.strptime(value.strip(), "%m/%d/%y")
    return parsed.date()


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def offset_date(price_as_of: dt.date, offset_name: str) -> dt.date:
    if offset_name == "current":
        return price_as_of
    if offset_name == "yesterday":
        return price_as_of - dt.timedelta(days=1)
    if offset_name == "week":
        return price_as_of - dt.timedelta(days=7)
    if offset_name == "month":
        return add_months(price_as_of, -1)
    if offset_name == "year":
        return add_months(price_as_of, -12)
    raise ValueError(f"Unknown row offset: {offset_name}")


def parse_aaa_page(
    page_html: str,
    *,
    source_kind: str,
    capture_timestamp: str,
    source_url: str,
    scraped_at_utc: str,
    start_date: dt.date,
    end_date: dt.date,
) -> list[PriceRecord]:
    text_parser = TextExtractor()
    text_parser.feed(page_html)
    page_text = text_parser.text()
    date_match = re.search(r"Price as of\s+(\d{1,2}/\d{1,2}/\d{2})", page_text)
    if not date_match:
        raise ScrapeError("Could not find AAA 'Price as of' date.")
    price_as_of = parse_aaa_date(date_match.group(1))

    table_parser = FirstTableParser()
    table_parser.feed(page_html)
    rows = table_parser.rows
    if not rows:
        raise ScrapeError("Could not find AAA national price table.")

    header_index = next(
        (
            i
            for i, row in enumerate(rows)
            if any(cell.lower() == "regular" for cell in row)
            and any(cell.lower() == "diesel" for cell in row)
        ),
        None,
    )
    if header_index is None:
        raise ScrapeError("Could not find AAA fuel header row.")

    header = rows[header_index]
    fuel_indexes: dict[int, str] = {}
    for idx, cell in enumerate(header):
        normalized = cell.lower().replace(" ", "").replace("-", "-")
        normalized = normalized.replace("mid-grade", "mid-grade")
        fuel = FUEL_COLUMNS.get(cell.lower()) or FUEL_COLUMNS.get(normalized)
        if fuel:
            fuel_indexes[idx] = fuel

    if not {"regular", "mid_grade", "premium", "diesel"}.issubset(
        set(fuel_indexes.values())
    ):
        raise ScrapeError(f"AAA fuel columns were incomplete: {header!r}")

    records: list[PriceRecord] = []
    for row in rows[header_index + 1 :]:
        if not row:
            continue
        label_key = row[0].lower()
        if label_key not in ROW_TO_OFFSET:
            continue
        source_row, offset_name = ROW_TO_OFFSET[label_key]
        record_date = offset_date(price_as_of, offset_name)
        if not (start_date <= record_date <= end_date):
            continue

        values: dict[str, float | None] = {
            "regular": None,
            "mid_grade": None,
            "premium": None,
            "diesel": None,
            "e85": None,
        }
        for idx, fuel_name in fuel_indexes.items():
            if idx < len(row):
                values[fuel_name] = parse_price(row[idx])

        records.append(
            PriceRecord(
                date=record_date,
                regular=values["regular"],
                mid_grade=values["mid_grade"],
                premium=values["premium"],
                diesel=values["diesel"],
                e85=values["e85"],
                source_kind=source_kind,
                source_row=source_row,
                source_price_as_of=price_as_of,
                capture_timestamp=capture_timestamp,
                source_url=source_url,
                scraped_at_utc=scraped_at_utc,
            )
        )

    return records


def http_request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> tuple[bytes, dict[str, str]]:
    request_headers = {
        "User-Agent": "aaa-gas-price-scraper/1.0 (+local data sync)",
        "Accept": "application/json,text/html,*/*",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers=request_headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        response_headers = {k.lower(): v for k, v in resp.headers.items()}
    if response_headers.get("content-encoding", "").lower() == "gzip":
        raw = gzip.decompress(raw)
    return raw, response_headers


def http_get_text(url: str, *, retries: int = 3, timeout: int = 45) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            if urllib.parse.urlparse(url).netloc == "web.archive.org":
                return curl_get_text(url, timeout=timeout)
            raw, headers = http_request(url, timeout=timeout)
            charset = "utf-8"
            content_type = headers.get("content-type", "")
            charset_match = re.search(r"charset=([^;\s]+)", content_type)
            if charset_match:
                charset = charset_match.group(1)
            return raw.decode(charset, errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt)
    raise RuntimeError(f"GET failed after {retries} attempts: {url}") from last_error


def curl_get_text(url: str, *, timeout: int) -> str:
    result = subprocess.run(
        [
            "curl",
            "-L",
            "--compressed",
            "-sS",
            "--max-time",
            str(timeout),
            "-A",
            "aaa-gas-price-scraper/1.0 (+local data sync)",
            url,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"curl failed with exit code {result.returncode}")
    if not result.stdout:
        raise RuntimeError("empty response")
    return result.stdout.decode("utf-8", errors="replace")


def fetch_live_records(start_date: dt.date, end_date: dt.date) -> list[PriceRecord]:
    scraped_at = utc_now()
    page = http_get_text(LIVE_AAA_URL)
    return parse_aaa_page(
        page,
        source_kind="live",
        capture_timestamp=scraped_at,
        source_url=LIVE_AAA_URL,
        scraped_at_utc=scraped_at,
        start_date=start_date,
        end_date=end_date,
    )


def fetch_wayback_captures(
    start_date: dt.date,
    end_date: dt.date,
) -> list[dict[str, str]]:
    params = {
        "url": WAYBACK_CDX_LOOKUP_URL,
        "from": start_date.strftime("%Y%m%d"),
        "to": end_date.strftime("%Y%m%d"),
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
    }
    cdx_url = f"{WAYBACK_CDX_URL}?{urllib.parse.urlencode(params)}"
    payload = http_get_text(cdx_url, retries=4, timeout=90)
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


def fetch_wayback_records(
    start_date: dt.date,
    end_date: dt.date,
    *,
    sleep_seconds: float,
    capture_retries: int,
    capture_timeout: int,
    limit_captures: int | None,
    target_capture_dates: set[dt.date] | None,
    verbose: bool,
) -> list[PriceRecord]:
    captures = fetch_wayback_captures(start_date, end_date)
    if target_capture_dates is not None:
        captures = [
            capture
            for capture in captures
            if capture_date(capture["timestamp"]) in target_capture_dates
        ]
    if limit_captures:
        captures = captures[:limit_captures]
    if verbose:
        print(f"Found {len(captures)} Wayback captures to inspect.", flush=True)

    records: list[PriceRecord] = []
    for index, capture in enumerate(captures, start=1):
        timestamp = capture["timestamp"]
        url = WAYBACK_CAPTURE_TEMPLATE.format(timestamp=timestamp)
        try:
            page = http_get_text(url, retries=capture_retries, timeout=capture_timeout)
            records.extend(
                parse_aaa_page(
                    page,
                    source_kind="wayback",
                    capture_timestamp=timestamp,
                    source_url=url,
                    scraped_at_utc=utc_now(),
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            if verbose:
                print(
                    f"Parsed Wayback capture {index}/{len(captures)}: {timestamp}",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"Warning: skipped Wayback capture {timestamp}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return records


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def capture_date(timestamp: str) -> dt.date:
    return dt.datetime.strptime(timestamp[:8], "%Y%m%d").date()


def record_rank(record: PriceRecord) -> tuple[int, int]:
    row_rank = ROW_PRIORITY.get(record.source_row, 9)
    source_rank = 0 if record.source_kind == "live" else 1
    return (row_rank, source_rank)


def is_better_record(candidate: PriceRecord, current: PriceRecord) -> bool:
    candidate_rank = record_rank(candidate)
    current_rank = record_rank(current)
    if candidate_rank != current_rank:
        return candidate_rank < current_rank
    return candidate.capture_timestamp > current.capture_timestamp


def merge_records(records: Iterable[PriceRecord]) -> list[PriceRecord]:
    best: dict[dt.date, PriceRecord] = {}
    for record in records:
        current = best.get(record.date)
        if current is None:
            best[record.date] = record
            continue
        if is_better_record(record, current):
            best[record.date] = record
    return [best[key] for key in sorted(best)]


def load_existing_records(path: Path) -> list[PriceRecord]:
    if not path.exists():
        return []
    records: list[PriceRecord] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") != "ok":
                continue
            records.append(
                PriceRecord(
                    date=dt.date.fromisoformat(row["date"]),
                    regular=parse_optional_float(row.get("regular")),
                    mid_grade=parse_optional_float(row.get("mid_grade")),
                    premium=parse_optional_float(row.get("premium")),
                    diesel=parse_optional_float(row.get("diesel")),
                    e85=parse_optional_float(row.get("e85")),
                    source_kind=row.get("source_kind", "cache"),
                    source_row=row.get("source_row", ""),
                    source_price_as_of=dt.date.fromisoformat(row["source_price_as_of"]),
                    capture_timestamp=row.get("capture_timestamp", ""),
                    source_url=row.get("source_url", ""),
                    scraped_at_utc=row.get("scraped_at_utc", ""),
                )
            )
    return records


def load_missing_dates(path: Path) -> set[dt.date]:
    if not path.exists():
        return set()
    missing: set[dt.date] = set()
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("status") == "missing":
                missing.add(dt.date.fromisoformat(row["date"]))
    return missing


def target_capture_dates_for_missing(missing_dates: Iterable[dt.date]) -> set[dt.date]:
    targets: set[dt.date] = set()
    for missing_date in missing_dates:
        likely_price_as_of_dates = {
            missing_date,
            missing_date + dt.timedelta(days=1),
            missing_date + dt.timedelta(days=7),
            add_months(missing_date, 1),
        }
        for price_as_of_date in likely_price_as_of_dates:
            targets.update(
                {
                    price_as_of_date - dt.timedelta(days=1),
                    price_as_of_date,
                    price_as_of_date + dt.timedelta(days=1),
                }
            )
    return targets


def parse_optional_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def date_range(start_date: dt.date, end_date: dt.date) -> Iterable[dt.date]:
    current = start_date
    while current <= end_date:
        yield current
        current += dt.timedelta(days=1)


def records_to_sheet_rows(
    records: list[PriceRecord],
    start_date: dt.date,
    end_date: dt.date,
) -> list[list[Any]]:
    by_date = {record.date: record for record in records}
    rows: list[list[Any]] = [HEADERS]
    for day in date_range(start_date, end_date):
        record = by_date.get(day)
        if record:
            rows.append(record.to_row())
        else:
            rows.append([day.isoformat(), "missing", "", "", "", "", "", "", "", "", "", "", ""])
    return rows


def records_to_google_sheet_rows(
    records: list[PriceRecord],
    start_date: dt.date,
    end_date: dt.date,
) -> list[list[Any]]:
    rows = records_to_sheet_rows(records, start_date, end_date)
    return [SHEET_HEADERS] + [
        row[: len(SHEET_HEADERS)] for row in reversed(rows[1:])
    ]


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class GoogleApiClient:
    def __init__(self, credentials_path: Path, scopes: tuple[str, ...]) -> None:
        if not credentials_path.exists():
            raise FileNotFoundError(f"Google credentials not found: {credentials_path}")
        self.credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        self.scopes = scopes
        self._token: str | None = None
        self._expires_at = 0

    def token(self) -> str:
        now = int(time.time())
        if self._token and now < self._expires_at - 60:
            return self._token

        header = {"alg": "RS256", "typ": "JWT"}
        claim = {
            "iss": self.credentials["client_email"],
            "scope": " ".join(self.scopes),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = (
            f"{b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{b64url(json.dumps(claim, separators=(',', ':')).encode())}"
        )
        signature = sign_rs256(self.credentials["private_key"], signing_input)
        assertion = f"{signing_input}.{b64url(signature)}"

        body = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            }
        ).encode("utf-8")
        raw, _ = http_request(
            "https://oauth2.googleapis.com/token",
            method="POST",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = json.loads(raw.decode("utf-8"))
        self._token = payload["access_token"]
        self._expires_at = now + int(payload.get("expires_in", 3600))
        return self._token

    def request_json(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request_body = None
        headers = {"Authorization": f"Bearer {self.token()}"}
        if body is not None:
            request_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            raw, _ = http_request(url, method=method, body=request_body, headers=headers)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Google API {method} {url} failed: {detail}") from exc
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))


def sign_rs256(private_key: str, signing_input: str) -> bytes:
    key_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
            key_path = handle.name
            handle.write(private_key)
        os.chmod(key_path, 0o600)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout
    finally:
        if key_path:
            try:
                os.unlink(key_path)
            except OSError:
                pass


def create_spreadsheet(client: GoogleApiClient, title: str) -> tuple[str, str]:
    payload = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": DATA_SHEET_NAME}}],
    }
    response = client.request_json(
        "POST",
        "https://sheets.googleapis.com/v4/spreadsheets",
        body=payload,
    )
    return response["spreadsheetId"], response.get("spreadsheetUrl", "")


def ensure_sheet(client: GoogleApiClient, spreadsheet_id: str, sheet_name: str) -> int:
    metadata = client.request_json(
        "GET",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
        params={"fields": "sheets.properties(sheetId,title)"},
    )
    for sheet in metadata.get("sheets", []):
        props = sheet["properties"]
        if props["title"] == sheet_name:
            return int(props["sheetId"])

    response = client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        body={"requests": [{"addSheet": {"properties": {"title": sheet_name}}}]},
    )
    return int(response["replies"][0]["addSheet"]["properties"]["sheetId"])


def update_sheet(
    client: GoogleApiClient,
    spreadsheet_id: str,
    sheet_name: str,
    rows: list[list[Any]],
) -> None:
    sheet_id = ensure_sheet(client, spreadsheet_id, sheet_name)
    encoded_range = urllib.parse.quote(f"'{sheet_name}'!A:Z", safe="")
    client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}:clear",
        body={},
    )

    update_range = urllib.parse.quote(f"'{sheet_name}'!A1", safe="")
    client.request_json(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{update_range}",
        params={"valueInputOption": "RAW"},
        body={"values": rows},
    )

    client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                        },
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": len(rows[0]) if rows else len(SHEET_HEADERS),
                        }
                    }
                },
            ]
        },
    )


def short_date_label(value: dt.date) -> str:
    return f"{value.month}/{value.day}/{str(value.year)[-2:]}"


def comparison_value(
    current_value: float | None,
    compare_value: float | None,
    *,
    mode: str,
) -> float | str:
    if current_value is None or compare_value is None:
        return ""
    difference = current_value - compare_value
    if mode == "difference":
        return difference
    if mode == "percent":
        return "" if compare_value == 0 else difference / compare_value
    raise ValueError(f"Unknown comparison mode: {mode}")


def comparison_sheet_rows(records: list[PriceRecord]) -> list[list[Any]]:
    by_date = {record.date: record for record in records}
    latest_date = max(by_date)
    latest_record = by_date[latest_date]
    rows: list[list[Any]] = [
        [
            "",
            "Regular",
            "Dif from today",
            "% change",
            "",
            "Diesel",
            "Dif from today",
            "% change",
        ],
        [
            short_date_label(latest_date),
            latest_record.regular,
            "",
            "",
            "",
            latest_record.diesel,
            "",
            "",
        ],
    ]

    comparisons = [
        ("Yesterday Avg.", latest_date - dt.timedelta(days=1)),
        ("Week Ago Avg.", latest_date - dt.timedelta(days=7)),
        (SPECIAL_COMPARISON_LABEL, SPECIAL_COMPARISON_DATE),
        ("Month Ago Avg.", add_months(latest_date, -1)),
        ("Year Ago Avg.", add_months(latest_date, -12)),
    ]
    for label, comparison_date in comparisons:
        comparison_record = by_date.get(comparison_date)
        regular = comparison_record.regular if comparison_record else None
        diesel = comparison_record.diesel if comparison_record else None
        rows.append(
            [
                label,
                regular if regular is not None else "",
                comparison_value(latest_record.regular, regular, mode="difference"),
                comparison_value(latest_record.regular, regular, mode="percent"),
                "",
                diesel if diesel is not None else "",
                comparison_value(latest_record.diesel, diesel, mode="difference"),
                comparison_value(latest_record.diesel, diesel, mode="percent"),
            ]
        )
    return rows


def update_comparison_sheet(
    client: GoogleApiClient,
    spreadsheet_id: str,
    records: list[PriceRecord],
) -> None:
    sheet_id = ensure_sheet(client, spreadsheet_id, COMPARISON_SHEET_NAME)
    rows = comparison_sheet_rows(records)
    encoded_range = urllib.parse.quote(f"'{COMPARISON_SHEET_NAME}'!A:H", safe="")
    client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{encoded_range}:clear",
        body={},
    )

    update_range = urllib.parse.quote(f"'{COMPARISON_SHEET_NAME}'!A1", safe="")
    client.request_json(
        "PUT",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{update_range}",
        params={"valueInputOption": "USER_ENTERED"},
        body={"values": rows},
    )

    light_grid = {"red": 0.88, "green": 0.88, "blue": 0.88}
    client.request_json(
        "POST",
        f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}:batchUpdate",
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "gridProperties": {
                                "rowCount": 20,
                                "columnCount": 8,
                                "frozenRowCount": 1,
                            },
                        },
                        "fields": "gridProperties(rowCount,columnCount,frozenRowCount)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 7,
                            "startColumnIndex": 0,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"fontSize": 14},
                                "verticalAlignment": "MIDDLE",
                            }
                        },
                        "fields": "userEnteredFormat(textFormat.fontSize,verticalAlignment)",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 1,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {
                                    "red": 0.96,
                                    "green": 0.96,
                                    "blue": 0.96,
                                },
                                "horizontalAlignment": "CENTER",
                                "textFormat": {"bold": True, "fontSize": 14},
                            }
                        },
                        "fields": (
                            "userEnteredFormat(backgroundColor,"
                            "horizontalAlignment,textFormat)"
                        ),
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 7,
                            "startColumnIndex": 0,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "horizontalAlignment": "LEFT",
                            }
                        },
                        "fields": "userEnteredFormat.horizontalAlignment",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 7,
                            "startColumnIndex": 1,
                            "endColumnIndex": 3,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "CURRENCY", "pattern": "$0.000"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 7,
                            "startColumnIndex": 5,
                            "endColumnIndex": 7,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "CURRENCY", "pattern": "$0.000"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": 2,
                            "startColumnIndex": 0,
                            "endColumnIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "DATE", "pattern": "m/d/yy"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 2,
                            "endRowIndex": 3,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "PERCENT", "pattern": "0.00%"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 3,
                            "endRowIndex": 7,
                            "startColumnIndex": 3,
                            "endColumnIndex": 4,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 2,
                            "endRowIndex": 7,
                            "startColumnIndex": 7,
                            "endColumnIndex": 8,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "PERCENT", "pattern": "0.0%"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                },
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 7,
                            "startColumnIndex": 0,
                            "endColumnIndex": 8,
                        },
                        "top": {"style": "SOLID", "width": 1, "color": light_grid},
                        "bottom": {"style": "SOLID", "width": 1, "color": light_grid},
                        "left": {"style": "SOLID", "width": 1, "color": light_grid},
                        "right": {"style": "SOLID", "width": 1, "color": light_grid},
                        "innerHorizontal": {
                            "style": "SOLID",
                            "width": 1,
                            "color": light_grid,
                        },
                        "innerVertical": {
                            "style": "SOLID",
                            "width": 1,
                            "color": light_grid,
                        },
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"pixelSize": 360},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 1,
                            "endIndex": 2,
                        },
                        "properties": {"pixelSize": 110},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 3,
                        },
                        "properties": {"pixelSize": 165},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 3,
                            "endIndex": 4,
                        },
                        "properties": {"pixelSize": 130},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 4,
                            "endIndex": 5,
                        },
                        "properties": {"pixelSize": 34},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 5,
                            "endIndex": 6,
                        },
                        "properties": {"pixelSize": 110},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 6,
                            "endIndex": 7,
                        },
                        "properties": {"pixelSize": 165},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 7,
                            "endIndex": 8,
                        },
                        "properties": {"pixelSize": 130},
                        "fields": "pixelSize",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 7,
                        },
                        "properties": {"pixelSize": 38},
                        "fields": "pixelSize",
                    }
                },
            ]
        },
    )


def share_spreadsheet(
    client: GoogleApiClient,
    spreadsheet_id: str,
    email_address: str,
) -> None:
    client.request_json(
        "POST",
        f"https://www.googleapis.com/drive/v3/files/{spreadsheet_id}/permissions",
        params={"sendNotificationEmail": "true"},
        body={"type": "user", "role": "writer", "emailAddress": email_address},
    )


def parse_date_arg(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def default_start_date() -> dt.date:
    today = dt.date.today()
    return dt.date(today.year, 1, 1)


def default_csv_path() -> Path:
    return Path("outputs/aaa_gas_prices/aaa_national_gas_prices.csv")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape AAA national gas prices and sync them to Google Sheets."
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Fetch historical AAA page captures from Wayback before syncing.",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="When backfilling, only inspect Wayback captures likely to fill missing CSV dates.",
    )
    parser.add_argument(
        "--start-date",
        type=parse_date_arg,
        default=None,
        help=(
            "Backfill start date in YYYY-MM-DD format. Defaults to the earliest "
            "cached CSV date, or Jan 1 of this year if there is no cache."
        ),
    )
    parser.add_argument(
        "--end-date",
        type=parse_date_arg,
        default=dt.date.today(),
        help="End date in YYYY-MM-DD format. Defaults to today.",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=default_csv_path(),
        help="Local CSV cache/output path.",
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
        help="Existing Google Sheet ID. If omitted, the script creates a new spreadsheet.",
    )
    parser.add_argument(
        "--share-with",
        default=os.environ.get("GOOGLE_SHARE_WITH_EMAIL", ""),
        help="Email address to share a newly created spreadsheet with.",
    )
    parser.add_argument(
        "--title",
        default=os.environ.get("GOOGLE_SHEET_TITLE", DEFAULT_TITLE),
        help="Title to use when creating a new spreadsheet.",
    )
    parser.add_argument(
        "--skip-google",
        action="store_true",
        help="Only write the local CSV; do not create or update Google Sheets.",
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
        help="Retries per Wayback capture. Keep low so one slow capture cannot stall the run.",
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
        help="Debug option: inspect only the first N Wayback captures.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print capture-level backfill progress.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    all_records: list[PriceRecord] = []
    cached = load_existing_records(args.csv)
    if cached:
        all_records.extend(cached)
        print(f"Loaded {len(cached)} cached records from {args.csv}.", flush=True)

    if args.start_date is None:
        args.start_date = min(record.date for record in cached) if cached else default_start_date()

    if args.end_date < args.start_date:
        print("Error: --end-date must be on or after --start-date.", file=sys.stderr)
        return 2

    if args.backfill:
        target_capture_dates = None
        if args.only_missing:
            missing_dates = {
                missing_date
                for missing_date in load_missing_dates(args.csv)
                if args.start_date <= missing_date <= args.end_date
            }
            target_capture_dates = target_capture_dates_for_missing(missing_dates)
            print(
                f"Targeting {len(missing_dates)} missing dates with "
                f"{len(target_capture_dates)} likely capture dates.",
                flush=True,
            )
        print(
            f"Backfilling AAA records from {args.start_date} to {args.end_date}.",
            flush=True,
        )
        all_records.extend(
            fetch_wayback_records(
                args.start_date,
                args.end_date,
                sleep_seconds=args.sleep,
                capture_retries=args.capture_retries,
                capture_timeout=args.capture_timeout,
                limit_captures=args.limit_captures,
                target_capture_dates=target_capture_dates,
                verbose=args.verbose,
            )
        )

    print("Fetching latest AAA national fuel prices.", flush=True)
    all_records.extend(fetch_live_records(args.start_date, args.end_date))

    merged = merge_records(all_records)
    csv_rows = records_to_sheet_rows(merged, args.start_date, args.end_date)
    sheet_rows = records_to_google_sheet_rows(merged, args.start_date, args.end_date)
    write_csv(args.csv, csv_rows)

    expected_days = (args.end_date - args.start_date).days + 1
    ok_days = sum(1 for row in csv_rows[1:] if row[1] == "ok")
    missing_days = expected_days - ok_days
    print(f"Wrote {args.csv} with {ok_days}/{expected_days} populated days.", flush=True)
    if missing_days:
        print(f"Missing days are included as blank rows: {missing_days}.", flush=True)

    if args.skip_google:
        print("Skipped Google Sheets sync because --skip-google was set.", flush=True)
        return 0

    if args.credentials is None:
        print(
            "Error: provide --credentials or set GOOGLE_APPLICATION_CREDENTIALS "
            "to your service account JSON path.",
            file=sys.stderr,
        )
        return 2

    client = GoogleApiClient(args.credentials, SCOPES)
    spreadsheet_id = args.spreadsheet_id
    spreadsheet_url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        if spreadsheet_id
        else ""
    )
    created = False
    if not spreadsheet_id:
        spreadsheet_id, spreadsheet_url = create_spreadsheet(client, args.title)
        created = True
        print(f"Created spreadsheet: {spreadsheet_url}", flush=True)

    update_sheet(client, spreadsheet_id, DATA_SHEET_NAME, sheet_rows)
    print(f"Updated Google Sheet tab '{DATA_SHEET_NAME}'.", flush=True)
    update_comparison_sheet(client, spreadsheet_id, merged)
    print(f"Updated Google Sheet tab '{COMPARISON_SHEET_NAME}'.", flush=True)

    if created and args.share_with:
        share_spreadsheet(client, spreadsheet_id, args.share_with)
        print(f"Shared spreadsheet with {args.share_with}.", flush=True)
    elif created:
        print(
            "Note: this spreadsheet is owned by the service account. Set "
            "GOOGLE_SHARE_WITH_EMAIL or pass --share-with to share it with yourself.",
            flush=True,
        )

    print(f"Spreadsheet ID: {spreadsheet_id}", flush=True)
    if spreadsheet_url:
        print(f"Spreadsheet URL: {spreadsheet_url}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
