from __future__ import annotations

import re
from typing import Any

from core.text import compact, scalar_or_none


def convert_scalar(number: float, unit_source: object, config: dict[str, Any]) -> int | float | None:
    """Convert an already parsed scalar using a unit found in its source text.

    Unit aliases and factors come exclusively from the shared field dictionary.
    A missing or unknown unit is deliberately not guessed.
    """
    text = compact(unit_source).upper()
    aliases = {compact(key).upper(): compact(val).upper() for key, val in config.get("aliases", {}).items()}
    factors = {compact(key).upper(): float(val) for key, val in config.get("factors", {}).items()}
    matches: list[tuple[int, str]] = []
    for alias, canonical in aliases.items():
        if not alias:
            continue
        if len(alias) == 1 and alias.isascii():
            matched = bool(re.search(rf"\d(?:\.\d+)?{re.escape(alias)}(?![A-Z])", text))
        else:
            matched = alias in text
        if matched:
            matches.append((len(alias), canonical))
    if not matches:
        return None
    unit = max(matches)[1]
    if unit not in factors:
        return None
    return scalar_or_none(float(number) * factors[unit])


def convert_number(value: object, config: dict[str, Any]) -> int | float | None:
    """Convert a number using unit aliases and factors declared by a rule package."""
    text = compact(value).upper()
    match = re.search(r"([-+]?\d+(?:\.\d+)?)([^\d]*)", text)
    if not match:
        return None
    number = float(match.group(1))
    raw_unit = match.group(2).strip()
    converted = convert_scalar(number, f"{match.group(1)}{raw_unit}", config)
    if converted is not None:
        return converted
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


def convert_values(
    value: object,
    config: dict[str, Any],
    fallback_unit_source: object | None = None,
) -> list[int | float]:
    """Convert every scalar, preferring units written beside each value.

    A parent header can provide a fallback unit, but must not override an
    explicit unit in the value cell (for example ``96 K`` below a ``bytes``
    group heading).
    """
    text = compact(value)
    matches = list(re.finditer(r"[-+]?\d+(?:\.\d+)?", text))
    aliases = {compact(key).upper(): compact(val).upper() for key, val in config.get("aliases", {}).items()}
    factors = {compact(key).upper(): float(val) for key, val in config.get("factors", {}).items()}
    upper = text.upper()
    local_units: set[str] = set()
    occupied_spans: list[tuple[int, int]] = []
    # Unit aliases overlap (for example B, BYTE, KBYTE and KBYTES).  Resolve
    # textual occurrences longest-first so a suffix contained in KBYTES does
    # not create a second, contradictory unit.  This is span based rather than
    # a memory-specific special case and therefore also applies to other unit
    # dictionaries with nested aliases.
    for alias, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if not alias:
            continue
        if len(alias) == 1 and alias.isascii():
            occurrences = list(re.finditer(rf"\d(?:\.\d+)?({re.escape(alias)})(?![A-Z])", upper))
            spans = [match.span(1) for match in occurrences]
        else:
            spans = [match.span() for match in re.finditer(re.escape(alias), upper)]
        for span in spans:
            if any(start <= span[0] and span[1] <= end for start, end in occupied_spans):
                continue
            occupied_spans.append(span)
            if canonical in factors:
                local_units.add(canonical)
    if len(local_units) == 1:
        factor = factors[next(iter(local_units))]
        return [scalar_or_none(float(match.group(0)) * factor) for match in matches]

    explicit: list[int | float] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        converted = convert_number(text[match.start():end], config)
        if converted is not None:
            explicit.append(converted)
    if explicit or fallback_unit_source is None:
        return explicit
    fallback: list[int | float] = []
    for match in matches:
        converted = convert_scalar(float(match.group(0)), fallback_unit_source, config)
        if converted is not None:
            fallback.append(converted)
    return fallback
