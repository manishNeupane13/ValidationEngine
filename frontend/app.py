"""REST-only UI: no ingestion, profiling, or matching business logic."""

import csv
import io
import json
import os
from pprint import pformat

import httpx
import streamlit as st

st.set_page_config(page_title="Data Integration", layout="wide")
st.title("Data Integration Workspace")
st.caption("Import files, inspect records, and reconcile data across sources.")
base = os.environ.get("INTEGRATION_API_URL", "http://127.0.0.1:8000")


def api(method, path, **kwargs):
    try:
        response = httpx.request(method, base + path, timeout=120, **kwargs)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, httpx.HTTPStatusError):
            st.error(error.response.text)
        else:
            st.error(f"Backend unavailable: {error}")
        st.stop()


def all_items(path):
    items, offset = [], 0
    while True:
        page = api("GET", path, params={"limit": 500, "offset": offset})
        items.extend(page["items"])
        offset += len(page["items"])
        if offset >= page["total"]:
            return items


NAVIGATION = {
    "Sources": {
        "Upload data": "upload",
        "Create source": "create_source",
        "API & Webhook": "connect_source",
        "Source dashboard": "source_dashboard",
        "Import history": "import_history",
    },
    "Find": {"Find records": "find"},
    "Explore": {
        "Browse rows": "explorer",
        "Profile columns": "profiling",
        "Map columns": "mapping",
    },
    "Transform": {
        "Build recipe": "build_recipe",
        "Run saved recipe": "run_recipe",
    },
    "Validate": {"Data quality": "quality"},
    "Is In": {"Check values across files": "presence"},
    "IF / ELSE": {"IF / ELSE match": "ordered_match"},
    "Match & Join": {
        "Join datasets": "join",
        "Fuzzy comparison": "compare",
    },
    "Reports": {
        "Transformation runs": "transform_history",
        "Validation runs": "quality_history",
        "Join runs": "join_history",
    },
}


NAVIGATION_DETAILS = {
    "Sources": (
        "folder_open",
        "Create a source, upload a new dataset version, or review previous imports.",
    ),
    "Find": (
        "search",
        "Search for a name, number, ID, or other value across imported records.",
    ),
    "Explore": (
        "table_view",
        "Browse rows, inspect column statistics, or map fields between datasets.",
    ),
    "Transform": (
        "tune",
        "Build and rerun recipes for cleaning, extracting, and standardizing values.",
    ),
    "Validate": (
        "fact_check",
        "Check required fields and duplicates, filter records, and export issues.",
    ),
    "Is In": (
        "checklist",
        "Check whether values from one file appear in a selected column of another.",
    ),
    "IF / ELSE": (
        "rule",
        "Run matching rules in order and review field differences on matched records.",
    ),
    "Match & Join": (
        "merge",
        "Join datasets or compare candidates with fuzzy similarity.",
    ),
    "Reports": (
        "assessment",
        "Reopen previous runs, review the rules used, and download the results.",
    ),
}

TASK_DETAILS = {
    "upload": "Upload Excel, CSV, JSON, or Parquet into an existing source.",
    "create_source": "Name a recurring file feed and configure how it will be refreshed.",
    "connect_source": "Fetch a file with HTTP GET or receive JSON from another system.",
    "source_dashboard": "Review imports, dataset versions, and records for each source or API endpoint.",
    "import_history": "See which dataset versions were imported and when.",
    "explorer": "Preview dataset rows and inspect source metadata.",
    "profiling": "Measure blanks, repeated values, types, and patterns in selected columns.",
    "mapping": "Save which fields correspond across source schemas.",
    "build_recipe": "Set an order for cleaning, extraction, and validation steps.",
    "run_recipe": "Apply saved steps to a specific version of the source.",
    "join": "Join on selected columns and inspect matched or unmatched rows.",
    "compare": "Score possible pairs by name, text, number, or exact phone agreement.",
    "transform_history": "Review recipe steps, output rows, and conversion exceptions.",
    "quality_history": "Reopen quality reports or rerun checks on updated files.",
    "join_history": "Reopen saved joins, Is In checks, and ordered matching runs.",
}


def set_navigation(group, task=None):
    st.session_state["nav_group"] = group
    if task:
        st.session_state[f"nav_task_{group}"] = task


def navigation_bar():
    group = st.session_state.get("nav_group", "Sources")
    with st.container(border=True, key="workspace-navigation"):
        for panel, section in zip(st.columns(len(NAVIGATION)), NAVIGATION, strict=True):
            icon, description = NAVIGATION_DETAILS[section]
            panel.button(
                section,
                icon=f":material/{icon}:",
                help=description,
                key=f"nav-{section}",
                type="primary" if group == section else "secondary",
                use_container_width=True,
                on_click=set_navigation,
                args=(section,),
            )
        st.caption(NAVIGATION_DETAILS[group][1])
        tasks = NAVIGATION[group]
        task = st.session_state.get(f"nav_task_{group}")
        if task not in tasks.values():
            task = next(iter(tasks.values()))
        if len(tasks) > 1:
            panels = st.columns(len(tasks))
            for panel, (title, slug) in zip(panels, tasks.items(), strict=False):
                panel.button(
                    title,
                    key=f"task-{slug}",
                    help=TASK_DETAILS[slug],
                    type="primary" if task == slug else "secondary",
                    use_container_width=True,
                    on_click=set_navigation,
                    args=(group, slug),
                )
            st.caption(TASK_DETAILS[task])
    return task


page = navigation_bar()
datasets = all_items("/datasets")
by_id = {d["id"]: d for d in datasets}


def label(identifier):
    d = by_id[identifier]
    source = f"{d['source_name']} · " if d.get("source_name") else ""
    return f"{source}{d['filename']} · {d['id'][:8]} · {d['row_count']} rows"


def columns(identifier):
    return [c["name"] for c in by_id[identifier]["schema"]]


def find_workspace():
    st.header("Find records")
    a, b = st.columns(2)
    dataset = a.selectbox(
        "Search in",
        [None, *by_id],
        format_func=lambda value: (
            label(value) if value else "Latest version of each source"
        ),
    )
    column = None
    if dataset is not None:
        column = b.selectbox(
            "Column",
            [None, *columns(dataset)],
            key=f"find-column-{dataset}",
            format_func=lambda value: value if value is not None else "All columns",
        )
    with st.form("find-form", border=False):
        text = st.text_input(
            "Value to find",
            placeholder="Try Chris Johnson, 555-0112, TST-000135, or a vehicle unit",
            max_chars=200,
        )
        submitted = st.form_submit_button(
            "Search records", icon=":material/search:", type="primary"
        )
    if submitted:
        if not text.strip():
            st.info("Enter part of a value to search.")
            return
        st.session_state["find-query"] = {
            "text": text.strip(),
            "dataset_id": dataset,
            "column": column,
        }
        st.session_state["find-offset"] = 0
    query = st.session_state.get("find-query")
    if not query or (query["dataset_id"], query["column"]) != (dataset, column):
        return
    offset = st.session_state["find-offset"]
    result = api("POST", "/find", json={**query, "offset": offset})
    total = result["total"]
    st.subheader("1 matching row" if total == 1 else f"{total:,} matching rows")
    st.caption(
        f"Searched {result['datasets_searched']:,} dataset versions for {query['text']!r}. Search ignores case."
    )
    if not result["items"]:
        st.info(
            "No records matched your search." if not total else "No rows on this page."
        )
    else:
        rows = result["items"]
        st.dataframe(
            [
                {
                    "Source": row["source_name"],
                    "File": row["filename"],
                    "Row": row["row"],
                    "Matching fields": ", ".join(row["matches"]),
                    "Matching values": " · ".join(
                        str(value) for value in row["matches"].values()
                    ),
                }
                for row in rows
            ],
            hide_index=True,
        )
        with st.expander("View a full source row"):
            index = st.selectbox(
                "Result",
                range(len(rows)),
                format_func=lambda i: (
                    f"{rows[i]['source_name']} · row {rows[i]['row']}"
                ),
                key=f"find-detail-{offset}-{dataset}-{column}-{query['text']}",
            )
            st.json(rows[index]["record"])
    if total > result["limit"] or offset:
        a, b, c = st.columns([1, 1, 4])
        a.button(
            "Previous",
            disabled=offset == 0,
            on_click=move_find_page,
            args=(max(0, offset - result["limit"]),),
        )
        b.button(
            "Next",
            disabled=offset + result["limit"] >= total,
            on_click=move_find_page,
            args=(offset + result["limit"],),
        )
        if result["items"]:
            c.caption(
                f"Rows {offset + 1:,}–{offset + len(result['items']):,} of {total:,}"
            )


