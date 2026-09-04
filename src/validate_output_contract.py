from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.output_contract import validate_data_contract, validate_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the fixed five-sheet SSOT output contract.")
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--extracted", type=Path)
    args = parser.parse_args()

    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    issues = validate_schema(schema)
    if args.extracted:
        payload = json.loads(args.extracted.read_text(encoding="utf-8"))
        data = payload.get("data")
        if not isinstance(data, dict):
            issues.append({"level": "error", "message": "extracted payload does not contain a data object"})
        else:
            issues = validate_data_contract(data, schema)
    report = {"status": "PASS" if not issues else "FAIL", "issues": issues}
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(1 if issues else 0)


if __name__ == "__main__":
    main()
