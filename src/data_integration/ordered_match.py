"""Stop at the first rule that produces candidates for a source row."""

from collections import Counter, defaultdict

from rapidfuzz import process
from rapidfuzz.fuzz import token_sort_ratio

from .engine import InputError, json_rows
from .quality import blank, clean, phone_digits

RULES = {
    "phone_digits": "Phone",
    "license_id": "Driver license",
    "trimmed_text": "Username",
    "fuzzy_name": "Name similarity",
}
NAME_THRESHOLD = 90


def normalize(value, method):
    if blank(value):
        return ""
    if method == "phone_digits":
        digits = phone_digits(value)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits if 7 <= len(digits) <= 15 else ""
    if method == "license_id":
        return "".join(clean(value).casefold().split())
    if method == "trimmed_text":
        return clean(value, remove_symbols=False).casefold()
    return clean(value).casefold()


def field_differences(source, reference, fields):
    differences = []
    seen = set()
    for key in fields:
        pair = (key.left, key.right, key.method)
        if pair in seen:
            continue
        seen.add(pair)
        a, b = source[key.left], reference[key.right]
        if type(a) is type(b) and a == b:
            continue
        if blank(a) or blank(b):
            if blank(a) and blank(b):
                continue
            reason = "Missing on source" if blank(a) else "Missing on reference"
        elif key.method == "exact":
            reason = "Different value"
        else:
            left = normalize(a, key.method)
            right = normalize(b, key.method)
            if key.method == "fuzzy_name":
                left, right = sorted(left.split()), sorted(right.split())
            if not left or not right:
                reason = "Unusable after cleaning"
            elif left == right:
                reason = "Format or word order"
            else:
                reason = "Different value"
        differences.append(
            {
                "Source field": key.left,
                "Reference field": key.right,
                "Source value": a,
                "Reference value": b,
                "Difference": reason,
            }
        )
    return differences


def ordered_match(left, right, keys, audit_fields=(), name_threshold=None):
    threshold = NAME_THRESHOLD if name_threshold is None else name_threshold
    methods = [key.method for key in keys]
    if len(methods) != len(set(methods)) or set(methods) - RULES.keys():
        raise InputError(
            "Select each of Phone, Driver license, Username, or Name similarity at most once."
        )

    a, b = json_rows(left), json_rows(right)
    lookups = []
    for key in keys:
        lookup = defaultdict(list)
        for index, row in enumerate(b, 1):
            if value := normalize(row[key.right], key.method):
                lookup[value].append(index)
        lookups.append(lookup)

    rows = []
    fuzzy_work = 0
    for source_row, source in enumerate(a, 1):
        attempts = []
        status = "No usable value"
        rule = None
        stage = None
        score = None
        candidates = []
        for stage_number, (key, lookup) in enumerate(
            zip(keys, lookups, strict=True), 1
        ):
            label = RULES[key.method]
            value = normalize(source[key.left], key.method)
            if not value:
                attempts.append(f"{label}: skipped (no usable value)")
                continue
            status = "Missing"
            if key.method == "fuzzy_name":
                fuzzy_work += len(lookup)
                if fuzzy_work > 10_000_000:
                    raise InputError(
                        "Name scoring exceeds 10 million comparisons. Narrow the source or put selective exact rules first."
                    )
                choices = process.extract(
                    value,
                    lookup.keys(),
                    scorer=token_sort_ratio,
                    score_cutoff=threshold,
                    limit=None,
                )
                candidates = [
                    (row, similarity)
                    for candidate, similarity, _ in choices
                    if similarity > threshold
                    for row in lookup[candidate]
                ]
            else:
                candidates = [(row, 100) for row in lookup.get(value, ())]
            if not candidates:
                attempts.append(f"{label}: no match")
                continue
            rule = label
            stage = stage_number
            score = round(candidates[0][1], 2)
            status = "Matched" if len(candidates) == 1 else "Ambiguous"
            attempts.append(
                f"{label}: {status.lower()} ({len(candidates)} candidate{'s' if len(candidates) != 1 else ''})"
            )
            break
        reference_row = candidates[0][0] if status == "Matched" else None
        reference = b[reference_row - 1] if reference_row else {}
        matched_key = keys[stage - 1] if stage else None
        differences = (
            field_differences(source, reference, [*keys, *audit_fields])
            if reference_row is not None
            else []
        )
        rows.append(
            {
                "status": status,
                "difference_count": len(differences),
                "field_differences": differences,
                "left_row": source_row,
                "right_row": reference_row,
                "matched_rule": rule,
                "rule_position": stage,
                "value_checked": source[matched_key.left] if matched_key else None,
                "value_found": reference.get(matched_key.right)
                if matched_key
                else None,
                "score_pct": score,
                "candidate_count": len(candidates),
                "candidate_rows": ", ".join(str(row) for row, _ in candidates[:10]),
                "checks_tried": " → ".join(attempts),
                **{f"Left · {column}": source[column] for column in left.columns},
                **{
                    f"Right · {column}": reference.get(column)
                    for column in right.columns
                },
            }
        )

    assigned = Counter(row["right_row"] for row in rows if row["right_row"] is not None)
    for row in rows:
        row["reference_reused"] = assigned[row["right_row"]] > 1
        if row["reference_reused"]:
            row["status"] = "Review"
            row["review_reason"] = "Reference row also matched another source row."
        elif row["status"] == "Ambiguous":
            row["review_reason"] = (
                "More than one reference candidate qualified at this rule."
            )
        else:
            row["review_reason"] = None
    counts = Counter(row["status"] for row in rows)
    return {
        "left_rows": len(a),
        "rows_with_differences": sum(bool(row["difference_count"]) for row in rows),
        "right_rows": len(b),
        "matched": counts["Matched"],
        "ambiguous": counts["Ambiguous"],
        "review": counts["Review"],
        "missing": counts["Missing"],
        "no_usable_value": counts["No usable value"],
        "reused_reference_rows": sum(count > 1 for count in assigned.values()),
        "matched_by_rule": dict(
            Counter(row["matched_rule"] for row in rows if row["status"] == "Matched")
        ),
    }, rows
