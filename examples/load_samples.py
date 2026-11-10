"""Generate imperfect fleet sample workbooks and load them through REST."""

import os
from pathlib import Path

import httpx
from openpyxl import Workbook

root = Path(__file__).parent / "sample-data"
root.mkdir(exist_ok=True)
samples = {
    "driver-master.xlsx": (
        ["Driver Name", "DL Number", "Truck"],
        [
            ["Jordan Lee", "001-23", "T1"],
            ["Jordan Lee", "009-87", "T2"],
            [" Ana  Ruiz ", "AA 123", "T3"],
            ["Sam Patel", None, "T4"],
        ],
    ),
    "training.xlsx": (
        ["attendee", "license_ref", "completed_on"],
        [
            ["Jordon Lee", "00123", "10/01/2026"],
            ["Ana Ruiz", "aa-123", "2026-10-02"],
            ["Ana Ruiz", "aa-123", "2026-10-15"],
            ["Sam Patel", None, None],
        ],
    ),
    "inspections.xlsx": (
        ["operator_label", "permit", "unit", "event_time", "received_time"],
        [
            [
                "Jordan Lee",
                "00123",
                "T1",
                "2026-10-16T06:00:00Z",
                "2026-10-18T07:00:00Z",
            ],
            ["Ana Ruiz", "AA123", "T4", "2026-10-16T06:00:00Z", "2026-10-16T06:01:00Z"],
            ["Ana Ruiz", "AA123", "T4", "2026-10-16T06:00:00Z", "2026-10-16T06:01:00Z"],
        ],
    ),
    "driving.xlsx": (
        ["person", "credential", "asset", "started_at"],
        [
            ["Jordan Lee", "00987", "T1", "2026-10-16T08:00:00Z"],
            ["Ana Ruiz", "AA123", "T4", "2026-10-16T08:00:00Z"],
            ["Sam Patel", None, "T3", "2026-10-17T08:00:00Z"],
        ],
    ),
}
with httpx.Client(
    base_url=os.environ.get("INTEGRATION_API_URL", "http://127.0.0.1:8000"), timeout=120
) as client:
    for filename, (headers, rows) in samples.items():
        wb = Workbook()
        wb.active.append(headers)
        for row in rows:
            wb.active.append(row)
        path = root / filename
        wb.save(path)
        source = client.post("/sources", json={"name": filename})
        source.raise_for_status()
        response = client.post(
            "/datasets/upload",
            data={"source_id": source.json()["id"]},
            files={"file": (filename, path.read_bytes())},
        )
        response.raise_for_status()
        print(filename, response.json()["id"])
