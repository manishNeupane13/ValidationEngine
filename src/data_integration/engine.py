import csv
import hashlib
import io
import json
import re
from collections import Counter
from datetime import date, datetime

import duckdb
import polars as pl
from dateutil.parser import parse as parse_date
from openpyxl import load_workbook
from rapidfuzz.fuzz import WRatio

from .store import now, uid


class InputError(ValueError):
    pass


def read_file(payload, filename, sheet=None, records_path=None):
    suffix = filename.rsplit(".", 1)[-1].lower()
    if records_path and suffix != "json":
        raise InputError("A JSON records path is only supported for JSON files.")
    stream = io.BytesIO(payload)
    if suffix == "csv":
        header = next(csv.reader(io.StringIO(payload.decode("utf-8-sig"))), [])
        if not header or any(not value.strip() for value in header):
            raise InputError("CSV requires a nonempty header for every column.")
        if len(set(header)) != len(header):
            raise InputError("CSV column names must be unique.")
        # Preserve identifiers, leading zeros, and original textual dates.
        return pl.read_csv(stream, infer_schema=False, try_parse_dates=False)
    if suffix == "parquet":
        return pl.read_parquet(stream)
    if suffix == "json":
        records = json.loads(payload)
        if records_path:
            if not records_path.startswith("/"):
                raise InputError("Use a JSON Pointer such as /data/items.")
            for part in records_path[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                if isinstance(records, dict) and key in records:
                    records = records[key]
                elif (
                    isinstance(records, list)
                    and key.isascii()
                    and key.isdecimal()
                    and len(key) < 10
                    and int(key) < len(records)
                ):
                    records = records[int(key)]
                else:
                    raise InputError(f"JSON records path was not found: {records_path}")
        if (
            isinstance(records, list)
            and records
            and all(isinstance(row, list) for row in records)
        ):
            headers, *rows = records
            if not headers or any(
                not isinstance(name, str) or not name.strip() for name in headers
            ):
                raise InputError(
                    "Use a range whose first row contains nonempty text column names."
                )
            if len(set(headers)) != len(headers):
                raise InputError("Spreadsheet column names must be unique.")
            if any(len(row) > len(headers) for row in rows):
                raise InputError("A data row has more cells than the header row.")
            return (
                pl.from_dicts(
                    [
                        dict(
                            zip(
                                headers,
                                row + [None] * (len(headers) - len(row)),
                                strict=True,
                            )
                        )
                        for row in rows
                    ],
                    infer_schema_length=None,
                    strict=False,
                )
                if rows
                else pl.DataFrame(schema={header: pl.String for header in headers})
            )
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list) or any(
            not isinstance(r, dict) for r in records
        ):
            raise InputError(
                "The selected JSON value must be an object or an array of objects."
            )
        return (
            pl.from_dicts(records, infer_schema_length=None, strict=False)
            if records
            else pl.DataFrame()
        )
    if suffix == "xlsx":
        workbook = load_workbook(stream, read_only=True, data_only=False)
        try:
            if sheet and sheet not in workbook.sheetnames:
                raise InputError(
                    f"Unknown sheet. Available sheets: {workbook.sheetnames}"
                )
            worksheet = workbook[sheet] if sheet else workbook.worksheets[0]
            rows = worksheet.iter_rows(values_only=True)
            header = next(rows, None)
            if not header or any(x is None or str(x).strip() == "" for x in header):
                raise InputError("Excel requires a nonempty header for every column.")
            names = list(map(str, header))
            if len(set(names)) != len(names):
                raise InputError("Excel column names must be unique.")
            return pl.DataFrame(
                list(rows),
                schema=names,
                orient="row",
                infer_schema_length=None,
                strict=False,
            )
        finally:
            workbook.close()
    raise InputError("Supported files: CSV, XLSX, JSON, Parquet.")


def json_rows(frame):
    return json.loads(frame.write_json())


def inferred_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (dict, list)):
        return "nested"
    if isinstance(value, (date, datetime)):
        return "datetime"
    if isinstance(value, (int, float)):
        return "numeric"
    value = str(value)
    if re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)", value.strip()):
        return "numeric_text"
    if re.search(r"\d[-/]\d|\dT\d", value):
        try:
            parse_date(value)
            return "datetime_text"
        except (ValueError, OverflowError):
            pass
    return "string"


