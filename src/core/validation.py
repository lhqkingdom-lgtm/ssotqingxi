from __future__ import annotations

import re
from typing import Any


def valid_pin_designator(value: Any) -> bool:
    """Accept positive lead numbers and standard BGA ball coordinates."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return float(value).is_integer() and int(value) > 0
    text = str(value).strip().upper()
    if re.fullmatch(r"\d+", text):
        return int(text) > 0
    return re.fullmatch(r"[A-Z]{1,2}[1-9]\d*", text) is not None


def validate(data: dict[str, list[dict[str, Any]]], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply only deterministic hard checks shared by every document family."""
    # Local import avoids a cycle because output_contract reuses the public
    # pin-designator predicate defined in this module.
    from core.output_contract import validate_data_contract

    return validate_data_contract(data, schema)
