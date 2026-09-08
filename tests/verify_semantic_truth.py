from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def verify(output_dir: Path, truth_path: Path) -> list[str]:
    payload = load_json(output_dir / "extracted.json")
    truth = load_json(truth_path)
    evidence = {
        item["evidence_id"]: item
        for line in (output_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
    }
    errors: list[str] = []
    for expected_record in truth["records"]:
        sheet = expected_record["sheet"]
        key = expected_record["key"]
        matches = [
            row
            for row in payload["data"].get(sheet, [])
            if all(row.get(name) == value for name, value in key.items())
        ]
        label = f"{sheet}:{key}"
        if len(matches) != 1:
            errors.append(f"{label} expected one row, found {len(matches)}")
            continue
        row = matches[0]
        if expected_record.get("source_file_contains") not in row.get("pdf_source_file", ""):
            errors.append(f"{label} source document attribution is wrong")
        for field, expected in expected_record["expected"].items():
            actual = row.get(field)
            if actual != expected:
                errors.append(f"{label}.{field}: expected {expected!r}, got {actual!r}")
                continue
            ids = row.get("_field_evidence", {}).get(field, [])
            pages = {evidence[item]["page"] for item in ids if item in evidence}
            expected_page = expected_record.get("evidence_pages", {}).get(
                field, expected_record.get("evidence_page")
            )
            if expected_page is None:
                errors.append(f"{label}.{field}: manual truth has no evidence page")
            elif expected_page not in pages:
                errors.append(
                    f"{label}.{field}: value matched but evidence page "
                    f"{expected_page} was absent; pages={sorted(pages)}"
                )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare extraction values and evidence to manual PDF truth.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    args = parser.parse_args()
    errors = verify(args.output_dir, args.truth)
    print(json.dumps({"status": "FAIL" if errors else "PASS", "errors": errors}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