def move_find_page(offset):
    st.session_state["find-offset"] = offset


def quality_workspace(*, compare=False):
    st.header("Fuzzy comparison" if compare else "Data quality")
    if compare:
        if len(datasets) < 2:
            st.info("Upload two datasets to compare them.")
            return
        a, b = st.columns(2)
        left = a.selectbox("First dataset", list(by_id), format_func=label)
        right = b.selectbox(
            "Second dataset", [key for key in by_id if key != left], format_func=label
        )
        left_name = a.selectbox(
            "First name, text, or number column",
            columns(left),
            index=None,
            placeholder="Skip fuzzy comparison",
            key=f"left-name-{left}",
        )
        right_name = b.selectbox(
            "Second name, text, or number column",
            columns(right),
            index=None,
            placeholder="Skip fuzzy comparison",
            key=f"right-name-{right}",
        )
        left_phone = a.selectbox(
            "First phone column",
            columns(left),
            index=None,
            placeholder="Skip phones",
            key=f"left-phone-{left}",
        )
        right_phone = b.selectbox(
            "Second phone column",
            columns(right),
            index=None,
            placeholder="Skip phones",
            key=f"right-phone-{right}",
        )
        algorithm = st.selectbox(
            "Similarity method",
            ["token_sort", "character"],
            format_func={
                "token_sort": "Words in any order (names)",
                "character": "Characters in order (strings or numbers)",
            }.get,
        )
        threshold = st.slider("Minimum score (%)", 0, 100, 80)
        name_weight = st.slider(
            "Fuzzy score weight when both scores are available (%)",
            0,
            100,
            50,
            disabled=not (left_name and right_name and left_phone and right_phone),
        )
        st.caption(
            "RapidFuzz compares the selected values and scores similarity from 0 to 100. Character mode treats numbers as text. The separate phone check scores equal digits at 100 and different digits at 0."
        )
        ready = (
            (left_name or left_phone)
            and bool(left_name) == bool(right_name)
            and bool(left_phone) == bool(right_phone)
        )
        if st.button("Find candidate matches", disabled=not ready, type="primary"):
            result = api(
                "POST",
                "/matching/preview",
                json={
                    "left_id": left,
                    "right_id": right,
                    "left_name": left_name,
                    "right_name": right_name,
                    "left_phone": left_phone,
                    "right_phone": right_phone,
                    "threshold": threshold,
                    "name_weight": name_weight,
                    "text_algorithm": algorithm,
                },
            )
            st.metric("Candidate pairs at or above threshold", result["total"])
            st.caption(
                f"Checked {result['pairs_checked']:,} pairs. Showing up to 100 candidates, ranked by score. Similarity scores describe the fields compared."
            )
            if result["items"]:
                st.dataframe(
                    result["items"],
                    hide_index=True,
                    column_config={
                        "left_name": "First value",
                        "right_name": "Second value",
                        "name_pct": st.column_config.NumberColumn(
                            "Fuzzy similarity", format="%.1f%%"
                        ),
                        "phone_pct": st.column_config.NumberColumn(
                            "Phone agreement", format="%.0f%%"
                        ),
                        "score_pct": st.column_config.NumberColumn(
                            "Combined score", format="%.1f%%"
                        ),
                    },
                )
            else:
                st.info("No pairs met the threshold.")
        return

    identifier = st.selectbox(
        "Dataset", list(by_id), format_func=label, key="quality-dataset"
    )
    names = columns(identifier)
    with st.form(f"quality-query-{identifier}"):
        a, b = st.columns(2)
        search = a.text_input("Search across all columns")
        group = b.selectbox(
            "Group rows by",
            names,
            index=None,
            placeholder="No grouping",
        )
        a, b, c = st.columns(3)
        filter_column = a.selectbox(
            "Filter column",
            names,
            index=None,
            placeholder="No filter",
        )
        filter_mode = b.selectbox("Filter condition", ["contains", "equals", "blank"])
        filter_value = c.text_input("Filter text", help="Used for contains and equals.")
        duplicate_columns = st.multiselect(
            "Check duplicates by",
            names,
            help="Rows must share every selected value. Blank keys are excluded. Duplicate counts use the filtered rows.",
        )
        required = st.multiselect("Fields that must have a value", names)
        duplicates_only = st.checkbox("Show only duplicate rows")
        clean_column = st.selectbox(
            "Preview cleaning on",
            names,
            index=None,
            placeholder="No cleaning",
        )
        remove_symbols = st.checkbox(
            "Remove emojis, punctuation, and symbols", value=True
        )
        st.caption(
            "Cleaning standardizes Unicode and collapses whitespace. The preview keeps both values."
        )
        offset = st.number_input("Skip rows", min_value=0, step=100)
        submitted = st.form_submit_button("Apply checks and filters", type="primary")
    key = f"quality-result-{identifier}"
    if submitted or key not in st.session_state:
        st.session_state[key] = api(
            "POST",
            f"/datasets/{identifier}/quality" + ("/runs" if submitted else ""),
            json={
                "search": search,
                "filter_column": filter_column,
                "filter_value": filter_value,
                "filter_mode": filter_mode,
                "group_by": group,
                "duplicate_columns": duplicate_columns,
                "required_columns": required,
                "duplicates_only": duplicates_only and bool(duplicate_columns),
                "clean_column": clean_column,
                "remove_symbols": remove_symbols,
                "offset": offset,
            },
        )
    result = st.session_state[key]
    source, dataset = result["source"], result["dataset"]
    st.subheader(source["name"])
    st.caption(
        f"{dataset['filename']} · {source['kind']} source · Imported {dataset['created_at']} · Version {dataset['id'][:8]}"
    )
    if lineage := dataset.get("lineage"):
        st.caption(
            f"Derived from {lineage['input_dataset_id']} · Transformation run {lineage['run_id']}"
        )
    if dataset.get("records_path"):
        st.caption(f"JSON records: {dataset['records_path']}")
    if dataset["sheet"]:
        st.caption(f"Worksheet: {dataset['sheet']}")
    a, b, c, d = st.columns(4)
    a.metric("Source rows", dataset["row_count"])
    b.metric("Filtered rows", result["filtered_count"])
    c.metric("Duplicate rows", result["duplicate_rows"])
    d.metric("Duplicate groups", result["duplicate_groups"])
    if run_id := result.get("run_id"):
        report_download(run_id, f"/quality/runs/{run_id}/report", "quality")
    st.subheader("Column information and issues")
    st.caption(
        "Blank and distinct counts describe the full dataset. Duplicate counts describe the filtered rows."
    )
    st.dataframe(
        result["columns"],
        hide_index=True,
        column_config={
            "column": "Source column",
            "type": "Stored type",
            "blank": "Blank rows",
            "distinct_nonblank": "Distinct nonblank values",
        },
    )
    drift = dataset["schema_drift"]
    if drift and any(drift.values()):
        st.warning("The schema changed since the previous import.")
        st.json(drift)
    if result["groups"]:
        st.subheader(f"Group counts ({result['group_count']:,} groups)")
        st.dataframe(result["groups"], hide_index=True)
    st.subheader("Rows")
    st.caption(f"{result['flagged_count']:,} rows in this view have validation issues.")
    st.caption(
        f"Showing {len(result['items']):,} of {result['visible_count']:,} rows. Use Skip rows to see the next page."
    )
    if result["items"]:
        records = [item["values"] for item in result["items"]]
        st.dataframe(records, hide_index=True)
        flagged = [
            {"Source row": item["row"], "Issues": "; ".join(item["issues"])}
            for item in result["items"]
            if item["issues"]
        ]
        if flagged:
            st.dataframe(flagged, hide_index=True)
        if "cleaned" in result["items"][0]:
            st.subheader("Cleaning preview")
            column = result["clean_column"]
            st.dataframe(
                [
                    {
                        "Source row": item["row"],
                        "Original": item["values"][column],
                        "Cleaned": item["cleaned"],
                    }
                    for item in result["items"]
                ],
                hide_index=True,
            )
    else:
        st.info("No rows to display.")


