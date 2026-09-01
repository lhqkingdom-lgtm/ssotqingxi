from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from core.metadata import METADATA_FIELDS


def valid_pin_designator(value: Any) -> bool:
    """Accept positive lead numbers and standard BGA ball coordinates."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value).is_integer() and int(value) > 0
    text = str(value).strip().upper()
    if re.fullmatch(r"\d+", text):
        return int(text) > 0
    return re.fullmatch(r"[A-Z]{1,2}[1-9]\d*", text) is not None


def validate(data: dict[str, list[dict[str, Any]]], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply only deterministic hard checks shared by every document family."""
    issues = []
    chip_ids = {row["chip_id"] for row in data["chips"]}
    for sheet, rows in data.items():
        key_fields = schema["sheets"][sheet]["key"]
        seen = set()
        for index, row in enumerate(rows, start=2):
            key = tuple(row.get(field) for field in key_fields)
            if key in seen:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"duplicate key {key}"})
            seen.add(key)
            if sheet != "chips" and row.get("chip_id") not in chip_ids:
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"unknown chip_id {row.get('chip_id')}"})
            if sheet == "pin_mux" and not valid_pin_designator(row.get("pin_number")):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"invalid pin_number {row.get('pin_number')}"})
            for field in METADATA_FIELDS:
                if field not in row:
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": f"missing metadata field {field}"})
            if not isinstance(row.get("pdf_source_file"), str) or not row.get("pdf_source_file"):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": "pdf_source_file must be non-empty text"})
            if not isinstance(row.get("evidence_pages"), str) or not row.get("evidence_pages"):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": "evidence_pages must be non-empty text"})
            if not isinstance(row.get("ingested_by"), str) or not row.get("ingested_by"):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": "ingested_by must be non-empty text"})
            if not isinstance(row.get("ingested_at"), str):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": "ingested_at must be ISO datetime text"})
            else:
                try:
                    datetime.fromisoformat(row["ingested_at"])
                except ValueError:
                    issues.append({"level": "error", "sheet": sheet, "row": index, "message": "ingested_at must be ISO datetime text"})
            if not isinstance(row.get("is_reviewed"), bool):
                issues.append({"level": "error", "sheet": sheet, "row": index, "message": "is_reviewed must be boolean"})
    for row in data["chips"]:
        if row.get("voltage_min") is not None and row.get("voltage_max") is not None and row["voltage_min"] > row["voltage_max"]:
            issues.append({"level": "error", "sheet": "chips", "key": row["chip_id"], "message": "voltage_min > voltage_max"})
    return issues
