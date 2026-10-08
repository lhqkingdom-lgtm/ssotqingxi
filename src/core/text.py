from __future__ import annotations

import math
import re


NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).replace("²", "2").replace("‐", "-").replace("–", "-").replace("—", "-")


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def parse_number(value: str) -> float | None:
    match = NUMBER_RE.search(compact(value).replace(",", ""))
    return float(match.group(0)) if match else None


def parse_size_kb(value: str) -> float | None:
    text = compact(value).upper().replace("字节", "BYTE")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(K|KB|KBYTE|BYTE|B|M|MB)?", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or ""
    if unit in {"M", "MB"}:
        return number * 1024
    if unit in {"BYTE", "B"} or (not unit and number >= 128):
        return number / 1024
    return number


def parse_ranges(value: str) -> list[tuple[float, float]]:
    text = compact(value).replace("至", "-").replace("～", "-").replace("~", "-")
    direct = [
        (match.start(), (float(match.group(1)), float(match.group(2))))
        for match in re.finditer(
        r"(-?\d+(?:\.\d+)?)\s*(?:[A-Za-z°℃]+)?\s*(?:-|TO)\s*"
        r"([-+]?\d+(?:\.\d+)?)",
        text,
        re.I,
        )
    ]
    tolerance = []
    for match in re.finditer(
        r"(-?\d+(?:\.\d+)?)\s*(?:[A-Za-z°℃]+)?\s*(?:±|\+/-)\s*"
        r"(\d+(?:\.\d+)?)\s*%",
        text,
        re.I,
    ):
        center = float(match.group(1))
        fraction = float(match.group(2)) / 100.0
        bounds = sorted((center * (1.0 - fraction), center * (1.0 + fraction)))
        tolerance.append((match.start(), (bounds[0], bounds[1])))
    return [item for _, item in sorted([*direct, *tolerance], key=lambda entry: entry[0])]


def parse_range(value: str) -> tuple[float, float] | None:
    ranges = parse_ranges(value)
    return ranges[0] if ranges else None


def scalar_or_none(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if math.isclose(value, round(value)) else round(value, 6)