JOIN_VIEWS = {
    "Inner · matched pairs only": "inner",
    "Unmatched · either side": "unmatched",
    "Left join · keep every left row": "left",
    "Right join · keep every right row": "right",
    "Full join · keep both sides": "full",
}


PRESENCE_METHODS = {
    "exact": (
        "Exact value",
        "Compare stored values as-is. A number and the same value stored as text are different.",
    ),
    "trimmed_text": (
        "Ignore case and extra spaces",
        "Compare as text after Unicode normalization, whitespace cleanup, and ignoring case. Punctuation and leading zeros are preserved.",
    ),
    "clean_text": (
        "Clean text and ignore case",
        "Compare as text after removing punctuation, symbols, and emojis and normalizing whitespace and case.",
    ),
    "phone_digits": (
        "Phone digits",
        "Compare the extracted digits. Include country codes consistently because they are part of the key.",
    ),
}


def presence_plan(configuration):
    key = configuration["keys"][0]
    left = configuration["left_id"]
    right = configuration["right_id"]
    with st.expander("Inspect selected fields and see the check code"):
        panels = st.columns(2)
        profile_key = f"presence-profile-{left}-{right}-{key['left']}-{key['right']}"
        if st.button("Load column counts and examples", key=f"load-{profile_key}"):
            st.session_state[profile_key] = [
                api(
                    "GET", f"/datasets/{dataset_id}/profile", params={"columns": column}
                )["columns"][0]
                for dataset_id, column in ((left, key["left"]), (right, key["right"]))
            ]
        profiles = st.session_state.get(profile_key, [])
        for panel, dataset_id, column, profile in zip(
            panels, (left, right), (key["left"], key["right"]), profiles, strict=False
        ):
            with panel:
                st.markdown(f"**{by_id[dataset_id]['filename']}**")
                st.caption(f"{column} · {profile['dtype']} · Version {dataset_id[:8]}")
                st.dataframe(
                    [
                        {
                            "Rows": profile["total_records"],
                            "Nulls": profile["null_count"],
                            "Distinct non-null": profile["distinct_count"],
                            "Repeated values": profile["duplicate_values"],
                        }
                    ],
                    hide_index=True,
                )
                st.dataframe(
                    [{"Sample value": value} for value in profile["sample_values"]],
                    hide_index=True,
                )
        if (
            profiles
            and key["method"] == "exact"
            and profiles[0]["dtype"] != profiles[1]["dtype"]
        ):
            st.info(
                "The stored column types differ. If the values are identifiers stored as text on one side, choose Ignore case and extra spaces to compare them as text."
            )
        st.markdown("**Comparison rule**")
        st.write(PRESENCE_METHODS[key["method"]][1])
        st.caption(
            "The engine indexes the reference column, then checks each source row against that index. Blank keys are reported separately. Repeated reference values do not increase the number of found source rows."
        )
        st.markdown("**Python to run this exact check**")
        st.caption(
            "This calls the same local API as Check presence and creates a new run from the selected dataset versions."
        )
        st.code(
            f"""import httpx

settings = {pformat(configuration, sort_dicts=False)}
with httpx.Client(base_url={base!r}, timeout=120) as client:
    response = client.post("/reconciliation/runs", json=settings)
    response.raise_for_status()
    run = response.json()
    report = client.get(f"/reconciliation/runs/{{run['id']}}/report")
    report.raise_for_status()

print(run["counts"])
rows = report.json()["items"]""",
            language="python",
        )


def presence_workspace():
    st.header("Is In · Check values across files")
    st.caption(
        "Find out whether values from one dataset appear in a selected column of another."
    )
    if len(by_id) < 2:
        st.info("Upload two datasets to start.")
        st.button(
            "Go to Upload data", on_click=set_navigation, args=("Sources", "upload")
        )
        return
    a, b = st.columns(2)
    left = a.selectbox("Dataset to check", list(by_id), format_func=label)
    right = b.selectbox(
        "Look for values in", [key for key in by_id if key != left], format_func=label
    )
    left_column = a.selectbox(
        "Column to check", columns(left), index=None, key=f"presence-left-{left}"
    )
    right_column = b.selectbox(
        "Column to look in", columns(right), index=None, key=f"presence-right-{right}"
    )
    method = st.selectbox(
        "Compare values using",
        list(PRESENCE_METHODS),
        format_func=lambda key: PRESENCE_METHODS[key][0],
    )
    configuration = {
        "left_id": left,
        "right_id": right,
        "view": "presence",
        "minimum_agreements": None,
        "keys": [
            {
                "left": left_column,
                "right": right_column,
                "method": method,
                "required": False,
            }
        ],
    }
    if left_column is not None and right_column is not None:
        st.subheader("Question")
        st.write(
            f"Which values in **{by_id[left]['filename']} → {left_column}** "
            f"are present in **{by_id[right]['filename']} → {right_column}**?"
        )
        presence_plan(configuration)
    if st.button(
        "Check presence",
        type="primary",
        disabled=left_column is None or right_column is None,
    ):
        st.session_state["presence-result"] = api(
            "POST", "/reconciliation/runs", json=configuration
        )
    result = st.session_state.get("presence-result")
    if result and result["configuration"] == configuration:
        show_presence(result)


