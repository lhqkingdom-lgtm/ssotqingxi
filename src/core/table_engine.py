from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from core.text import clean, compact


TABLE_TITLE_RE = re.compile(r"(?:T\s*a\s*b\s*l\s*e|表)\s*([0-9]+(?:[-.]\d+)?)\s*[.:：]?\s*(.*)", re.I)
CONTINUED_RE = re.compile(r"\(?\s*(?:continued|续|续表)\s*\)?", re.I)


@dataclass(frozen=True)
class TableFragment:
    page: int
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]
    page_text: str
    title: str
    table_number: str | None
    continued: bool
    rotation: int
    grid_rows: tuple[tuple[str, ...], ...] = ()
    cell_bboxes: tuple[tuple[tuple[float, float, float, float] | None, ...], ...] = ()

    @property
    def normalized_text(self) -> str:
        return compact(" ".join(cell for row in self.rows for cell in row)).lower()


@dataclass(frozen=True)
class TableGroup:
    key: str
    fragments: tuple[TableFragment, ...]

    @property
    def pages(self) -> list[int]:
        return [fragment.page for fragment in self.fragments]

    @property
    def normalized_text(self) -> str:
        return " ".join(fragment.normalized_text for fragment in self.fragments)


def rectangular_rows(rows: Iterable[Iterable[Any]]) -> list[list[str]]:
    materialized = [[clean(cell) for cell in row] for row in rows]
    if not materialized:
        return []
    width = max(len(row) for row in materialized)
    normalized = [row + [""] * (width - len(row)) for row in materialized]
    keep = [column for column in range(width) if any(row[column] for row in normalized)]
    return [[row[column] for column in keep] for row in normalized]


def title_near_table(page: Any, bbox: tuple[float, float, float, float], page_text: str) -> str:
    top = float(bbox[1])
    candidates: list[str] = []
    try:
        crop_top = max(0.0, top - 110.0)
        cropped = page.crop((0.0, crop_top, float(page.width), top + 3.0)).extract_text() or ""
        candidates.extend(line.strip() for line in cropped.splitlines() if line.strip())
    except Exception:
        pass
    if not candidates:
        candidates.extend(line.strip() for line in page_text.splitlines() if line.strip())
    for line in reversed(candidates):
        if TABLE_TITLE_RE.search(line):
            return clean(line)
    return clean(candidates[-1]) if candidates else ""


def table_identity(title: str) -> tuple[str | None, str, bool]:
    match = TABLE_TITLE_RE.search(title)
    number = match.group(1) if match else None
    body = match.group(2) if match else title
    continued = bool(CONTINUED_RE.search(body))
    base = compact(CONTINUED_RE.sub("", body)).lower()
    return number, base, continued


def iter_table_fragments(
    pdf: Any,
    page_texts: list[str],
    *,
    page_limit: int = 0,
    progress: Callable[[int, int, int], None] | None = None,
) -> Iterator[TableFragment]:
    pages = pdf.pages[:page_limit] if page_limit else pdf.pages
    for page_index, page in enumerate(pages):
        page_table_count = 0
        page_text = page_texts[page_index] if page_index < len(page_texts) else (page.extract_text() or "")
        for table in page.find_tables():
            extracted = table.extract()
            rows = rectangular_rows(extracted)
            if not rows:
                continue
            grid_width = max((len(row) for row in extracted), default=0)
            grid_rows = tuple(
                tuple(str(cell or "").replace("\x00", "").strip() for cell in row) + ("",) * (grid_width - len(row))
                for row in extracted
            )
            cell_bboxes = tuple(
                tuple(tuple(float(value) for value in cell) if cell is not None else None for cell in table_row.cells)
                + (None,) * (grid_width - len(table_row.cells))
                for table_row in table.rows
            )
            bbox = tuple(float(value) for value in table.bbox)
            title = title_near_table(page, bbox, page_text)
            number, _, continued = table_identity(title)
            page_table_count += 1
            yield TableFragment(
                page=page_index + 1,
                bbox=bbox,
                rows=rows,
                page_text=page_text,
                title=title,
                table_number=number,
                continued=continued,
                rotation=int(getattr(page, "rotation", 0) or 0),
                grid_rows=grid_rows,
                cell_bboxes=cell_bboxes,
            )
        if progress is not None:
            progress(page_index + 1, len(pages), page_table_count)


