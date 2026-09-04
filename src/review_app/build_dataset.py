from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


DATASET_VERSION = 1


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_id(sheet: str, row: dict[str, Any], key_fields: list[str]) -> str:
    key = [row.get(field) for field in key_fields]
    digest = hashlib.sha1(_canonical_json([sheet, key]).encode("utf-8")).hexdigest()[:16]
    return f"row-{digest}"


def _load_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _natural_key(value: object) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", str(value or ""))
        if part
    )


def pin_record_sort_key(record: dict[str, Any]) -> tuple[object, ...]:
    values = record.get("values", {})
    pin_number = values.get("pin_number")
    number_key: tuple[object, ...]
    if isinstance(pin_number, (int, float)) and not isinstance(pin_number, bool):
        number_key = (0, float(pin_number))
    elif re.fullmatch(r"\d+(?:\.0+)?", str(pin_number or "").strip()):
        number_key = (0, float(pin_number))
    else:
        number_key = (1, _natural_key(pin_number))
    return (
        number_key,
        _natural_key(values.get("pin_name")),
        _natural_key(values.get("function_name")),
        record.get("row_id", ""),
    )


def _public_record(
    sheet: str,
    row: dict[str, Any],
    sheet_schema: dict[str, Any],
) -> dict[str, Any]:
    fields = sheet_schema["fields"]
    key_fields = sheet_schema["key"]
    field_evidence = {
        field: _ordered_unique(str(item) for item in evidence_ids)
        for field, evidence_ids in row.get("_field_evidence", {}).items()
        if field in fields
    }
    evidence_ids = _ordered_unique(
        [*(str(item) for item in row.get("_evidence_ids", [])),
         *(item for ids in field_evidence.values() for item in ids)]
    )
    return {
        "row_id": _row_id(sheet, row, key_fields),
        "key": [row.get(field) for field in key_fields],
        "values": {field: row.get(field) for field in fields},
        "evidence_ids": evidence_ids,
        "field_evidence": field_evidence,
        "confidence": row.get("_confidence"),
    }