def show_presence(result):
    st.subheader("Answer")
    counts = result["counts"]
    found = counts["matched_left_rows"]
    missing = counts["left_missing"]
    unavailable = counts["left_not_checkable"]
    checked = found + missing
    a, b, c, d = st.columns(4)
    a.metric("Found", found)
    b.metric("Missing", missing)
    c.metric("No usable value", unavailable)
    d.metric("Match rate", f"{found / checked:.1%}" if checked else "—")
    st.caption(
        f"{checked:,} of {counts['left_rows']:,} source rows had a usable value. "
        f"{counts['left_multiple_candidates']:,} source rows were found more than once in the reference."
    )
    status = st.selectbox(
        "Show rows",
        ["All rows", "Found", "Missing", "No usable key"],
        format_func=lambda value: (
            "No usable value" if value == "No usable key" else value
        ),
        key=f"presence-status-{result['id']}",
    )
    offset = st.number_input(
        "Skip rows",
        min_value=0,
        step=100,
        key=f"presence-offset-{result['id']}-{status}",
    )
    route = f"/reconciliation/runs/{result['id']}/report"
    params = {} if status == "All rows" else {"status": status}
    preview = api("GET", route, params={"limit": 100, "offset": offset, **params})
    key = result["configuration"]["keys"][0]
    st.caption(
        f"Showing {len(preview['items']):,} of {preview['total']:,} rows in this view. One reference example is shown per found row."
    )
    if preview["items"]:
        st.dataframe(
            [
                {
                    "Result": "No usable value"
                    if row["status"] == "No usable key"
                    else row["status"],
                    "Source row": row["left_row"],
                    "Value checked": row[f"Left · {key['left']}"],
                    "Reference row": row["right_row"],
                    "Example value found": row[f"Right · {key['right']}"],
                    "Reference matches": row["left_candidate_count"],
                }
                for row in preview["items"]
            ],
            hide_index=True,
        )
    else:
        st.info("No rows in this view.")
    report_download(result["id"] + f"-{status}", route, "presence", params=params)


ORDERED_RULE_LABELS = {
    "phone_digits": "Phone",
    "license_id": "Driver license",
    "trimmed_text": "Username",
    "fuzzy_name": "Name similarity",
}


def ordered_match_workspace():
    st.header("IF / ELSE match & compare")
    st.caption(
        "Check one rule at a time. The first rule that finds a candidate ends the search for that source row."
    )
    if len(by_id) < 2:
        st.info("Upload two datasets to start.")
        return
    a, b = st.columns(2)
    left = a.selectbox("Drivers to check", list(by_id), format_func=label)
    right = b.selectbox(
        "Reference drivers", [key for key in by_id if key != left], format_func=label
    )
    remaining = list(ORDERED_RULE_LABELS)
    keys = []
    incomplete = False
    st.caption(
        "Choose the order and columns for each IF / ELSE IF condition. Leave both columns blank to skip a rule."
    )
    for index in range(4):
        a, b, c = st.columns([1.2, 1, 1])
        method = a.selectbox(
            "IF · first rule" if index == 0 else f"ELSE IF · rule {index + 1}",
            remaining,
            format_func=ORDERED_RULE_LABELS.get,
            key=f"ordered-method-{index}-{'-'.join(remaining)}",
        )
        remaining.remove(method)
        source = b.selectbox(
            f"Source column for step {index + 1}",
            columns(left),
            index=None,
            key=f"ordered-left-{left}-{method}",
        )
        reference = c.selectbox(
            f"Reference column for step {index + 1}",
            columns(right),
            index=None,
            key=f"ordered-right-{right}-{method}",
        )
        if source is not None and reference is not None:
            keys.append(
                {
                    "left": source,
                    "right": reference,
                    "method": method,
                    "required": False,
                }
            )
        elif (source is None) != (reference is None):
            incomplete = True
    if incomplete:
        st.info("Choose both columns for each enabled rule.")
    if keys:
        st.write(
            "Run order: "
            + " → ".join(ORDERED_RULE_LABELS[key["method"]] for key in keys)
        )
    st.caption("ELSE → mark the row Missing if no rule finds a match.")
    name_threshold = None
    if any(key["method"] == "fuzzy_name" for key in keys):
        name_threshold = st.slider("Name similarity must be above (%)", 0, 99, 90)
    audit_fields = []
    with st.expander("Compare additional fields after a match"):
        st.caption(
            "Find changed or missing fields on the chosen pair, such as status or vehicle. This does not run another matching condition."
        )
        count = st.number_input("Additional field pairs", min_value=0, max_value=12)
        for index in range(count):
            a, b, c = st.columns(3)
            left_field = a.selectbox(
                f"Source field {index + 1}",
                columns(left),
                key=f"audit-left-{left}-{index}",
            )
            right_field = b.selectbox(
                f"Reference field {index + 1}",
                columns(right),
                key=f"audit-right-{right}-{index}",
            )
            method = c.selectbox(
                f"Compare pair {index + 1}",
                ["exact", "trimmed_text", "clean_text"],
                format_func=lambda key: PRESENCE_METHODS[key][0],
            )
            audit_fields.append(
                {
                    "left": left_field,
                    "right": right_field,
                    "method": method,
                    "required": False,
                }
            )
    a, b = st.columns(2)
    a.caption(f"Source imported: {by_id[left]['created_at']}")
    b.caption(f"Reference imported: {by_id[right]['created_at']}")
    with st.expander("How the rules compare values"):
        st.write(
            "Phone compares digits and treats a leading +1 as optional for 10-digit numbers. "
            "DL ignores separators and case, preserving leading zeros. "
            "Username ignores case and extra spaces. "
            "Name compares cleaned text with word order ignored. "
            "If a rule finds multiple candidates, the row stops for review."
        )
    configuration = {
        "left_id": left,
        "right_id": right,
        "view": "ordered",
        "minimum_agreements": None,
        "keys": keys,
    }
    if name_threshold is not None:
        configuration["name_threshold"] = name_threshold
    if audit_fields:
        configuration["audit_fields"] = audit_fields
    if st.button("Run ordered match", type="primary", disabled=incomplete or not keys):
        st.session_state["ordered-match-result"] = api(
            "POST", "/reconciliation/runs", json=configuration
        )
    result = st.session_state.get("ordered-match-result")
    if result and result["configuration"] == configuration:
        show_ordered_match(result)


def show_ordered_match(result):
    counts = result["counts"]
    st.subheader("Match results")
    panels = st.columns(5)
    metrics = [
        ("Matched", counts["matched"]),
        ("Ambiguous", counts["ambiguous"]),
        ("Review", counts.get("review", 0)),
        ("Missing", counts["missing"]),
        ("No usable value", counts["no_usable_value"]),
    ]
    for panel, (title, value) in zip(panels, metrics, strict=True):
        with panel.container(border=True):
            st.metric(title, value)
    st.caption(f"{counts['matched']:,} of {counts['left_rows']:,} source rows matched.")
    if "rows_with_differences" in counts:
        st.caption(
            f"{counts['rows_with_differences']:,} linked rows had field differences."
        )
    differences_only = st.checkbox(
        "Show rows with field differences only",
        key=f"ordered-differences-{result['id']}",
    )
    if counts["reused_reference_rows"]:
        st.warning(
            f"{counts['reused_reference_rows']} reference rows matched more than one source row. Review before merging."
        )
    if counts["matched_by_rule"]:
        st.dataframe(
            [
                {"Rule": rule, "Matched rows": count}
                for rule, count in counts["matched_by_rule"].items()
            ],
            hide_index=True,
        )
    status = st.selectbox(
        "Show results",
        ["All rows", "Matched", "Ambiguous", "Review", "Missing", "No usable value"],
        key=f"ordered-status-{result['id']}",
    )
    offset = st.number_input(
        "Skip rows",
        min_value=0,
        step=100,
        key=f"ordered-offset-{result['id']}-{status}",
    )
    route = f"/reconciliation/runs/{result['id']}/report"
    params = {} if status == "All rows" else {"status": status}
    if differences_only:
        params["differences_only"] = True
    preview = api("GET", route, params={"limit": 100, "offset": offset, **params})
    st.caption(
        f"Showing {len(preview['items']):,} of {preview['total']:,} rows in this view."
    )
    if preview["items"]:
        st.dataframe(
            [
                {
                    "Status": row["status"],
                    "Source row": row["left_row"],
                    "Reference row": row["right_row"],
                    "Stopped at": row["matched_rule"],
                    "Source value": row["value_checked"],
                    "Reference value": row["value_found"],
                    "Name similarity (%)": row["score_pct"]
                    if row["matched_rule"] == "Name similarity"
                    else None,
                    "Candidates": row["candidate_count"],
                    "Candidate row numbers": row["candidate_rows"],
                    "Field differences": row.get("difference_count", 0),
                    "Checks tried": row["checks_tried"],
                    "Review reason": row["review_reason"],
                }
                for row in preview["items"]
            ],
            hide_index=True,
        )
    else:
        st.info("No rows in this view.")
    changed = {
        row["left_row"]: row["field_differences"]
        for row in preview["items"]
        if row.get("difference_count")
    }
    if changed:
        with st.expander(
            "Review original values side by side", expanded=differences_only
        ):
            selected = st.selectbox(
                "Source row", list(changed), key=f"difference-row-{result['id']}"
            )
            st.dataframe(changed[selected], hide_index=True)
    report_download(
        result["id"] + f"-{status}-{differences_only}",
        route,
        "ordered-match",
        params=params,
    )


