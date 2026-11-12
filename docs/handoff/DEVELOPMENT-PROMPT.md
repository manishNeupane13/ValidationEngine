# Build the Data Integration Workspace

Build a working local-first data integration and reconciliation app. If the repository already contains the app, inspect it and extend the existing implementation. If starting from scratch, use Python, FastAPI, Streamlit, Polars, SQLite, Parquet, and RapidFuzz. Keep import, validation, and matching logic in the backend so the UI and API use the same rules.

## Product requirements

| Top navigation | Workflow |
| --- | --- |
| Sources | Upload CSV, Excel, JSON, and Parquet. Add a named HTTPS GET connection or authenticated JSON webhook. Show import history and source metadata. |
| Find | Search values across the latest import of each source or narrow the search to a dataset version and column. Show file, row number, matching fields, and the original row. |
| Explore | Browse rows, profile columns, compare schemas between versions, and map columns with different names. |
| Transform | Build ordered cleaning and extraction recipes, preview changes and errors, save recipes, and publish clean output as a new version. |
| Validate | Check required fields and duplicates, filter exceptions, and export the rows behind the counts. |
| Is In | Check whether values from a selected column in one file appear in a selected column in another. Show found, missing, and unusable counts, match rate, row evidence, and a filtered CSV. |
| IF / ELSE | Build and run a configurable sequence of matching rules. Review matched rows, ambiguity, missing records, and field differences. |
| Match & Join | Join on selected fields and separately preview fuzzy candidates for names, strings, and numbers represented as text. |
| Reports | Reopen previous runs with their dataset versions and settings, rerun saved checks on newer imports, and export the evidence. |

Make Is In and IF / ELSE visible as top-level buttons. Give each page a short description, clear input labels, summary counts, and a result table. Use column selectors from the actual dataset schema. Support nested JSON record paths such as /data/drivers. Preserve the raw response and record source name, original filename where supplied, origin, import timestamp, and version. Keep uploaded versions immutable. Store connector secrets on the server. Restrict GET to trusted configured HTTPS hosts and use bearer authentication for webhook ingestion.

## Matching and difference rules

- Default to Phone → Driver license → Username → Fuzzy name. Let users reorder or omit stages and select a separate column on each side. Source-specific IDs are not interchangeable unless the user maps them intentionally.
- For each source row, run the next rule only if the current rule has no candidates. Skip missing or unusable values. A unique candidate stops matching. Multiple candidates stop for review, and reused reference rows require review. Produce one outcome per source row.
- Compare normalized phone digits, including optional US country code handling. Compare license IDs ignoring separators and case while retaining leading zeros. Compare usernames ignoring case and extra spaces. Fuzzy name compares cleaned words regardless of order with a configurable threshold, defaulting to strictly above 90%. Show the score and the rules actually attempted.
- Once a pair is found, independently compare selected fields to identify format-only changes, missing values, and actual value conflicts. Show original values side by side. For example, Ada 🚚 versus Ada is formatting, while Pending versus Complete is a value conflict. A newer import timestamp does not prove which field value is correct.
- Use indexed exact matching for large datasets. Apply fuzzy scoring only to rows that reach that stage. Similarity is candidate evidence. Never silently overwrite source data or merge ambiguous identities.

## Deliver and verify

Build the screens and endpoints, provide startup commands and environment settings, and use synthetic examples. Verify a pair of 50-row Fleet App and PTI datasets with 44 true overlaps, different headers, misspellings, missing names, punctuation, phone formatting, shared phones, and license mistakes. Test that a phone match skips all lower match rules while its status conflict still appears in the comparison report. Test ambiguous candidates, schema changes, filename provenance, webhook authentication, and CSV export. Report what you verified and call out any remaining limitations.

If I supply the companion skill, read it before implementing. If this platform has no skill loader, use its Markdown as project instructions.
