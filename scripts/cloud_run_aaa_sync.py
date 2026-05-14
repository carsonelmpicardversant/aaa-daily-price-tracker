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


def main() -> int:
    bucket = required_env("AAA_GCS_BUCKET")
    object_name = os.environ.get(
        "AAA_GCS_OBJECT", str(DEFAULT_CSV_PATH)
    ).strip() or str(DEFAULT_CSV_PATH)
    csv_path = Path(os.environ.get("AAA_CSV_PATH", str(DEFAULT_CSV_PATH)))
    credentials_path = required_env("GOOGLE_APPLICATION_CREDENTIALS")
    spreadsheet_id = required_env("GOOGLE_SHEET_ID")

    token = metadata_access_token()
    download_cache(bucket, object_name, csv_path, token)

    command = [
        sys.executable,
        "scripts/aaa_gas_prices_to_sheets.py",
        "--credentials",
        credentials_path,
        "--spreadsheet-id",
        spreadsheet_id,
        "--csv",
        str(csv_path),
    ]
    subprocess.run(command, check=True)

    upload_cache(bucket, object_name, csv_path, token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