def reconciliation_workspace():
    st.header("Join & Reconcile")
    if len(by_id) < 2:
        st.info("Upload two datasets to start.")
        return
    left_panel, right_panel = st.columns(2)
    left = left_panel.selectbox("Left dataset", list(by_id), format_func=label)
    right = right_panel.selectbox(
        "Right dataset", [key for key in by_id if key != left], format_func=label
    )
    view = st.selectbox("Report view", list(JOIN_VIEWS))
    key_count = st.number_input("Number of join keys", min_value=1, max_value=20)
    st.caption(
        "Choose how many fields must agree. Blank values earn no agreement. Multiple candidates are shown separately."
    )
    partial = st.checkbox("Allow partial agreement", disabled=key_count < 2)
    minimum = (
        st.number_input(
            "Minimum agreeing fields",
            1,
            key_count,
            key_count,
            key=f"agreement-{key_count}",
        )
        if partial and key_count > 1
        else key_count
    )
    keys = []
    for index in range(key_count):
        a, b, c, d = st.columns([3, 3, 3, 2])
        keys.append(
            {
                "left": a.selectbox(
                    f"Left column {index + 1}",
                    columns(left),
                    key=f"join-left-{left}-{index}",
                ),
                "right": b.selectbox(
                    f"Right column {index + 1}",
                    columns(right),
                    key=f"join-right-{right}-{index}",
                ),
                "required": d.checkbox(
                    f"Key {index + 1} must agree", disabled=minimum == key_count
                ),
                "method": c.selectbox(
                    f"Comparison {index + 1}",
                    ["exact", "trimmed_text", "clean_text", "phone_digits"],
                    format_func={
                        "exact": "Exact value",
                        "trimmed_text": "Trimmed text",
                        "clean_text": "Cleaned text",
                        "phone_digits": "Phone digits",
                    }.get,
                ),
            }
        )
    configuration = {
        "left_id": left,
        "right_id": right,
        "view": JOIN_VIEWS[view],
        "keys": keys,
        "minimum_agreements": minimum,
    }
    if st.button("Run reconciliation", type="primary"):
        st.session_state["reconciliation-result"] = api(
            "POST", "/reconciliation/runs", json=configuration
        )
    result = st.session_state.get("reconciliation-result")
    if result and result["configuration"] == configuration:
        show_reconciliation(result)


def show_reconciliation(result):
    if result["configuration"]["view"] == "ordered":
        show_ordered_match(result)
        return
    if result["configuration"]["view"] == "presence":
        show_presence(result)
        return
    counts = result["counts"]
    a, b, c = st.columns(3)
    a.metric("Matched pairs", counts["matched_pairs"])
    b.metric("Left only", counts["left_only"])
    c.metric("Right only", counts["right_only"])
    st.dataframe(
        [
            {
                "Source": side.title(),
                "Rows": counts[f"{side}_rows"],
                "Matched source rows": counts[f"matched_{side}_rows"],
                "Incomplete keys": counts[f"{side}_blank_keys"],
                "Multiple candidates": counts.get(f"{side}_multiple_candidates", 0),
                "Duplicate key groups": counts[f"{side}_duplicate_keys"],
            }
            for side in ("left", "right")
        ],
        hide_index=True,
    )
    if "items" in result:
        st.caption(f"Showing up to 100 of {result['output_rows']:,} report rows.")
        st.dataframe(result["items"], hide_index=True)
    report_download(
        result["id"], f"/reconciliation/runs/{result['id']}/report", "reconciliation"
    )


def report_download(run_id, route, report_type, *, params=None):
    if st.button("Prepare full CSV report", key=f"prepare-{run_id}"):
        records = api("GET", route, params=params)["items"]
        if records:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(records[0]))
            writer.writeheader()
            for row in records:
                writer.writerow(
                    {
                        key: "'" + value
                        if isinstance(value, str)
                        and value.lstrip().startswith(("=", "+", "-", "@"))
                        else value
                        for key, value in row.items()
                    }
                )
            st.session_state[f"csv-{run_id}"] = output.getvalue()
        else:
            st.info("This report has no rows.")
    if report := st.session_state.get(f"csv-{run_id}"):
        st.download_button(
            "Download CSV",
            report,
            file_name=f"{report_type}-{run_id}.csv",
            mime="text/csv",
        )


def newer_version_selector(original, key, title):
    source_id = by_id[original]["source_id"]
    versions = [
        identifier
        for identifier, dataset in by_id.items()
        if dataset["source_id"] == source_id
    ]
    return st.selectbox(title, versions, format_func=label, key=key)


def choose_run(title, runs, format_run, key):
    indexed = {run["id"]: run for run in runs}
    if pending := st.session_state.pop(f"next-{key}", None):
        st.session_state[key] = pending
    identifier = st.selectbox(
        title,
        list(indexed),
        format_func=lambda run_id: format_run(indexed[run_id]),
        key=key,
    )
    return indexed[identifier]


