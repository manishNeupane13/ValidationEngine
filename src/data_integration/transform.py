"""Ordered transformations and validation over immutable snapshots."""

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from .engine import InputError, frame_for, json_rows
from .quality import blank, clean, phone_digits

Operation = Literal[
    "copy",
    "extract_field",
    "trim",
    "clean_text",
    "phone_digits",
    "parse_date",
    "parse_number",
    "required",
    "unique",
]
CHECKS = {"required", "unique"}


class Step(BaseModel):
    operation: Operation
    column: str = Field(min_length=1)
    output: str | None = Field(None, min_length=1)
    field_path: str = ""
    date_format: str = Field("%Y-%m-%d", min_length=1)


class Recipe(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_id: str
    steps: list[Step] = Field(min_length=1, max_length=30)


class RecipeExecution(BaseModel):
    dataset_id: str


def execute_recipe(store, dataset_id, recipe):
    frame = frame_for(store, dataset_id)
    available = set(frame.columns)
    for number, step in enumerate(recipe.steps, 1):
        if step.column not in available:
            raise InputError(
                f"Step {number} references missing column {step.column!r}."
            )
        if step.operation == "extract_field" and not step.field_path.startswith("/"):
            raise InputError(
                f"Step {number} requires a nested field path such as /contact/phone."
            )
        if step.operation not in CHECKS:
            available.add(step.output or step.column)

    originals = json_rows(frame)
    rows = [row.copy() for row in originals]
    exceptions = []
    trace = []
    for number, step in enumerate(recipe.steps, 1):
        values = [row.get(step.column) for row in rows]
        counts = (
            Counter(repr(value) for value in values if not blank(value))
            if step.operation == "unique"
            else {}
        )
        failures = 0
        changes = 0
        output = step.output or step.column
        for index, value in enumerate(values):
            try:
                if step.operation in CHECKS:
                    if step.operation == "required" and blank(value):
                        raise ValueError("Required value is blank.")
                    if step.operation == "unique" and counts.get(repr(value), 0) > 1:
                        raise ValueError("Value occurs more than once.")
                    continue
                transformed = convert(value, step)
                changes += int(
                    output not in rows[index] or transformed != rows[index][output]
                )
                rows[index][output] = transformed
            except (ValueError, InvalidOperation) as error:
                failures += 1
                exceptions.append(
                    {
                        "source_row": index + 1,
                        "step": number,
                        "column": step.column,
                        "value": value,
                        "reason": str(error),
                    }
                )
                if step.operation not in CHECKS:
                    rows[index][output] = None
        trace.append(
            {
                "step": number,
                "operation": step.operation,
                "column": step.column,
                "output": output if step.operation not in CHECKS else None,
                "rows_checked": len(rows),
                "values_changed": changes,
                "issues": failures,
            }
        )
    return rows, originals, trace, exceptions


def convert(value, step):
    if step.operation == "copy" or value is None:
        return value
    if step.operation == "extract_field":
        for part in step.field_path[1:].split("/"):
            key = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(value, dict) or key not in value:
                raise ValueError(f"Nested field {step.field_path!r} is missing.")
            value = value[key]
        return value
    if step.operation == "trim":
        return clean(value, remove_symbols=False)
    if step.operation == "clean_text":
        return clean(value)
    if step.operation == "phone_digits":
        digits = phone_digits(value)
        if not digits and not blank(value):
            raise ValueError("No phone digits found.")
        return digits or None
    if blank(value):
        return None
    if step.operation == "parse_date":
        return datetime.strptime(str(value).strip(), step.date_format).isoformat()
    try:
        parsed = Decimal(str(value).strip())
    except InvalidOperation as error:
        raise ValueError("Expected a decimal number.") from error
    if not parsed.is_finite():
        raise ValueError("Expected a finite number.")
    # Keep exact decimals as text for CSV export and subsequent comparisons.
    return str(parsed)
