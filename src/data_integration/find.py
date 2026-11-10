"""Case-insensitive value search across immutable dataset snapshots."""

from pydantic import BaseModel, Field

from .engine import InputError, frame_for, json_rows
from .quality import require_columns


class FindQuery(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    dataset_id: str | None = None
    column: str | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=100)


def find_rows(store, query):
    needle = query.text.strip().casefold()
    if not needle:
        raise InputError("Enter a value to find.")
    if query.column is not None and query.dataset_id is None:
        raise InputError(
            "Choose a dataset version before restricting the search to a column."
        )
    if query.dataset_id is not None:
        datasets = [store.get("datasets", query.dataset_id)]
    else:
        # Dataset history is newest first. Select one snapshot per source.
        latest = {}
        for dataset in store.list("datasets"):
            latest.setdefault(dataset["source_id"], dataset)
        datasets = list(latest.values())

    items = []
    total = 0
    for dataset in datasets:
        frame = frame_for(store, dataset["id"])
        require_columns(frame, [query.column])
        fields = [query.column] if query.column is not None else frame.columns
        source = store.get("sources", dataset["source_id"])
        for number, row in enumerate(json_rows(frame), 1):
            matches = {
                field: row[field]
                for field in fields
                if row[field] is not None and needle in str(row[field]).casefold()
            }
            if not matches:
                continue
            if total >= query.offset and len(items) < query.limit:
                items.append(
                    {
                        "dataset_id": dataset["id"],
                        "filename": dataset["filename"],
                        "source_name": source["name"],
                        "row": number,
                        "matches": matches,
                        "record": row,
                    }
                )
            total += 1
    return {
        "items": items,
        "total": total,
        "offset": query.offset,
        "limit": query.limit,
        "datasets_searched": len(datasets),
    }
