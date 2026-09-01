from __future__ import annotations

import re
from typing import Any

from core.text import compact, scalar_or_none


def convert_number(value: object, config: dict[str, Any]) -> int | float | None:
    """Convert a number using unit aliases and factors declared by a rule package."""
    text = compact(value).upper()
    match = re.search(r"([-+]?\d+(?:\.\d+)?)([^\d]*)", text)
    if not match:
        return None
    number = float(match.group(1))
    raw_unit = match.group(2).strip()
    aliases = {compact(key).upper(): compact(val).upper() for key, val in config.get("aliases", {}).items()}
    unit = aliases.get(raw_unit, raw_unit)
    if not unit:
        implicit = config.get("implicit", {})
        threshold = implicit.get("threshold")
        if threshold is not None:
            unit = implicit.get("large_unit") if number >= float(threshold) else implicit.get("small_unit")
        else:
            unit = config.get("default_unit", "")
    factors = {compact(key).upper(): float(val) for key, val in config.get("factors", {}).items()}
    if unit not in factors:
        return None
    return scalar_or_none(number * factors[unit])
