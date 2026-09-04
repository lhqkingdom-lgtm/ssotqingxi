from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


def source_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def matches(row: dict[str, Any], key: dict[str, Any]) -> bool:
    return all(row.get(field) == value for field, value in key.items())


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify selected field-to-PDF evidence ownership.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    parser.add_argument("--set", required=True, dest="expectation_set")
    parser.add_argument("--review-html", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.extracted.read_text(encoding="utf-8"))
    evidence = {
        item["evidence_id"]: item
        for line in args.evidence.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for item in [json.loads(line)]
    }
    cases = json.loads(args.expectations.read_text(encoding="utf-8"))[args.expectation_set]
    errors = []
    reviewed_ids: set[str] = set()
    for case in cases:
        rows = [row for row in payload["data"][case["sheet"]] if matches(row, case["key"])]
        if len(rows) != 1:
            errors.append({"kind": "row_match", "case": case, "actual": len(rows)})
            continue
        ids = rows[0].get("_field_evidence", {}).get(case["field"], [])
        reviewed_ids.update(ids)
        actual = sorted({
            (source_name(evidence[item_id]["source_file"]), int(evidence[item_id]["page"]))
            for item_id in ids
            if item_id in evidence
        })
        expected = sorted((case["source_file"], int(page)) for page in case["pages"])
        if actual != expected:
            errors.append({"kind": "field_source_page", "case": case, "expected": expected, "actual": actual})

    if args.review_html:
        html = args.review_html.read_text(encoding="utf-8")
        match = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S)
        if not match:
            errors.append({"kind": "review_payload_missing"})
        else:
            review = json.loads(match.group(1))
            image_ids = {image["evidence_id"] for card in review["cards"] for image in card["images"]}
            missing = sorted(reviewed_ids - image_ids)
            if missing:
                errors.append({"kind": "review_missing_field_images", "ids": missing})
            if "showFieldEvidence" not in html or "_field_pages" not in html:
                errors.append({"kind": "review_field_navigation_missing"})
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "cases": len(cases), "errors": errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
