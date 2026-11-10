"""Indexed reconciliation between immutable source versions."""

from collections import Counter, defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from .engine import InputError, frame_for, json_rows
from .ordered_match import ordered_match
from .quality import blank, clean, phone_digits, require_columns


class JoinKey(BaseModel):
    left: str
    right: str
    method: Literal[
        "exact",
        "trimmed_text",
        "clean_text",
        "phone_digits",
        "license_id",
        "fuzzy_name",
    ] = "exact"
    required: bool = False


class JoinRequest(BaseModel):
    left_id: str
    right_id: str
    keys: list[JoinKey] = Field(min_length=1, max_length=20)
    audit_fields: list[JoinKey] | None = Field(None, max_length=12)
    name_threshold: float | None = Field(None, ge=0, lt=100)
    minimum_agreements: int | None = Field(None, ge=1, le=20)
    view: Literal[
        "inner", "unmatched", "left", "right", "full", "presence", "ordered"
    ] = "inner"


def reconcile(store, request):
    left = frame_for(store, request.left_id)
    right = frame_for(store, request.right_id)
    require_columns(left, [key.left for key in request.keys])
    require_columns(right, [key.right for key in request.keys])

    if request.view == "ordered":
        audit = request.audit_fields or []
        require_columns(left, [key.left for key in audit])
        require_columns(right, [key.right for key in audit])
        return ordered_match(left, right, request.keys, audit, request.name_threshold)
    if any(key.method in {"license_id", "fuzzy_name"} for key in request.keys):
        raise InputError(
            "Use Ordered driver match for license normalization or fuzzy name fallback."
        )

    threshold = request.minimum_agreements or len(request.keys)
    if threshold > len(request.keys):
        raise InputError(
            "Minimum agreements cannot exceed the number of selected fields."
        )
    if threshold < len(request.keys) and len(
        {(key.left, key.right) for key in request.keys}
    ) != len(request.keys):
        raise InputError("Each column pair can count only once.")
    required = {index for index, key in enumerate(request.keys) if key.required}

    def index_rows(frame, side):
        complete = defaultdict(list)
        records = json_rows(frame)
        normalized = {}
        for number, row in enumerate(records, 1):
            values = {}
            for index, key in enumerate(request.keys):
                value = row[getattr(key, side)]
                if blank(value):
                    continue
                if key.method == "clean_text":
                    value = clean(value).casefold()
                elif key.method == "trimmed_text":
                    value = clean(value, remove_symbols=False).casefold()
                elif key.method == "phone_digits":
                    value = phone_digits(value)
                if value != "":
                    values[index] = repr(value)
            if len(values) == len(request.keys):
                complete[tuple(values.values())].append(number)
            normalized[number] = values
        return records, complete, normalized

    a, a_keys, a_values = index_rows(left, "left")
    b, b_keys, b_values = index_rows(right, "right")
    matched_a = Counter()
    matched_b = Counter()
    accepted = []
    example_right = {}
    if threshold == len(request.keys):
        for key in a_keys.keys() & b_keys.keys():
            for i in a_keys[key]:
                matched_a[i] = len(b_keys[key])
                if request.view == "presence":
                    example_right[i] = b_keys[key][0]
            for j in b_keys[key]:
                matched_b[j] = len(a_keys[key])
        pairs = sum(matched_a.values())
    else:
        postings = defaultdict(list)
        for j, values in b_values.items():
            if required <= values.keys() and len(values) >= threshold:
                for index, value in values.items():
                    postings[index, value].append(j)
        comparisons = 0
        for i, values in a_values.items():
            if not required <= values.keys() or len(values) < threshold:
                continue
            candidate_counts = Counter()
            for index, value in values.items():
                hits = postings[index, value]
                comparisons += len(hits)
                if comparisons > 5_000_000:
                    raise InputError(
                        "The selected fields produce too many candidate comparisons. Use all fields or remove common values from the comparison."
                    )
                candidate_counts.update(hits)
            for j, count in candidate_counts.items():
                if count < threshold or any(
                    values[index] != b_values[j][index] for index in required
                ):
                    continue
                matched_a[i] += 1
                matched_b[j] += 1
                if request.view == "presence":
                    example_right.setdefault(i, j)
                elif request.view != "unmatched":
                    accepted.append((i, j))
                    if len(accepted) > 100_000:
                        raise InputError(
                            "Too many qualifying pairs. Increase the minimum agreements or choose Unmatched."
                        )
        pairs = sum(matched_a.values())
    a_only = len(a) - len(matched_a)
    b_only = len(b) - len(matched_b)
    not_checkable = {
        row
        for row, values in a_values.items()
        if len(values) < threshold or not required <= values.keys()
    }
    counts = {
        "left_rows": len(a),
        "right_rows": len(b),
        "matched_pairs": pairs,
        "matched_left_rows": len(matched_a),
        "matched_right_rows": len(matched_b),
        "left_multiple_candidates": sum(count > 1 for count in matched_a.values()),
        "right_multiple_candidates": sum(count > 1 for count in matched_b.values()),
        "left_only": a_only,
        "left_missing": a_only - len(not_checkable),
        "left_not_checkable": len(not_checkable),
        "right_only": b_only,
        "left_blank_keys": len(a) - sum(map(len, a_keys.values())),
        "right_blank_keys": len(b) - sum(map(len, b_keys.values())),
        "left_duplicate_keys": sum(n > 1 for n in map(len, a_keys.values())),
        "right_duplicate_keys": sum(n > 1 for n in map(len, b_keys.values())),
    }
    output_count = {
        "inner": pairs,
        "unmatched": a_only + b_only,
        "left": pairs + a_only,
        "right": pairs + b_only,
        "full": pairs + a_only + b_only,
        "presence": len(a),
    }[request.view]
    if output_count > 100_000:
        raise InputError(
            "This view would produce more than 100,000 rows. Add a key column to narrow the join or select Unmatched."
        )

    def result(status, left_row=None, right_row=None):
        left_values = a_values.get(left_row, {})
        right_values = b_values.get(right_row, {})
        agreements = [
            index
            for index, value in left_values.items()
            if right_values.get(index) == value
        ]
        return {
            "agreement_count": len(agreements),
            "fields_compared": len(left_values.keys() & right_values.keys()),
            "agreed_fields": "; ".join(
                f"{request.keys[index].left} ↔ {request.keys[index].right}"
                for index in agreements
            ),
            "left_candidate_count": matched_a[left_row] if left_row else None,
            "right_candidate_count": matched_b[right_row] if right_row else None,
            "status": status,
            "left_row": left_row,
            "right_row": right_row,
            **{
                f"Left · {name}": a[left_row - 1][name] if left_row else None
                for name in left.columns
            },
            **{
                f"Right · {name}": b[right_row - 1][name] if right_row else None
                for name in right.columns
            },
        }

    rows = []
    if request.view == "presence":
        for i in range(1, len(a) + 1):
            if i in matched_a:
                rows.append(result("Found", i, example_right[i]))
            else:
                status = "No usable key" if i in not_checkable else "Missing"
                rows.append(result(status, i))
        return counts, rows
    if request.view != "unmatched":
        candidate_pairs = (
            (
                (i, j)
                for key in a_keys
                if key in b_keys
                for i in a_keys[key]
                for j in b_keys[key]
            )
            if threshold == len(request.keys)
            else accepted
        )
        rows.extend(result("Matched", i, j) for i, j in candidate_pairs)
    if request.view in {"unmatched", "left", "full"}:
        rows.extend(
            result("Left only", i) for i in range(1, len(a) + 1) if i not in matched_a
        )
    if request.view in {"unmatched", "right", "full"}:
        rows.extend(
            result("Right only", right_row=j)
            for j in range(1, len(b) + 1)
            if j not in matched_b
        )
    return counts, rows
