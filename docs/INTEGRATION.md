# Dynamic integration application — Phase 1

Working local MVP for configurable file ingestion, schema discovery, profiling, and column mapping. The existing Samsara polling service remains a separate command. This phase does not implement identity matching, reconciliation, remote connectors, validation rules, DAG execution, or the PTI effectiveness analysis; those are Phases 2–5. No menu or endpoint pretends to implement them.

## Install and run

Python 3.12 or later:

```bash
cd /Users/manishneupane/Downloads/samsara-poller
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
integration-api
```

In another terminal, activate the same environment:

```bash
source .venv/bin/activate
streamlit run frontend/app.py
```

Open Streamlit at http://localhost:8501 and the interactive API documentation at http://localhost:8000/docs. OpenAPI JSON is at `/openapi.json`.

```bash
# While the backend is running, generate and upload four imperfect fleet workbooks:
python examples/load_samples.py
# All new and existing tests:
pytest -q
# New application lint:
ruff check src/data_integration frontend examples/load_samples.py tests/integration
# Alternative: start both applications with persistent named-volume storage:
docker compose -f compose.integration.yaml up --build
```

Environment variables are read from the process; `.env` is a reference and is not automatically loaded. Defaults work without credentials. `INTEGRATION_DATA_DIR` defaults to `./integration-data`; `INTEGRATION_API_URL` controls the UI's backend URL; `INTEGRATION_HOST` and `INTEGRATION_PORT` control the API listener. `INTEGRATION_MAX_UPLOAD_MB` defaults to 50. All are documented in `.env.example`.

## Try the acceptance path for Phase 1

1. Create two file sources on Sources and connections. Upload two XLSX files with different headers. Select the worksheet name explicitly if needed; the default is the first sheet.
2. Inspect every column and paginated original-value preview in Dataset explorer.
3. Select arbitrary columns on Data profiling. Physical types and inferred value-type distributions are reported separately.
4. In Column mapping, select two or more datasets. Request explained suggestions, then confirm fields manually. Add multiple mappings, including mappings to a third dataset. Choose copy, combine, or split; preview and save.
5. Load a saved mapping, edit its JSON configuration, and save another version. History is retained in SQLite. Editing the JSON also supports changing all dataset references to newly imported versions.
6. Upload revised data into the same source. Identical input is reused; changed input creates a new dataset linked to its predecessor. Inspect added/removed/type-changed columns. Saved mappings report missing columns against latest source versions without modifying historical mappings.

## Data and transformation semantics

Original upload bytes are stored exactly in `raw/<dataset-id>`; parsed values are stored in `datasets/<dataset-id>.parquet`. Dataset IDs are UUIDs. User filenames are metadata only and never become storage paths. Identical content, format, and worksheet are deduplicated within a source; every attempt has ingestion history. Concurrent registration is serialized with SQLite `BEGIN IMMEDIATE` and a uniqueness constraint.

CSV columns are preserved as strings so `00123` remains `00123`. Value-type profiling suggests numeric and temporal strings without mutating them. XLSX uses native cell values; formula text is preserved rather than evaluated, and display formatting (such as numeric cells formatted with leading zeros) lives only in the original workbook. XLSX requires unique nonempty headers. JSON accepts an array of objects and retains nested structs/lists; it does not flatten or infer an API response envelope. Parquet retains native types.

Profiles report record/null counts, distinct values, singleton values (`unique_values`), duplicate occurrences beyond the first (`duplicate_values`), min/max, type and string-length distributions, common patterns, whitespace, case variants, likely identifiers, temporal text, and coarse date-format differences. Nulls are excluded from distinct/duplicate counts. A potential identifier is only a uniqueness heuristic, not an identity claim. Invalid format counts require explicit rules in Phase 3; the profile returns `null` with an explanation instead of guessing validity. Date profiling uses heuristic parsing and does not establish a locale, timezone, or temporal correctness.

Mapping suggestions combine name similarity (60%), physical-type equality (20%), and overlap of the first 100 non-null sample values (20%). Every suggestion requires confirmation, including high scores. Scores describe column correspondence, not person identity. A mapping accepts any dataset and column names. Copy requires one input and output; combine joins inputs from one dataset using an explicit separator; split produces components with surplus text retained in the last output. Null inputs propagate to null. Preview runs on at most 20 source rows and never writes to a target dataset or implies that rows in two datasets are aligned. Derived expressions and normalization pipelines belong to Phase 2.

## Architecture and storage schema

`src/data_integration/api.py` owns HTTP validation, stable IDs, pagination, and consistent error envelopes. `engine.py` owns parsing, profiles, drift, suggestions, and mapping validation. `store.py` owns SQLite transactions. `models.py` defines typed request contracts. `frontend/app.py` uses HTTPX exclusively for business operations.

SQLite tables (created automatically by `Store`):

| Table | Columns and constraints | Purpose |
| --- | --- | --- |
| sources | id PK, document JSON text | Name, connector metadata, refresh strategy |
| datasets | id PK, source_id FK, fingerprint, created_at, document; UNIQUE(source_id, fingerprint) | Version metadata, schema, predecessor and drift |
| mappings | id PK, document JSON text | Configuration version, previous versions |
| ingestion_history | id PK, source_id FK, created_at, document JSON text | Success, reuse, or failure |

Parquet provides parsed immutable snapshots; DuckDB reads preview pages directly from Parquet; Polars performs parsing/profiling; RapidFuzz provides column-name scores. FastAPI publishes its exact API contract. SQLite WAL and transaction constraints support concurrent local requests. Readers see only committed registrations; process failure between file creation and database registration can leave unreferenced files that require eventual cleanup. A backup must capture SQLite and the complete raw/datasets directories together.

## REST reference

