import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from samsara_poller.storage import JsonlFileStore


class Example(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_store_appends_jsonl(tmp_path: Path):
    store = JsonlFileStore(tmp_path)
    await store.initialize()
    await store.append_result(Example(value=1))
    await store.append_result({"value": 2})
    rows = [json.loads(line) for line in (tmp_path / "vehicle_stats.jsonl").read_text().splitlines()]
    assert rows == [{"value": 1}, {"value": 2}]
