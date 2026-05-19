#!/usr/bin/env python3
"""Cloud Run entrypoint for the AAA gas prices sync.

Cloud Run job containers are ephemeral, so this wrapper stores the CSV cache in
Cloud Storage between runs. It downloads the cache, runs the existing sync
script, then uploads the refreshed CSV.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


DEFAULT_CSV_PATH = Path("outputs/aaa_gas_prices/aaa_national_gas_prices.csv")
DEFAULT_STATE_CSV_DIR = Path("outputs/aaa_gas_prices/states")
DEFAULT_STATE_GCS_PREFIX = "outputs/aaa_gas_prices/states"
DEFAULT_STATE_START_DATE = "2026-05-19"
DEFAULT_STATE_COMPARISON_SHEET_NAME = "State Comparison Since May 19"
DEFAULT_STATE_SHEET_WRITE_SLEEP = "5"
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def metadata_access_token() -> str:
    request = urllib.request.Request(
        METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["access_token"]


def gcs_object_url(bucket: str, object_name: str) -> str:
    quoted_bucket = urllib.parse.quote(bucket, safe="")
    quoted_object = urllib.parse.quote(object_name, safe="/")
    return f"https://storage.googleapis.com/{quoted_bucket}/{quoted_object}"


def gcs_list_url(bucket: str, prefix: str, page_token: str = "") -> str:
    query = {
        "prefix": prefix,
        "fields": "items/name,nextPageToken",
    }
    if page_token:
        query["pageToken"] = page_token
    return (
        f"https://storage.googleapis.com/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o?"
        f"{urllib.parse.urlencode(query)}"
    )


def gcs_request(
    method: str,
    bucket: str,
    object_name: str,
    token: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        gcs_object_url(bucket, object_name),
        data=body,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def gcs_get_json(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def gcs_list_objects(bucket: str, prefix: str, token: str) -> list[str]:
    objects: list[str] = []
    page_token = ""
    while True:
        payload = gcs_get_json(gcs_list_url(bucket, prefix, page_token), token)
        objects.extend(item["name"] for item in payload.get("items", []))
        page_token = payload.get("nextPageToken", "")
        if not page_token:
            return objects


def download_cache(bucket: str, object_name: str, csv_path: Path, token: str) -> None:
    try:
        data = gcs_request("GET", bucket, object_name, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(
                f"No existing GCS cache found at gs://{bucket}/{object_name}; "
                "using bundled CSV if present.",
                flush=True,
            )
            return
        raise

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(data)
    print(
        f"Downloaded CSV cache from gs://{bucket}/{object_name} "
        f"to {csv_path}.",
        flush=True,
    )


def download_state_caches(
    bucket: str,
    state_prefix: str,
    state_csv_dir: Path,
    token: str,
) -> int:
    object_prefix = state_prefix.rstrip("/") + "/"
    object_names = [
        object_name
        for object_name in gcs_list_objects(bucket, object_prefix, token)
        if object_name.endswith(".csv")
    ]
    if not object_names:
        print(
            f"No existing state CSV caches found at gs://{bucket}/{object_prefix}; "
            f"state tracking will start from the configured baseline date.",
            flush=True,
        )
        return 0

    state_csv_dir.mkdir(parents=True, exist_ok=True)
    for object_name in object_names:
        local_path = state_csv_dir / Path(object_name).name
        local_path.write_bytes(gcs_request("GET", bucket, object_name, token))
    print(
        f"Downloaded {len(object_names)} state CSV caches from "
        f"gs://{bucket}/{object_prefix}.",
        flush=True,
    )
    return len(object_names)


def upload_cache(bucket: str, object_name: str, csv_path: Path, token: str) -> None:
    data = csv_path.read_bytes()
    gcs_request(
        "PUT",
        bucket,
        object_name,
        token,
        body=data,
        content_type="text/csv; charset=utf-8",
    )
    print(
        f"Uploaded refreshed CSV cache to gs://{bucket}/{object_name}.",
        flush=True,
    )


def upload_state_caches(
    bucket: str,
    state_prefix: str,
    state_csv_dir: Path,
    token: str,
) -> int:
    object_prefix = state_prefix.rstrip("/") + "/"
    csv_paths = sorted(state_csv_dir.glob("*.csv"))
    for csv_path in csv_paths:
        upload_cache(bucket, f"{object_prefix}{csv_path.name}", csv_path, token)
    print(
        f"Uploaded {len(csv_paths)} state CSV caches to "
        f"gs://{bucket}/{object_prefix}.",
        flush=True,
    )
    return len(csv_paths)


def truthy_env(name: str, default: str = "0") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def run_command(command: list[str]) -> None:
    print(f"Running: {' '.join(command)}", flush=True)
    subprocess.run(command, check=True)


def main() -> int:
    bucket = required_env("AAA_GCS_BUCKET")
    object_name = os.environ.get(
        "AAA_GCS_OBJECT", str(DEFAULT_CSV_PATH)
    ).strip() or str(DEFAULT_CSV_PATH)
    csv_path = Path(os.environ.get("AAA_CSV_PATH", str(DEFAULT_CSV_PATH)))
    credentials_path = required_env("GOOGLE_APPLICATION_CREDENTIALS")
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")
    sync_states = truthy_env("AAA_SYNC_STATES")
    state_csv_dir = Path(os.environ.get("AAA_STATE_CSV_DIR", str(DEFAULT_STATE_CSV_DIR)))
    state_gcs_prefix = os.environ.get(
        "AAA_STATE_GCS_PREFIX", DEFAULT_STATE_GCS_PREFIX
    ).strip() or DEFAULT_STATE_GCS_PREFIX
    state_start_date = os.environ.get(
        "AAA_STATE_START_DATE", DEFAULT_STATE_START_DATE
    ).strip() or DEFAULT_STATE_START_DATE
    state_comparison_sheet_name = os.environ.get(
        "AAA_STATE_COMPARISON_SHEET_NAME",
        DEFAULT_STATE_COMPARISON_SHEET_NAME,
    ).strip() or DEFAULT_STATE_COMPARISON_SHEET_NAME
    state_sheet_write_sleep = os.environ.get(
        "AAA_STATE_SHEET_WRITE_SLEEP",
        DEFAULT_STATE_SHEET_WRITE_SLEEP,
    ).strip() or DEFAULT_STATE_SHEET_WRITE_SLEEP

    token = metadata_access_token()
    download_cache(bucket, object_name, csv_path, token)

    national_command = [
        sys.executable,
        "scripts/aaa_gas_prices_to_sheets.py",
        "--credentials",
        credentials_path,
        "--spreadsheet-id",
        spreadsheet_id,
        "--csv",
        str(csv_path),
    ]
    run_command(national_command)
    upload_cache(bucket, object_name, csv_path, token)

    if sync_states:
        download_state_caches(bucket, state_gcs_prefix, state_csv_dir, token)
        state_command = [
            sys.executable,
            "scripts/aaa_state_gas_prices_to_sheets.py",
            "--states",
            "all",
            "--csv-dir",
            str(state_csv_dir),
            "--baseline-date",
            state_start_date,
            "--comparison-sheet-name",
            state_comparison_sheet_name,
            "--sheet-write-sleep",
            state_sheet_write_sleep,
            "--credentials",
            credentials_path,
            "--spreadsheet-id",
            spreadsheet_id,
        ]
        run_command(state_command)
        upload_state_caches(bucket, state_gcs_prefix, state_csv_dir, token)
    else:
        print("State sync skipped because AAA_SYNC_STATES is not enabled.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
