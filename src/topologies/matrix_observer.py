from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from core.ir import BBox, CellIR, TableIR
from core.text import compact


HeaderPredicate = Callable[[str], bool]
ColumnSelector = Callable[[list[list[str]], str, dict[str, Any]], int]


@dataclass(frozen=True)
class MatrixObservation:
    observation_id: str
    entity_key: str
    source_table_id: str
    page: int
    row: int
    column: int
    label_path: tuple[str, ...]
    row_label: str
    raw_value: str
    source_cell_id: str
    bbox: BBox | None


def text_direction_variants(value: str) -> tuple[str, ...]:
    normalized = compact(value).upper()
    reversed_text = normalized[::-1]
    return (normalized,) if reversed_text == normalized else (normalized, reversed_text)


def structural_header_row_count(rows: list[list[str]], header_predicate: HeaderPredicate) -> int:
    if not rows:
        return 0
    count = 1
    for row in rows[1:6]:
        cells = [compact(cell).upper() for cell in row[1:] if compact(cell)]
        has_entity = any(any(header_predicate(item) for item in text_direction_variants(cell)) for cell in cells)
        short_codes = [
            cell
            for cell in cells
            if len(cell) <= 4 and re.fullmatch(r"(?:_[A-Z0-9]+|[A-Z][A-Z0-9]*\d[A-Z0-9]*)", cell)
        ]
        if has_entity or (len(short_codes) >= 2 and len(short_codes) * 2 >= len(cells)):
            count += 1
            continue
        break
    return count


def structural_column_headers(rows: list[list[str]], header_predicate: HeaderPredicate) -> dict[int, str]:
    end = structural_header_row_count(rows, header_predicate)
    width = max((len(row) for row in rows), default=0)
    return {
        column: compact(" ".join(row[column] for row in rows[:end] if column < len(row))).upper()
        for column in range(width)
    }


def first_entity_column(rows: list[list[str]], header_predicate: HeaderPredicate) -> int:
    headers = structural_column_headers(rows, header_predicate)
    candidates = [
        column
        for column, header in headers.items()
        if any(header_predicate(item) for item in text_direction_variants(header))
    ]
    return min(candidates, default=1)


def table_grids(table: TableIR) -> list[tuple[int, int, list[list[str]], dict[tuple[int, int], CellIR]]]:
    result = []
    for fragment in sorted({cell.fragment for cell in table.cells}):
        fragment_cells = [cell for cell in table.cells if cell.fragment == fragment]
        height = max(cell.row for cell in fragment_cells) + 1
        width = max(cell.column for cell in fragment_cells) + 1
        rows = [["" for _ in range(width)] for _ in range(height)]
        cells = {(cell.row, cell.column): cell for cell in fragment_cells}
        for cell in fragment_cells:
            rows[cell.row][cell.column] = cell.raw_text
        result.append((fragment, fragment_cells[0].page, rows, cells))
    return result


def _source_cell(
    cells: dict[tuple[int, int], CellIR],
    row: int,
    column: int,
    first_value_column: int,
) -> CellIR | None:
    for candidate in range(column, first_value_column - 1, -1):
        cell = cells.get((row, candidate))
        if cell and cell.raw_text:
            return cell
    return None


def observe_matrix(
    table: TableIR,
    target_entity: str,
    matrix_config: dict[str, Any],
    column_selector: ColumnSelector,
    header_predicate: HeaderPredicate,
) -> list[MatrixObservation]:
    observations: list[MatrixObservation] = []
    inherited_column: int | None = None
    for fragment, page, rows, cells in table_grids(table):
        header_rows = structural_header_row_count(rows, header_predicate)
        value_start = first_entity_column(rows, header_predicate)
        label_context = [""] * value_start
        try:
            column = column_selector(rows, target_entity, matrix_config)
            inherited_column = column
        except ValueError:
            if inherited_column is None:
                continue
            column = inherited_column
        for row_index, row in enumerate(rows[header_rows:], start=header_rows):
            label_cells = [row[index] if index < len(row) else "" for index in range(value_start)]
            for index, value in enumerate(label_cells):
                if value:
                    label_context[index] = value
                    for deeper in range(index + 1, len(label_context)):
                        label_context[deeper] = ""
            source = _source_cell(cells, row_index, column, value_start)
            if source is None:
                continue
            label_path = tuple(value for value in label_context if value)
            # The rightmost non-empty label cell is the semantic leaf.  Earlier
            # cells may be merged group headings that list several child fields
            # (for example a communication block mentioning SPI/I2C/UART).
            row_label = compact(next((value for value in reversed(label_cells) if value), ""))
            observations.append(
                MatrixObservation(
                    observation_id=f"OBS-{table.table_id}-f{fragment}-r{row_index}-c{column}",
                    entity_key=target_entity,
                    source_table_id=table.table_id,
                    page=page,
                    row=row_index,
                    column=column,
                    label_path=label_path,
                    row_label=row_label,
                    raw_value=source.raw_text,
                    source_cell_id=source.cell_id,
                    bbox=source.bbox,
                )
            )
    return observations


def table_matches(table: TableIR, locator: dict[str, Any]) -> bool:
    title = compact(table.title).lower()
    title_any = [compact(item).lower() for item in locator.get("title_any", [])]
    if title_any and not any(item in title for item in title_any):
        return False
    text = compact(" ".join(cell.raw_text for cell in table.cells)).lower()
    required_all = [compact(item).lower() for item in locator.get("required_all", [])]
    required_any = [compact(item).lower() for item in locator.get("required_any", [])]
    return all(item in text for item in required_all) and (
        not required_any or any(item in text for item in required_any)
    )


def select_matrix(
    tables: Iterable[TableIR],
    target_entity: str,
    matrix_config: dict[str, Any],
    column_selector: ColumnSelector,
    header_predicate: HeaderPredicate,
    identity_terms: Iterable[str] = (),
) -> tuple[TableIR, list[MatrixObservation]]:
    locator = {
        "title_any": matrix_config.get("title_any", []),
        **matrix_config.get("locator", {}),
    }
    score_terms = [compact(item).upper() for item in identity_terms]
    candidates = []
    for table in tables:
        if not table_matches(table, locator):
            continue
        observations = observe_matrix(table, target_entity, matrix_config, column_selector, header_predicate)
        if not observations:
            continue
        text = compact(" ".join(cell.raw_text for cell in table.cells)).upper()
        score = len(observations) + sum(20 for item in score_terms if item and item in text)
        candidates.append((score, table, observations))
    if not candidates:
        raise ValueError(f"no matrix matched {target_entity}")
    _, table, observations = max(candidates, key=lambda item: (item[0], -item[1].pages[0]))
    return table, observations
