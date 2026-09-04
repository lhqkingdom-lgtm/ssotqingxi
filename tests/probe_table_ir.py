from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.documents import extract_page_texts
from core.table_ir_builder import build_document_ir


def main() -> None:
    parser = argparse.ArgumentParser(description="Build vendor-neutral Table IR without extracting business fields.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--page-limit", type=int, default=45)
    parser.add_argument("--title-contains", default="features and peripheral counts")
    args = parser.parse_args()
    texts = extract_page_texts(args.pdf)
    document = build_document_ir(args.pdf, texts, page_limit=args.page_limit)
    needle = args.title_contains.lower()
    tables = [table for table in document.tables if needle in table.title.lower()]
    report = {
        "source_file": document.source_file,
        "page_count": document.page_count,
        "tables": [
            {
                "table_id": table.table_id,
                "title": table.title,
                "pages": table.pages,
                "continued": table.continued,
                "orientations": table.orientations,
                "cell_count": len(table.cells),
                "located_cells": sum(cell.bbox is not None for cell in table.cells),
                "merged_placeholders": sum(cell.inherited_from is not None for cell in table.cells),
                "first_cells": [
                    {"id": cell.cell_id, "text": cell.raw_text, "page": cell.page, "row": cell.row, "column": cell.column, "bbox": cell.bbox}
                    for cell in table.cells
                    if cell.raw_text
                ][:8],
            }
            for table in tables
        ],
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
