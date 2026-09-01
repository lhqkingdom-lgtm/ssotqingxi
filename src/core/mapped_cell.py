from __future__ import annotations

from dataclasses import dataclass

from core.ir import BBox


@dataclass(frozen=True)
class MappedCell:
    key: str
    label: str
    value: str
    page: int
    column: int
    row_label: str = ""
    source_cell_id: str = ""
    bbox: BBox | None = None
    entity_key: str = ""
