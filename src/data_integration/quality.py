"""Read-only quality checks and candidate comparison for dataset snapshots."""

import unicodedata as ud
from collections import Counter
from itertools import product
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz.fuzz import ratio, token_sort_ratio

from .engine import InputError, frame_for, json_rows


class QualityQuery(BaseModel):
    search: str = ""
    filter_column: str | None = None
    filter_value: str = ""
    filter_mode: Literal["contains", "equals", "blank"] = "contains"
    group_by: str | None = None
    required_columns: list[str] = Field(default_factory=list)
    duplicate_columns: list[str] = Field(default_factory=list)
    duplicates_only: bool = False
    clean_column: str | None = None
    remove_symbols: bool = True
    offset: int = Field(0, ge=0)


class MatchQuery(BaseModel):
    left_id: str
    right_id: str
    left_name: str | None = None
    right_name: str | None = None
    left_phone: str | None = None
    right_phone: str | None = None
    threshold: float = Field(80, ge=0, le=100)
    name_weight: float = Field(50, ge=0, le=100)
    text_algorithm: Literal["token_sort", "character"] = "token_sort"


def clean(value, remove_symbols=True):
    if value is None:
        return None
    value = ud.normalize("NFKC", str(value))
    if remove_symbols:
        value = "".join(
            ""
            if ud.category(char) in {"Cf", "Me"} or char in "\ufe0e\ufe0f"
            else char
            if char.isalnum() or char.isspace() or ud.category(char) in {"Mn", "Mc"}
            else " "
            for char in value
        )
    return " ".join(value.split())


def phone_digits(value):
    if value is None:
        return ""
    return "".join(str(ud.decimal(char)) for char in str(value) if char.isdecimal())


def blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def require_columns(frame, columns):
    unknown = {column for column in columns if column is not None} - set(frame.columns)
    if unknown:
        raise InputError(f"Unknown columns: {sorted(unknown)}")


def inspect_dataset(store, dataset_id, query, *, full_report=False):
    frame = frame_for(store, dataset_id)
    require_columns(
        frame,
        [
            query.filter_column,
            query.group_by,
            query.clean_column,
            *query.duplicate_columns,
            *query.required_columns,
        ],
    )
    records = json_rows(frame)
    metadata = [
        {
            "column": column,
            "type": str(dtype),
            "blank": sum(blank(row[column]) for row in records),
            "distinct_nonblank": len(
                {repr(row[column]) for row in records if not blank(row[column])}
            ),
        }
        for column, dtype in frame.schema.items()
    ]
    search = query.search.strip().casefold()
    value = query.filter_value.strip().casefold()
    rows = []
    for index, row in enumerate(records, 1):
        if search and not any(
            search in str(v).casefold() for v in row.values() if v is not None
        ):
            continue
        if query.filter_column is not None:
            cell = row[query.filter_column]
            normalized = "" if cell is None else str(cell).strip().casefold()
            passes = (
                blank(cell)
                if query.filter_mode == "blank"
                else normalized == value
                if query.filter_mode == "equals"
                else value in normalized
            )
            if not passes:
                continue
        rows.append((index, row))

    def duplicate_key(row):
        values = [row[column] for column in query.duplicate_columns]
        if not values or any(blank(value) for value in values):
            return None
        return tuple(map(repr, values))

    keys = Counter(key for _, row in rows if (key := duplicate_key(row)) is not None)
    duplicates = {key: count for key, count in keys.items() if count > 1}
    filtered_count = len(rows)
    if query.duplicates_only:
        rows = [(index, row) for index, row in rows if duplicate_key(row) in duplicates]
    groups = Counter()
    if query.group_by is not None:
        groups.update(repr(row[query.group_by]) for _, row in rows)

    def row_issues(row):
        issues = [
            f"Required value is blank: {column}"
            for column in query.required_columns
            if blank(row[column])
        ]
        if duplicate_key(row) in duplicates:
            issues.append("Duplicate key in filtered rows")
        return issues

    flagged_count = sum(bool(row_issues(row)) for _, row in rows)
    items = []
    selected_rows = rows if full_report else rows[query.offset : query.offset + 100]
    for index, row in selected_rows:
        item = {"row": index, "values": row, "issues": row_issues(row)}
        if query.clean_column is not None:
            item["cleaned"] = clean(row[query.clean_column], query.remove_symbols)
        items.append(item)
    dataset = store.get("datasets", dataset_id)
    source = store.get("sources", dataset["source_id"])
    return {
        "clean_column": query.clean_column,
        "source": {
            key: source[key] for key in ("id", "name", "kind", "refresh_strategy")
        },
        "dataset": dataset,
        "columns": metadata,
        "filtered_count": filtered_count,
        "flagged_count": flagged_count,
        "duplicate_groups": len(duplicates),
        "duplicate_rows": sum(duplicates.values()),
        "visible_count": len(rows),
        "items": items,
        "group_count": len(groups),
        "groups": [
            {"value": key, "rows": count} for key, count in groups.most_common(100)
        ],
    }


def compare_datasets(store, query):
    left, right = (
        frame_for(store, identifier) for identifier in (query.left_id, query.right_id)
    )
    require_columns(left, [query.left_name, query.left_phone])
    require_columns(right, [query.right_name, query.right_phone])
    if query.left_id == query.right_id:
        raise InputError("Choose two different dataset versions.")
    if bool(query.left_name) != bool(query.right_name) or bool(
        query.left_phone
    ) != bool(query.right_phone):
        raise InputError("Select both columns for each comparison you enable.")
    if not (query.left_name or query.left_phone):
        raise InputError("Select text or number columns, phone columns, or both.")
    if left.height * right.height > 250_000:
        raise InputError(
            "Comparison exceeds 250,000 possible pairs. Use smaller datasets for this preview."
        )

    scorer = token_sort_ratio if query.text_algorithm == "token_sort" else ratio

    def normalized_text(value):
        if value is None:
            return ""
        if query.text_algorithm == "character":
            return (clean(value, remove_symbols=False) or "").casefold()
        return (clean(value) or "").casefold()

    def values(frame, name, phone):
        return [
            (
                index,
                row.get(name),
                row.get(phone),
                normalized_text(row.get(name)),
                phone_digits(row.get(phone)),
            )
            for index, row in enumerate(json_rows(frame), 1)
        ]

    hits = []
    for a, b in product(
        values(left, query.left_name, query.left_phone),
        values(right, query.right_name, query.right_phone),
    ):
        name_score = scorer(a[3], b[3]) if a[3] and b[3] else None
        phone_score = (100 if a[4] == b[4] else 0) if a[4] and b[4] else None
        if name_score is None and phone_score is None:
            continue
        if name_score is None or phone_score is None:
            score = phone_score if name_score is None else name_score
        else:
            weight = query.name_weight / 100
            score = name_score * weight + phone_score * (1 - weight)
        if score >= query.threshold:
            hits.append(
                {
                    "left_row": a[0],
                    "right_row": b[0],
                    "left_name": a[1],
                    "right_name": b[1],
                    "left_phone": a[2],
                    "right_phone": b[2],
                    "name_pct": round(name_score, 1)
                    if name_score is not None
                    else None,
                    "phone_pct": phone_score,
                    "score_pct": round(score, 1),
                }
            )
    hits.sort(key=lambda hit: -hit["score_pct"])
    return {
        "total": len(hits),
        "items": hits[:100],
        "pairs_checked": left.height * right.height,
    }