def run_history(*, quality=False):
    st.header("Validation runs" if quality else "Join runs")
    if quality:
        quality_runs = all_items("/quality/runs")
        if not quality_runs:
            st.info("Apply checks and filters to record a data quality run.")
            return
        run = choose_run(
            "Data quality run",
            quality_runs,
            lambda run: f"{run['created_at']} · {by_id[run['dataset_id']]['filename']}",
            "history-quality",
        )
        with st.expander("Rerun saved checks"):
            target = newer_version_selector(
                run["dataset_id"], f"rerun-quality-{run['id']}", "Dataset version"
            )
            if st.button("Rerun quality checks"):
                repeated = api(
                    "POST",
                    f"/datasets/{target}/quality/runs",
                    json={**run["configuration"], "offset": 0},
                )
                st.session_state["next-history-quality"] = repeated["run_id"]
                st.rerun()
        summary = run["summary"]
        a, b, c = st.columns(3)
        a.metric("Filtered rows", summary["filtered_count"])
        b.metric("Duplicate rows", summary["duplicate_rows"])
        c.metric("Duplicate groups", summary["duplicate_groups"])
        st.dataframe(summary["columns"], hide_index=True)
        if summary["groups"]:
            st.dataframe(summary["groups"], hide_index=True)
        with st.expander("Source version and checks"):
            st.json({"dataset_id": run["dataset_id"], "checks": run["configuration"]})
        report_download(run["id"], f"/quality/runs/{run['id']}/report", "quality")
        st.download_button(
            "Download quality summary (JSON)",
            json.dumps(run, indent=2),
            file_name=f"quality-{run['id']}.json",
            mime="application/json",
        )
        return
    runs = all_items("/reconciliation/runs")
    if not runs:
        st.info("Run a reconciliation to create a report here.")
        return
    selected = choose_run(
        "Reconciliation run",
        runs,
        lambda run: (
            f"{run['created_at']} · {by_id[run['configuration']['left_id']]['filename']} / "
            f"{by_id[run['configuration']['right_id']]['filename']} · {run['configuration']['view']}"
        ),
        "history-reconciliation",
    )
    settings = selected["configuration"]
    with st.expander("Source versions and join settings"):
        st.json(settings)
    with st.expander("Rerun saved join"):
        left = newer_version_selector(
            settings["left_id"], f"rerun-left-{selected['id']}", "Left dataset version"
        )
        right = newer_version_selector(
            settings["right_id"],
            f"rerun-right-{selected['id']}",
            "Right dataset version",
        )
        if st.button("Rerun reconciliation"):
            repeated = api(
                "POST",
                "/reconciliation/runs",
                json={**settings, "left_id": left, "right_id": right},
            )
            st.session_state["next-history-reconciliation"] = repeated["id"]
            st.rerun()
    show_reconciliation(selected)


TRANSFORMATION_OPERATIONS = {
    "copy": "Copy / map field",
    "extract_field": "Extract nested JSON field",
    "trim": "Normalize whitespace",
    "clean_text": "Remove punctuation and emojis",
    "phone_digits": "Extract phone digits",
    "parse_date": "Parse date with format",
    "parse_number": "Normalize decimal text",
    "required": "Check required value",
    "unique": "Check uniqueness",
}


def transformation_workspace(task):
    st.header(
        {
            "build_recipe": "Build a transformation recipe",
            "run_recipe": "Run a saved recipe",
            "transform_history": "Transformation runs",
        }[task]
    )
    if task == "build_recipe":
        identifier = st.selectbox(
            "Design from a dataset", list(by_id), format_func=label
        )
        name = st.text_input("Recipe name")
        count = st.number_input("Ordered steps", min_value=1, max_value=30)
        available = columns(identifier)
        steps = []
        for index in range(count):
            st.subheader(f"Step {index + 1}")
            a, b, c = st.columns(3)
            operation = a.selectbox(
                "Operation",
                list(TRANSFORMATION_OPERATIONS),
                format_func=TRANSFORMATION_OPERATIONS.get,
                key=f"transform-op-{identifier}-{index}",
            )
            column = b.selectbox(
                "Input column", available, key=f"transform-column-{identifier}-{index}"
            )
            check = operation in {"required", "unique"}
            output = (
                c.text_input(
                    "Output column",
                    placeholder=column,
                    key=f"transform-output-{identifier}-{index}",
                ).strip()
                if not check
                else ""
            )
            step = {"operation": operation, "column": column, "output": output or None}
            if operation == "extract_field":
                step["field_path"] = st.text_input(
                    "Path inside input field",
                    placeholder="/contact/phone",
                    key=f"transform-path-{identifier}-{index}",
                )
            if operation == "parse_date":
                step["date_format"] = st.text_input(
                    "Input date format",
                    "%Y-%m-%d",
                    key=f"transform-format-{identifier}-{index}",
                )
            if output and output not in available:
                available.append(output)
            steps.append(step)
        st.caption(
            "Later steps read the output of earlier steps. A blank output name updates the working column."
        )
        if st.button("Save and run recipe", type="primary", disabled=not name.strip()):
            recipe = api(
                "POST",
                "/transformation/recipes",
                json={
                    "name": name.strip(),
                    "source_id": by_id[identifier]["source_id"],
                    "steps": steps,
                },
            )
            st.session_state["new-transformation"] = api(
                "POST",
                f"/transformation/recipes/{recipe['id']}/runs",
                json={"dataset_id": identifier},
            )
        if run := st.session_state.get("new-transformation"):
            display_transformation(run)
    elif task == "run_recipe":
        recipes = all_items("/transformation/recipes")
        if not recipes:
            st.info("Save a recipe first.")
            return
        recipe = st.selectbox(
            "Recipe", recipes, format_func=lambda recipe: recipe["name"]
        )
        versions = [
            identifier
            for identifier, dataset in by_id.items()
            if dataset["source_id"] == recipe["source_id"]
        ]
        if not versions:
            st.info("Upload a dataset version for this source.")
            return
        version = st.selectbox("Dataset version", versions, format_func=label)
        with st.expander("Saved steps"):
            st.dataframe(recipe["steps"], hide_index=True)
        if st.button("Run recipe", type="primary"):
            st.session_state["saved-transformation"] = api(
                "POST",
                f"/transformation/recipes/{recipe['id']}/runs",
                json={"dataset_id": version},
            )
        run = st.session_state.get("saved-transformation")
        if run and run["recipe"]["id"] == recipe["id"] and run["dataset_id"] == version:
            display_transformation(run)
    else:
        runs = all_items("/transformation/runs")
        if not runs:
            st.info("Run a recipe to see its history.")
            return
        run = st.selectbox(
            "Run",
            runs,
            format_func=lambda run: f"{run['created_at']} · {run['recipe']['name']}",
        )
        display_transformation(run)


def display_transformation(run):
    st.subheader(run["recipe"]["name"])
    st.caption(f"Source version: {label(run['dataset_id'])} · Run: {run['id']}")
    a, b, c = st.columns(3)
    a.metric("Source rows", run["row_count"])
    b.metric("Flagged rows", run["flagged_rows"])
    c.metric("Issues", run["issue_count"])
    st.dataframe(run["trace"], hide_index=True)
    if run.get("items"):
        st.write("Output preview")
        st.dataframe(run["items"], hide_index=True)
    if run.get("exceptions"):
        st.write("Exception preview")
        st.dataframe(run["exceptions"], hide_index=True)
    route = f"/transformation/runs/{run['id']}/report"
    report_download(run["id"] + "-rows", route, "transformed-rows")
    if run["issue_count"]:
        with st.expander("Exceptions CSV"):
            report_download(
                run["id"] + "-exceptions",
                route + "?kind=exceptions",
                "transformation-exceptions",
            )
        st.warning(
            "Resolve the reported issues before publishing this output for joins."
        )
    elif run["row_count"] and st.button(
        "Publish dataset for joins", key=f"publish-{run['id']}"
    ):
        output = api("POST", f"/transformation/runs/{run['id']}/publish")
        st.success(f"Published {output['filename']}. Select it in Join & Reconcile.")


