from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from core.documents import extract_page_texts
from family_rules.stm32_matrix import select_table_ir
from core.recipe_executor import reduce_values
from core.table_ir_builder import build_document_ir


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage-2 probe: locate and map one STM32 feature matrix without producing SSOT rows.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    profile = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
    recipe_path = (ROOT / "config" / "profiles" / profile["table_presets"]["feature_matrix"]).resolve()
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    texts = extract_page_texts(args.pdf)
    document = build_document_ir(args.pdf, texts, page_limit=int(profile["stm32"]["feature_page_limit"]))
    selected, mapped = select_table_ir(
        list(document.tables),
        args.model.upper(),
        recipe,
        profile["stm32"],
    )
    report = {
        "file": args.pdf.name,
        "model": args.model.upper(),
        "table_group": selected.table_id,
        "pages": selected.pages,
        "rotation": selected.orientations,
        "mapped": [item.__dict__ for item in mapped],
        "reduced": reduce_values(mapped, recipe, package_pattern=profile["patterns"]["package"]),
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
