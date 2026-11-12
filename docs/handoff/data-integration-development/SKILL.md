---
name: data-integration-development
description: Build or extend a local data integration workspace for versioned imports, cleaning, validation, record matching, and reconciliation reports. Use when implementing its backend or analyst UI.
---

# Data integration development

Read the user's build prompt or existing project requirements. Inspect the repository before selecting an architecture. Extend existing APIs and persisted configurations when present. Keep the instructions portable across coding agents and run the project in its normal local environment.

## Preserve these behaviors

- Import snapshots are immutable. Record source, original filename, origin, timestamp, and version. Discover schemas at runtime and populate both sides of every field selector from the selected versions. A rerun should identify missing mapped columns.
- Keep transformation, validation, search, and matching in shared backend code. Preserve identifiers as text where leading zeros matter. Show original values and cleaning output together.
- Provide distinct navigation for Find, Is In, and IF / ELSE. Find searches values across records. Is In tests one column's values against another and reports coverage. IF / ELSE resolves candidates in the user's chosen priority order.
- Exact rules use indexes. Evaluate fallback rules only when an earlier rule has no candidate. Stop on a unique candidate or on ambiguity. Flag reference reuse. Show the rule, evidence, and attempts per source row. Separate fuzzy similarity previews from match decisions.
- Compare fields on an already selected pair independently of matching. Distinguish formatting, missing values, and conflicts. Keep timestamps visible and leave source-of-truth decisions to the user.
- Keep run history and exports traceable to source versions and settings. Restrict connector destinations to configured trusted HTTPS hosts. Authenticate webhook ingestion and keep credential values out of exported data and logs.

## Verify with realistic examples

Use synthetic Fleet App JSON and a PTI workbook with different headers and shared drivers. Include blank values, duplicate phones, casing, emojis, missing middle names, license separators, and true field conflicts. Verify early exit after a phone match, a separate status difference report, an ambiguous duplicate candidate, and a newer upload with renamed columns. Exercise the actual UI and backend entry points. Report tested outcomes and material limits.
