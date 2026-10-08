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
    # Preserve whitespace while locating numbers so prose boundaries (for
    # example "up to 48 MHz") remain distinguishable from identifiers such
    # as "QZ575". Individual conversion helpers still compact their slice.
    text = str(value or "")
    # A hyphen between two positive alternatives is a separator, not a unary
    # minus. Preserve a leading/sign-delimited minus for genuinely signed data.
    text = re.sub(r"(?<=[A-Za-z0-9)])\s*-\s*(?=\d)", " ", text)
    aliases = {compact(key).upper(): compact(val).upper() for key, val in config.get("aliases", {}).items()}
    factors = {compact(key).upper(): float(val) for key, val in config.get("factors", {}).items()}
    multiplied: list[tuple[int, int | float]] = []
    multiplied_spans: list[tuple[int, int]] = []
    if config.get("allow_count_multiplier"):
        unit_alias_pattern = "|".join(
            re.escape(alias) for alias in sorted(aliases, key=len, reverse=True) if alias
        )
        for match in re.finditer(
            rf"(\d+(?:\.\d+)?)\s*[X×]\s*(\d+(?:\.\d+)?)\s*({unit_alias_pattern})",
            text,
            re.I,
        ):
            quantity = convert_number(f"{match.group(2)}{match.group(3)}", config)
            if quantity is None:
                continue
            multiplied.append(
                (match.start(), scalar_or_none(float(match.group(1)) * float(quantity)))
            )
            multiplied_spans.append(match.span())
    upper = text.upper()
    def embedded_identifier_number(match: re.Match[str]) -> bool:
        before = upper[match.start() - 1] if match.start() else ""
        if before.isascii() and before.isalpha():
            return True
        after = upper[match.end() :]
        if after and after[0].isascii() and after[0].isalpha():
            return not any(after.startswith(alias) for alias in aliases if alias)
        return False

    matches = [
        match
        for match in re.finditer(r"[-+]?\d+(?:\.\d+)?", text)
        if not any(start <= match.start() and match.end() <= end for start, end in multiplied_spans)
        and not embedded_identifier_number(match)
    ]
    local_units: set[str] = set()
    occupied_spans: list[tuple[int, int]] = []
    # Unit aliases overlap (for example B, BYTE, KBYTE and KBYTES).  Resolve
    # textual occurrences longest-first so a suffix contained in KBYTES does
    # not create a second, contradictory unit.  This is span based rather than
    # a memory-specific special case and therefore also applies to other unit
    # dictionaries with nested aliases.
    compact_upper = compact(text).upper()
    for alias, canonical in sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True):
        if not alias:
            continue
        if len(alias) == 1 and alias.isascii():
            occurrences = list(re.finditer(rf"\d(?:\.\d+)?({re.escape(alias)})(?![A-Z])", compact_upper))
            spans = [match.span(1) for match in occurrences]
        else:
            spans = [match.span() for match in re.finditer(re.escape(alias), compact_upper)]
        for span in spans:
            if any(start <= span[0] and span[1] <= end for start, end in occupied_spans):
                continue
            occupied_spans.append(span)
            if canonical in factors:
                local_units.add(canonical)
    if len(local_units) == 1:
        factor = factors[next(iter(local_units))]
        ordinary = [
            (match.start(), scalar_or_none(float(match.group(0)) * factor))
            for match in matches
        ]
        return [value for _, value in sorted([*multiplied, *ordinary])]

    explicit: list[int | float] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        converted = convert_number(text[match.start():end], config)
        if converted is not None:
            explicit.append(converted)
    if explicit or fallback_unit_source is None:
        return [value for _, value in multiplied] + explicit
    fallback: list[int | float] = []
    for match in matches:
        converted = convert_scalar(float(match.group(0)), fallback_unit_source, config)
        if converted is not None:
            fallback.append(converted)
    return [value for _, value in multiplied] + fallback