def source_dashboard():
    st.header("Source dashboard")
    sources = all_items("/sources")
    if not sources:
        st.info("Create a source to start tracking its imports.")
        return
    source = st.selectbox(
        "Source or API endpoint", sources, format_func=lambda item: item["name"]
    )
    source_id = source["id"]
    versions = [item for item in datasets if item["source_id"] == source_id]
    if source["kind"] == "http":
        settings = source["connection_settings"]
        method = "monday.com board" if settings.get("monday_board_id") else "HTTP GET"
        st.caption(method + " · " + settings["url"].split("?", 1)[0])
        if params := settings.get("query_params"):
            with st.expander("Request parameters"):
                st.json(params)
        if st.button("Import and refresh dashboard", type="primary"):
            api("POST", f"/sources/{source_id}/refresh")
            st.session_state.pop(f"dashboard-version-{source_id}", None)
            st.rerun()
    if versions:
        ids = [item["id"] for item in versions]
        selected = st.selectbox(
            "Dataset version",
            ids,
            format_func=label,
            key=f"dashboard-version-{source_id}",
        )
        current = by_id[selected]
        a, b, c = st.columns(3)
        a.metric("Rows", current["row_count"])
        b.metric("Columns", len(current["schema"]))
        c.metric("Versions", len(versions))
        st.caption(f"{current['filename']} · Imported {current['created_at']}")
        if len(versions) > 1:
            with st.expander("Row counts by version"):
                st.line_chart(
                    [
                        {"Imported": item["created_at"], "Rows": item["row_count"]}
                        for item in reversed(versions)
                    ],
                    x="Imported",
                    y="Rows",
                )
        st.subheader("Data preview")
        offset = st.number_input(
            "Skip rows", min_value=0, step=50, key=f"dashboard-offset-{selected}"
        )
        result = api(
            "GET",
            f"/datasets/{selected}/preview",
            params={"limit": 50, "offset": offset},
        )
        st.dataframe(result["items"], hide_index=True)
        with st.expander("Schema and provenance"):
            st.json(current)
    else:
        st.info("This source has no imported versions yet.")
    with st.expander("Import history"):
        st.dataframe(all_items(f"/sources/{source_id}/history"), hide_index=True)


def connection_workspace():
    st.header("API & Webhook")
    transport = st.radio(
        "Connection type", ["HTTP GET", "monday.com board", "Webhook"], horizontal=True
    )
    monday = transport == "monday.com board"
    kind = "webhook" if transport == "Webhook" else "http"
    saved = [
        source
        for source in all_items("/sources")
        if source["kind"] == kind
        and bool(source["connection_settings"].get("monday_board_id")) == monday
    ]
    if saved:
        source = st.selectbox(
            "Saved connection", saved, format_func=lambda item: item["name"]
        )
        if kind == "http":
            st.caption(
                "Import the first page of active board items."
                if monday
                else "Run a GET request and import the response as a dataset version."
            )
            if st.button(
                "Import board items" if monday else "Run GET and import", type="primary"
            ):
                result = api("POST", f"/sources/{source['id']}/refresh")
                st.success(
                    f"{result['filename']} · {result['row_count']:,} rows imported."
                )
                st.json(result["origin"])
        else:
            hook_base = os.environ.get(
                "INTEGRATION_WEBHOOK_BASE_URL", "http://127.0.0.1:8000"
            ).rstrip("/")
            st.caption(
                "Configure your sender to POST JSON to this URL with the bearer token generated at setup."
            )
            st.code(f"{hook_base}/webhooks/{source['id']}", language="text")
            if secret := st.session_state.get(f"webhook-token-{source['id']}"):
                st.code(f"Authorization: Bearer {secret}", language="text")
                st.button(
                    "Hide token",
                    on_click=st.session_state.pop,
                    args=(f"webhook-token-{source['id']}", None),
                )
        with st.expander("Import history"):
            st.dataframe(all_items(f"/sources/{source['id']}/history"), hide_index=True)
    with (
        st.expander("Add connection", expanded=not saved),
        st.form(f"connection-{kind}"),
    ):
        name = st.text_input("Source name")
        board_id = st.text_input("Board ID") if monday else ""
        if monday:
            st.caption(
                "Reads up to 500 active items. Allow api.monday.com on the API server."
            )
        url = (
            "https://api.monday.com/v2"
            if monday
            else (
                st.text_input(
                    "HTTPS endpoint", placeholder="https://api.example.com/drivers"
                )
                if kind == "http"
                else ""
            )
        )
        auth_mode = "None"
        env_name = ""
        token_name = ""
        if monday:
            env_name = st.text_input(
                "Token environment variable",
                placeholder="INTEGRATION_HTTP_TOKEN_MONDAY",
            )
            auth_mode = "Custom header"
            token_name = "Authorization"
        elif kind == "http":
            auth_mode = st.selectbox(
                "Authentication",
                ["None", "Bearer token", "Custom header", "Query parameter"],
            )
            env_name = st.text_input(
                "Token environment variable",
                placeholder="INTEGRATION_HTTP_TOKEN_FLEET",
                help="Required for token authentication. Enter the variable name.",
            )
            token_name = st.text_input(
                "Custom header or query parameter name",
                placeholder="X-API-Key or api_key",
                help="Fill this in for Custom header or Query parameter.",
            )
        query_text = (
            st.text_area(
                "Query parameters (JSON object, optional)",
                placeholder='{"limit": 100, "types": ["speed", "location"]}',
                help="Use ordinary request parameters here. Store credentials in a server environment variable.",
            )
            if kind == "http" and not monday
            else ""
        )
        filename = (
            ""
            if monday
            else st.text_input(
                "Filename override (optional)", placeholder="drivers.json"
            )
        )
        records = (
            ""
            if monday
            else st.text_input(
                "JSON records path (optional)", placeholder="/data/drivers"
            )
        )
        if kind == "http":
            st.caption(
                "The server must allow the API host in INTEGRATION_HTTP_ALLOWED_HOSTS. Store credentials in a server environment variable."
            )
        else:
            st.caption("Copy the generated bearer token after creating the webhook.")
        if st.form_submit_button("Create connection", type="primary"):
            configuration = {
                key: value.strip()
                for key, value in {
                    "url": url,
                    "filename": filename,
                    "records_path": records,
                }.items()
                if value.strip()
            }
            if monday:
                if not board_id.strip():
                    st.error("Enter the monday.com board ID.")
                    st.stop()
                configuration["monday_board_id"] = board_id.strip()
            if auth_mode != "None":
                if not env_name.strip():
                    st.error("Enter the server environment variable for the token.")
                    st.stop()
                if auth_mode == "Bearer token":
                    configuration["bearer_env"] = env_name.strip()
                else:
                    if not token_name.strip():
                        st.error(
                            "Enter the header or query parameter name required by the API."
                        )
                        st.stop()
                    configuration["custom_token"] = {
                        "location": "header"
                        if auth_mode == "Custom header"
                        else "query",
                        "name": token_name.strip(),
                        "token_env": env_name.strip(),
                    }
            if query_text.strip():
                try:
                    parameters = json.loads(query_text)
                except json.JSONDecodeError:
                    st.error("Enter query parameters as a valid JSON object.")
                    st.stop()
                if not isinstance(parameters, dict):
                    st.error("Query parameters must be a JSON object.")
                    st.stop()
                configuration["query_params"] = parameters
            created = api(
                "POST",
                "/sources",
                json={
                    "name": name.strip(),
                    "kind": kind,
                    "connection_settings": configuration,
                },
            )
            if "webhook_token" in created:
                st.session_state[f"webhook-token-{created['id']}"] = created[
                    "webhook_token"
                ]
            st.rerun()


if page == "source_dashboard":
    source_dashboard()
elif page == "connect_source":
    connection_workspace()
elif page == "create_source":
    st.header("Create a source")
    st.caption("Give each recurring file feed a name so you can track its versions.")
    with st.form("source"):
        name = st.text_input("Source name")
        with st.expander("Advanced source settings"):
            refresh = st.selectbox("Refresh strategy", ["manual", "on_upload"])
            settings = st.text_area("Connection metadata (JSON)", "{}")
        if st.form_submit_button("Create source"):
            try:
                parsed = json.loads(settings)
            except ValueError:
                st.error("Settings must be valid JSON.")
                st.stop()
            api(
                "POST",
                "/sources",
                json={
                    "name": name,
                    "refresh_strategy": refresh,
                    "connection_settings": parsed,
                },
            )
            set_navigation("Sources", "upload")
            st.rerun()
