from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


BASE_KEY_FIELDS = ("chip_id", "pin_name", "pin_number", "function_name")
VALUE_FIELDS = ("is_supported", "af_code", "default_function", "constraints")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(output_dir: Path, truth_path: Path) -> list[str]:
    payload = _read(output_dir / "extracted.json")
    truth = _read(truth_path)
    expected_records = [*truth["expected"], *truth.get("forbidden", [])]
    key_fields = (
        ("chip_id", "package_type", "pin_name", "pin_number", "function_name")
        if any("package_type" in row for row in expected_records)
        else BASE_KEY_FIELDS
    )
    actual = {
        tuple(row.get(field) for field in key_fields): row
        for row in payload["data"]["pin_mux"]
    }
    issues: list[str] = []
    for expected in truth["expected"]:
        key = tuple(expected.get(field) for field in key_fields)
        row = actual.get(key)
        if row is None:
            issues.append(f"missing pin record {key}")
            continue
        for field in VALUE_FIELDS:
            if field in expected and row.get(field) != expected[field]:
                issues.append(
                    f"{key} {field} expected={expected[field]!r} actual={row.get(field)!r}"
                )
        pages = {part.strip() for part in str(row.get("evidence_pages") or "").split(",")}
        for page in expected.get("evidence_pages", []):
            if str(page) not in pages:
                issues.append(f"{key} evidence page {page} not in {sorted(pages)}")
        source = str(row.get("pdf_source_file") or "")
        if expected.get("source_file_contains") not in source:
            issues.append(f"{key} source mismatch: {source!r}")
    for forbidden in truth.get("forbidden", []):
        key = tuple(forbidden.get(field) for field in key_fields)
        if key in actual:
            issues.append(f"forbidden pin record present {key}")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify pin projection against manually reviewed PDF truth.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    args = parser.parse_args()
    issues = verify(args.output_dir, args.truth)
    if issues:
        print(*issues, sep="\n")
        raise SystemExit(1)
    print("PASS: every manually certified pin key, value, source, and evidence page matches")


if __name__ == "__main__":
    main()
