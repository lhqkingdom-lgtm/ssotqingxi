from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class CellIR:
    cell_id: str
    page: int
    fragment: int
    row: int
    column: int
    raw_text: str
    normalized_text: str
    bbox: BBox | None
    row_span: int = 1
    column_span: int = 1
    inherited_from: str | None = None
    row_header_path: tuple[str, ...] = ()
    column_header_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class TableIR:
    table_id: str
    title: str
    pages: tuple[int, ...]
    continued: bool
    orientations: tuple[int, ...]
    bboxes: tuple[BBox, ...]
    cells: tuple[CellIR, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PageIR:
    page: int
    width: float
    height: float
    rotation: int
    text: str
    table_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentIR:
    source_file: str
    page_count: int
    pages: tuple[PageIR, ...]
    tables: tuple[TableIR, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FieldCandidateIR:
    candidate_id: str
    entity_type: str
    entity_key: str
    field_group: str
    field_name: str
    label_path: tuple[str, ...]
    row_label: str
    raw_value: str
    parsed_value: Any
    unit: str | None
    condition: str | None
    source_table_id: str
    source_cell_ids: tuple[str, ...]
    evidence_pages: tuple[int, ...]
    bboxes: tuple[BBox, ...]
    confidence: str = "B"


@dataclass(frozen=True)
class AggregatedFieldIR:
    aggregate_id: str
    entity_type: str
    entity_key: str
    field_group: str
    field_name: str
    value: Any
    unit: str | None
    candidate_ids: tuple[str, ...]
    source_cell_ids: tuple[str, ...]
    evidence_pages: tuple[int, ...]
    bboxes: tuple[BBox, ...]
    raw_values: tuple[str, ...]


@dataclass(frozen=True)
class ValidationIssueIR:
    rule_id: str
    severity: str
    entity_key: str
    field_name: str | None
    message: str
    candidate_ids: tuple[str, ...] = ()
