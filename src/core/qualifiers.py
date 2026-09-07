from __future__ import annotations

import re
from collections.abc import Iterable

from core.text import compact


def has_qualifier(value: str, qualifier: str) -> bool:
    """Match a header qualifier without treating a one-letter code as prose."""
    normalized = compact(value).lower()
    token = compact(qualifier).lower()
    if not token or token == "unqualified":
        return False
    variants = (normalized, normalized[::-1])
    if len(token) <= 2:
        return any(
            re.search(rf"(?:^|[_/\-]){re.escape(token)}(?:$|[_/\-])", item) is not None
            for item in variants
        )
    return any(token in item for item in variants)


def preferred_qualifier_indices(values: Iterable[str], preferences: Iterable[str]) -> list[int]:
    materialized = list(values)
    ordered = [compact(item).lower() for item in preferences if compact(item)]
    known = [item for item in ordered if item != "unqualified"]
    for preference in ordered:
        if preference == "unqualified":
            matches = [
                index for index, value in enumerate(materialized)
                if not any(has_qualifier(value, item) for item in known)
            ]
        else:
            matches = [
                index for index, value in enumerate(materialized)
                if has_qualifier(value, preference)
            ]
        if matches:
            return matches
    return list(range(len(materialized)))
