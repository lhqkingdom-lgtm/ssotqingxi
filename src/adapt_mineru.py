from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.table_ir_validation import build_evidence_manifest, validate_special_structures
from input_adapters.mineru import load_mineru_document


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert a MinerU layout directory into the shared Table IR.")
    parser.add_argument("--layout", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    document = load_mineru_document(args.layout)
    report = validate_special_structures(document)
    evidence_manifest = build_evidence_manifest(document, report)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "mineru_table_ir.json").open("w", encoding="utf-8") as stream:
        json.dump(document.to_dict(), stream, ensure_ascii=False, indent=2)
    with (args.output_dir / "mineru_adapter_report.json").open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    with (args.output_dir / "mineru_evidence_manifest.json").open("w", encoding="utf-8") as stream:
        json.dump(evidence_manifest, stream, ensure_ascii=False, indent=2)
    print(json.dumps({
        "status": "PASS" if not report["hard_errors"] else "FAIL",
        "pages": document.page_count,
        "tables": len(document.tables),
        "hard_errors": len(report["hard_errors"]),
        "special_structures": {key: len(value) for key, value in report["special_structures"].items()},
    }, ensure_ascii=False))
    if report["hard_errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
