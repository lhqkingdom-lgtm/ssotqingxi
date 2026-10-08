from __future__ import annotations

import argparse
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CELL_ERROR = re.compile(r"^(?:#REF!|#DIV/0!|#VALUE!|#NAME\?|#N/A)$")
CELL_REF = re.compile(r"([A-Z]+)")


def column_index(reference: str) -> int:
    match = CELL_REF.match(reference)
    if not match:
        raise ValueError(f"invalid cell reference: {reference}")
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - 64
    return value - 1


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    values: list[str] = []
    with archive.open("xl/sharedStrings.xml") as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag == f"{{{MAIN_NS}}}si":
                values.append("".join(node.text or "" for node in element.iter(f"{{{MAIN_NS}}}t")))
                element.clear()
    return values


def workbook_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {item.attrib["Id"]: item.attrib["Target"] for item in rels.findall(f"{{{PKG_REL_NS}}}Relationship")}
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    result = []
    for sheet in workbook.findall(f".//{{{MAIN_NS}}}sheet"):
        target = targets[sheet.attrib[f"{{{REL_NS}}}id"]].replace("\\", "/")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target.lstrip("./")
        result.append((sheet.attrib["name"], path))
    return result


def cell_value(cell: ET.Element, strings: list[str]) -> Any:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{{{MAIN_NS}}}t"))
    node = cell.find(f"{{{MAIN_NS}}}v")
    if node is None or node.text is None:
        return None
    raw = node.text
    if cell_type == "s":
        return strings[int(raw)]
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return raw


def sheet_rows(archive: zipfile.ZipFile, path: str, strings: list[str]) -> Iterator[tuple[int, list[Any], list[tuple[int, str]]]]:
    with archive.open(path) as stream:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag != f"{{{MAIN_NS}}}row":
                continue
            row_number = int(element.attrib.get("r", "0"))
            values: list[Any] = []
            formulas: list[tuple[int, str]] = []
            for cell in element.findall(f"{{{MAIN_NS}}}c"):
                index = column_index(cell.attrib["r"])
                values.extend([None] * (index + 1 - len(values)))
                values[index] = cell_value(cell, strings)
                formula = cell.find(f"{{{MAIN_NS}}}f")
                if formula is not None:
                    formulas.append((index, "=" + (formula.text or "")))
            yield row_number, values, formulas
            element.clear()


def excel_date_serial(value: Any) -> float | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", str(value or ""))
    if not match:
        return None
    moment = datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)
    return (moment - datetime(1899, 12, 30, tzinfo=timezone.utc)).total_seconds() / 86400


def expected_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "ingested_at":
        return excel_date_serial(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def equivalent(left: Any, right: Any) -> bool:
    if left in (None, "") and right in (None, ""):
        return True
    numeric = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool)
    if numeric(left) and numeric(right):
        return math.isclose(float(left), float(right), abs_tol=1e-8)
    return left == right


def key_text(row: dict[str, Any], fields: list[str]) -> tuple[str, ...]:
    return tuple(str(row.get(field) or "").casefold().replace("_", " ") for field in fields)


