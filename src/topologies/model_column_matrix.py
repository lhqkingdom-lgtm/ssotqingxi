from __future__ import annotations

import re
from typing import Any

from core.text import compact
from topologies.matrix_observer import HeaderPredicate, structural_column_headers, text_direction_variants


def numbers(value: object) -> list[float]:
    return [float(item) for item in re.findall(r"\d+(?:\.\d+)?", str(value or ""))]


def wildcard_match(
    header: str,
    target: str,
    wildcard_markers: tuple[str, ...] = ("X",),
) -> bool:
    if not header or not target:
        return False
    expression = re.escape(compact(header).upper())
    for marker in wildcard_markers:
        normalized = compact(marker).upper()
        if normalized:
            expression = expression.replace(re.escape(normalized), "[A-Z0-9]")
    return bool(re.fullmatch(expression, target, re.I))


def intersect_model_patterns(
    left: str,
    right: str,
    wildcard_markers: tuple[str, ...] = ("X",),
) -> str | None:
    """Return the most specific compatible model pattern, without guessing.

    A wildcard is resolved only when the other source supplies a concrete
    character.  If both sources are wildcard at a position, the result stays
    wildcard and therefore cannot be mistaken for a concrete model.
    """
    first = compact(left).upper()
    second = compact(right).upper()
    if not first or not second:
        return None
    # A feature-table header may show the full ordering template while the
    # document scope names the shorter base sales type. Intersect only their
    # shared prefix; callers still require a concrete model accepted by the
    # profile and its declared ordering relations.
    if len(first) < len(second):
        return None
    if len(first) > len(second):
        first = first[: len(second)]
    markers = {compact(item).upper() for item in wildcard_markers if compact(item)}
    result: list[str] = []
    for first_char, second_char in zip(first, second):
        first_wildcard = first_char in markers
        second_wildcard = second_char in markers
        if first_char == second_char:
            result.append(first_char)
        elif first_wildcard and not second_wildcard:
            result.append(second_char)
        elif second_wildcard and not first_wildcard:
            result.append(first_char)
        else:
            return None
    return "".join(result)


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
