from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from core.documents import extract_page_texts
from family_rules.stm32_matrix import select_observations
from core.field_dictionary import load_table_recipe
from core.field_rules import observations_to_candidates
from core.candidate_executor import candidate_to_mapped
from core.profile import load_profile
from core.recipe_executor import reduce_values
from core.table_ir_builder import build_document_ir
from core.table_index import build_table_index
from family_extractors.stm32 import feature_candidate_tables, source_ordering_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage-2 probe: locate and map one STM32 feature matrix without producing SSOT rows.")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()

    profile = load_profile(ROOT / "config" / "profiles" / "stm32_v1.json")
    recipe = load_table_recipe(profile.topology_preset("feature_matrix"), profile.field_aliases)
    texts = extract_page_texts(args.pdf)
    document = build_document_ir(args.pdf, texts)
    table_index = build_table_index(document)
    selected, observations = select_observations(
        feature_candidate_tables(document, table_index), args.model.upper(), recipe,
        source_ordering_config(profile, texts),
    )
    mapped = [candidate_to_mapped(item) for item in observations_to_candidates(observations, recipe, entity_type="chip")]
    report = {
        "file": args.pdf.name,
        "model": args.model.upper(),
        "table_group": selected.table_id,
        "pages": selected.pages,
        "rotation": selected.orientations,
        "mapped": [item.__dict__ for item in mapped],
        "reduced": reduce_values(mapped, recipe, package_pattern=profile.package_re.pattern),
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
