from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class JsonlFileStore:
    """Append-only JSONL output and durable failure journal.

    Use a single service instance per output directory. For multi-process writes,
    use a database or a storage backend with atomic append/locking guarantees.
    """

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._results_path = output_dir / "vehicle_stats.jsonl"
        self._failures_path = output_dir / "failures.jsonl"
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._output_dir.mkdir, parents=True, exist_ok=True)

    async def append_result(self, record: BaseModel | dict[str, Any]) -> None:
        await self._append(self._results_path, record)

    async def append_failure(self, record: BaseModel | dict[str, Any]) -> None:
        await self._append(self._failures_path, record)

    async def _append(self, path: Path, record: BaseModel | dict[str, Any]) -> None:
        payload = record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append_sync, path, line)

    @staticmethod
    def _append_sync(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
