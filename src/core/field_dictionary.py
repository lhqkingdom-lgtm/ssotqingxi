from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable

from core.text import compact


REQUIRED_FIELDS = {"flash_kb", "ram_kb", "max_freq_mhz"}
REQUIRED_UNITS = {"memory_kb", "frequency_mhz"}


def load_field_dictionary(
    path: Path,
    family_aliases: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Load the shared dictionary and add only family-specific aliases."""
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"common field dictionary not found: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    missing_fields = REQUIRED_FIELDS - set(payload.get("fields", {}))
    missing_units = REQUIRED_UNITS - set(payload.get("units", {}))
    if missing_fields or missing_units:
        raise ValueError(
            f"common field dictionary incomplete: fields={sorted(missing_fields)} units={sorted(missing_units)}"
        )
    result = copy.deepcopy(payload)
    for field_name, aliases in (family_aliases or {}).items():
        if field_name not in result["fields"]:
            raise ValueError(f"family alias targets unknown common field: {field_name}")
        existing = result["fields"][field_name].setdefault("aliases", [])
        existing.extend(alias for alias in aliases if alias not in existing)
    return result


def match_common_field(label: str, dictionary: dict[str, Any]) -> str | None:
    normalized = compact(label).casefold()
    matches: list[tuple[int, str]] = []
    for field_name, rule in dictionary["fields"].items():
        exact_aliases = [compact(item).casefold() for item in rule.get("exact_aliases", [])]
        if normalized in exact_aliases:
            matches.append((len(normalized) + 1000, field_name))
            continue
        aliases = [compact(item).casefold() for item in rule.get("aliases", [])]
        conjunctions = [
            [compact(item).casefold() for item in group]
            for group in rule.get("aliases_all", [])
        ]
        excludes = [compact(item).casefold() for item in rule.get("exclude_aliases", [])]
        if any(item in normalized for item in excludes):
            continue
        scores = [len(item) for item in aliases if item in normalized]
        scores.extend(
            sum(len(item) for item in group)
            for group in conjunctions
            if group and all(item in normalized for item in group)
        )
        if scores:
            matches.append((max(scores), field_name))
    return max(matches)[1] if matches else None


def apply_dictionary_to_recipe(
    recipe: dict[str, Any],
    dictionary: dict[str, Any],
    field_names: Iterable[str] = REQUIRED_FIELDS,
) -> dict[str, Any]:
    """Inject shared aliases and units into a topology recipe without fallback."""
    result = copy.deepcopy(recipe)
    result["units"] = copy.deepcopy(dictionary["units"])
    for field_name in field_names:
        if field_name not in result.get("fields", {}):
            raise ValueError(f"topology recipe missing common field: {field_name}")
        common = dictionary["fields"][field_name]
        rule = result["fields"][field_name]
        rule["rows"] = list(common.get("aliases", []))
        rule["rows_all"] = copy.deepcopy(common.get("aliases_all", []))
        rule["exclude_rows"] = list(common.get("exclude_aliases", []))
        rule["unit"] = common["unit"]
    return result


def load_table_recipe(path: Path, family_aliases: dict[str, list[str]] | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    recipe = json.loads(resolved.read_text(encoding="utf-8"))
    dictionary_ref = recipe.get("field_dictionary_file")
    if not dictionary_ref:
        raise ValueError(f"table recipe has no field_dictionary_file: {resolved}")
    dictionary = load_field_dictionary(resolved.parent / dictionary_ref, family_aliases)
    return apply_dictionary_to_recipe(recipe, dictionary)
