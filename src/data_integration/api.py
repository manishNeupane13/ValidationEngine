import hmac
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated, Literal
from zipfile import BadZipFile

import httpx
import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .connectors import (
    SUPPORTED,
    ConnectionSettings,
    approved_url,
    get_payload,
    original_filename,
    token_digest,
    upload_limit,
)
from .engine import (
    InputError,
    frame_for,
    ingest,
    mapping_issues,
    preview,
    profile,
    suggestions,
)
from .find import FindQuery, find_rows
from .models import MappingCreate, SourceCreate, SuggestRequest
from .quality import MatchQuery, QualityQuery, compare_datasets, inspect_dataset
from .reconcile import JoinRequest, reconcile
from .store import Store, now, uid
from .transform import Recipe, RecipeExecution, execute_recipe


def create_app(data_dir=None):
    app = FastAPI(title="Dynamic Data Integration — Phase 1", version="0.1.0")
    store = Store(
        data_dir or os.environ.get("INTEGRATION_DATA_DIR", "./integration-data")
    )
    app.state.store = store

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "message": str(error)}},
        )

    @app.exception_handler(InputError)
    async def invalid(request, error):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_input", "message": str(error)}},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return JSONResponse(
            status_code=error.status_code,
            content={"error": {"code": "request_error", "message": str(error.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": str(error)}},
        )

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logging.getLogger(__name__).exception("Unexpected API failure")
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "Unexpected server error; see server logs.",
                }
            },
        )

    def page(items, offset, limit):
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "offset": offset,
            "limit": limit,
        }

    @app.get("/health")
    def health():
        with store.db() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "phase": 1}

    @app.post("/sources", status_code=201)
    def create_source(body: SourceCreate):
        document = dict(id=uid(), created_at=now(), **body.model_dump())
        secret = None
        if body.kind in {"http", "webhook"}:
            try:
                settings = ConnectionSettings.model_validate(body.connection_settings)
            except ValidationError as error:
                raise InputError(
                    "Invalid connection settings. Check the URL, filename, and authentication settings."
                ) from error
            if body.kind == "http":
                approved_url(settings.url)
            elif settings.url or settings.bearer_env or settings.custom_token:
                raise InputError("Webhook sources receive data at a generated URL.")
            if settings.filename and (
                original_filename(settings.filename) != settings.filename
                or Path(settings.filename).suffix.lower()
                not in (SUPPORTED if body.kind == "http" else {".json"})
            ):
                raise InputError(
                    "Enter a filename with a supported extension and no directory path."
                )
            document["connection_settings"] = settings.model_dump(exclude_none=True)
            if body.kind == "webhook":
                secret = secrets.token_urlsafe(32)
                document["webhook_digest"] = token_digest(secret)
        store.put("sources", document)
        result = public_source(document)
        if secret:
            result["webhook_token"] = secret
        return result

    def public_source(source):
        return {key: value for key, value in source.items() if key != "webhook_digest"}

    @app.get("/sources")
    def sources(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return page(
            [public_source(source) for source in store.list("sources")], offset, limit
        )

    @app.get("/sources/{source_id}/history")
    def history(
        source_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        store.get("sources", source_id)
        return page(
            [r for r in store.list("ingestion_history") if r["source_id"] == source_id],
            offset,
            limit,
        )

    def ingest_connected(source, payload, filename, origin):
        try:
            return ingest(
                store,
                source["id"],
                payload,
                filename,
                records_path=source["connection_settings"].get("records_path"),
                origin=origin,
            )
        except (ValueError, pl.exceptions.PolarsError, BadZipFile) as error:
            store.history(
                source["id"],
                status="failed",
                filename=filename,
                origin=origin,
                error="Response did not contain readable records.",
            )
            raise InputError(
                "Could not import records. Check the response format, filename, and JSON records path."
            ) from error

    @app.post("/sources/{source_id}/refresh", status_code=201)
    def refresh_source(source_id: str):
        source = store.get("sources", source_id)
        if source["kind"] != "http":
            raise InputError("Choose an HTTP GET source.")
        settings = ConnectionSettings.model_validate(source["connection_settings"])
        try:
            payload, filename, endpoint, response_filename = get_payload(settings)
        except (httpx.HTTPError, InputError) as error:
            store.history(
                source_id,
                status="failed",
                error="GET import failed.",
                origin={"transport": "http_get"},
            )
            if isinstance(error, InputError):
                raise
            status = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            detail = (
                f"The API returned HTTP {status}."
                if status
                else "Could not reach the configured API."
            )
            raise HTTPException(502, detail) from error
        origin = {"transport": "http_get", "endpoint": endpoint, "filename": filename}
        if response_filename:
            origin["response_filename"] = response_filename
        return ingest_connected(source, payload, filename, origin)

    @app.post("/webhooks/{source_id}", status_code=201)
    async def receive_webhook(source_id: str, request: Request):
        source = store.get("sources", source_id)
        header = request.headers.get("authorization", "")
        if source["kind"] != "webhook":
            raise HTTPException(404, "Webhook source not found.")
        if not header.startswith("Bearer ") or not hmac.compare_digest(
            source["webhook_digest"], token_digest(header[7:])
        ):
            raise HTTPException(401, "A valid webhook bearer token is required.")
        payload = bytearray()
        limit = upload_limit()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > limit:
                raise HTTPException(413, "Upload exceeds configured limit.")
            payload.extend(chunk)
        sent_name = original_filename(request.headers.get("x-source-filename"))
        filename = source["connection_settings"].get("filename") or (
            sent_name if sent_name.lower().endswith(".json") else "webhook.json"
        )
        origin = {
            "transport": "webhook",
            "endpoint": f"/webhooks/{source_id}",
            "filename": filename,
        }
        if sent_name:
            origin["sender_filename"] = sent_name
        return await run_in_threadpool(
            ingest_connected, source, bytes(payload), filename, origin
        )

    @app.post("/datasets/upload", status_code=201)
    def upload(
        source_id: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
        sheet: Annotated[str | None, Form()] = None,
        records_path: Annotated[str | None, Form()] = None,
    ):
        if store.get("sources", source_id)["kind"] == "derived":
            raise InputError(
                "Publish a transformation run to update this derived source."
            )
        limit = upload_limit()
        payload = file.file.read(limit + 1)
        if len(payload) > limit:
            raise HTTPException(413, "Upload exceeds configured limit.")
        filename = original_filename(file.filename) or "upload"
        try:
            return ingest(store, source_id, payload, filename, sheet, records_path)
        except (KeyError, HTTPException):
            raise
        except Exception as error:
            store.history(
                source_id, status="failed", filename=filename, error=str(error)
            )
            raise InputError(f"Unable to ingest file: {error}") from error

    @app.get("/datasets")
    def datasets(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return page(store.list("datasets"), offset, limit)

    @app.get("/datasets/{dataset_id}/schema")
    def schema(dataset_id: str):
        return store.get("datasets", dataset_id)

    @app.get("/datasets/{dataset_id}/preview")
    def data_preview(
        dataset_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=500),
    ):
        dataset = store.get("datasets", dataset_id)
        return {
            "items": preview(store, dataset_id, offset, limit),
            "total": dataset["row_count"],
            "offset": offset,
            "limit": limit,
        }

    @app.get("/datasets/{dataset_id}/profile")
    def data_profile(
        dataset_id: str, columns: Annotated[list[str] | None, Query()] = None
    ):
        # Convert temporal/decimal statistics through a JSON serializer.
        return json.loads(
            json.dumps(profile(frame_for(store, dataset_id), columns), default=str)
        )

    @app.post("/find")
    def find(body: FindQuery):
        return find_rows(store, body)

    @app.post("/mappings/suggest")
    def suggest(
        body: SuggestRequest,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        return page(
            suggestions(store, body.left_dataset_id, body.right_dataset_id),
            offset,
            limit,
        )

    def save_mapping(body, identifier):
        document = dict(id=identifier, updated_at=now(), **body.model_dump())
        issues = mapping_issues(store, document)
        if issues:
            raise InputError("; ".join(issues))
        try:
            previous = store.get("mappings", identifier)
        except KeyError:
            previous = {}
        document["version"] = previous.get("version", 0) + 1
        document["history"] = previous.get("history", []) + (
            [{k: v for k, v in previous.items() if k != "history"}] if previous else []
        )
        store.put("mappings", document)
        return document

    @app.post("/mappings", status_code=201)
    def create_mapping(body: MappingCreate):
        return save_mapping(body, uid())

    @app.put("/mappings/{mapping_id}")
    def update_mapping(mapping_id: str, body: MappingCreate):
        store.get("mappings", mapping_id)
        return save_mapping(body, mapping_id)

    def mapping_status(document):
        return {
            **document,
            "issues": mapping_issues(store, document),
            "latest_source_issues": mapping_issues(store, document, latest=True),
        }

    @app.get("/mappings")
    def mappings(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return page([mapping_status(m) for m in store.list("mappings")], offset, limit)

    @app.get("/mappings/{mapping_id}")
    def mapping(mapping_id: str):
        return mapping_status(store.get("mappings", mapping_id))

    @app.post("/mappings/preview")
    def mapping_preview(body: MappingCreate):
        document = body.model_dump()
        issues = mapping_issues(store, document)
        if issues:
            raise InputError("; ".join(issues))
        output = []
        for field in body.fields:
            rows = preview(store, field.inputs[0].dataset_id, 0, 20)
            records = []
            for row in rows:
                values = [row[r.column] for r in field.inputs]
                if field.operation == "copy":
                    transformed = values
                elif field.operation == "combine":
                    transformed = [
                        None
                        if any(v is None for v in values)
                        else field.separator.join(map(str, values))
                    ]
                else:
                    transformed = (
                        [None] * len(field.outputs)
                        if values[0] is None
                        else str(values[0]).split(
                            field.separator, len(field.outputs) - 1
                        )
                    )
                    transformed += [None] * (len(field.outputs) - len(transformed))
                records.append(
                    {
                        "original": values,
                        "outputs": [
                            {"dataset_id": r.dataset_id, "column": r.column, "value": v}
                            for r, v in zip(field.outputs, transformed)
                        ],
                    }
                )
            output.append({"operation": field.operation, "rows": records})
        return {
            "fields": output,
            "note": "Preview only; source records and target datasets are unchanged. No row alignment or identity relationship is inferred.",
        }

    @app.post("/datasets/{dataset_id}/quality")
    def quality(dataset_id: str, body: QualityQuery):
        return inspect_dataset(store, dataset_id, body)

    @app.post("/datasets/{dataset_id}/quality/runs", status_code=201)
    def run_quality(dataset_id: str, body: QualityQuery):
        report = inspect_dataset(store, dataset_id, body)
        summary = {
            key: report[key]
            for key in (
                "filtered_count",
                "flagged_count",
                "visible_count",
                "duplicate_rows",
                "duplicate_groups",
                "columns",
                "group_count",
                "groups",
            )
        }
        run = {
            "id": uid(),
            "created_at": now(),
            "dataset_id": dataset_id,
            "configuration": body.model_dump(),
            "summary": summary,
        }
        store.put("quality_runs", run)
        return {**report, "run_id": run["id"]}

    @app.get("/quality/runs")
    def quality_history(
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        return page(store.list("quality_runs"), offset, limit)

    @app.get("/quality/runs/{run_id}/report")
    def quality_report(run_id: str):
        run = store.get("quality_runs", run_id)
        report = inspect_dataset(
            store,
            run["dataset_id"],
            QualityQuery.model_validate(run["configuration"]),
            full_report=True,
        )
        return {
            "items": [
                {
                    "Source row": row["row"],
                    "Issues": "; ".join(row["issues"]),
                    **({"Cleaned value": row["cleaned"]} if "cleaned" in row else {}),
                    **{
                        f"Original · {column}": value
                        for column, value in row["values"].items()
                    },
                }
                for row in report["items"]
            ]
        }

    @app.post("/matching/preview")
    def compare(body: MatchQuery):
        return compare_datasets(store, body)

    @app.post("/reconciliation/runs", status_code=201)
    def run_reconciliation(body: JoinRequest):
        counts, rows = reconcile(store, body)
        run = {
            "id": uid(),
            "created_at": now(),
            "configuration": body.model_dump(
                exclude={
                    field
                    for field in ("audit_fields", "name_threshold")
                    if getattr(body, field) is None
                }
            ),
            "counts": counts,
            "output_rows": len(rows),
        }
        store.put("reconciliation_runs", run)
        return {**run, "items": rows[:100]}

    @app.get("/reconciliation/runs")
    def reconciliation_history(
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        return page(store.list("reconciliation_runs"), offset, limit)

    @app.get("/reconciliation/runs/{run_id}/report")
    def reconciliation_report(
        run_id: str,
        status: Annotated[str | None, Query()] = None,
        differences_only: bool = False,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=500)] = None,
    ):
        run = store.get("reconciliation_runs", run_id)
        _, rows = reconcile(store, JoinRequest.model_validate(run["configuration"]))
        if status:
            rows = [row for row in rows if row["status"] == status]
        if differences_only:
            rows = [row for row in rows if row.get("difference_count")]
        if limit is not None:
            return page(rows, offset, limit)
        return {"items": rows[offset:]} if offset else {"items": rows}

    @app.get("/transformation/recipes")
    def transformation_recipes(
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        return page(store.list("transformation_recipes"), offset, limit)

    @app.post("/transformation/recipes", status_code=201)
    def save_transformation_recipe(body: Recipe):
        store.get("sources", body.source_id)
        recipe = {"id": uid(), "created_at": now(), **body.model_dump()}
        store.put("transformation_recipes", recipe)
        return recipe

    @app.get("/transformation/runs")
    def transformation_runs(
        offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
    ):
        return page(store.list("transformation_runs"), offset, limit)

    @app.post("/transformation/recipes/{recipe_id}/runs", status_code=201)
    def run_transformation(recipe_id: str, body: RecipeExecution):
        saved = store.get("transformation_recipes", recipe_id)
        dataset = store.get("datasets", body.dataset_id)
        if saved["source_id"] != dataset["source_id"]:
            raise InputError("Choose a dataset version from the recipe's source.")
        rows, originals, trace, exceptions = execute_recipe(
            store, body.dataset_id, Recipe.model_validate(saved)
        )
        identifier = uid()
        run = {
            "id": identifier,
            "created_at": now(),
            "recipe": saved,
            "dataset_id": body.dataset_id,
            "trace": trace,
            "row_count": len(rows),
            "issue_count": len(exceptions),
            "flagged_rows": len({issue["source_row"] for issue in exceptions}),
        }
        report = {"rows": rows, "originals": originals, "exceptions": exceptions}
        report_path = store.root / "transformation_reports" / f"{identifier}.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        store.put("transformation_runs", run)
        return {**run, "items": rows[:25], "exceptions": exceptions[:25]}

    @app.get("/transformation/runs/{run_id}/report")
    def transformation_report(
        run_id: str, kind: Annotated[Literal["rows", "exceptions"], Query()] = "rows"
    ):
        store.get("transformation_runs", run_id)
        report_path = store.root / "transformation_reports" / f"{run_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if kind == "exceptions":
            return {"items": report["exceptions"]}
        return {
            "items": [
                {
                    "Source row": index,
                    **{f"Original · {key}": value for key, value in original.items()},
                    **{f"Output · {key}": value for key, value in transformed.items()},
                }
                for index, (original, transformed) in enumerate(
                    zip(report["originals"], report["rows"], strict=True), 1
                )
            ]
        }

    @app.post("/transformation/runs/{run_id}/publish", status_code=201)
    def publish_transformation(run_id: str):
        run = store.get("transformation_runs", run_id)
        if run["issue_count"] or not run["row_count"]:
            raise InputError(
                "Publishing requires at least one row and no validation or conversion issues."
            )
        recipe = run["recipe"]
        source_id = recipe["id"]
        store.put(
            "sources",
            {
                "id": source_id,
                "name": f"{recipe['name']} (transformed)",
                "kind": "derived",
                "connection_settings": {},
                "refresh_strategy": "manual",
                "created_at": recipe["created_at"],
            },
        )
        report_path = store.root / "transformation_reports" / f"{run_id}.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return ingest(
            store,
            source_id,
            json.dumps(report["rows"], ensure_ascii=False).encode(),
            f"{recipe['name']}.json",
            lineage={
                "run_id": run_id,
                "input_dataset_id": run["dataset_id"],
                "recipe_id": recipe["id"],
            },
        )

    return app


def run():
    import uvicorn

    uvicorn.run(
        "data_integration.api:create_app",
        factory=True,
        host=os.environ.get("INTEGRATION_HOST", "127.0.0.1"),
        port=int(os.environ.get("INTEGRATION_PORT", "8000")),
    )
