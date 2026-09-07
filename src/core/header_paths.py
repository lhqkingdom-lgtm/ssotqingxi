from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping, Sequence

from core.ir import CellIR, TableIR
from core.qualifiers import preferred_qualifier_indices
from core.text import compact


def first_complete_header_prefix(
    rows: Sequence[Sequence[object]],
    is_complete: Callable[[Sequence[Sequence[object]]], bool],
) -> int | None:
    """Return the shortest leading row band that identifies a table header.

    Header depth is derived from semantic/structural evidence supplied by the
    topology.  It deliberately has no document-family row-count setting.
    """
    for depth in range(1, len(rows) + 1):
        if is_complete(rows[:depth]):
            return depth
    return None


def _resolved_text(
    cells: Mapping[tuple[int, int], CellIR],
    by_id: Mapping[str, CellIR],
    row: int,
    column: int,
) -> str:
    cell = cells.get((row, column))
    if cell is None:
        return ""
    source = by_id.get(cell.inherited_from, cell) if cell.inherited_from else cell
    return compact(source.raw_text)


def fragment_header_paths(
    cells: Mapping[tuple[int, int], CellIR],
    *,
    header_row_count: int,
    header_column_count: int = 0,
) -> tuple[dict[int, tuple[str, ...]], dict[int, tuple[str, ...]]]:
    """Build hierarchical column and row labels from merged-cell geometry."""
    if not cells:
        return {}, {}
    by_id = {cell.cell_id: cell for cell in cells.values()}
    height = max(row for row, _ in cells) + 1
    width = max(column for _, column in cells) + 1

    column_paths: dict[int, tuple[str, ...]] = {}
    for column in range(width):
        values: list[str] = []
        for row in range(min(header_row_count, height)):
            value = _resolved_text(cells, by_id, row, column)
            if value and (not values or values[-1] != value):
                values.append(value)
        column_paths[column] = tuple(values)

    row_paths: dict[int, tuple[str, ...]] = {}
    for row in range(height):
        values = []
        for column in range(min(header_column_count, width)):
            value = _resolved_text(cells, by_id, row, column)
            if value and (not values or values[-1] != value):
                values.append(value)
        row_paths[row] = tuple(values)
    return column_paths, row_paths


def annotate_header_paths(
    table: TableIR,
    *,
    header_rows_by_fragment: Mapping[int, int],
    header_columns_by_fragment: Mapping[int, int] | None = None,
) -> TableIR:
    """Return a TableIR whose cells carry topology-derived header paths."""
    header_columns_by_fragment = header_columns_by_fragment or {}
    replacements: dict[str, CellIR] = {}
    for fragment in sorted({cell.fragment for cell in table.cells}):
        cells = {
            (cell.row, cell.column): cell
            for cell in table.cells
            if cell.fragment == fragment
        }
        column_paths, row_paths = fragment_header_paths(
            cells,
            header_row_count=header_rows_by_fragment.get(fragment, 0),
            header_column_count=header_columns_by_fragment.get(fragment, 0),
        )
        for cell in cells.values():
            replacements[cell.cell_id] = replace(
                cell,
                column_header_path=column_paths.get(cell.column, ()),
                row_header_path=row_paths.get(cell.row, ()),
            )
    return replace(table, cells=tuple(replacements.get(cell.cell_id, cell) for cell in table.cells))


def select_column_by_header_path(
    paths: Mapping[int, tuple[str, ...]],
    *,
    leaf_terms: tuple[str, ...],
    identity_terms: tuple[str, ...] = (),
    qualifier_preferences: tuple[str, ...] = (),
) -> int | None:
    """Select a column by its full hierarchical identity, then its leaf."""
    normalized_leafs = tuple(compact(term).upper() for term in leaf_terms if compact(term))
    normalized_identities = tuple(compact(term).upper() for term in identity_terms if compact(term))
    normalized_preferences = tuple(compact(term).lower() for term in qualifier_preferences if compact(term))

    def variants(path: tuple[str, ...]) -> tuple[str, ...]:
        parts = tuple(compact(item).upper() for item in path if compact(item))
        return parts + tuple(item[::-1] for item in parts)

    for leaf in normalized_leafs:
        matches = [
            column
            for column, path in paths.items()
            if any(leaf == item or leaf in item for item in variants(path))
        ]
        if not matches:
            continue
        identity_matches = [
            column
            for column in matches
            if any(identity in item for identity in normalized_identities for item in variants(paths[column]))
        ]
        if identity_matches:
            matches = identity_matches
        if len(matches) > 1 and normalized_preferences:
            selected_indices = preferred_qualifier_indices(
                [" ".join(paths[column]) for column in matches],
                normalized_preferences,
            )
            matches = [matches[index] for index in selected_indices]
        return matches[0] if len(matches) == 1 else None
    return None


def disambiguate_semantic_column(
    candidates: tuple[int, ...],
    headers: Mapping[int, tuple[str, ...]],
    *,
    exclude_terms: tuple[str, ...] = (),
    preference: str = "leftmost",
) -> int | None:
    """Resolve several alias matches using structural header context."""
    normalized_exclusions = tuple(compact(term).lower() for term in exclude_terms if compact(term))
    eligible = [
        column
        for column in candidates
        if not any(
            term in compact(" ".join(headers.get(column, ()))).lower()
            for term in normalized_exclusions
        )
    ]
    pool = eligible or list(candidates)
    if not pool:
        return None
    return max(pool) if preference == "rightmost" else min(pool)
