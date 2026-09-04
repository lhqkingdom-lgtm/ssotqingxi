from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.ir import DocumentIR, TableIR
from core.text import compact


PACKAGE_RE = re.compile(r"\b(?:LQFP|TSSOP|UFQFPN|UFBGA|TFBGA|WLCSP|SO|SOP|SSOP|QFN|BGA)\s*\d*\b", re.IGNORECASE)
PIN_RE = re.compile(r"^(?:P[A-Z]\d+|P\d+\.\d+|VDD|VSS|NRST|BOOT\d*)\b", re.IGNORECASE)
AF_RE = re.compile(r"^AF(?:1[0-5]|[0-9])$", re.IGNORECASE)
def table_shape(table: TableIR) -> tuple[int, int]:
    if not table.cells:
        return 0, 0
    return max(cell.row for cell in table.cells) + 1, max(cell.column for cell in table.cells) + 1


def _anchor_texts(table: TableIR) -> list[str]:
    return [compact(cell.raw_text) for cell in table.cells if not cell.inherited_from and compact(cell.raw_text)]


def _continuation_key(table: TableIR) -> str:
    title = re.sub(r"<[^>]+>", "", table.title).casefold()
    title = re.sub(r"\(?\s*continued\s*\)?|（?\s*续(?:表)?\s*）?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title)
    return f"title:{title}" if title else ""


def validate_special_structures(document: DocumentIR) -> dict[str, Any]:
    table_reports: list[dict[str, Any]] = []
    categories: dict[str, list[str]] = {
        "rotated_tables": [],
        "multi_level_headers": [],
        "multi_package_pin_tables": [],
        "af_matrices": [],
    }
    hard_errors: list[dict[str, str]] = []
    by_continuation: dict[str, list[TableIR]] = defaultdict(list)

    for table in document.tables:
        rows, columns = table_shape(table)
        anchors = [cell for cell in table.cells if not cell.inherited_from]
        merged = [cell for cell in anchors if cell.row_span > 1 or cell.column_span > 1]
        texts = _anchor_texts(table)
        package_headers = sorted(set(match.group(0).upper().replace(" ", "") for text in texts for match in PACKAGE_RE.finditer(text)))
        pin_rows = [text for text in texts if PIN_RE.match(text)]
        af_headers = sorted(set(text.upper() for text in texts if AF_RE.fullmatch(text)))
        is_rotated = any(orientation % 180 != 0 for orientation in table.orientations)
        is_multi_level = any(cell.row < min(4, rows) and (cell.row_span > 1 or cell.column_span > 1) for cell in anchors)
        is_multi_package = len(package_headers) >= 2 and bool(pin_rows)
        is_af = len(af_headers) >= 2 and bool(pin_rows)

        if is_rotated:
            categories["rotated_tables"].append(table.table_id)
        if is_multi_level:
            categories["multi_level_headers"].append(table.table_id)
        if is_multi_package:
            categories["multi_package_pin_tables"].append(table.table_id)
        if is_af:
            categories["af_matrices"].append(table.table_id)

        key = _continuation_key(table)
        if key:
            by_continuation[key].append(table)
        image_exists = bool(table.metadata.get("image_exists"))
        if rows == 0 or columns == 0:
            hard_errors.append({"table_id": table.table_id, "reason": "empty logical grid"})
        if not image_exists:
            hard_errors.append({"table_id": table.table_id, "reason": "evidence image missing"})
        table_reports.append({
            "table_id": table.table_id,
            "title": table.title,
            "pages": list(table.pages),
            "rows": rows,
            "columns": columns,
            "merged_cells": len(merged),
            "orientation": list(table.orientations),
            "image_path": table.metadata.get("resolved_image_path", ""),
            "image_exists": image_exists,
            "package_headers": package_headers,
            "af_headers": af_headers,
            "pin_like_cells": len(pin_rows),
            "replacement_characters": sum(text.count("�") for text in texts),
        })

    continuation_groups = []
    for key, tables in by_continuation.items():
        ordered = sorted(tables, key=lambda item: min(item.pages))
        runs: list[list[TableIR]] = []
        current: list[TableIR] = []
        for table in ordered:
            if current and min(table.pages) - max(current[-1].pages) != 1:
                if len(current) > 1:
                    runs.append(current)
                current = []
            current.append(table)
        if len(current) > 1:
            runs.append(current)
        for run in runs:
            continuation_groups.append({
                "key": key,
                "table_ids": [table.table_id for table in run],
                "pages": [page for table in run for page in table.pages],
            })

    return {
        "source_file": document.source_file,
        "page_count": document.page_count,
        "table_count": len(document.tables),
        "hard_error_count": len(hard_errors),
        "hard_errors": hard_errors,
        "special_structures": {**categories, "cross_page_candidates": continuation_groups},
        "tables": table_reports,
    }


def build_evidence_manifest(document: DocumentIR, report: dict[str, Any]) -> dict[str, Any]:
    tags_by_table: dict[str, list[str]] = defaultdict(list)
    structures = report["special_structures"]
    for category in ("rotated_tables", "multi_level_headers", "multi_package_pin_tables", "af_matrices"):
        for table_id in structures[category]:
            tags_by_table[table_id].append(category)
    groups = []
    for index, group in enumerate(structures["cross_page_candidates"], start=1):
        group_id = f"cross-page-{index}"
        groups.append({"group_id": group_id, **group})
        for table_id in group["table_ids"]:
            tags_by_table[table_id].append("cross_page_candidate")

    return {
        "source_file": document.source_file,
        "backend": "mineru",
        "tables": [
            {
                "evidence_id": table.table_id,
                "table_id": table.table_id,
                "title": table.title,
                "pages": list(table.pages),
                "bboxes": [list(bbox) for bbox in table.bboxes],
                "image_paths": [table.metadata.get("resolved_image_path", "")],
                "structure_tags": tags_by_table.get(table.table_id, []),
            }
            for table in document.tables
        ],
        "groups": groups,
    }
