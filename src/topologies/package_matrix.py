from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from core.text import clean, compact
from core.qualifiers import preferred_qualifier_indices
from topologies.matrix_observer import table_grids, text_direction_variants


PACKAGE_KIND_ORDER = ("LQFP", "UFQFPN", "TSSOP", "VFQFPN", "UFBGA", "TFBGA", "BGA", "WLCSP")


def _canonical_order(values: list[str], preferred_kinds: Iterable[str] = ()) -> list[str]:
    """Keep pin-map selection deterministic when the output has no package key."""
    preferred = tuple(compact(item).upper() for item in preferred_kinds if compact(item))
    return sorted(
        values,
        key=lambda value: (
            0 if any(value.startswith(kind) for kind in preferred) else 1,
            next(
                (index for index, kind in enumerate(PACKAGE_KIND_ORDER) if value.startswith(kind)),
                len(PACKAGE_KIND_ORDER),
            ),
        ),
    )


def parse_package_names(text: str, package_pattern: str) -> list[str]:
    values: list[str] = []
    values_by_variant: list[list[str]] = []
    dewrapped = re.sub(r"\s*[\r\n]+\s*", "", str(text))
    variants = [dewrapped, clean(text)] if dewrapped != clean(text) else [clean(text)]
    for variant in variants:
        variant_values: list[str] = []
        for match in re.finditer(package_pattern, variant, re.I):
            # Removing a line break can place a physical dimension directly
            # after a valid package count ("LQFP48\n7 mm"). Do not absorb the
            # first dimension digit into the package identity.
            suffix = variant[match.end() :]
            if re.match(r"\s*(?:MM\b|X\s*\d)", suffix, re.I):
                continue
            value = compact(match.group(0)).upper()
            if value not in values:
                values.append(value)
            if value not in variant_values:
                variant_values.append(value)
        values_by_variant.append(variant_values)
    if len(values_by_variant) < 2:
        return values
    dewrapped_values, wrapped_values = values_by_variant
    parsed = {
        value: re.fullmatch(r"([A-Z]+)(\d+)(.*)", value)
        for value in values
    }
    return [
        value
        for value in values
        if not any(
            other != value
            and value in wrapped_values
            and value not in dewrapped_values
            and other in dewrapped_values
            and parsed[value] is not None
            and parsed[other] is not None
            and parsed[value].group(1) == parsed[other].group(1)
            and parsed[other].group(2).startswith(parsed[value].group(2))
            and len(parsed[other].group(2)) > len(parsed[value].group(2))
            for other in values
        )
    ]


def find_package_values(
    document: Any,
    *,
    pin_counts: Iterable[int],
    package_pattern: str,
    package_label_terms: Iterable[str],
    pin_header_terms: Iterable[str],
    preferred_package_kinds: Iterable[str] = (),
    qualifier_preferences: Iterable[str] = (),
    candidate_table_ids: Iterable[str] = (),
) -> tuple[list[str], int | None, tuple[float, ...] | None]:
    expected_counts = tuple(int(item) for item in pin_counts)
    if not expected_counts:
        return [], None, None
    labels = tuple(compact(item).lower() for item in package_label_terms)
    allowed_ids = set(candidate_table_ids)
    for table in document.tables:
        if allowed_ids and table.table_id not in allowed_ids:
            continue
        for fragment, page, rows, _ in table_grids(table):
            for row in rows:
                label = compact(clean(row[0])).lower()
                if not any(term in label for term in labels):
                    continue
                matches: list[str] = []
                for cell in row[1:]:
                    for package in parse_package_names(clean(cell), package_pattern):
                        if any(str(count) in package for count in expected_counts) and package not in matches:
                            matches.append(package)
                if matches:
                    bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                    return _canonical_order(matches, preferred_package_kinds), page, bbox
    normalized_pin_headers = tuple(compact(item).lower() for item in pin_header_terms)
    for table in document.tables:
        if allowed_ids and table.table_id not in allowed_ids:
            continue
        for fragment, page, rows, _ in table_grids(table):
            if not rows:
                continue
            header_text = compact(
                " ".join(
                    variant for row in rows[:2] for cell in row for variant in text_direction_variants(clean(cell))
                )
            ).lower()
            if not any(term in header_text for term in normalized_pin_headers):
                continue
            qualified_matches: list[tuple[str, str]] = []
            for cell in (cell for row in rows[:2] for cell in row):
                for variant in text_direction_variants(clean(cell)):
                    for package in parse_package_names(variant, package_pattern):
                        count_match = re.search(r"(\d+)$", package)
                        if count_match and int(count_match.group(1)) in expected_counts:
                            item = (package, clean(cell))
                            if item not in qualified_matches:
                                qualified_matches.append(item)
            selected = preferred_qualifier_indices(
                [header for _, header in qualified_matches], qualifier_preferences
            )
            matches = list(dict.fromkeys(qualified_matches[index][0] for index in selected))
            if matches:
                bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                return _canonical_order(matches, preferred_package_kinds), page, bbox
    return [], None, None