List endpoints use `offset >= 0` and `1 <= limit <= 500`, returning `{items, total, offset, limit}`. Invalid input returns `{error: {code, message}}` with HTTP 422; missing IDs use 404; excessive uploads use 413. No dataset values are interpolated into SQL.

| Method | Path | Input / output |
| --- | --- | --- |
| GET | /health | SQLite readiness |
| POST / GET | /sources | Source configuration / paginated sources |
| GET | /sources/{id}/history | Paginated ingestion attempts |
| POST | /datasets/upload | Multipart source_id, file, optional sheet |
| GET | /datasets | Paginated immutable dataset versions |
| GET | /datasets/{id}/schema | Physical schema, drift, predecessor |
| GET | /datasets/{id}/preview | Paginated original parsed values |
| GET | /datasets/{id}/profile | Optional repeated `columns` query parameters |
| POST | /mappings/suggest | left_dataset_id and right_dataset_id; paginated evidence |
| POST | /mappings/preview | Mapping configuration; transformed sample values |
| POST / GET | /mappings | Save / list mappings with current-schema issues |
| GET / PUT | /mappings/{id} | Read / version an existing mapping |

A mapping request example (replace IDs and column names with discovered values):

```json
{
  "name": "Confirmed column relationship",
  "fields": [{
    "inputs": [{"dataset_id": "left-uuid", "column": "Any input column"}],
    "outputs": [{"dataset_id": "right-uuid", "column": "Any output column"}],
    "operation": "copy",
    "separator": " "
  }]
}
```

## Scope and deployment limits

This is a local, single-instance MVP. Profiling loads a full selected snapshot into memory; suggestions enumerate column pairs and sample 100 rows, and preview is capped at 500 rows. Upload request parsing can spool content before the configured parser read limit. Parsing occurs synchronously in FastAPI's thread pool. Excel workbook decompression can consume more memory than compressed file size. There is no authentication, tenancy, background scheduler, retention manager, or external credential vault yet; Docker ports bind to loopback. Source connection settings are plain metadata and should not contain secrets. File refresh strategies are recorded, with ingestion triggered explicitly through upload; remote refresh execution belongs to Phase 4.

The sample generator includes duplicate names, misspellings, missing credentials, license format variants, reassigned vehicles, repeated training events, conflicting/duplicate inspections, missing inspections, and late arrival timestamps. These fixtures support later phases, but Phase 1 does not infer regulatory requirements or evaluate training effectiveness. A PTI project configuration and full workflow are Phase 5 deliverables.

## Analyst workspaces

**Data Quality** searches and filters source rows, groups counts, checks selected duplicate keys, and previews cleaned values. Applying checks records the snapshot ID, settings, column counts, and duplicate summary. **Run History** exports the recorded quality summary as JSON.

**Join & Reconcile** compares two dataset versions with independently selected columns. All selected key pairs must agree. Choose exact value, cleaned text, or phone digits for each pair. Inner, left, right, full, and unmatched views report source row numbers and preserve both sides' columns. Blank keys remain unmatched. Repeated keys may produce multiple pairs, so the report distinguishes matched pairs from matched source rows. Indexed lookups support ordinary joins of 10,000-row sources. A report view is capped at 100,000 output rows.

Join history records the exact versions, settings, timestamp, and counts. CSV reports are rebuilt from those immutable snapshots. The separate fuzzy comparison preview uses local name similarity and exact phone digit agreement, with a 250,000 possible-pair cap. Similarity scores are candidate evidence and are not used to create join rows.

For a saved JSON response, upload the file into its existing source. Enter a JSON records path such as `/data/items` to extract rows from an envelope. Leave the path blank for a root array or a single root object. The original response is preserved and the selected path is recorded with the dataset version.

To inspect whether records exist on another platform, choose **Unmatched** in **Join & Reconcile**. In **Allow partial agreement**, select the minimum number of agreeing fields and optionally mark specific fields as mandatory. Blank values never contribute to the threshold. The result reports agreed fields, comparable field count, and candidate counts for each side. Multiple qualifying pairs remain visible for analyst review. Partial agreement uses deterministic configured comparisons and caps candidate work when common values make a comparison too broad.

In **Data Quality**, select fields that must have values and the key that should be unique. Apply the checks to record a run. The row CSV includes source row numbers, validation reasons, original data, and the selected cleaning preview. Run History also provides the quality summary and configuration. These inspection steps are run independently against pinned source versions.

In **Sources → API & Webhook**, add an HTTP GET connection with a source name and HTTPS endpoint. Configure trusted hostnames in the API process with `INTEGRATION_HTTP_ALLOWED_HOSTS` (comma-separated). For a bearer credential, set an environment variable such as `INTEGRATION_HTTP_TOKEN_FLEET` in the API process and enter its **name** in the connection form. Keep credentials out of endpoint URLs. Click **Run GET and import** to import a single response. The importer reads the response filename when supplied, then tries the URL filename, and defaults to JSON. An optional filename override selects the parser. Use the JSON records path for a nested response. The server uses port 443 and does not follow redirects.

A Webhook source receives JSON at `POST /webhooks/{source_id}`. Copy the generated token from the setup screen and send it as `Authorization: Bearer <token>`. Send `X-Source-Filename` if the sender has an original filename. A remote sender needs a reachable HTTPS endpoint. Configure your reverse proxy to expose only the webhook route and set `INTEGRATION_WEBHOOK_BASE_URL` in Streamlit to display that public address. GET, webhook, and file imports record the source and filename in dataset metadata and import history.

In **Match & Join → Fuzzy comparison**, compare word-order-independent names or use character similarity for strings and numbers as written. RapidFuzz scores are similarity measures. The optional phone comparison remains exact after extracting digits.
