from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from core.ir import DocumentIR, TableIR
from core.text import compact


PACKAGE_RE = re.compile(
    r"\b(?:LQFP|TSSOP|UFQFPN|VFQFPN|UFBGA|TFBGA|LFBGA|VFBGA|WLCSP|BGA|QFN|SOP|SSOP)\s*\d+\b",
    re.I,
)
PIN_RE = re.compile(r"^(?:P[A-Z]\d{1,2}|P\d+\.\d+)(?=$|[/\s(])", re.I)
AF_RE = re.compile(r"^AF(?:\d|1[0-9]|2[0-9]|3[0-1])$", re.I)
MIN_TYP_MAX = {"MIN", "TYP", "TYPE", "TYPICAL", "MAX", "MINIMUM", "MAXIMUM", "最小值", "典型值", "最大值"}


@dataclass(frozen=True)
class TableIndexEntry:
    table_id: str
    pages: tuple[int, ...]
    title: str
    table_number: str | None
    row_count: int
    column_count: int
    orientations: tuple[int, ...]
    continued: bool
    normalized_text: str
    top_row_texts: tuple[str, ...]
    structural_header_candidates: tuple[str, ...]
    package_headers: tuple[str, ...]
    af_headers: tuple[str, ...]
    pin_like_cells: int
    merged_cells: int
    features: frozenset[str]


@dataclass(frozen=True)
class TableCandidateQuery:
    title_any: tuple[str, ...] = ()
    header_any: tuple[str, ...] = ()
    text_any: tuple[str, ...] = ()
    required_features: frozenset[str] = frozenset()
    preferred_features: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RankedTable:
    table_id: str
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DocumentTableIndex:
    source_file: str
    entries: tuple[TableIndexEntry, ...]

    def get(self, table_id: str) -> TableIndexEntry:
        return next(entry for entry in self.entries if entry.table_id == table_id)

    def rank(self, query: TableCandidateQuery) -> tuple[RankedTable, ...]:
        ranked: list[RankedTable] = []
        for entry in self.entries:
            if not query.required_features.issubset(entry.features):
                continue
            score = 0
            reasons: list[str] = []
            if query.required_features:
                required = sorted(query.required_features)
                score += len(required)
                reasons.append(f"required:{','.join(required)}")
            normalized_title = compact(entry.title).casefold()
            normalized_headers = compact(" ".join(entry.structural_header_candidates)).casefold()
            for label, terms, haystack, weight in (
                ("title", query.title_any, normalized_title, 8),
                ("header", query.header_any, normalized_headers, 5),
                ("text", query.text_any, entry.normalized_text, 2),
            ):
                matches = [term for term in _normalized_terms(terms) if term in haystack]
                if matches:
                    score += weight * len(matches)
                    reasons.append(f"{label}:{','.join(matches)}")
            preferred = sorted(query.preferred_features.intersection(entry.features))
            if preferred:
                score += 3 * len(preferred)
                reasons.append(f"features:{','.join(preferred)}")
            if score:
                ranked.append(RankedTable(entry.table_id, score, tuple(reasons)))
        return tuple(sorted(ranked, key=lambda item: (-item.score, min(self.get(item.table_id).pages), item.table_id)))


def _normalized_terms(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(compact(value).casefold() for value in values if compact(value))


def _shape(table: TableIR) -> tuple[int, int]:
    if not table.cells:
        return 0, 0
    fragments = sorted({cell.fragment for cell in table.cells})
    row_count = sum(
        max(cell.row for cell in table.cells if cell.fragment == fragment) + 1
        for fragment in fragments
    )
    column_count = max(cell.column for cell in table.cells) + 1
    return row_count, column_count


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _directional_variants(value: str) -> tuple[str, ...]:
    """Return searchable text in both reading directions.

    PDF extractors commonly emit vertically printed table headers in reverse
    character order even when the surrounding table is not rotated.  The
    candidate index must preserve that structural evidence so downstream
    topologies can locate the table without vendor- or model-specific hints.
    """
    normalized = compact(value)
    if not normalized:
        return ()
    reversed_text = normalized[::-1]
    return (normalized,) if reversed_text == normalized else (normalized, reversed_text)


def _entry(table: TableIR) -> TableIndexEntry:
    anchors = [cell for cell in table.cells if not cell.inherited_from and compact(cell.raw_text)]
    texts = [compact(cell.raw_text) for cell in anchors]
    searchable_texts = _unique(
        variant
        for cell in anchors
        for variant in _directional_variants(cell.raw_text)
    )
    normalized_text = compact(" ".join(searchable_texts)).casefold()
    top_rows = _unique(
        variant
        for fragment in sorted({cell.fragment for cell in anchors})
        for cell in anchors
        if cell.fragment == fragment and cell.row == 0
        for variant in _directional_variants(cell.raw_text)
    )
    structural_headers = _unique(
        variant
        for cell in anchors
        if cell.row == 0 or cell.row_span > 1 or cell.column_span > 1
        for variant in _directional_variants(cell.raw_text)
    )
    packages = _unique(
        match.group(0).upper().replace(" ", "")
        for text in texts
        for variant in (text, text[::-1])
        for match in PACKAGE_RE.finditer(variant)
    )
    af_headers = _unique(text.upper() for text in searchable_texts if AF_RE.fullmatch(text))
    pin_like = sum(
        any(PIN_RE.match(variant) for variant in _directional_variants(cell.raw_text))
        for cell in anchors
    )
    merged = sum(cell.row_span > 1 or cell.column_span > 1 for cell in anchors)
    vocabulary = {text.upper() for text in searchable_texts}
    features: set[str] = set()
    if any(orientation % 180 for orientation in table.orientations):
        features.add("rotated")
    if table.continued or len(table.pages) > 1:
        features.add("continued")
    if merged:
        features.add("merged_cells")
    if len(packages) >= 2:
        features.add("multi_package")
    if pin_like:
        features.add("pin_like")
    if len(af_headers) >= 2:
        features.add("af_columns")
    if pin_like and len(af_headers) >= 2:
        features.add("af_matrix")
    if len(MIN_TYP_MAX.intersection(vocabulary)) >= 2:
        features.add("min_typ_max")
    rows, columns = _shape(table)
    return TableIndexEntry(
        table_id=table.table_id,
        pages=table.pages,
        title=table.title,
        table_number=table.metadata.get("table_number"),
        row_count=rows,
        column_count=columns,
        orientations=table.orientations,
        continued=table.continued,
        normalized_text=normalized_text,
        top_row_texts=top_rows,
        structural_header_candidates=structural_headers,
        package_headers=packages,
        af_headers=af_headers,
        pin_like_cells=pin_like,
        merged_cells=merged,
        features=frozenset(features),
    )


def build_table_index(document: DocumentIR) -> DocumentTableIndex:
    """Create one searchable, immutable index over every Table IR in a PDF."""
    return DocumentTableIndex(document.source_file, tuple(_entry(table) for table in document.tables))
