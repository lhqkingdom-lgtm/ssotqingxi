from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.collector import Collector  # noqa: E402
from core.documents import choose_pdfs, extract_page_texts  # noqa: E402
from core.pipeline import load_family  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject an STM32 AF failure and verify section-level retention.")
    parser.add_argument("--input", required=True, type=Path, help="Folder containing representative PDFs")
    parser.add_argument("--filename", required=True, help="Representative PDF file name")
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args()

    docs = choose_pdfs(args.input)
    matches = [doc for doc in docs if doc["path"].name.casefold() == args.filename.casefold()]
    if len(matches) != 1:
        raise SystemExit(f"expected one PDF named {args.filename!r}, got {len(matches)}")
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    family = load_family(args.profile)
    collector = Collector(schema)
    doc = matches[0]
    doc["page_texts"] = extract_page_texts(doc["path"])

    with patch.object(family.extractor, "unique_pin_af_lookup", side_effect=ValueError("injected AF failure")):
        record, evidences = family.extractor.process_document(doc, collector, family.aliases, family.profile)

    counts = {sheet: len(rows) for sheet, rows in collector.data.items()}
    failures = []
    if record.get("status") != "partial":
        failures.append(f"document status={record.get('status')!r}")
    if record.get("sections", {}).get("pin_af") != "failed":
        failures.append(f"pin_af status={record.get('sections', {}).get('pin_af')!r}")
    if record.get("sections", {}).get("pin_mux") != "partial":
        failures.append(f"pin_mux status={record.get('sections', {}).get('pin_mux')!r}")
    for sheet in ("chips", "memory", "peripherals", "electrical", "pin_mux"):
        if counts.get(sheet, 0) <= 0:
            failures.append(f"{sheet} was not retained")
    if not any(item.get("section") == "pin_af" and item.get("status") == "failed" for item in record.get("review_items", [])):
        failures.append("failed AF section was not queued for review")

    result = {
        "status": "FAIL" if failures else "PASS",
        "document_status": record.get("status"),
        "sections": record.get("sections"),
        "rows": counts,
        "evidence": len(evidences),
        "failures": failures,
    }
    print(json.dumps(result, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