def profile(frame, selected=None):
    selected = frame.columns if selected is None else selected
    missing = set(selected) - set(frame.columns)
    if missing:
        raise InputError(f"Unknown columns: {sorted(missing)}")
    result = []
    for name in selected:
        series = frame[name]
        values = series.to_list()
        present = [v for v in values if v is not None]
        counts = Counter(json.dumps(v, default=str, sort_keys=True) for v in present)
        strings = [v for v in present if isinstance(v, str)]
        patterns = Counter(
            re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", v)) for v in strings
        )
        types = Counter(inferred_type(v) for v in values)
        folded = {}
        for value in strings:
            folded.setdefault(value.casefold(), set()).add(value)
        dates = [v for v in strings if inferred_type(v) == "datetime_text"]
        date_patterns = Counter(
            "iso" if re.match(r"^\d{4}-", v) else "slash" if "/" in v else "other"
            for v in dates
        )
        comparable = bool(present) and not any(
            isinstance(v, (dict, list)) for v in present
        )
        try:
            minimum, maximum = (
                (min(present), max(present)) if comparable else (None, None)
            )
        except TypeError:
            minimum, maximum = None, None
        result.append(
            {
                "name": name,
                "dtype": str(series.dtype),
                "total_records": len(values),
                "null_count": len(values) - len(present),
                "null_percentage": 100
                * (len(values) - len(present))
                / max(1, len(values)),
                "distinct_count": len(counts),
                "unique_values": sum(n == 1 for n in counts.values()),
                "duplicate_values": sum(n - 1 for n in counts.values()),
                "type_distribution": dict(types),
                "minimum": minimum,
                "maximum": maximum,
                "string_lengths": dict(Counter(map(len, strings))),
                "common_patterns": patterns.most_common(10),
                "whitespace_count": sum(v != v.strip() for v in strings),
                "case_inconsistencies": sum(len(v) > 1 for v in folded.values()),
                "date_patterns": dict(date_patterns),
                "date_format_inconsistency": len(date_patterns) > 1,
                "invalid_formats": None,
                "invalid_formats_note": "Requires an explicit validation rule; formats are not assumed.",
                "potential_identifier": bool(values)
                and len(counts) == len(values)
                and len(present) == len(values),
                "sample_values": present[:5],
                "nested": series.dtype.is_nested(),
            }
        )
    return {"row_count": frame.height, "columns": result}


def ingest(
    store,
    source_id,
    payload,
    filename,
    sheet=None,
    records_path=None,
    *,
    lineage=None,
    origin=None,
):
    source = store.get("sources", source_id)
    origin = origin or {
        "transport": "transformation" if lineage else "upload",
        "filename": filename,
    }
    fingerprint = hashlib.sha256(
        payload
        + b"\x00"
        + filename.rsplit(".", 1)[-1].lower().encode()
        + b"\x00"
        + (sheet or "").encode()
        + (b"\x00" + records_path.encode() if records_path else b"")
        + (b"\x00" + json.dumps(lineage, sort_keys=True).encode() if lineage else b"")
        + (
            b"\x00" + json.dumps(origin, sort_keys=True).encode()
            if origin["transport"] in {"http_get", "webhook"}
            else b""
        )
    ).hexdigest()
    with store.db() as db:
        existing = db.execute(
            "SELECT id FROM datasets WHERE source_id=? AND fingerprint=?",
            (source_id, fingerprint),
        ).fetchone()
    if existing:
        document = store.get("datasets", existing["id"])
        store.history(
            source_id,
            status="reused",
            dataset_id=document["id"],
            filename=filename,
            origin=origin,
        )
        return {**document, "reused": True}
    frame = read_file(payload, filename, sheet, records_path)
    if not frame.columns:
        raise InputError("The dataset must contain at least one column.")
    identifier = uid()
    raw_path = store.root / "raw" / identifier
    parquet_path = store.root / "datasets" / f"{identifier}.parquet"
    raw_path.write_bytes(payload)
    frame.write_parquet(parquet_path)
    schema = [{"name": n, "dtype": str(t)} for n, t in frame.schema.items()]
    with store.db() as db:
        # Serialize source-version comparison and registration across concurrent uploads.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM datasets WHERE source_id=? AND fingerprint=?",
            (source_id, fingerprint),
        ).fetchone()
        if existing:
            document = store.get("datasets", existing["id"])
        else:
            previous = db.execute(
                "SELECT document FROM datasets WHERE source_id=? ORDER BY rowid DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            old = (
                {
                    c["name"]: c["dtype"]
                    for c in json.loads(previous["document"])["schema"]
                }
                if previous
                else {}
            )
            current = {c["name"]: c["dtype"] for c in schema}
            drift = (
                {
                    "added": sorted(current.keys() - old.keys()),
                    "removed": sorted(old.keys() - current.keys()),
                    "type_changes": [
                        {"column": k, "before": old[k], "after": current[k]}
                        for k in old.keys() & current.keys()
                        if old[k] != current[k]
                    ],
                }
                if previous
                else None
            )
            document = {
                "id": identifier,
                "source_id": source_id,
                "source_name": source["name"],
                "origin": origin,
                "filename": filename,
                "sheet": sheet,
                "records_path": records_path,
                "fingerprint": fingerprint,
                "created_at": now(),
                "row_count": frame.height,
                "schema": schema,
                "schema_drift": drift,
                "previous_dataset_id": json.loads(previous["document"])["id"]
                if previous
                else None,
            }
            if lineage:
                document["lineage"] = lineage
            db.execute(
                "INSERT INTO datasets VALUES (?,?,?,?,?)",
                (
                    identifier,
                    source_id,
                    fingerprint,
                    document["created_at"],
                    json.dumps(document),
                ),
            )
    store.history(
        source_id,
        status="reused" if existing else "succeeded",
        dataset_id=document["id"],
        filename=filename,
        origin=origin,
    )
    return {**document, "reused": bool(existing)}


