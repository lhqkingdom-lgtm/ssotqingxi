from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(output_dir: Path, truth_path: Path) -> list[str]:
    payload = _read(output_dir / "extracted.json")
    truth = _read(truth_path)
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for group in truth["electrical_groups"]:
        for model in group["models"]:
            for param_name, values in group["expected"].items():
                expected[(model, param_name)] = {
                    **values,
                    "source_file_contains": group["source_file_contains"],
                }
    actual = {
        (row["chip_id"], row["param_name"]): row
        for row in payload["data"]["electrical"]
    }
    issues: list[str] = []
    if set(actual) != set(expected):
        issues.append(
            f"electrical key mismatch missing={sorted(set(expected) - set(actual))} "
            f"unexpected={sorted(set(actual) - set(expected))}"
        )
    for key in sorted(set(actual) & set(expected)):
        row, target = actual[key], expected[key]
        for field in ("min_value", "max_value", "unit"):
            if row.get(field) != target[field]:
                issues.append(
                    f"{key} {field} expected={target[field]!r} actual={row.get(field)!r}"
                )
        pages = {item.strip() for item in str(row.get("evidence_pages") or "").split(",")}
        if str(target["evidence_page"]) not in pages:
            issues.append(
                f"{key} evidence page {target['evidence_page']} not in {sorted(pages)}"
            )
        if target["source_file_contains"] not in str(row.get("pdf_source_file") or ""):
            issues.append(
                f"{key} source file does not contain {target['source_file_contains']!r}"
            )
        if row.get("conditions") is not None:
            issues.append(f"{key} invented electrical conditions")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify electrical projection against independently reviewed PDF truth."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    args = parser.parse_args()
    issues = verify(args.output_dir, args.truth)
    if issues:
        print(*issues, sep="\n")
        raise SystemExit(1)
    print("PASS: every expected electrical record, value, unit, and evidence page matches manual truth")


if __name__ == "__main__":
    main()
