from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


VOLATILE_TOP_LEVEL = {"rules_file"}
METADATA_FIELDS = {"pdf_source_file", "evidence_pages", "ingested_by", "ingested_at", "is_reviewed"}
ADDITIVE_INTERNAL_FIELDS = {"_field_evidence"}
ADDITIVE_DOCUMENT_FIELDS = {"status", "sections", "review_items"}
ADDITIVE_TOP_LEVEL_FIELDS = {"review_queue_count"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def source_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in document.items() if key not in ADDITIVE_DOCUMENT_FIELDS}
    result["source_file"] = source_name(result.get("source_file"))
    result["duplicate_paths"] = sorted(source_name(item) for item in result.get("duplicate_paths", []))
    return result


def normalize_evidence(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["source_file"] = source_name(result.get("source_file"))
    return result


def normalized_payload(
    extracted_path: Path,
    evidence_path: Path,
    ignored_fields: set[str] | None = None,
) -> dict[str, Any]:
    ignored_fields = ignored_fields or set()
    payload = load_json(extracted_path)
    normalized = {
        key: value
        for key, value in payload.items()
        if key not in VOLATILE_TOP_LEVEL | ADDITIVE_TOP_LEVEL_FIELDS | {"schema_version"}
    }
    normalized["documents"] = sorted(
        (normalize_document(item) for item in payload.get("documents", [])),
        key=lambda item: item.get("doc_id", ""),
    )
    normalized["data"] = {
        sheet: sorted(
            (
                {
                    key: value
                    for key, value in row.items()
                    if key not in METADATA_FIELDS | ADDITIVE_INTERNAL_FIELDS | ignored_fields
                }
                for row in rows
            ),
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True),
        )
        for sheet, rows in payload.get("data", {}).items()
    }
    evidence = []
    if evidence_path.exists():
        for line in evidence_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                evidence.append(normalize_evidence(json.loads(line)))
    normalized["evidence"] = sorted(evidence, key=lambda item: item.get("evidence_id", ""))
    return normalized


def first_difference(expected: Any, actual: Any, path: str = "root") -> str | None:
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        expected_keys = set(expected)
        actual_keys = set(actual)
        if expected_keys != actual_keys:
            return f"{path}: keys missing={sorted(expected_keys-actual_keys)} extra={sorted(actual_keys-expected_keys)}"
        for key in sorted(expected):
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, f"{path}[{index}]")
            if difference:
                return difference
        return None
    if expected != actual:
        return f"{path}: {expected!r} != {actual!r}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze and compare deterministic extraction results.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--extracted", required=True, type=Path)
    freeze.add_argument("--evidence", required=True, type=Path)
    freeze.add_argument("--output", required=True, type=Path)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--baseline", required=True, type=Path)
    compare.add_argument("--extracted", required=True, type=Path)
    compare.add_argument("--evidence", required=True, type=Path)
    compare.add_argument("--ignore-rule-version", action="store_true", help="Compare business data and evidence while allowing an intentional rule package version change.")
    compare.add_argument(
        "--ignore-field",
        action="append",
        default=[],
        help="Ignore one intentionally added business field during a controlled compatibility comparison.",
    )
    args = parser.parse_args()

    if args.command == "freeze":
        snapshot = normalized_payload(args.extracted, args.evidence)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "frozen", "summary": snapshot.get("summary"), "evidence": len(snapshot["evidence"])}, ensure_ascii=False))
        return

    expected = load_json(args.baseline)
    actual = normalized_payload(args.extracted, args.evidence, set(args.ignore_field))
    expected.pop("schema_version", None)
    actual.pop("schema_version", None)
    if args.ignore_rule_version:
        expected.pop("rule_version", None)
        actual.pop("rule_version", None)
    difference = first_difference(expected, actual)
    if difference:
        print(json.dumps({"status": "FAIL", "first_difference": difference}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps({"status": "PASS", "summary": actual.get("summary"), "evidence": len(actual["evidence"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
