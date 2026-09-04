from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from core.text import clean, compact, scalar_or_none


@dataclass(frozen=True)
class OrderingCodeLegend:
    """Business-code meanings stated explicitly in an ordering-information section."""

    pin_count_codes: dict[str, int | list[int]]
    flash_code_kb: dict[str, float | int]
    package_type_codes: dict[str, str]
    evidence_pages: tuple[int, ...]


SECTION_ALIASES = {
    "pin_count": ("pincount", "numberofpins", "引脚数", "管脚数"),
    "flash": (
        "flashmemorysize",
        "flashsize",
        "codesize",
        "memorysize",
        "flash容量",
        "闪存容量",
        "代码容量",
    ),
    "package": ("packagetype", "package", "封装类型", "封装形式"),
}

SECTION_BOUNDARIES = (
    "temperaturerange",
    "option",
    "options",
    "producttype",
    "devicefamily",
    "devicesubfamily",
    "subfamily",
    "orderingexample",
    "订货示例",
    "温度范围",
    "产品类型",
)


def _heading_key(value: str) -> str:
    """Normalize punctuation and whitespace used in ordering-code headings."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _section_for_line(line: str) -> str | None:
    normalized = _heading_key(compact(line))
    for section, aliases in SECTION_ALIASES.items():
        if normalized in {_heading_key(compact(alias)) for alias in aliases}:
            return section
    return None


def _memory_kb(value: str) -> float | int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(KBYTES?|KB|MBYTES?|MB)\b", value, re.I)
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2).upper().startswith("M"):
        number *= 1024
    return scalar_or_none(number)


def parse_ordering_code_legend(page_texts: Iterable[str]) -> OrderingCodeLegend:
    pin_counts: dict[str, int | list[int]] = {}
    flash_sizes: dict[str, float | int] = {}
    package_types: dict[str, str] = {}
    pages: set[int] = set()
    entry_pattern = re.compile(r"^\s*([A-Z0-9])\s*=\s*(.+?)\s*$", re.I)

    for page, text in enumerate(page_texts, start=1):
        normalized_page = compact(text).casefold()
        if "orderinginformation" not in normalized_page and "订货信息" not in normalized_page:
            continue
        section: str | None = None
        pending_code: str | None = None
        for raw_line in text.splitlines():
            line = clean(raw_line)
            detected = _section_for_line(line)
            if detected is not None:
                section = detected
                pending_code = None
                continue
            normalized_line = _heading_key(compact(line))
            if normalized_line in {_heading_key(item) for item in SECTION_BOUNDARIES}:
                section = None
                pending_code = None
                continue
            match = entry_pattern.match(line)
            continuation = re.match(r"^\s*(?:\(\d+\))?\s*=\s*(.+?)\s*$", line)
            parsed_entry = False
            if match is None and continuation is not None and pending_code is not None:
                code, meaning = pending_code, continuation.group(1)
                pending_code = None
                parsed_entry = True
            elif match is not None:
                code, meaning = match.group(1).upper(), match.group(2)
                pending_code = None
                parsed_entry = True
            else:
                standalone_code = re.match(r"^\s*([A-Z0-9])\s*(?:\(\d+\))?\s*$", line, re.I)
                if section is not None and standalone_code is not None:
                    pending_code = standalone_code.group(1).upper()
                    continue
                pending_code = None
            if match is None and section is not None and "=" not in line and len(normalized_line) <= 30:
                # An unknown short heading ends the previous code section. This
                # prevents later code groups from inheriting the wrong meaning.
                section = None
                continue
            if section is None or not parsed_entry:
                continue
            if section == "pin_count":
                numbers = list(dict.fromkeys(int(item) for item in re.findall(r"\b(\d+)\b", meaning)))
                if numbers:
                    pin_counts[code] = numbers[0] if len(numbers) == 1 else numbers
                    pages.add(page)
            elif section == "flash":
                size = _memory_kb(meaning)
                if size is not None:
                    flash_sizes[code] = size
                    pages.add(page)
            elif section == "package":
                package = re.search(r"\b[A-Z][A-Z0-9-]{2,}\b", meaning.upper())
                if package:
                    package_types[code] = package.group(0)
                    pages.add(page)

    return OrderingCodeLegend(pin_counts, flash_sizes, package_types, tuple(sorted(pages)))
