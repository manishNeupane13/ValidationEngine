# Dynamic data integration — Phase 1

The new FastAPI + Streamlit application supports XLSX, CSV, JSON, and Parquet ingestion, immutable versions, dynamic schema discovery, profiling, explained column mapping suggestions, saved mappings, and schema drift checks.

See [setup, API, architecture, storage schema, and scope](docs/INTEGRATION.md) for full instructions.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
integration-api
# In another activated terminal:
streamlit run frontend/app.py
# With the API running:
python examples/load_samples.py
pytest -q
```

Phases 2–5 (normalization, identity resolution, reconciliation, connectors, workflows, and PTI effectiveness analysis) are not implemented yet. The existing polling service remains available below.

---

# Samsara polling service

A Python 3.11+ async polling service for fetching the Samsara vehicle inventory and then requesting vehicle stats. Default poll interval is 60 seconds. Output and failure journal are append-only JSON Lines files.

## Important endpoint assumptions to verify

This project intentionally isolates endpoint-specific assumptions in `gateway.py`:

1. `GET /fleet/vehicles` returns a JSON object with `data: [{"id": "..."}]` and cursor pagination fields `pagination.hasNextPage` / `pagination.endCursor`, with request parameters `limit` and `after`. Verify the actual response and pagination contract for the API version your account uses; update this adapter if it differs.
2. `GET /fleet/vehicles/stats` accepts `vehicleIds` as a comma-separated query parameter and `types` as a comma-separated query parameter. `SAMSARA_STATS_TYPES` defaults to `engineStates` as an example. Verify supported stats types and whether multi-ID batching is supported before increasing `VEHICLE_BATCH_SIZE`.
3. The stats response is stored as a raw batch payload. The service does not assume a particular stats response shape. `vehicle_id` in the output record is the comma-joined IDs for that batch; for batch size 1 it is a single ID.
4. A 60-second cadence means one cycle starts after the previous cycle completes plus the configured interval. This avoids overlap but does not promise exact wall-clock start times.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# Set SAMSARA_API_TOKEN in .env; never commit this file.
samsara-poller
```

The CLI reads environment variables directly; `env_file` is for Docker Compose, not automatically loaded by Python. For local runs either export the variables in your shell or use a local environment loader if desired.

Output files:

- `data/vehicle_stats.jsonl`: successful raw stats responses, one record per batch.
- `data/failures.jsonl`: failed batches with vehicle IDs and error details. The next polling cycle re-discovers the inventory and tries those vehicles again. This is a durable failure journal, not a separate replay queue.

## Test and lint

```bash
pip install -e '.[test]'
pytest
ruff check .
```

## Reliability behavior

- Reuses one HTTPX async client and connection pool.
- Bounded concurrent batches and a process-local request-rate limiter.
- Retries transient transport failures and HTTP 408/425/429/500/502/503/504 with capped exponential backoff and jitter; honors `Retry-After` when present.
- Does not retry most other HTTP 4xx responses, invalid JSON, or schema errors.
- Pagination cursor repetition is detected to prevent infinite loops.
- IDs are deduplicated during inventory pagination.
- Per-batch failures are recorded without discarding successful batches.
- JSONL appends flush and fsync each record. One process should own the output directory.
- SIGINT/SIGTERM initiate graceful stop between polling cycles; in-flight work is allowed to finish unless the process is force-killed.

## Production limitations / next steps

- The rate limiter is process-local and approximate. Multiple replicas require a shared rate limiter or a single active scheduler/leader.
- File storage is suitable for a single-instance service and modest volumes. It does not provide transactions, indexed queries, or automatic replay of old failure records. For stronger recovery, use SQLite/PostgreSQL with unique keys and a durable retry table/outbox.
- No metrics endpoint, alerting integration, or health server is included. Add operational monitoring before relying on it for critical workflows.
- `VEHICLE_BATCH_SIZE=1` is conservative. If the stats endpoint supports batching, increase it after verifying parameter limits and response semantics; this can greatly reduce API calls.
- The example stats type may not match the exact field or type you need. Verify it against the current Samsara API documentation and account permissions.
- Avoid storing unnecessary sensitive data. Restrict filesystem permissions, set retention/rotation for JSONL files, and protect logs.
