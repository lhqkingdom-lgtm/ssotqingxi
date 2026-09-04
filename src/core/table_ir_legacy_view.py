from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from core.header_paths import fragment_header_paths
from core.ir import CellIR, DocumentIR
from topologies.matrix_observer import table_grids


class IRTableObject:
    def __init__(self, table_id: str, page: int, rows: list[list[str]], bbox: Iterable[float], cells: dict[tuple[int, int], CellIR]) -> None:
        self.table_id = table_id
        self.page = page
        self._rows = rows
        self.bbox = tuple(float(value) for value in bbox)
        self._cells = cells

    def extract(self) -> list[list[str]]:
        return self._rows

    def column_header_paths(self, header_row_count: int) -> dict[int, tuple[str, ...]]:
        paths, _ = fragment_header_paths(self._cells, header_row_count=header_row_count)
        return paths

    def cell_id(self, row: int, column: int) -> str:
        cell = self._cells.get((row, column))
        return cell.cell_id if cell else ""

    def cell_bboxes(self) -> dict[tuple[int, int], tuple[float, float, float, float] | None]:
        """Expose normalized cell geometry without leaking mutable CellIR objects."""
        return {position: cell.bbox for position, cell in self._cells.items()}


class IRPageObject:
    def __init__(self, tables: list[IRTableObject]) -> None:
        self._tables = tables

    def find_tables(self) -> list[IRTableObject]:
        return self._tables


class IRDocumentTableView:
    """Temporary table API view for migrating legacy family rules onto DocumentIR."""

    def __init__(self, document: DocumentIR) -> None:
        by_page: dict[int, list[IRTableObject]] = defaultdict(list)
        for table in document.tables:
            for fragment, page, rows, cells in table_grids(table):
                bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                by_page[page].append(IRTableObject(table.table_id, page, rows, bbox, cells))
        self.pages = [IRPageObject(by_page.get(page, [])) for page in range(1, document.page_count + 1)]
