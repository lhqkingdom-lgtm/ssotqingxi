from __future__ import annotations

from datetime import datetime
from typing import Any

from core.validation import valid_pin_designator


EXPECTED_SHEETS = ("chips", "pin_mux", "peripherals", "electrical", "memory")
SUPPORTED_TYPES = {"string", "number", "integer", "boolean", "array_string", "iso_datetime", "pin_designator"}


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array_string":
        return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
    if expected == "iso_datetime":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
        return True
    if expected == "pin_designator":
        return valid_pin_designator(value)
    return False


def validate_schema(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the fixed five-sheet public output contract itself."""
    issues: list[dict[str, Any]] = []
    order = schema.get("sheet_order")
    sheets = schema.get("sheets")
    metadata = schema.get("metadata_fields")
    if order != list(EXPECTED_SHEETS):
        issues.append({"level": "error", "message": f"sheet_order must be {list(EXPECTED_SHEETS)}"})
    if not isinstance(sheets, dict) or list(sheets) != list(EXPECTED_SHEETS):
        issues.append({"level": "error", "message": f"sheets must be ordered as {list(EXPECTED_SHEETS)}"})
        return issues
    if not isinstance(metadata, list) or len(metadata) != 5 or len(set(metadata)) != 5:
        issues.append({"level": "error", "message": "metadata_fields must contain exactly five unique fields"})
        metadata = []
    for sheet, config in sheets.items():
        fields = config.get("fields", [])
        key = config.get("key", [])
        nullable_key_fields = config.get("nullable_key_fields", [])
        required = config.get("required", [])
        field_types = config.get("field_types", {})
        if not fields or len(fields) != len(set(fields)):
            issues.append({"level": "error", "sheet": sheet, "message": "fields must be non-empty and unique"})
        if metadata and fields[-len(metadata):] != metadata:
            issues.append({"level": "error", "sheet": sheet, "message": "the five metadata fields must be the final columns"})
        for label, values in (("key", key), ("required", required)):
            unknown = [field for field in values if field not in fields]
            if unknown:
                issues.append({"level": "error", "sheet": sheet, "message": f"{label} contains unknown fields {unknown}"})
        if any(field not in key for field in nullable_key_fields):
            issues.append({"level": "error", "sheet": sheet, "message": "nullable_key_fields must be a subset of key"})
        if set(field_types) != set(fields):
            issues.append({"level": "error", "sheet": sheet, "message": "field_types must describe every field exactly once"})
        for field, types in field_types.items():
            if not isinstance(types, list) or not types or any(item not in SUPPORTED_TYPES for item in types):
                issues.append({"level": "error", "sheet": sheet, "field": field, "message": f"invalid field types {types}"})
        unknown_units = [field for field in config.get("canonical_units", {}) if field not in fields]
        if unknown_units:
            issues.append({"level": "error", "sheet": sheet, "message": f"canonical_units contains unknown fields {unknown_units}"})
        for field, bounds in config.get("ranges", {}).items():
            if field not in fields or not isinstance(bounds, list) or len(bounds) != 2 or not all(isinstance(value, (int, float)) for value in bounds) or bounds[0] > bounds[1]:
                issues.append({"level": "error", "sheet": sheet, "field": field, "message": f"invalid numeric range {bounds}"})
        for pair in config.get("ordered_pairs", []):
            if not isinstance(pair, list) or len(pair) != 2 or any(field not in fields for field in pair):
                issues.append({"level": "error", "sheet": sheet, "message": f"invalid ordered pair {pair}"})
    return issues


def validate_data_contract(data: dict[str, list[dict[str, Any]]], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate public fields, types, keys, relations and ordered numeric pairs."""
    issues = validate_schema(schema)
    if issues:
        return issues
    expected_sheets = schema["sheet_order"]
    if list(data) != expected_sheets:
        issues.append({"level": "error", "message": f"data sheets must be ordered as {expected_sheets}"})
        return issues
    chip_ids = {row.get("chip_id") for row in data["chips"]}
    for sheet in expected_sheets:
        config = schema["sheets"][sheet]
        fields = config["fields"]
        field_set = set(fields)
        key_fields = config["key"]
        nullable_key_fields = set(config.get("nullable_key_fields", []))
        seen: set[tuple[Any, ...]] = set()
        for index, row in enumerate(data[sheet], start=2):
            public_fields = [field for field in row if not field.startswith("_")]
            missing_fields = [field for field in fields if field not in row]
            extra_fields = [field for field in public_fields if field not in field_set]
            if missing_fields:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"missing public fields {missing_fields}"})
            if schema.get("allow_additional_public_fields") is False and extra_fields:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"unknown public fields {extra_fields}"})
            for field in config["required"]:
                value = row.get(field)
                if value is None or value == "" or value == []:
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"required field {field} is empty"})
            for field, types in config["field_types"].items():
                value = row.get(field)
                if value is None:
                    continue
                if not any(_matches_type(value, expected) for expected in types):
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"{field} has invalid type/value {value!r}; expected {types}"})
            for field, bounds in config.get("ranges", {}).items():
                value = row.get(field)
                if value is not None and isinstance(value, (int, float)) and not isinstance(value, bool) and not bounds[0] <= value <= bounds[1]:
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"{field} is outside canonical range {bounds}: {value}"})
            key = tuple(row.get(field) for field in key_fields)
            if any(
                row.get(field) in (None, "") and field not in nullable_key_fields
                for field in key_fields
            ):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"blank key {key}"})
            elif key in seen:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"duplicate key {key}"})
            seen.add(key)
            if sheet != "chips" and row.get("chip_id") not in chip_ids:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"unknown chip_id {row.get('chip_id')}"})
            for low_field, high_field in config.get("ordered_pairs", []):
                low, high = row.get(low_field), row.get(high_field)
                if low is not None and high is not None and low > high:
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"{low_field} > {high_field}"})
    return issues
