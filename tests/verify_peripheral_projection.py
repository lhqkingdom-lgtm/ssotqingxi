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
    for group in truth["groups"]:
        for model in group["models"]:
            for peripheral_type, values in group["expected"].items():
                evidence_page = group.get("evidence_pages", {}).get(
                    peripheral_type, group.get("evidence_page")
                )
                expected[(model, peripheral_type)] = {
                    **values,
                    "evidence_page": str(evidence_page),
                    "source_file_contains": group["source_file_contains"],
                }

    actual = {
        (row["chip_id"], row["peripheral_type"]): row
        for row in payload["data"]["peripherals"]
    }
    issues: list[str] = []
    if set(actual) != set(expected):
        issues.append(
            f"peripheral key mismatch missing={sorted(set(expected) - set(actual))} "
            f"unexpected={sorted(set(actual) - set(expected))}"
        )
    for key in sorted(set(actual) & set(expected)):
        row = actual[key]
        target = expected[key]
        for field in ("instance_count", "channel_count"):
            expected_value = target.get(field)
            if row.get(field) != expected_value:
                issues.append(
                    f"{key} {field} expected={expected_value!r} actual={row.get(field)!r}"
                )
        pages = {item.strip() for item in str(row.get("evidence_pages") or "").split(",")}
        if target["evidence_page"] not in pages:
            issues.append(
                f"{key} evidence page {target['evidence_page']} not in {sorted(pages)}"
            )
        if target["source_file_contains"] not in str(row.get("pdf_source_file") or ""):
            issues.append(
                f"{key} source file does not contain {target['source_file_contains']!r}"
            )
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify peripheral projection against manually reviewed source truth."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    args = parser.parse_args()
    issues = verify(args.output_dir, args.truth)
    if issues:
        print(*issues, sep="\n")
        raise SystemExit(1)
    print("PASS: every expected peripheral record, value, source, and evidence page matches manual truth")


if __name__ == "__main__":
    main()
