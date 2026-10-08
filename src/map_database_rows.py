from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.database_mapping import (
    database_workbook_payload,
    load_database_mapping,
    map_payload_to_database,
)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Map legacy SSOT extraction rows to cpdb-test column names.")
    parser.add_argument("--input", required=True, type=Path, help="Extracted JSON produced by src/extract.py")
    parser.add_argument("--output", required=True, type=Path, help="Database-shaped JSON output")
    parser.add_argument(
        "--workbook-output",
        type=Path,
        default=None,
        help="Optional database-shaped rows under the stable five Excel tab names",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=project_root / "config" / "database_mapping.json",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="Required when any source row has is_reviewed=true; written to reviewer",
    )
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    mapping = load_database_mapping(args.mapping)
    mapped = map_payload_to_database(payload, mapping, reviewer=args.reviewer)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(mapped, ensure_ascii=False, indent=2), encoding="utf-8")
    workbook_output = None
    if args.workbook_output is not None:
        workbook = database_workbook_payload(mapped, mapping)
        args.workbook_output.parent.mkdir(parents=True, exist_ok=True)
        args.workbook_output.write_text(
            json.dumps(workbook, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        workbook_output = str(args.workbook_output)
    print(json.dumps({
        "output": str(args.output),
        "workbook_output": workbook_output,
        "summary": mapped["summary"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
