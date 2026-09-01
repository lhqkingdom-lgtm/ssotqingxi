from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from core.ir import DocumentIR
from topologies.matrix_observer import table_grids


class IRTableObject:
    def __init__(self, rows: list[list[str]], bbox: Iterable[float]) -> None:
        self._rows = rows
        self.bbox = tuple(float(value) for value in bbox)

    def extract(self) -> list[list[str]]:
        return self._rows


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
            for fragment, page, rows, _ in table_grids(table):
                bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                by_page[page].append(IRTableObject(rows, bbox))
        self.pages = [IRPageObject(by_page.get(page, [])) for page in range(1, document.page_count + 1)]
