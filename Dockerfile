FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ ./scripts/
COPY outputs/aaa_gas_prices/aaa_national_gas_prices.csv ./outputs/aaa_gas_prices/aaa_national_gas_prices.csv

ENTRYPOINT ["python3", "scripts/cloud_run_aaa_sync.py"]
