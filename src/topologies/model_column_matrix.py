from __future__ import annotations

import re
from typing import Any

from core.text import compact
from topologies.matrix_observer import HeaderPredicate, structural_column_headers, text_direction_variants


def numbers(value: object) -> list[float]:
    return [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]


def wildcard_match(header: str, target: str) -> bool:
    if not header:
        return False
    expression = re.escape(header).replace("X", "[A-Z0-9]")
    return bool(re.fullmatch(expression, target, re.I))


def select_exact_or_wildcard_column(
    rows: list[list[str]],
    target_entity: str,
    matrix_config: dict[str, Any],
    header_predicate: HeaderPredicate,
) -> int:
    del matrix_config
    target = compact(target_entity).upper()
    headers = structural_column_headers(rows, header_predicate)
    direct = [
        column
        for column, header in headers.items()
        if any(target == variant or target in variant for variant in text_direction_variants(header))
    ]
    if direct:
        return direct[0]
    wildcard = [
        column
        for column, header in headers.items()
        if any(wildcard_match(variant, target) for variant in text_direction_variants(header))
    ]
    if wildcard:
        return wildcard[0]
    raise ValueError(f"cannot select target column for {target_entity}")