elif page in {"upload", "import_history"}:
    st.header("Upload data" if page == "upload" else "Import history")
    sources = [
        source
        for source in all_items("/sources")
        if page == "import_history" or source["kind"] == "file"
    ]
    if not sources:
        st.info("Create a source to start tracking imports.")
        st.button(
            "Create your first source",
            on_click=set_navigation,
            args=("Sources", "create_source"),
        )
    else:
        source = st.selectbox("Source", sources, format_func=lambda s: s["name"])
    if sources and page == "upload":
        file = st.file_uploader(
            "CSV, XLSX, JSON, or Parquet", type=["csv", "xlsx", "json", "parquet"]
        )
        sheet = (
            st.text_input("Excel sheet (blank selects first sheet)")
            if file and file.name.lower().endswith(".xlsx")
            else None
        )
        records_path = (
            st.text_input(
                "JSON records path",
                placeholder="/data/items",
                help="Use the path to the object or array you want to inspect. Leave blank to use the root.",
            )
            if file and file.name.lower().endswith(".json")
            else None
        )
        if st.button("Upload dataset", disabled=file is None, type="primary"):
            result = api(
                "POST",
                "/datasets/upload",
                data={
                    "source_id": source["id"],
                    **({"sheet": sheet} if sheet else {}),
                    **({"records_path": records_path} if records_path else {}),
                },
                files={"file": (file.name, file.getvalue())},
            )
            st.success(
                f"{result['filename']} · {result['row_count']:,} rows ready to explore."
            )
            with st.expander("Import details"):
                st.json(result)
            st.button(
                "Open dataset explorer",
                on_click=set_navigation,
                args=("Explore", "explorer"),
            )
    if sources and page == "import_history":
        records = all_items(f"/sources/{source['id']}/history")
        if records:
            st.dataframe(records, hide_index=True)
        else:
            st.info("This source has no imports yet.")
else:
    if not datasets:
        st.info("Upload a dataset to use this workspace.")
        st.button(
            "Go to Upload data", on_click=set_navigation, args=("Sources", "upload")
        )
        st.stop()
    if page in {"build_recipe", "run_recipe", "transform_history"}:
        transformation_workspace(page)
    elif page in {"quality", "compare"}:
        quality_workspace(compare=page == "compare")
    elif page == "find":
        find_workspace()
    elif page == "ordered_match":
        ordered_match_workspace()
    elif page == "presence":
        presence_workspace()
    elif page == "join":
        reconciliation_workspace()
    elif page in {"quality_history", "join_history"}:
        run_history(quality=page == "quality_history")
    elif page in {"explorer", "profiling"}:
        st.header("Browse rows" if page == "explorer" else "Profile columns")
        identifier = st.selectbox("Dataset version", list(by_id), format_func=label)
        if page == "explorer":
            current = by_id[identifier]
            a, b, c = st.columns(3)
            a.metric("Rows", current["row_count"])
            b.metric("Columns", len(current["schema"]))
            previous = by_id.get(current.get("previous_dataset_id"))
            if previous:
                c.metric(
                    "Rows in previous version",
                    previous["row_count"],
                    delta=current["row_count"] - previous["row_count"],
                )
            st.caption(
                f"File: {current['filename']} · Imported: {current['created_at']}"
            )
            if previous:
                drift = current.get("schema_drift") or {}
                changed = sum(len(fields) for fields in drift.values())
                st.caption(
                    f"{changed} schema changes since {previous['filename']} ({previous['created_at']})."
                )
            with st.expander("Source metadata and schema"):
                st.json(api("GET", f"/datasets/{identifier}/schema"))
            offset = st.number_input("Row offset", min_value=0, step=50)
            st.dataframe(
                api(
                    "GET", f"/datasets/{identifier}/preview", params={"offset": offset}
                )["items"]
            )
        else:
            selected = st.multiselect(
                "Columns to profile",
                columns(identifier),
                default=columns(identifier),
                key=f"profile-{identifier}",
            )
            if st.button("Calculate profile", disabled=not selected):
                result = api(
                    "GET",
                    f"/datasets/{identifier}/profile",
                    params=[("columns", c) for c in selected],
                )
                for column in result["columns"]:
                    with st.expander(column["name"], expanded=True):
                        st.json(column)
    elif page == "mapping":
        st.header("Map columns")
        chosen = st.multiselect(
            "Datasets (select two or more)", list(by_id), format_func=label
        )
        if len(chosen) < 2:
            st.info("Select at least two dataset versions.")
            st.stop()
        if st.button("Suggest column mappings for first two datasets"):
            st.dataframe(
                api(
                    "POST",
                    "/mappings/suggest",
                    json={"left_dataset_id": chosen[0], "right_dataset_id": chosen[1]},
                )["items"]
            )
            st.caption(
                "Suggestions are evidence only. Confirm the columns below to save a mapping."
            )
        draft_key = "mapping-draft-" + "-".join(chosen)
        draft = st.session_state.setdefault(draft_key, [])
        st.subheader("Add a field mapping")
        left = st.selectbox("Input dataset", chosen, format_func=label)
        right = st.selectbox("Output dataset", chosen, index=1, format_func=label)
        inputs = st.multiselect("Input columns", columns(left), key=f"in-{left}")
        outputs = st.multiselect("Output columns", columns(right), key=f"out-{right}")
        operation = st.selectbox("Operation", ["copy", "combine", "split"])
        separator = st.text_input("Separator", " ")
        if st.button("Confirm and add field", disabled=not inputs or not outputs):
            draft.append(
                {
                    "inputs": [{"dataset_id": left, "column": c} for c in inputs],
                    "outputs": [{"dataset_id": right, "column": c} for c in outputs],
                    "operation": operation,
                    "separator": separator,
                }
            )
        st.json(draft)
        if st.button("Clear draft"):
            st.session_state[draft_key] = []
            st.rerun()
        name = st.text_input("Mapping name", "My mapping")
        if st.button("Preview mapping", disabled=not draft):
            st.json(
                api("POST", "/mappings/preview", json={"name": name, "fields": draft})
            )
        if st.button("Save confirmed mapping", disabled=not draft):
            st.json(api("POST", "/mappings", json={"name": name, "fields": draft}))
        st.subheader("Load and edit saved mappings")
        saved = all_items("/mappings")
        if saved:
            mapping = st.selectbox(
                "Saved mapping",
                saved,
                format_func=lambda m: f"{m['name']} v{m['version']}",
            )
            if mapping["latest_source_issues"]:
                st.error(
                    "Schema drift invalidates this mapping against current source versions."
                )
                st.write(mapping["latest_source_issues"])
            st.caption(
                "Saved mappings bind immutable dataset versions. To rerun, replace dataset IDs with the desired versions; missing columns are rejected."
            )
            edited = st.text_area(
                "Configuration JSON",
                json.dumps({k: mapping[k] for k in ("name", "fields")}, indent=2),
                height=300,
                key=f"edit-{mapping['id']}-{mapping['version']}",
            )
            if st.button("Validate and save edited version"):
                try:
                    configuration = json.loads(edited)
                except ValueError:
                    st.error("Invalid JSON.")
                    st.stop()
                st.json(api("PUT", f"/mappings/{mapping['id']}", json=configuration))
                st.rerun()