def frame_for(store, identifier):
    store.get("datasets", identifier)
    return pl.read_parquet(store.root / "datasets" / f"{identifier}.parquet")


def preview(store, identifier, offset, limit):
    store.get("datasets", identifier)
    # User strings never enter SQL identifiers or expressions.
    with duckdb.connect() as connection:
        result = connection.execute(
            "SELECT * FROM read_parquet(?) LIMIT ? OFFSET ?",
            [str(store.root / "datasets" / f"{identifier}.parquet"), limit, offset],
        ).to_arrow_table()
    return json_rows(pl.from_arrow(result))


def mapping_issues(store, document, latest=False):
    issues = []
    for field in document["fields"]:
        for ref in field["inputs"] + field["outputs"]:
            try:
                dataset = store.get("datasets", ref["dataset_id"])
                if latest:
                    dataset = next(
                        d
                        for d in store.list("datasets")
                        if d["source_id"] == dataset["source_id"]
                    )
                if ref["column"] not in [c["name"] for c in dataset["schema"]]:
                    issues.append(
                        f"Column {ref['column']!r} missing from dataset {dataset['id']}"
                    )
            except KeyError as error:
                issues.append(str(error))
        op = field["operation"]
        if op == "copy" and (len(field["inputs"]) != 1 or len(field["outputs"]) != 1):
            issues.append(
                "Copy requires one input and one output; use combine or split."
            )
        if op == "combine" and len(field["outputs"]) != 1:
            issues.append("Combine requires exactly one output.")
        if op == "split" and (len(field["inputs"]) != 1 or not field["separator"]):
            issues.append("Split requires one input and a nonempty separator.")
        if len({r["dataset_id"] for r in field["inputs"]}) > 1:
            issues.append(
                "Transformation inputs must come from one dataset; joins require reconciliation."
            )
    return issues


def suggestions(store, left, right):
    a, b = frame_for(store, left), frame_for(store, right)
    results = []
    for x in a.columns:
        for y in b.columns:
            name_score = WRatio(
                x.casefold().replace("_", " "), y.casefold().replace("_", " ")
            )
            compatible = a[x].dtype == b[y].dtype
            av = {
                str(v).strip().casefold()
                for v in a[x].head(100).to_list()
                if v is not None
            }
            bv = {
                str(v).strip().casefold()
                for v in b[y].head(100).to_list()
                if v is not None
            }
            overlap = len(av & bv) / max(1, len(av | bv))
            score = round(0.6 * name_score + 20 * compatible + 20 * overlap, 2)
            results.append(
                {
                    "left_column": x,
                    "right_column": y,
                    "score": score,
                    "name_similarity": round(name_score, 2),
                    "dtype_compatible": compatible,
                    "sample_overlap": overlap,
                    "requires_confirmation": True,
                    "explanation": "60% column-name similarity + 20% physical-type compatibility + 20% sample set overlap (first 100 rows).",
                }
            )
    return sorted(
        results, key=lambda r: (-r["score"], r["left_column"], r["right_column"])
    )