def validate_structure(workbook_path: Path, schema_path: Path, mode: str) -> dict[str, Any]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"mode": mode, "sheets": {}, "errors": [], "formulas": 0, "reader": "streaming-ooxml"}
    with zipfile.ZipFile(workbook_path) as archive:
        strings = shared_strings(archive)
        sheets = workbook_sheets(archive)
        expected_names = list(schema["sheets"])
        actual_names = [name for name, _ in sheets]
        if actual_names != expected_names:
            report["errors"].append({"kind": "sheet_structure", "expected": expected_names, "actual": actual_names})
        paths = dict(sheets)
        for sheet_name, config in schema["sheets"].items():
            if sheet_name not in paths:
                continue
            fields = config["fields"]
            header: list[str] = []
            rows = blank_rows = used_columns = formula_count = 0
            for row_number, values, formulas in sheet_rows(archive, paths[sheet_name], strings):
                used_columns = max(used_columns, len(values))
                if row_number == 1:
                    header = [str(value or "") for value in values[: len(fields)]]
                elif any(value not in (None, "") for value in values[: len(fields)]):
                    rows += 1
                else:
                    blank_rows += 1
                for column, formula in formulas:
                    formula_count += 1
                    if mode == "clean":
                        report["errors"].append({"sheet": sheet_name, "kind": "formula_in_clean_workbook", "row": row_number, "column": column + 1})
                    allowed = fields.index(config["evidence_link_field"])
                    if mode == "evidence" and column != allowed:
                        report["errors"].append({"sheet": sheet_name, "kind": "formula_in_wrong_column", "row": row_number, "column": column + 1})
                    if mode == "evidence" and not re.match(r'^=HYPERLINK\("evidence/index\.html#row-[0-9a-f]{16}",', formula):
                        report["errors"].append({"sheet": sheet_name, "kind": "invalid_evidence_formula", "row": row_number, "column": column + 1})
                for column, value in enumerate(values):
                    if CELL_ERROR.match(str(value or "")):
                        report["errors"].append({"sheet": sheet_name, "kind": "formula_error", "row": row_number, "column": column + 1, "value": value})
            if used_columns != len(fields):
                report["errors"].append({"sheet": sheet_name, "kind": "column_count", "expected": len(fields), "actual": used_columns})
            if header != fields:
                report["errors"].append({"sheet": sheet_name, "kind": "header_mismatch", "expected": fields, "actual": header})
            if blank_rows:
                report["errors"].append({"sheet": sheet_name, "kind": "blank_formatted_rows", "count": blank_rows})
            report["formulas"] += formula_count
            report["sheets"][sheet_name] = {"rows": rows, "used_range_rows": rows + blank_rows, "blank_formatted_rows": blank_rows, "columns": len(header), "formulas": formula_count}
    return report


def validate_data(workbook_path: Path, extracted_path: Path, schema_path: Path) -> dict[str, Any]:
    extracted = json.loads(extracted_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    report: dict[str, Any] = {"sheets": {}, "differences": [], "reader": "streaming-ooxml"}
    with zipfile.ZipFile(workbook_path) as archive:
        strings = shared_strings(archive)
        paths = dict(workbook_sheets(archive))
        for sheet_name, config in schema["sheets"].items():
            fields = config["fields"]
            expected_rows = sorted(extracted.get("data", {}).get(sheet_name, []), key=lambda row: key_text(row, config["key"]))
            actual_count = 0
            for row_number, values, _ in sheet_rows(archive, paths[sheet_name], strings):
                if row_number == 1 or not any(value not in (None, "") for value in values[: len(fields)]):
                    continue
                index = actual_count
                actual_count += 1
                if index >= len(expected_rows):
                    if len(report["differences"]) < 100:
                        report["differences"].append({"sheet": sheet_name, "kind": "unexpected_row", "row": row_number})
                    continue
                expected = expected_rows[index]
                for column, field in enumerate(fields):
                    actual = values[column] if column < len(values) else None
                    wanted = expected_value(field, expected.get(field))
                    if not equivalent(actual, wanted) and len(report["differences"]) < 100:
                        report["differences"].append({"sheet": sheet_name, "row": row_number, "field": field, "expected": wanted, "actual": actual})
            if actual_count != len(expected_rows):
                report["differences"].append({"sheet": sheet_name, "kind": "row_count", "expected": len(expected_rows), "actual": actual_count})
            report["sheets"][sheet_name] = {"rows": actual_count, "fields": len(fields)}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream-validate large SSOT workbooks.")
    commands = parser.add_subparsers(dest="command", required=True)
    structure = commands.add_parser("structure")
    structure.add_argument("workbook", type=Path)
    structure.add_argument("schema", type=Path)
    structure.add_argument("mode", choices=("clean", "evidence"), default="clean", nargs="?")
    data = commands.add_parser("data")
    data.add_argument("workbook", type=Path)
    data.add_argument("extracted", type=Path)
    data.add_argument("schema", type=Path)
    args = parser.parse_args()
    if args.command == "structure":
        report = validate_structure(args.workbook, args.schema, args.mode)
        failed = bool(report["errors"])
    else:
        report = validate_data(args.workbook, args.extracted, args.schema)
        failed = bool(report["differences"])
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(2 if failed else 0)


if __name__ == "__main__":
    main()
