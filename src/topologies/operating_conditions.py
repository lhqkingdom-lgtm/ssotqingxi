from __future__ import annotations

import re
from typing import Any

from core.table_index import DocumentTableIndex, TableCandidateQuery
from core.text import clean, compact
from topologies.matrix_observer import table_grids


def is_absolute_maximum_context(value: object) -> bool:
    """Identify stress-limit sections that must not supply operating values."""
    normalized = compact(value).lower()
    return any(
        term in normalized
        for term in (
            "absolutemaximum",
            "maximumrating",
            "maximumratings",
            "stressrating",
            "极限参数",
            "绝对最大",
            "最大额定",
        )
    )


def number_from_cell(value: object) -> float | None:
    text = clean(value).replace("–", "-").replace("−", "-")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def parse_compound_operating_conditions(value: object) -> dict[str, float]:
    """Split order-independent voltage/temperature ranges by their units."""
    text = clean(value).replace("–", "-").replace("—", "-").replace("−", "-")
    result: dict[str, float] = {}
    pattern = re.compile(
        r"(-?\d+(?:\.\d+)?)\s*(V|℃|°\s*C)?\s*(?:-|~|～|至|TO)\s*"
        r"(-?\d+(?:\.\d+)?)\s*(V|℃|°\s*C)",
        re.I,
    )
    for match in pattern.finditer(text):
        first_unit = compact(match.group(2) or "").upper()
        final_unit = compact(match.group(4)).upper()
        if first_unit and first_unit != final_unit:
            continue
        low, high = float(match.group(1)), float(match.group(3))
        if final_unit == "V":
            result["voltage_min"], result["voltage_max"] = low, high
        else:
            result["temperature_min"], result["temperature_max"] = low, high
    return result


def is_primary_voltage_row(row: list[Any], recipe: dict[str, Any]) -> bool:
    symbols = {compact(item).upper() for item in recipe.get("primary_voltage_symbols", ["VDD"])}
    return bool(row) and compact(row[0]).upper() in symbols


def extract_operating_conditions(
    document: Any,
    table_index: DocumentTableIndex,
    recipe: dict[str, Any],
) -> dict[str, Any] | None:
    """Extract voltage and temperature ranges from a generic min/typ/max table."""
    table_terms = [compact(item).lower() for item in recipe["table_any"]]
    temperature_terms = [compact(item).lower() for item in recipe["temperature_any"]]
    temperature_exclusions = [compact(item).lower() for item in recipe["temperature_exclude_any"]]
    by_id = {table.table_id: table for table in document.tables}
    candidates = [
        by_id[item.table_id]
        for item in table_index.rank(
            TableCandidateQuery(
                text_any=tuple(table_terms),
                required_features=(
                    frozenset({"min_typ_max"})
                    if recipe.get("require_min_typ_max")
                    else frozenset()
                ),
                preferred_features=frozenset({"min_typ_max"}),
            )
        )
        if item.table_id in by_id
    ]
    for table in candidates:
        for fragment, page, rows, _ in table_grids(table):
            joined = compact(" ".join(clean(cell) for row in rows for cell in row)).lower()
            if not any(term in joined for term in table_terms):
                continue
            bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
            result: dict[str, Any] = {"page": page, "bbox": bbox, "rows": rows}
            for row in rows:
                label = compact(" ".join(clean(cell) for cell in row[:2])).lower()
                if any(term in label for term in table_terms) and is_primary_voltage_row(row, recipe):
                    values = [number for cell in row if (number := number_from_cell(cell)) is not None]
                    if len(values) >= 2:
                        result["voltage_min"], result["voltage_max"] = values[-2], values[-1]
                full_row = compact(" ".join(clean(cell) for cell in row)).lower()
                if any(term in label for term in temperature_terms) and not any(
                    term in full_row for term in temperature_exclusions
                ):
                    values = [number for cell in row if (number := number_from_cell(cell)) is not None]
                    if len(values) >= 2:
                        result["temperature_min"], result["temperature_max"] = values[-2], values[-1]
            return result
    return None
