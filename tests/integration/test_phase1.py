import io
import json
from concurrent.futures import ThreadPoolExecutor

import polars as pl
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from data_integration.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def source(client, name="Any domain"):
    response = client.post("/sources", json={"name": name})
    assert response.status_code == 201
    return response.json()["id"]


def upload(
    client,
    sid,
    content=b"Unusual Key,Some Value\n001,Alpha\n002,Alpha\n",
    name="input.csv",
    **data,
):
    return client.post(
        "/datasets/upload",
        data={"source_id": sid, **data},
        files={"file": (name, content)},
    )


def workbook(headers, rows):
    wb = Workbook()
    wb.active.title = "Custom"
    wb.active.append(headers)
    for row in rows:
        wb.active.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def field(a, x, b, y, operation="copy"):
    return {
        "inputs": [{"dataset_id": a, "column": x}],
        "outputs": [{"dataset_id": b, "column": y}],
        "operation": operation,
    }


def test_excel_discovery_profile_mapping_drift_persistence(client):
    sid = source(client)
    raw = workbook(
        ["Arbitrary Ref", "Label"], [["001", " Ada "], ["002", "ada"], [None, "ada"]]
    )
    response = upload(client, sid, raw, "first.xlsx", sheet="Custom")
    assert response.status_code == 201, response.text
    a = response.json()["id"]
    second = upload(
        client,
        source(client),
        workbook(["External Id", "Description"], [["001", "Ada"]]),
        "second.xlsx",
    ).json()["id"]
    schema = client.get(f"/datasets/{a}/schema").json()
    assert [c["name"] for c in schema["schema"]] == ["Arbitrary Ref", "Label"]
    assert (
        client.get(f"/datasets/{a}/preview").json()["items"][0]["Arbitrary Ref"]
        == "001"
    )
    stats = client.get(f"/datasets/{a}/profile", params={"columns": "Label"}).json()[
        "columns"
    ]
    assert len(stats) == 1
    assert stats[0]["duplicate_values"] == 1
    assert stats[0]["whitespace_count"] == 1
    assert (
        client.get(
            f"/datasets/{a}/profile", params={"columns": "not there"}
        ).status_code
        == 422
    )
    suggestions = client.post(
        "/mappings/suggest", json={"left_dataset_id": a, "right_dataset_id": second}
    ).json()["items"]
    assert suggestions and all(s["requires_confirmation"] for s in suggestions)
    mapping = client.post(
        "/mappings",
        json={
            "name": "Confirmed",
            "fields": [field(a, "Arbitrary Ref", second, "External Id")],
        },
    ).json()
    assert mapping["version"] == 1
    newer = upload(
        client, sid, workbook(["New Ref", "Label"], [["003", "Bob"]]), "new.xlsx"
    ).json()
    assert newer["schema_drift"]["removed"] == ["Arbitrary Ref"]
    status = client.get(f"/mappings/{mapping['id']}").json()
    assert not status["issues"] and status["latest_source_issues"]
    reopened = TestClient(create_app(client.app.state.store.root))
    assert reopened.get(f"/mappings/{mapping['id']}").json()["name"] == "Confirmed"
    assert (client.app.state.store.root / "raw" / a).read_bytes() == raw


@pytest.mark.parametrize("extension", ["csv", "json", "parquet"])
def test_formats_and_idempotency(client, extension):
    frame = pl.DataFrame({"opaque": ["0007", "8"], "nested_text": ["one", None]})
    buffer = io.BytesIO()
    if extension == "parquet":
        frame.write_parquet(buffer)
        payload = buffer.getvalue()
    elif extension == "json":
        payload = frame.write_json().encode()
    else:
        payload = frame.write_csv().encode()
    sid = source(client)
    first = upload(client, sid, payload, f"data.{extension}").json()
    second = upload(client, sid, payload, f"data.{extension}").json()
    assert first["id"] == second["id"]
    assert second["reused"]
    assert client.get("/datasets").json()["total"] == 1
    assert client.get(f"/sources/{sid}/history").json()["total"] == 2


