from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_DEFINITION_KEYS = {
    "required_header_terms",
    "required_header_groups",
    "pin_name_aliases",
    "pin_name_alias_groups",
    "pin_name_exclude",
    "function_aliases",
    "default_function_aliases",
    "column_qualifier_preferences",
}
REQUIRED_AF_KEYS = {"title_any", "header_pattern", "minimum_af_columns", "pin_scan_columns"}


def load_pin_recipe(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"pin topology recipe not found: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    topologies = payload.get("topologies", {})
    definition = topologies.get("pin_definition", {})
    af_matrix = topologies.get("af_matrix", {})
    missing_definition = REQUIRED_DEFINITION_KEYS - set(definition)
    missing_af = REQUIRED_AF_KEYS - set(af_matrix)
    if payload.get("topologies", {}).keys() < {"pin_definition", "af_matrix"}:
        raise ValueError("pin topology recipe must define pin_definition and af_matrix")
    if missing_definition or missing_af:
        raise ValueError(
            f"pin topology recipe incomplete: definition={sorted(missing_definition)} af={sorted(missing_af)}"
        )
    return payload