def group_continued_tables(fragments: Iterable[TableFragment]) -> list[TableGroup]:
    groups: list[list[TableFragment]] = []
    for fragment in sorted(fragments, key=lambda item: (item.page, item.bbox[1], item.bbox[0])):
        if fragment.table_number:
            existing = next(
                (
                    group
                    for group in reversed(groups)
                    if group[-1].table_number == fragment.table_number
                    and fragment.page - group[-1].page <= 2
                ),
                None,
            )
            if existing is not None:
                existing.append(fragment)
                continue
        groups.append([fragment])
    result = []
    for index, group in enumerate(groups, start=1):
        number = group[0].table_number
        key = f"table-{number}" if number else f"fragment-{index}"
        result.append(TableGroup(key=key, fragments=tuple(group)))
    return result


def terms_match(text: str, *, required_all: Iterable[str] = (), required_any: Iterable[str] = ()) -> bool:
    normalized = compact(text).lower()
    all_terms = [compact(term).lower() for term in required_all]
    any_terms = [compact(term).lower() for term in required_any]
    return all(term in normalized for term in all_terms) and (not any_terms or any(term in normalized for term in any_terms))


def group_matches(
    group: TableGroup,
    *,
    title_any: Iterable[str] = (),
    required_all: Iterable[str] = (),
    required_any: Iterable[str] = (),
) -> bool:
    title = " ".join(fragment.title for fragment in group.fragments)
    normalized_title = compact(title).lower()
    title_terms = [compact(term).lower() for term in title_any]
    if title_terms and not any(term in normalized_title for term in title_terms):
        return False
    return terms_match(group.normalized_text, required_all=required_all, required_any=required_any)


def logical_row_model_table(
    rows: list[list[str]],
    *,
    model_tokens: Callable[[str], list[str]],
    package_re: re.Pattern[str],
) -> dict[str, Any] | None:
    starts = []
    for row_index, row in enumerate(rows):
        joined = " ".join(row)
        models = model_tokens(joined)
        if models and (package_re.search(joined) or re.search(r"\b(?:\d+(?:\.\d+)?K|\d{3,5})\b", joined, re.I)):
            starts.append(row_index)
    if not starts:
        return None
    first = min(starts)
    logical_cols = sorted({column for row_index in starts for column, cell in enumerate(rows[row_index]) if cell})
    if len(logical_cols) < 3:
        return None
    headers: dict[int, list[str]] = {column: [] for column in logical_cols}
    for row in rows[:first]:
        for column, value in enumerate(row):
            if not value:
                continue
            nearest = min(logical_cols, key=lambda item: (abs(item - column), item))
            if value not in headers[nearest]:
                headers[nearest].append(value)
    records = []
    current: dict[str, Any] | None = None
    for row in rows[first:]:
        joined = " ".join(row)
        if model_tokens(joined):
            if current:
                records.append(current)
            current = {"values": {}}
        if current:
            for column in logical_cols:
                value = row[column] if column < len(row) else ""
                if value:
                    current["values"].setdefault(column, []).append(value)
    if current:
        records.append(current)
    return {
        "headers": {column: compact(" ".join(values)).upper() for column, values in headers.items()},
        "records": [
            {"values": {column: compact(" ".join(values)) for column, values in record["values"].items()}}
            for record in records
        ],
    }


def logical_transposed_model_table(
    rows: list[list[str]],
    *,
    exact_model_tokens: Callable[[str], list[str]],
    model_header_terms: Iterable[str],
) -> dict[str, Any] | None:
    model_row_index = None
    model_columns: list[tuple[int, str]] = []
    normalized_terms = [compact(term) for term in model_header_terms]
    for row_index, row in enumerate(rows[:6]):
        found: list[tuple[int, str]] = []
        for column, cell in enumerate(row):
            ids = exact_model_tokens(cell)
            if ids:
                found.append((column, ids[0]))
        joined = compact(" ".join(row))
        if len(found) >= 2 and any(term in joined for term in normalized_terms):
            model_row_index = row_index
            model_columns = found
            break
    if model_row_index is None:
        return None
    headers: dict[int, str] = {0: "芯片型号"}
    records = [{"values": {0: model}} for _, model in model_columns]
    parent_label = ""
    for row_index, row in enumerate(rows[model_row_index + 1 :], start=1):
        first = row[0] if row else ""
        second = row[1] if len(row) > 1 else ""
        if first:
            parent_label = first
        label = compact(f"{parent_label}{second}" if second else first).upper()
        if not label:
            continue
        headers[row_index] = label
        values = [row[column] if column < len(row) else "" for column, _ in model_columns]
        nonempty = [value for value in values if value]
        if len(nonempty) == 1 and len(values) > 1:
            values = [nonempty[0] for _ in values]
        for record, value in zip(records, values):
            if value:
                record["values"][row_index] = compact(value)
    return {"headers": headers, "records": records}