def test_nested_json_and_type_drift(client):
    sid = source(client)
    first = upload(
        client,
        sid,
        json.dumps([{"dynamic": {"deep": [1, 2]}, "value": 1}]).encode(),
        "x.json",
    ).json()
    assert client.get(f"/datasets/{first['id']}/profile").json()["columns"][0]["nested"]
    second = upload(
        client, sid, b'[{"dynamic":{"deep":[1]},"value":"text"}]', "x.json"
    ).json()
    assert second["schema_drift"]["type_changes"][0]["column"] == "value"


def test_mapping_combine_split_edit_and_invalid_columns(client):
    a = upload(
        client, source(client), b"First,Last,Full\nAda,Lovelace,Ada Lovelace\n"
    ).json()["id"]
    b = upload(
        client, source(client), b"Given,Family,Name\nAda,Lovelace,Ada Lovelace\n"
    ).json()["id"]
    combine = field(a, "First", b, "Name", "combine")
    combine["inputs"].append({"dataset_id": a, "column": "Last"})
    body = {"name": "Names", "fields": [combine]}
    preview = client.post("/mappings/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert (
        preview.json()["fields"][0]["rows"][0]["outputs"][0]["value"] == "Ada Lovelace"
    )
    created = client.post("/mappings", json=body).json()
    split = field(a, "Full", b, "Given", "split")
    split["outputs"].append({"dataset_id": b, "column": "Family"})
    body["fields"] = [split]
    assert (
        client.post("/mappings/preview", json=body).json()["fields"][0]["rows"][0][
            "outputs"
        ][1]["value"]
        == "Lovelace"
    )
    edited = client.put(f"/mappings/{created['id']}", json=body).json()
    assert edited["version"] == 2 and len(edited["history"]) == 1
    body["fields"][0]["inputs"][0]["column"] = "missing"
    assert client.post("/mappings", json=body).status_code == 422


def test_errors_pagination_and_failed_history(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/datasets/missing/schema").status_code == 404
    assert "error" in client.get("/datasets", params={"limit": 0}).json()
    sid = source(client)
    assert upload(client, sid, b"not valid", "bad.xlsx").status_code == 422
    assert (
        client.get(f"/sources/{sid}/history").json()["items"][0]["status"] == "failed"
    )
    upload(client, sid)
    assert len(client.get("/datasets", params={"limit": 1}).json()["items"]) == 1
    assert client.get("/datasets", params={"offset": 1}).json()["items"] == []


def test_concurrent_idempotent_ingestion(client):
    sid = source(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upload(client, sid).json(), range(2)))
    assert results[0]["id"] == results[1]["id"]
    assert client.get("/datasets").json()["total"] == 1


def test_quality_filters_groups_and_cleaning(client):
    dataset = upload(
        client,
        source(client),
        "Contact,Team\n Ada 😀 ,Blue\n Ada 😀 ,Blue\nBob,Red\n,Blue\n".encode(),
    ).json()["id"]
    result = client.post(
        f"/datasets/{dataset}/quality",
        json={
            "search": "ada",
            "filter_column": "Team",
            "filter_value": "BLUE",
            "filter_mode": "equals",
            "group_by": "Team",
            "duplicate_columns": ["Contact", "Team"],
            "duplicates_only": True,
            "clean_column": "Contact",
        },
    ).json()
    assert result["dataset"]["row_count"] == 4
    assert result["filtered_count"] == result["duplicate_rows"] == 2
    assert result["duplicate_groups"] == 1
    assert result["groups"][0]["rows"] == 2
    assert result["columns"][0]["blank"] == 1
    assert result["items"][0]["values"]["Contact"] == " Ada 😀 "
    assert result["items"][0]["cleaned"] == "Ada"


def test_match_uses_selected_columns_and_separate_scores(client):
    left = upload(
        client,
        source(client),
        "First Label,Tel\nAda Lovelace 😀,(312) 555-0100\n".encode(),
    ).json()["id"]
    right = upload(
        client,
        source(client),
        b"Full,Number\nLovelace Ada,3125550100\nLovelace Ada,3125550101\n",
    ).json()["id"]
    result = client.post(
        "/matching/preview",
        json={
            "left_id": left,
            "right_id": right,
            "left_name": "First Label",
            "right_name": "Full",
            "left_phone": "Tel",
            "right_phone": "Number",
            "name_weight": 70,
            "threshold": 60,
        },
    ).json()
    assert [
        (row["name_pct"], row["phone_pct"], row["score_pct"]) for row in result["items"]
    ] == [
        (100, 100, 100),
        (100, 0, 70),
    ]


