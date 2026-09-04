from __future__ import annotations

import gc
from pathlib import Path
from typing import Any, Callable, Iterable

import pdfplumber

from core.ir import BBox, CellIR, DocumentIR, PageIR, TableIR
from core.table_engine import TableFragment, TableGroup, group_continued_tables, iter_table_fragments
from core.text import compact


def _rounded_edges(values: Iterable[float]) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > 0.5:
            result.append(float(value))
    return result


def _edge_index(edges: list[float], value: float) -> int:
    return min(range(len(edges)), key=lambda index: abs(edges[index] - value))


def _fragment_cells(table_id: str, fragment_index: int, fragment: TableFragment) -> list[CellIR]:
    grid = fragment.grid_rows or fragment.rows
    boxes = fragment.cell_bboxes or tuple(tuple(None for _ in row) for row in grid)
    all_boxes = [bbox for row in boxes for bbox in row if bbox is not None]
    x_edges = _rounded_edges(value for bbox in all_boxes for value in (bbox[0], bbox[2]))
    y_edges = _rounded_edges(value for bbox in all_boxes for value in (bbox[1], bbox[3]))
    anchors: dict[tuple[int, int], tuple[str, int, int]] = {}
    cells: list[CellIR] = []

    for row_index, row in enumerate(grid):
        for column_index, raw_text in enumerate(row):
            bbox = boxes[row_index][column_index] if row_index < len(boxes) and column_index < len(boxes[row_index]) else None
            cell_id = f"{table_id}-f{fragment_index}-p{fragment.page}-r{row_index}-c{column_index}"
            row_span = 1
            column_span = 1
            if bbox is not None and len(x_edges) >= 2 and len(y_edges) >= 2:
                x0 = _edge_index(x_edges, bbox[0])
                x1 = _edge_index(x_edges, bbox[2])
                y0 = _edge_index(y_edges, bbox[1])
                y1 = _edge_index(y_edges, bbox[3])
                column_span = max(1, x1 - x0)
                row_span = max(1, y1 - y0)
                anchors[(row_index, column_index)] = (cell_id, row_span, column_span)
            cells.append(
                CellIR(
                    cell_id=cell_id,
                    page=fragment.page,
                    fragment=fragment_index,
                    row=row_index,
                    column=column_index,
                    raw_text=raw_text,
                    normalized_text=compact(raw_text).lower(),
                    bbox=bbox,
                    row_span=row_span,
                    column_span=column_span,
                )
            )

    inherited: dict[tuple[int, int], str] = {}
    for (row, column), (cell_id, row_span, column_span) in anchors.items():
        for target_row in range(row, row + row_span):
            for target_column in range(column, column + column_span):
                if (target_row, target_column) != (row, column):
                    inherited[(target_row, target_column)] = cell_id
    return [
        CellIR(**{**cell.__dict__, "inherited_from": inherited.get((cell.row, cell.column))})
        if cell.bbox is None and (cell.row, cell.column) in inherited
        else cell
        for cell in cells
    ]


def group_to_table_ir(group: TableGroup) -> TableIR:
    cells: list[CellIR] = []
    for fragment_index, fragment in enumerate(group.fragments, start=1):
        cells.extend(_fragment_cells(group.key, fragment_index, fragment))
    titles = [fragment.title for fragment in group.fragments if fragment.title]
    return TableIR(
        table_id=group.key,
        title=titles[0] if titles else "",
        pages=tuple(group.pages),
        continued=len(group.fragments) > 1 or any(fragment.continued for fragment in group.fragments),
        orientations=tuple(fragment.rotation for fragment in group.fragments),
        bboxes=tuple(fragment.bbox for fragment in group.fragments),
        cells=tuple(cells),
        metadata={"table_number": group.fragments[0].table_number},
    )


def build_document_ir(
    path: Path,
    page_texts: list[str],
    *,
    page_limit: int = 0,
    progress: Callable[[int, int, int], None] | None = None,
) -> DocumentIR:
    with pdfplumber.open(path) as pdf:
        fragments = list(iter_table_fragments(pdf, page_texts, page_limit=page_limit, progress=progress))
        selected_pages = pdf.pages[:page_limit] if page_limit else pdf.pages
        page_specs = tuple(
            (float(page.width), float(page.height), int(getattr(page, "rotation", 0) or 0))
            for index, page in enumerate(selected_pages)
        )
    # pdfplumber caches page layouts and detected geometry. Close that tree
    # before materializing CellIR so both representations do not peak together.
    gc.collect()
    groups = group_continued_tables(fragments)
    fragments.clear()
    table_values: list[TableIR] = []
    for index, group in enumerate(groups):
        table_values.append(group_to_table_ir(group))
        groups[index] = None  # type: ignore[assignment]
    tables = tuple(table_values)
    page_to_tables: dict[int, list[str]] = {}
    for table in tables:
        for page in table.pages:
            page_to_tables.setdefault(page, []).append(table.table_id)
    pages = tuple(
        PageIR(
            page=index + 1,
            width=width,
            height=height,
            rotation=rotation,
            text=page_texts[index] if index < len(page_texts) else "",
            table_ids=tuple(page_to_tables.get(index + 1, [])),
        )
        for index, (width, height, rotation) in enumerate(page_specs)
    )
    return DocumentIR(source_file=path.name, page_count=len(page_texts), pages=pages, tables=tables)
