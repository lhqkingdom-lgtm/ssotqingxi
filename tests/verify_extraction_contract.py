from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify extraction keys, evidence ownership and hard-result contract.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()

    payload = load_json(args.extracted)
    schema = load_json(args.schema)
    evidences = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence_by_id = {item["evidence_id"]: item for item in evidences}
    document_ids = {item["doc_id"] for item in payload.get("documents", [])}
    errors: list[dict[str, Any]] = []

    if len(evidence_by_id) != len(evidences):
        errors.append({"kind": "duplicate_evidence_id"})
    for evidence in evidences:
        if evidence.get("doc_id") not in document_ids:
            errors.append({"kind": "orphan_evidence", "evidence_id": evidence.get("evidence_id")})

    for sheet, config in schema["sheets"].items():
        primary_key = config["key"]
        seen: set[tuple[Any, ...]] = set()
        for index, row in enumerate(payload.get("data", {}).get(sheet, []), start=1):
            key = tuple(row.get(field) for field in primary_key)
            if any(value in (None, "") for value in key):
                errors.append({"kind": "blank_primary_key", "sheet": sheet, "row": index, "key": key})
            if key in seen:
                errors.append({"kind": "duplicate_primary_key", "sheet": sheet, "row": index, "key": key})
            seen.add(key)
            evidence_ids = row.get("_evidence_ids") or []
            if not evidence_ids:
                errors.append({"kind": "row_without_evidence", "sheet": sheet, "row": index, "key": key})
            missing = [evidence_id for evidence_id in evidence_ids if evidence_id not in evidence_by_id]
            if missing:
                errors.append({"kind": "missing_evidence_reference", "sheet": sheet, "row": index, "key": key, "ids": missing})
            field_evidence = row.get("_field_evidence") or {}
            for field in config["fields"]:
                if field in {"pdf_source_file", "evidence_pages", "ingested_by", "ingested_at", "is_reviewed"}:
                    continue
                if row.get(field) in (None, "", []):
                    continue
                field_ids = field_evidence.get(field) or []
                if not field_ids:
                    errors.append({"kind": "field_without_evidence", "sheet": sheet, "row": index, "key": key, "field": field})
                missing_field_ids = [evidence_id for evidence_id in field_ids if evidence_id not in evidence_by_id]
                if missing_field_ids:
                    errors.append({"kind": "missing_field_evidence", "sheet": sheet, "row": index, "key": key, "field": field, "ids": missing_field_ids})

    if args.require_clean:
        for field in ("conflicts", "validation_issues", "errors"):
            if payload.get(field):
                errors.append({"kind": f"nonempty_{field}", "count": len(payload[field])})

    report = {
        "status": "PASS" if not errors else "FAIL",
        "sheets": {sheet: len(payload.get("data", {}).get(sheet, [])) for sheet in schema["sheets"]},
        "documents": len(payload.get("documents", [])),
        "evidence": len(evidences),
        "conflicts": len(payload.get("conflicts", [])),
        "validation_issues": len(payload.get("validation_issues", [])),
        "extraction_errors": len(payload.get("errors", [])),
        "contract_errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