@pytest.mark.parametrize(
    ("view", "statuses"),
    [
        ("inner", ["Matched", "Matched"]),
        ("left", ["Matched", "Matched", "Left only", "Left only"]),
        ("right", ["Matched", "Matched", "Right only"]),
        ("full", ["Matched", "Matched", "Left only", "Left only", "Right only"]),
        ("unmatched", ["Left only", "Left only", "Right only"]),
    ],
)
def test_reconciliation_views_and_report(client, view, statuses):
    left = upload(
        client, source(client), b"Name,Team\n Ada! ,X\nAda,X\nBob,X\n,X\n"
    ).json()["id"]
    right = upload(client, source(client), b"Contact,Group\nada,X\nada,Y\n").json()[
        "id"
    ]
    response = client.post(
        "/reconciliation/runs",
        json={
            "left_id": left,
            "right_id": right,
            "view": view,
            "keys": [
                {"left": "Name", "right": "Contact", "method": "clean_text"},
                {"left": "Team", "right": "Group"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["counts"]["matched_right_rows"] == 1
    assert run["counts"]["left_blank_keys"] == 1
    assert run["counts"]["left_duplicate_keys"] == 1
    assert [row["status"] for row in run["items"]] == statuses
    assert (
        client.get(f"/reconciliation/runs/{run['id']}/report").json()["items"]
        == run["items"]
    )
    assert client.get("/reconciliation/runs").json()["items"][0]["id"] == run["id"]


def test_quality_run_tracks_dataset_settings_and_counts(client):
    dataset = upload(client, source(client)).json()["id"]
    response = client.post(
        f"/datasets/{dataset}/quality/runs", json={"duplicate_columns": ["Some Value"]}
    )
    assert response.status_code == 201, response.text
    run = client.get("/quality/runs").json()["items"][0]
    assert run["id"] == response.json()["run_id"]
    assert run["dataset_id"] == dataset
    assert run["configuration"]["duplicate_columns"] == ["Some Value"]
    assert run["summary"]["duplicate_rows"] == 2


def test_nested_json_selection_and_partial_governed_presence(client):
    sid = source(client)
    payload = b'{"data":{"items":[{"A":"Ada","B":"X","C":"001"},{"A":"Bob","B":"Y","C":"002"}]}}'
    left = upload(client, sid, payload, "api.json", records_path="/data/items")
    assert left.status_code == 201, left.text
    assert left.json()["records_path"] == "/data/items"
    assert (
        upload(client, sid, payload, "api.json", records_path="/missing").status_code
        == 422
    )
    right = upload(
        client,
        source(client),
        b"Name,Group,Ref\nAda,X,999\nAda,Z,001\nBob,Y,999\nBob,Y,888\n",
    ).json()["id"]
    result = client.post(
        "/reconciliation/runs",
        json={
            "left_id": left.json()["id"],
            "right_id": right,
            "view": "full",
            "minimum_agreements": 2,
            "keys": [
                {"left": "A", "right": "Name"},
                {"left": "B", "right": "Group", "required": True},
                {"left": "C", "right": "Ref"},
            ],
        },
    ).json()
    assert result["counts"]["matched_pairs"] == 3
    assert result["counts"]["left_multiple_candidates"] == 1
    assert result["counts"]["right_only"] == 1
    assert result["items"][0]["agreement_count"] == 2
    assert "B ↔ Group" in result["items"][0]["agreed_fields"]
    assert result["items"][-1]["status"] == "Right only"


def test_quality_row_report_includes_required_and_duplicate_issues(client):
    dataset = upload(client, source(client), b"ID,Name\n001, Ada \n001,\n").json()["id"]
    run = client.post(
        f"/datasets/{dataset}/quality/runs",
        json={
            "required_columns": ["Name"],
            "duplicate_columns": ["ID"],
            "clean_column": "Name",
            "offset": 100,
        },
    ).json()
    assert run["flagged_count"] == 2
    assert run["items"] == []
    report = client.get(f"/quality/runs/{run['run_id']}/report").json()["items"]
    assert report[0]["Cleaned value"] == "Ada"
    assert report[0]["Original · Name"] == " Ada "
    assert report[1]["Source row"] == 2
    assert "Required value is blank: Name" in report[1]["Issues"]
    assert "Duplicate key" in report[1]["Issues"]


def test_ordered_recipe_validation_rerun_and_publication(client):
    sid = source(client)
    invalid = upload(client, sid, b"Date,Name\n2026-01-02, Ada! \nbad,Ada\n").json()[
        "id"
    ]
    recipe = client.post(
        "/transformation/recipes",
        json={
            "name": "Contacts",
            "source_id": sid,
            "steps": [
                {"operation": "clean_text", "column": "Name", "output": "Clean name"},
                {"operation": "unique", "column": "Clean name"},
                {
                    "operation": "parse_date",
                    "column": "Date",
                    "date_format": "%Y-%m-%d",
                },
            ],
        },
    ).json()
    route = f"/transformation/recipes/{recipe['id']}/runs"
    first = client.post(route, json={"dataset_id": invalid})
    assert first.status_code == 201, first.text
    failed = first.json()
    assert [step["issues"] for step in failed["trace"]] == [0, 2, 1]
    base = f"/transformation/runs/{failed['id']}"
    assert client.post(f"{base}/publish").status_code == 422
    assert len(client.get(f"{base}/report?kind=exceptions").json()["items"]) == 3
    report = client.get(f"{base}/report").json()["items"]
    assert report[0]["Original · Name"] == " Ada! "
    assert report[0]["Output · Clean name"] == "Ada"
    assert report[1]["Output · Date"] is None

    corrected = upload(
        client, sid, b"Date,Name\n2026-01-02,Ada\n2026-01-03,Bob\n"
    ).json()["id"]
    passed = client.post(route, json={"dataset_id": corrected}).json()
    assert passed["issue_count"] == 0
    publish = f"/transformation/runs/{passed['id']}/publish"
    output = client.post(publish).json()
    assert output["lineage"]["input_dataset_id"] == corrected
    assert client.post(publish).json()["id"] == output["id"]
    result = client.post(
        "/reconciliation/runs",
        json={
            "left_id": output["id"],
            "right_id": corrected,
            "keys": [{"left": "Clean name", "right": "Name"}],
        },
    ).json()
    assert result["counts"]["matched_pairs"] == 2
    assert len(client.get("/transformation/runs").json()["items"]) == 2

    changed = upload(client, sid, b"Date,Label\n2026-01-02,Ada\n").json()["id"]
    rejected = client.post(route, json={"dataset_id": changed})
    assert rejected.status_code == 422
    assert "Step 1" in rejected.json()["error"]["message"]


def test_presence_checks_arbitrary_columns_without_expanding_matches(client):
    source_values = (
        b"Asset code,Description\n"
        + b" TR-01 ,Truck\n" * 400
        + b"TR-02,Missing\n,Blank\n"
    )
    reference_values = b"Inventory Ref\n" + b"tr-01\n" * 400 + b"TR01\n"
    left = upload(client, source(client), source_values).json()["id"]
    right = upload(client, source(client), reference_values).json()["id"]
    response = client.post(
        "/reconciliation/runs",
        json={
            "left_id": left,
            "right_id": right,
            "view": "presence",
            "keys": [
                {
                    "left": "Asset code",
                    "right": "Inventory Ref",
                    "method": "trimmed_text",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["output_rows"] == 402
    assert run["counts"]["matched_pairs"] == 160_000
    assert run["counts"]["matched_left_rows"] == 400
    assert run["counts"]["left_missing"] == 1
    assert run["counts"]["left_not_checkable"] == 1
    assert run["items"][0]["left_candidate_count"] == 400
    route = f"/reconciliation/runs/{run['id']}/report"
    missing = client.get(route, params={"status": "Missing", "limit": 100}).json()
    assert missing["total"] == 1
    assert missing["items"][0]["Left · Asset code"] == "TR-02"
    assert (
        client.get(route, params={"status": "No usable key"}).json()["items"][0][
            "left_row"
        ]
        == 402
    )


def test_ordered_match_stops_at_first_candidate_and_can_reorder(client):
    left = upload(
        client,
        source(client),
        b"Mobile,License,Login,Full Name\n"
        b"(312) 555-0101,TST-002,,Bob Baker\n"
        b"3125550199,TST-002,other,Bob Baker\n"
        b",,User3,Charlie Doe\n"
        b",,,Zimmerman Olivia\n"
        b",,,abcdefghij\n"
        b"3125550105,TST-005,,Grace Green\n"
        b",,,\n",
    ).json()["id"]
    right = upload(
        client,
        source(client),
        b"Contact,DL,Username,Name\n"
        b"+1 312 555 0101,TST-001,user1,Alice Adams\n"
        b",tst002,user2,Bob Baker\n"
        b",, user3 ,Charlie Doe\n"
        b",,,Olivia Zimmerman\n"
        b",,,abcdefghix\n"
        b"3125550105,TST-005,,Grace Green\n"
        b"3125550105,TST-006,,Greta Gray\n",
    ).json()["id"]
    settings = {
        "left_id": left,
        "right_id": right,
        "view": "ordered",
        "keys": [
            {"left": "Mobile", "right": "Contact", "method": "phone_digits"},
            {"left": "License", "right": "DL", "method": "license_id"},
            {"left": "Login", "right": "Username", "method": "trimmed_text"},
            {"left": "Full Name", "right": "Name", "method": "fuzzy_name"},
        ],
    }
    response = client.post("/reconciliation/runs", json=settings)
    assert response.status_code == 201, response.text
    run = response.json()
    rows = run["items"]
    assert run["output_rows"] == 7
    assert [row["status"] for row in rows] == [
        "Matched",
        "Matched",
        "Matched",
        "Matched",
        "Missing",
        "Ambiguous",
        "No usable value",
    ]
    assert [row["matched_rule"] for row in rows[:4]] == [
        "Phone",
        "Driver license",
        "Username",
        "Name similarity",
    ]
    assert rows[0]["right_row"] == 1
    assert rows[0]["checks_tried"] == "Phone: matched (1 candidate)"
    assert rows[1]["checks_tried"].startswith(
        "Phone: no match → Driver license: matched"
    )
    assert rows[3]["score_pct"] == 100
    assert rows[5]["right_row"] is None
    assert rows[5]["candidate_count"] == 2
    assert rows[5]["checks_tried"] == "Phone: ambiguous (2 candidates)"
    report = client.get(
        f"/reconciliation/runs/{run['id']}/report", params={"status": "Missing"}
    ).json()
    assert [row["left_row"] for row in report["items"]] == [5]

    settings["keys"][0], settings["keys"][1] = settings["keys"][1], settings["keys"][0]
    reordered = client.post("/reconciliation/runs", json=settings).json()
    assert reordered["counts"]["review"] == 2
    assert reordered["items"][0]["status"] == "Review"
    assert reordered["items"][1]["status"] == "Review"
    assert reordered["items"][0]["right_row"] == 2
    assert (
        reordered["items"][0]["checks_tried"] == "Driver license: matched (1 candidate)"
    )


def test_find_searches_latest_sources_and_can_scope_to_an_old_version(client):
    orders = source(client, "Orders")
    old = upload(client, orders, b"Order,Contact\nOld-1,ADA\n").json()["id"]
    latest = upload(client, orders, b"Order,Contact\nAda-2,Ada\nAda-3,Bob\n").json()[
        "id"
    ]
    people = upload(
        client,
        source(client, "Fleet"),
        b'[{"profile":{"name":"Ada"},"driver":"D-001"}]',
        "fleet.json",
    ).json()["id"]
    result = client.post(
        "/find", json={"text": " aDA ", "offset": 1, "limit": 1}
    ).json()
    assert result["total"] == 3
    assert result["datasets_searched"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["dataset_id"] == latest
    assert result["items"][0]["matches"] == {"Order": "Ada-2", "Contact": "Ada"}
    scoped = client.post(
        "/find", json={"text": "ada", "dataset_id": latest, "column": "Contact"}
    ).json()
    assert scoped["total"] == 1
    assert scoped["items"][0]["matches"] == {"Contact": "Ada"}
    original = client.post("/find", json={"text": "ada", "dataset_id": old}).json()
    assert original["items"][0]["record"]["Order"] == "Old-1"
    nested = client.post("/find", json={"text": "ADA", "dataset_id": people}).json()
    assert nested["items"][0]["matches"] == {"profile": {"name": "Ada"}}
    assert client.post("/find", json={"text": "   "}).status_code == 422


@pytest.mark.parametrize("auth_mode", ["bearer", "header", "query"])
def test_get_connector_imports_original_filename_and_records_origin(
    client, monkeypatch, auth_mode
):
    from contextlib import contextmanager

    import httpx

    monkeypatch.setenv("INTEGRATION_HTTP_ALLOWED_HOSTS", "fleet.example.test")
    monkeypatch.setenv("INTEGRATION_HTTP_TOKEN_FLEET", "fake-bearer")

    def upstream(request):
        assert request.method == "GET"
        if auth_mode == "bearer":
            assert request.headers["authorization"] == "Bearer fake-bearer"
        elif auth_mode == "header":
            assert request.headers["x-api-key"] == "fake-bearer"
        else:
            assert request.url.params["api_key"] == "fake-bearer"
        assert request.url.params["page"] == "1"
        assert request.url.params["limit"] == "25"
        assert request.url.params.get_list("types") == ["speed", "location"]
        return httpx.Response(
            200,
            headers={"content-disposition": 'attachment; filename="fleet-export.json"'},
            json={"data": {"drivers": [{"Driver": "Ada"}]}},
        )

    @contextmanager
    def stream(method, url, **kwargs):
        assert kwargs.pop("follow_redirects") is False
        assert kwargs.pop("trust_env") is False
        with (
            httpx.Client(transport=httpx.MockTransport(upstream)) as remote,
            remote.stream(method, url, **kwargs) as response,
        ):
            yield response

    monkeypatch.setattr(httpx, "stream", stream)
    rejected = client.post(
        "/sources",
        json={
            "name": "Unapproved",
            "kind": "http",
            "connection_settings": {
                "url": "https://fleet.example.test.other.test/export"
            },
        },
    )
    assert rejected.status_code == 422
    connection = client.post(
        "/sources",
        json={
            "name": "Fleet API",
            "kind": "http",
            "connection_settings": {
                "url": "https://fleet.example.test/export?page=1",
                "query_params": {"limit": 25, "types": ["speed", "location"]},
                "records_path": "/data/drivers",
                **(
                    {"bearer_env": "INTEGRATION_HTTP_TOKEN_FLEET"}
                    if auth_mode == "bearer"
                    else {
                        "custom_token": {
                            "location": auth_mode,
                            "name": "X-API-Key" if auth_mode == "header" else "api_key",
                            "token_env": "INTEGRATION_HTTP_TOKEN_FLEET",
                        }
                    }
                ),
            },
        },
    )
    assert connection.status_code == 201, connection.text
    sid = connection.json()["id"]
    response = client.post(f"/sources/{sid}/refresh")
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["filename"] == "fleet-export.json"
    assert data["source_name"] == "Fleet API"
    assert data["row_count"] == 1
    assert data["origin"]["endpoint"] == "https://fleet.example.test/export"
    assert (
        client.get(f"/sources/{sid}/history").json()["items"][0]["origin"]
        == data["origin"]
    )


def test_webhook_requires_secret_and_preserves_sender_filename(client):
    source = client.post(
        "/sources", json={"name": "Inbound feed", "kind": "webhook"}
    ).json()
    route = f"/webhooks/{source['id']}"
    assert client.post(route, json=[{"Driver": "Ada"}]).status_code == 401
    result = client.post(
        route,
        json=[{"Driver": "Ada"}],
        headers={
            "Authorization": f"Bearer {source['webhook_token']}",
            "X-Source-Filename": "driver-webhook.json",
        },
    )
    assert result.status_code == 201, result.text
    assert result.json()["origin"]["sender_filename"] == "driver-webhook.json"
    assert result.json()["source_name"] == "Inbound feed"
    sources = client.get("/sources").json()["items"]
    assert all(
        "webhook_token" not in row and "webhook_digest" not in row for row in sources
    )


def test_fuzzy_character_comparison_supports_numeric_values(client):
    left = upload(client, source(client), b'[{"code":12345}]', "a.json").json()["id"]
    right = upload(client, source(client), b'[{"other":12346}]', "b.json").json()["id"]
    result = client.post(
        "/matching/preview",
        json={
            "left_id": left,
            "right_id": right,
            "left_name": "code",
            "right_name": "other",
            "text_algorithm": "character",
            "threshold": 75,
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["name_pct"] == 80


def test_ordered_match_reports_conflicts_after_early_match(client):
    left = upload(
        client,
        source(client),
        "Phone,Name,Status\n3125550100,Ada 🚚,Pending\n".encode(),
    ).json()["id"]
    right = upload(
        client, source(client), b"Mobile,Full Name,PTI\n(312) 555-0100,Ada,Complete\n"
    ).json()["id"]
    response = client.post(
        "/reconciliation/runs",
        json={
            "left_id": left,
            "right_id": right,
            "view": "ordered",
            "name_threshold": 95,
            "keys": [
                {"left": "Phone", "right": "Mobile", "method": "phone_digits"},
                {"left": "Name", "right": "Full Name", "method": "fuzzy_name"},
            ],
            "audit_fields": [{"left": "Status", "right": "PTI", "method": "exact"}],
        },
    )
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["counts"]["matched"] == run["counts"]["rows_with_differences"] == 1
    row = run["items"][0]
    assert row["checks_tried"] == "Phone: matched (1 candidate)"
    assert [change["Difference"] for change in row["field_differences"]] == [
        "Format or word order",
        "Format or word order",
        "Different value",
    ]
    assert row["field_differences"][-1]["Reference value"] == "Complete"
    report = client.get(
        f"/reconciliation/runs/{run['id']}/report", params={"differences_only": True}
    ).json()
    assert report["items"] == [row]


def test_json_spreadsheet_range_uses_first_row_as_headers(client):
    result = upload(
        client,
        source(client),
        b'{"values":[["Driver","Code"],["Ada","001"],["Ben"]]}',
        "range.json",
        records_path="/values",
    )
    assert result.status_code == 201, result.text
    rows = client.get(f"/datasets/{result.json()['id']}/preview").json()["items"]
    assert rows == [{"Driver": "Ada", "Code": "001"}, {"Driver": "Ben", "Code": None}]


def test_monday_board_uses_graphql_post_and_imports_items(client, monkeypatch):
    from contextlib import contextmanager

    import httpx

    monkeypatch.setenv("INTEGRATION_HTTP_ALLOWED_HOSTS", "api.monday.com")
    monkeypatch.setenv("INTEGRATION_HTTP_TOKEN_MONDAY", "test-token")

    def upstream(request):
        assert request.method == "POST"
        assert request.headers["authorization"] == "test-token"
        body = json.loads(request.content)
        assert body["variables"] == {"board": ["123"]}
        assert body["query"].startswith("query BoardItems")
        return httpx.Response(
            200,
            json={
                "data": {
                    "boards": [
                        {
                            "items_page": {
                                "cursor": None,
                                "items": [{"id": "1", "name": "Ada"}],
                            }
                        }
                    ]
                }
            },
        )

    @contextmanager
    def stream(method, url, **kwargs):
        kwargs.pop("trust_env")
        kwargs.pop("follow_redirects")
        with (
            httpx.Client(transport=httpx.MockTransport(upstream)) as remote,
            remote.stream(method, url, **kwargs) as response,
        ):
            yield response

    monkeypatch.setattr(httpx, "stream", stream)
    result = client.post(
        "/sources",
        json={
            "name": "Board",
            "kind": "http",
            "connection_settings": {
                "url": "https://api.monday.com/v2",
                "monday_board_id": "123",
                "custom_token": {
                    "location": "header",
                    "name": "Authorization",
                    "token_env": "INTEGRATION_HTTP_TOKEN_MONDAY",
                },
            },
        },
    )
    assert result.status_code == 201, result.text
    source_id = result.json()["id"]
    dataset = client.post(f"/sources/{source_id}/refresh")
    assert dataset.status_code == 201, dataset.text
    assert dataset.json()["origin"]["transport"] == "monday_graphql"
    assert dataset.json()["row_count"] == 1
