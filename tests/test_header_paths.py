from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.header_paths import (
    annotate_header_paths,
    disambiguate_semantic_column,
    first_complete_header_prefix,
    select_column_by_header_path,
)
from core.table_engine import TableFragment, TableGroup
from core.table_ir_builder import group_to_table_ir


def layered_pin_fragment() -> TableFragment:
    return TableFragment(
        page=12,
        bbox=(0.0, 0.0, 600.0, 300.0),
        rows=[],
        page_text="Table 5. Pin definitions",
        title="Table 5. Pin definitions",
        table_number="5",
        continued=False,
        rotation=0,
        grid_rows=(
            ("Pin number", "STM32F405xx", "", "STM32F407xx", "", "Pin name"),
            ("", "LQFP100", "LQFP176", "LQFP100", "LQFP176", ""),
            ("1", "1", "A1", "2", "B2", "PA0"),
        ),
        cell_bboxes=(
            (
                (0.0, 0.0, 100.0, 200.0),
                (100.0, 0.0, 300.0, 100.0),
                None,
                (300.0, 0.0, 500.0, 100.0),
                None,
                (500.0, 0.0, 600.0, 200.0),
            ),
            (None, (100.0, 100.0, 200.0, 200.0), (200.0, 100.0, 300.0, 200.0), (300.0, 100.0, 400.0, 200.0), (400.0, 100.0, 500.0, 200.0), None),
            ((0.0, 200.0, 100.0, 300.0), (100.0, 200.0, 200.0, 300.0), (200.0, 200.0, 300.0, 300.0), (300.0, 200.0, 400.0, 300.0), (400.0, 200.0, 500.0, 300.0), (500.0, 200.0, 600.0, 300.0)),
        ),
    )


class HeaderPathTests(unittest.TestCase):
    def test_finds_shortest_complete_multilevel_header(self) -> None:
        rows = [
            ["Pin", "", "Pin name", "Alternate functions"],
            ["LQFP64", "LQFP100", "", ""],
            ["1", "1", "PA0", "TIM2_CH1"],
        ]

        def complete(prefix: list[list[str]]) -> bool:
            values = {str(cell) for row in prefix for cell in row}
            return "Alternate functions" in values and "LQFP64" in values

        self.assertEqual(first_complete_header_prefix(rows, complete), 2)

    def test_returns_none_when_required_header_roles_are_absent(self) -> None:
        rows = [["Pin", "Name"], ["1", "PA0"]]
        self.assertIsNone(
            first_complete_header_prefix(
                rows,
                lambda prefix: any("Package" in row for row in prefix),
            )
        )

    def test_merged_parent_headers_are_propagated_to_leaf_columns(self) -> None:
        table = group_to_table_ir(TableGroup("table-5", (layered_pin_fragment(),)))
        annotated = annotate_header_paths(table, header_rows_by_fragment={1: 2})
        paths = {
            cell.column: cell.column_header_path
            for cell in annotated.cells
            if cell.row == 2
        }
        self.assertEqual(paths[1], ("STM32F405xx", "LQFP100"))
        self.assertEqual(paths[2], ("STM32F405xx", "LQFP176"))
        self.assertEqual(paths[3], ("STM32F407xx", "LQFP100"))
        self.assertEqual(paths[4], ("STM32F407xx", "LQFP176"))

    def test_row_header_paths_can_be_derived_independently(self) -> None:
        table = group_to_table_ir(TableGroup("table-5", (layered_pin_fragment(),)))
        annotated = annotate_header_paths(
            table,
            header_rows_by_fragment={1: 2},
            header_columns_by_fragment={1: 1},
        )
        value = next(cell for cell in annotated.cells if cell.row == 2 and cell.column == 4)
        self.assertEqual(value.row_header_path, ("1",))

    def test_column_selection_uses_parent_identity_before_duplicate_leaf(self) -> None:
        paths = {
            1: ("STM32F405xx", "LQFP176"),
            2: ("STM32F407xx", "LQFP176"),
        }
        self.assertEqual(
            select_column_by_header_path(
                paths,
                leaf_terms=("LQFP176",),
                identity_terms=("STM32F407",),
            ),
            2,
        )

    def test_column_selection_rejects_unresolved_duplicate_paths(self) -> None:
        paths = {
            1: ("STM32F407xx", "LQFP176 option A"),
            2: ("STM32F407xx", "LQFP176 option B"),
        }
        self.assertIsNone(
            select_column_by_header_path(
                paths,
                leaf_terms=("LQFP176",),
                identity_terms=("STM32F407",),
            )
        )

    def test_semantic_column_excludes_package_number_group_with_same_alias(self) -> None:
        headers = {
            0: ("Pin/ball name", "LQFP100"),
            8: ("Pin name (function after reset)",),
        }
        self.assertEqual(
            disambiguate_semantic_column(
                (0, 8),
                headers,
                exclude_terms=("lqfp", "bga", "pin number"),
                preference="rightmost",
            ),
            8,
        )


if __name__ == "__main__":
    unittest.main()