def _source_issues(extracted: dict[str, Any], review_queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for kind, values in (
        ("conflict", extracted.get("conflicts", [])),
        ("validation", extracted.get("validation_issues", [])),
        ("document_error", extracted.get("errors", [])),
        ("review_queue", review_queue),
    ):
        for value in values:
            issues.append({"kind": kind, "detail": value})
    return issues


def build_review_dataset(
    extracted: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    schema: dict[str, Any],
    *,
    extracted_sha256: str,
    review_queue: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sheets = schema["sheets"]
    data = extracted["data"]
    if set(data) != set(sheets):
        raise ValueError(f"business sheets do not match schema: data={sorted(data)} schema={sorted(sheets)}")

    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in evidence_rows:
        evidence_id = str(evidence.get("evidence_id") or "")
        if not evidence_id:
            raise ValueError("evidence_id must not be empty")
        if evidence_id in evidence_by_id:
            raise ValueError(f"duplicate evidence_id: {evidence_id}")
        evidence_by_id[evidence_id] = evidence

    chip_rows = data["chips"]
    chip_ids = [str(row.get("chip_id") or "") for row in chip_rows]
    if any(not chip_id for chip_id in chip_ids):
        raise ValueError("chips contains an empty chip_id")
    if len(chip_ids) != len(set(chip_ids)):
        raise ValueError("chips contains duplicate chip_id values")

    rows_by_chip: dict[str, dict[str, list[dict[str, Any]]]] = {
        chip_id: {sheet: [] for sheet in sheets} for chip_id in chip_ids
    }
    seen_row_ids: set[str] = set()
    referenced_evidence: set[str] = set()
    for sheet, rows in data.items():
        for row in rows:
            chip_id = str(row.get("chip_id") or "")
            if chip_id not in rows_by_chip:
                raise ValueError(f"{sheet} row refers to unknown chip_id: {chip_id!r}")
            record = _public_record(sheet, row, sheets[sheet])
            if record["row_id"] in seen_row_ids:
                raise ValueError(f"duplicate review row_id: {record['row_id']}")
            seen_row_ids.add(record["row_id"])
            missing = [item for item in record["evidence_ids"] if item not in evidence_by_id]
            if missing:
                raise ValueError(f"{sheet} {record['key']} references missing evidence: {missing}")
            referenced_evidence.update(record["evidence_ids"])
            rows_by_chip[chip_id][sheet].append(record)

    for chip_tables in rows_by_chip.values():
        chip_tables["pin_mux"].sort(key=pin_record_sort_key)

    chip_items: list[dict[str, Any]] = []
    empty_section_count = 0
    for chip_id in chip_ids:
        tables = rows_by_chip[chip_id]
        empty_sections = [sheet for sheet, rows in tables.items() if sheet != "chips" and not rows]
        empty_section_count += len(empty_sections)
        chip_evidence = _ordered_unique(
            evidence_id
            for sheet in sheets
            for record in tables[sheet]
            for evidence_id in record["evidence_ids"]
        )
        documents = _ordered_unique(
            str(record["values"].get("pdf_source_file") or "")
            for sheet in sheets
            for record in tables[sheet]
        )
        chip_items.append({
            "chip_id": chip_id,
            "documents": documents,
            "counts": {sheet: len(tables[sheet]) for sheet in sheets},
            "empty_sections": empty_sections,
            "evidence_ids": chip_evidence,
            "tables": tables,
        })

    evidence_index = []
    for evidence_id in sorted(referenced_evidence):
        evidence = evidence_by_id[evidence_id]
        evidence_index.append({
            "evidence_id": evidence_id,
            "doc_id": evidence.get("doc_id"),
            "source_file": evidence.get("source_file"),
            "source_name": Path(str(evidence.get("source_file") or "")).name,
            "page": evidence.get("page"),
            "bbox": evidence.get("bbox"),
            "kind": evidence.get("kind"),
            "confidence": evidence.get("confidence"),
        })

    source_issues = _source_issues(extracted, review_queue or [])
    return {
        "dataset_version": DATASET_VERSION,
        "source_schema_version": extracted.get("schema_version"),
        "base_extracted_sha256": extracted_sha256,
        "run_id": extracted_sha256[:16],
        "sheet_order": list(sheets),
        "sheet_contracts": {
            sheet: {"fields": value["fields"], "key": value["key"]}
            for sheet, value in sheets.items()
        },
        "summary": {
            "chips": len(chip_items),
            "records": {sheet: len(data[sheet]) for sheet in sheets},
            "referenced_evidence": len(evidence_index),
            "empty_sections": empty_section_count,
            "source_issues": len(source_issues),
        },
        "chips": chip_items,
        "evidence": evidence_index,
        "source_issues": source_issues,
    }


def validate_review_dataset(dataset: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    sheet_order = dataset.get("sheet_order", [])
    chips = dataset.get("chips", [])
    chip_ids = [chip.get("chip_id") for chip in chips]
    if not sheet_order or "chips" not in sheet_order:
        errors.append("sheet_order is missing chips")
    if len(chip_ids) != len(set(chip_ids)):
        errors.append("chip_id values are not unique")
    evidence_items = dataset.get("evidence", [])
    evidence_ids = {item.get("evidence_id") for item in evidence_items}
    if len(evidence_ids) != len(evidence_items):
        errors.append("evidence_id values are empty or not unique")
    for item in evidence_items:
        evidence_id = item.get("evidence_id")
        if not item.get("source_file"):
            errors.append(f"{evidence_id}: source_file is empty")
        if not isinstance(item.get("page"), int) or item["page"] <= 0:
            errors.append(f"{evidence_id}: page must be a positive integer")
        bbox = item.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(value, (int, float)) for value in bbox):
            errors.append(f"{evidence_id}: bbox must contain four numbers")
    observed_counts: dict[str, int] = defaultdict(int)
    row_ids: set[str] = set()
    for chip in chips:
        tables = chip.get("tables", {})
        if set(tables) != set(sheet_order):
            errors.append(f"{chip.get('chip_id')}: table set does not match sheet_order")
            continue
        for sheet, records in tables.items():
            observed_counts[sheet] += len(records)
            contract = dataset.get("sheet_contracts", {}).get(sheet, {})
            expected_fields = contract.get("fields", [])
            key_fields = contract.get("key", [])
            for record in records:
                row_id = record.get("row_id")
                if not row_id or row_id in row_ids:
                    errors.append(f"duplicate or empty row_id: {row_id}")
                row_ids.add(row_id)
                if record.get("values", {}).get("chip_id") != chip.get("chip_id"):
                    errors.append(f"{row_id}: chip ownership mismatch")
                if list(record.get("values", {})) != expected_fields:
                    errors.append(f"{row_id}: value fields do not match the sheet contract")
                expected_key = [record.get("values", {}).get(field) for field in key_fields]
                if record.get("key") != expected_key:
                    errors.append(f"{row_id}: key does not match record values")
                for evidence_id in record.get("evidence_ids", []):
                    if evidence_id not in evidence_ids:
                        errors.append(f"{row_id}: unknown evidence_id {evidence_id}")
                field_ids = {
                    evidence_id
                    for ids in record.get("field_evidence", {}).values()
                    for evidence_id in ids
                }
                if not field_ids.issubset(set(record.get("evidence_ids", []))):
                    errors.append(f"{row_id}: field evidence is absent from row evidence")
        actual_counts = {sheet: len(tables[sheet]) for sheet in sheet_order}
        if chip.get("counts") != actual_counts:
            errors.append(f"{chip.get('chip_id')}: chip counts differ")
    expected_counts = dataset.get("summary", {}).get("records", {})
    if dict(observed_counts) != expected_counts:
        errors.append(f"record counts differ: observed={dict(observed_counts)} expected={expected_counts}")
    if dataset.get("summary", {}).get("chips") != len(chips):
        errors.append("summary chip count differs")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the immutable chip-centered review dataset.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--review-queue", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    extracted_bytes = args.extracted.read_bytes()
    extracted = json.loads(extracted_bytes.decode("utf-8"))
    evidence_rows = _load_jsonl(args.evidence)
    review_queue = _load_jsonl(args.review_queue)
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    dataset = build_review_dataset(
        extracted,
        evidence_rows,
        schema,
        extracted_sha256=hashlib.sha256(extracted_bytes).hexdigest(),
        review_queue=review_queue,
    )
    errors = validate_review_dataset(dataset)
    if errors:
        raise SystemExit("review dataset validation failed:\n- " + "\n- ".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), **dataset["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
