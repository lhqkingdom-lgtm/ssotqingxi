from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.documents import extract_page_texts
from core.candidate_executor import reduce_candidates, validate_hard_rules
from core.field_rules import observations_to_candidates
from core.table_ir_builder import build_document_ir
from family_rules.stm32_matrix import select_observations


def main() -> None:
    parser = argparse.ArgumentParser(description="Build FieldCandidate IR from vendor-neutral Table IR observations.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    profile = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
    recipe_path = (ROOT / "config" / "profiles" / profile["table_presets"]["feature_matrix"]).resolve()
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    texts = extract_page_texts(args.pdf)
    document = build_document_ir(args.pdf, texts, page_limit=int(profile["stm32"]["feature_page_limit"]))
    table, observations = select_observations(list(document.tables), args.model.upper(), recipe, profile["stm32"])
    candidates = observations_to_candidates(observations, recipe, entity_type="chip")
    reduced, aggregates = reduce_candidates(candidates, recipe, package_pattern=profile["patterns"]["package"])
    issues = validate_hard_rules(candidates, aggregates, recipe)
    report = {
        "file": args.pdf.name,
        "model": args.model.upper(),
        "table_id": table.table_id,
        "pages": table.pages,
        "observation_count": len(observations),
        "candidate_count": len(candidates),
        "fields": sorted({f"{item.field_group}:{item.field_name}" for item in candidates}),
        "all_have_cell_evidence": all(item.source_cell_ids and item.bboxes for item in candidates),
        "reduced": reduced,
        "aggregates": [item.__dict__ for item in aggregates],
        "hard_issues": [item.__dict__ for item in issues],
        "candidates": [item.__dict__ for item in candidates],
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
