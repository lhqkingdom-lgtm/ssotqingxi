from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_rows(payload: dict[str, Any], sheet: str, sources: set[str] | None = None) -> list[dict[str, Any]]:
    rows = payload["data"][sheet]
    if sources is not None:
        rows = [row for row in rows if row.get("pdf_source_file") in sources]
    # These are run metadata rather than extraction results. Isolated and mixed
    # runs naturally have different operators/timestamps and must still compare
    # equal on keys, business fields and evidence ownership.
    rows = [
        {key: value for key, value in row.items() if key not in {"ingested_by", "ingested_at"}}
        for row in rows
    ]
    return sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare a mixed-family run with the union of isolated family results.")
    parser.add_argument("--mixed", required=True, type=Path)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--stm32", required=True, type=Path)
    parser.add_argument("--ca-source", required=True)
    parser.add_argument("--stm32-source", required=True)
    args = parser.parse_args()
    mixed, ca, stm32 = load(args.mixed), load(args.ca), load(args.stm32)
    errors = []
    for sheet in mixed["data"]:
        expected = normalized_rows(ca, sheet, {args.ca_source}) + normalized_rows(stm32, sheet, {args.stm32_source})
        expected = sorted(expected, key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
        actual = normalized_rows(mixed, sheet)
        if expected != actual:
            errors.append({"kind": "sheet_union_mismatch", "sheet": sheet, "expected": len(expected), "actual": len(actual)})
    families = sorted(document.get("family_id") for document in mixed.get("documents", []))
    if families != ["ca_miai_v1", "stm32_v1"]:
        errors.append({"kind": "family_assignment", "actual": families})
    for field in ("conflicts", "validation_issues", "errors"):
        if mixed.get(field):
            errors.append({"kind": f"nonempty_{field}", "count": len(mixed[field])})
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "summary": mixed.get("summary"), "families": families, "errors": errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
