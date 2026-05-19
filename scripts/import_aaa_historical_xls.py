#!/usr/bin/env python3
"""Import fully populated AAA historical fuel rows from a legacy .xls file.

The source workbook is an old binary Excel file, so this script includes a
small BIFF/Compound File reader for the simple table shape used by AAA's
FuelGaugeHistory export. It writes rows using the same CSV schema as
aaa_gas_prices_to_sheets.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import struct
import sys
from pathlib import Path
from typing import Any

import aaa_gas_prices_to_sheets as aaa


DEFAULT_SOURCE = Path(
    "/Users/207000019@bwt3.com/Downloads/FuelGaugeHistory 2017 to 2016_0410.xls"
)


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def i32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


class XlsReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        if self.data[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
            raise ValueError(f"Not a legacy .xls Compound File: {path}")

        self.sector_size = 1 << u16(self.data, 30)
        self.first_directory_sector = i32(self.data, 48)
        self.fat = self._read_fat()

    def _sector(self, index: int) -> bytes:
        offset = (index + 1) * self.sector_size
        return self.data[offset : offset + self.sector_size]

    def _read_fat(self) -> list[int]:
        difat = [
            sector_index
            for sector_index in struct.unpack_from("<109i", self.data, 76)
            if sector_index >= 0
        ]
        fat: list[int] = []
        for fat_sector in difat:
            sector = self._sector(fat_sector)
            fat.extend(struct.unpack_from(f"<{self.sector_size // 4}i", sector, 0))
        return fat

    def _chain(self, start_sector: int) -> list[int]:
        chain: list[int] = []
        seen: set[int] = set()
        sector = start_sector
        while sector >= 0 and sector not in seen and sector < len(self.fat):
            seen.add(sector)
            chain.append(sector)
            next_sector = self.fat[sector]
            if next_sector in (-1, -2, -3, -4):
                break
            sector = next_sector
        return chain

    def _stream(self, start_sector: int, size: int) -> bytes:
        return b"".join(self._sector(sector) for sector in self._chain(start_sector))[
            :size
        ]

    def workbook_stream(self) -> bytes:
        directory = self._stream(self.first_directory_sector, 1_000_000)
        for offset in range(0, len(directory) - 127, 128):
            entry = directory[offset : offset + 128]
            name_length = u16(entry, 64)
            name = (
                entry[: name_length - 2].decode("utf-16le", errors="ignore")
                if name_length >= 2
                else ""
            )
            if name in ("Workbook", "Book"):
                return self._stream(i32(entry, 116), u64(entry, 120))
        raise ValueError("Workbook stream was not found in .xls file.")


def parse_biff_records(workbook: bytes) -> list[tuple[int, int, bytes]]:
    records: list[tuple[int, int, bytes]] = []
    offset = 0
    while offset + 4 <= len(workbook):
        record_id = u16(workbook, offset)
        length = u16(workbook, offset + 2)
        records.append((offset, record_id, workbook[offset + 4 : offset + 4 + length]))
        offset += 4 + length
    return records


def parse_sheet_names(records: list[tuple[int, int, bytes]]) -> dict[int, str]:
    sheets: dict[int, str] = {}
    for offset, record_id, payload in records:
        if record_id != 0x0085 or len(payload) < 8:
            continue
        stream_position = u32(payload, 0)
        char_count = payload[6]
        flags = payload[7]
        raw_name = payload[8:]
        if flags & 1:
            name = raw_name[: char_count * 2].decode("utf-16le", errors="ignore")
        else:
            name = raw_name[:char_count].decode("latin1", errors="ignore")
        sheets[stream_position] = name
    return sheets


def parse_shared_strings(records: list[tuple[int, int, bytes]]) -> list[str]:
    chunks: list[bytes] = []
    collecting = False
    for _offset, record_id, payload in records:
        if record_id == 0x00FC:
            chunks = [payload]
            collecting = True
        elif collecting and record_id == 0x003C:
            chunks.append(payload)
        elif collecting:
            collecting = False

    if not chunks:
        return []

    data = b"".join(chunks)
    unique_count = u32(data, 4)
    offset = 8
    strings: list[str] = []
    for _ in range(unique_count):
        if offset + 3 > len(data):
            break
        char_count = u16(data, offset)
        flags = data[offset + 2]
        offset += 3
        has_rich_text = flags & 0x08
        has_extension = flags & 0x04
        is_utf16 = flags & 0x01
        rich_text_runs = 0
        extension_length = 0
        if has_rich_text:
            rich_text_runs = u16(data, offset)
            offset += 2
        if has_extension:
            extension_length = u32(data, offset)
            offset += 4

        byte_count = char_count * (2 if is_utf16 else 1)
        raw_value = data[offset : offset + byte_count]
        offset += byte_count
        strings.append(
            raw_value.decode("utf-16le" if is_utf16 else "latin1", errors="ignore")
        )
        offset += rich_text_runs * 4 + extension_length
    return strings


def decode_rk(value: int) -> float:
    multiplied_by_100 = value & 1
    is_integer = value & 2
    raw = value & 0xFFFFFFFC
    if is_integer:
        decoded = struct.unpack("<i", struct.pack("<I", raw))[0] >> 2
    else:
        decoded = struct.unpack("<d", struct.pack("<II", 0, raw))[0]
    return decoded / 100.0 if multiplied_by_100 else decoded


def worksheet_cells(path: Path, sheet_name: str) -> dict[tuple[int, int], Any]:
    reader = XlsReader(path)
    records = parse_biff_records(reader.workbook_stream())
    sheet_names = parse_sheet_names(records)
    shared_strings = parse_shared_strings(records)

    current_sheet: str | None = None
    cells: dict[tuple[int, int], Any] = {}
    for offset, record_id, payload in records:
        if record_id == 0x0809 and len(payload) >= 4 and u16(payload, 2) == 0x0010:
            current_sheet = sheet_names.get(offset)
        elif record_id == 0x000A:
            current_sheet = None
        elif current_sheet != sheet_name:
            continue
        elif record_id == 0x00FD and len(payload) >= 10:
            row, col, _xf, string_index = struct.unpack_from("<HHHI", payload, 0)
            cells[(row, col)] = (
                shared_strings[string_index]
                if string_index < len(shared_strings)
                else f"<sst {string_index}>"
            )
        elif record_id == 0x0203 and len(payload) >= 14:
            row, col, _xf = struct.unpack_from("<HHH", payload, 0)
            cells[(row, col)] = struct.unpack_from("<d", payload, 6)[0]
        elif record_id == 0x027E and len(payload) >= 10:
            row, col, _xf, rk_value = struct.unpack_from("<HHHI", payload, 0)
            cells[(row, col)] = decode_rk(rk_value)
        elif record_id == 0x00BD and len(payload) >= 6:
            row, first_col = struct.unpack_from("<HH", payload, 0)
            last_col = payload[-1]
            data_offset = 4
            col = first_col
            while data_offset + 6 <= len(payload) - 1 and col <= last_col:
                rk_value = u32(payload, data_offset + 2)
                cells[(row, col)] = decode_rk(rk_value)
                data_offset += 6
                col += 1
        elif record_id == 0x0006 and len(payload) >= 14:
            row, col, _xf = struct.unpack_from("<HHH", payload, 0)
            value = struct.unpack_from("<d", payload, 6)[0]
            if math.isfinite(value):
                cells[(row, col)] = value
    return cells


def parse_excel_date(value: Any) -> dt.date | None:
    if isinstance(value, str):
        try:
            return dt.datetime.strptime(value, "%m/%d/%Y").date()
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value > 20000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(value))
    return None


def parse_optional_price(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def historical_records(
    path: Path,
    sheet_name: str,
    *,
    allow_missing_e85: bool = False,
) -> list[aaa.PriceRecord]:
    cells = worksheet_cells(path, sheet_name)
    rows = sorted({row for row, _col in cells if row >= 2})
    source_mtime = (
        dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
    imported_at = aaa.utc_now()
    records: list[aaa.PriceRecord] = []
    for row in rows:
        row_date = parse_excel_date(cells.get((row, 0))) or parse_excel_date(
            cells.get((row, 1))
        )
        prices = [parse_optional_price(cells.get((row, col))) for col in range(2, 7)]
        required_prices = prices[:4] if allow_missing_e85 else prices
        if row_date is None or any(price is None for price in required_prices):
            continue
        records.append(
            aaa.PriceRecord(
                date=row_date,
                regular=prices[0],
                mid_grade=prices[1],
                premium=prices[2],
                diesel=prices[3],
                e85=prices[4],
                source_kind="historical_xls",
                source_row="Fuel Gauge History",
                source_price_as_of=row_date,
                capture_timestamp=source_mtime,
                source_url=str(path),
                scraped_at_utc=imported_at,
            )
        )
    return records


def merge_prefer_historical(
    existing_records: list[aaa.PriceRecord], imported_records: list[aaa.PriceRecord]
) -> list[aaa.PriceRecord]:
    by_date = {record.date: record for record in existing_records}
    for record in imported_records:
        by_date[record.date] = record
    return [by_date[date] for date in sorted(by_date)]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import complete AAA historical rows from a FuelGaugeHistory .xls file."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--sheet-name", default="Fuel Gauge History")
    parser.add_argument("--csv", type=Path, default=aaa.default_csv_path())
    parser.add_argument(
        "--allow-missing-e85",
        action="store_true",
        help=(
            "Import rows where regular, midgrade, premium, and diesel are populated "
            "even when E85 is blank."
        ),
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if not args.source.exists():
        print(f"Source file not found: {args.source}", file=sys.stderr)
        return 2

    existing = aaa.load_existing_records(args.csv)
    imported = historical_records(
        args.source,
        args.sheet_name,
        allow_missing_e85=args.allow_missing_e85,
    )
    if not imported:
        print("No fully populated historical rows found.", file=sys.stderr)
        return 1

    merged = merge_prefer_historical(existing, imported)
    start_date = min(record.date for record in merged)
    end_date = max(record.date for record in merged)
    rows = aaa.records_to_sheet_rows(merged, start_date, end_date)
    aaa.write_csv(args.csv, rows)

    print(f"Imported fully populated rows: {len(imported)}")
    print(f"Historical date range: {imported[0].date} to {imported[-1].date}")
    print(f"Merged populated rows: {sum(1 for row in rows[1:] if row[1] == 'ok')}")
    print(f"Wrote CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
