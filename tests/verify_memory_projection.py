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
    if truth.get("memory_groups"):
        return _verify_memory_groups(payload, truth["memory_groups"])
    chip_truth = {
        item["key"]["chip_id"]: item
        for item in truth["records"]
        if item["sheet"] == "chips"
    }
    rows = payload["data"]["memory"]
    by_key = {(row["chip_id"], row["memory_type"]): row for row in rows}
    issues: list[str] = []
    expected_keys = {
        (chip_id, memory_type)
        for chip_id in chip_truth
        for memory_type in ("Flash", "SRAM_Internal")
    }
    actual_keys = set(by_key)
    if actual_keys != expected_keys:
        issues.append(
            f"memory key mismatch missing={sorted(expected_keys - actual_keys)} "
            f"unexpected={sorted(actual_keys - expected_keys)}"
        )
    mapping = {"Flash": "flash_kb", "SRAM_Internal": "ram_kb"}
    for key in sorted(expected_keys & actual_keys):
        chip_id, memory_type = key
        source_field = mapping[memory_type]
        expected = chip_truth[chip_id]["expected"][source_field]
        row = by_key[key]
        if row.get("size_kb") != expected:
            issues.append(f"{key} size_kb expected={expected!r} actual={row.get('size_kb')!r}")
        expected_page = str(chip_truth[chip_id]["evidence_pages"][source_field])
        pages = {item.strip() for item in str(row.get("evidence_pages") or "").split(",")}
        if expected_page not in pages:
            issues.append(f"{key} evidence page {expected_page} not in {sorted(pages)}")
        if row.get("organization") is not None or row.get("features") is not None:
            issues.append(f"{key} invented optional memory attributes")
    return issues


def _verify_memory_groups(
    payload: dict[str, Any], groups: list[dict[str, Any]]
) -> list[str]:
    rows = payload["data"]["memory"]
    expected: dict[tuple[str, str], tuple[int | float, str, str]] = {}
    for group in groups:
        for model in group["models"]:
            for memory_type, size_kb in group["expected"].items():
                expected[(model, memory_type)] = (
                    size_kb,
                    str(group["evidence_pages"][memory_type]),
                    group["source_file_contains"],
                )
    actual = {(row["chip_id"], row["memory_type"]): row for row in rows}
    issues: list[str] = []
    if set(actual) != set(expected):
        issues.append(
            f"memory key mismatch missing={sorted(set(expected) - set(actual))} "
            f"unexpected={sorted(set(actual) - set(expected))}"
        )
    for key in sorted(set(actual) & set(expected)):
        size_kb, evidence_page, source_token = expected[key]
        row = actual[key]
        if row.get("size_kb") != size_kb:
            issues.append(f"{key} size_kb expected={size_kb!r} actual={row.get('size_kb')!r}")
        pages = {item.strip() for item in str(row.get("evidence_pages") or "").split(",")}
        if evidence_page not in pages:
            issues.append(f"{key} evidence page {evidence_page} not in {sorted(pages)}")
        if source_token not in str(row.get("pdf_source_file") or ""):
            issues.append(f"{key} source file does not contain {source_token!r}")
        if row.get("organization") is not None or row.get("features") is not None:
            issues.append(f"{key} invented optional memory attributes")
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify memory projection against independently reviewed chip truth."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    args = parser.parse_args()
    issues = verify(args.output_dir, args.truth)
    if issues:
        print(*issues, sep="\n")
        raise SystemExit(1)
    print("PASS: every expected memory record, value, and evidence page matches manual truth")


if __name__ == "__main__":
    main()
