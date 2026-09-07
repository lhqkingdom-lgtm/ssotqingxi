from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.table_engine import TableFragment, TableGroup
from core.table_ir_builder import group_to_table_ir
from topologies.matrix_observer import table_grids


def fragment(page: int, rotation: int = 0, continued: bool = False) -> TableFragment:
    return TableFragment(
        page=page,
        bbox=(10.0, 20.0, 210.0, 120.0),
        rows=[["Models", "A", "B"], ["Flash", "16", "32"]],
        page_text="Table 2. features" + (" continued" if continued else ""),
        title="Table 2. features" + (" continued" if continued else ""),
        table_number="2",
        continued=continued,
        rotation=rotation,
        grid_rows=(("Models", "A", "B"), ("Flash", "16", "32")),
        cell_bboxes=(
            ((10.0, 20.0, 160.0, 70.0), None, (160.0, 20.0, 210.0, 70.0)),
            ((10.0, 70.0, 110.0, 120.0), (110.0, 70.0, 160.0, 120.0), (160.0, 70.0, 210.0, 120.0)),
        ),
    )


class TableIRTests(unittest.TestCase):
    def test_cells_keep_geometry_text_and_merged_relationship(self) -> None:
        table = group_to_table_ir(TableGroup("table-2", (fragment(3),)))
        anchor = next(cell for cell in table.cells if cell.row == 0 and cell.column == 0)
        placeholder = next(cell for cell in table.cells if cell.row == 0 and cell.column == 1)
        self.assertEqual((anchor.raw_text, anchor.normalized_text, anchor.column_span), ("Models", "models", 2))
        self.assertEqual(placeholder.inherited_from, anchor.cell_id)
        self.assertEqual(table.pages, (3,))

    def test_merged_anchor_value_is_visible_in_every_spanned_grid_column(self) -> None:
        table = group_to_table_ir(TableGroup("table-2", (fragment(3),)))
        _, _, rows, _ = table_grids(table, inherit_merged_values=True)[0]
        self.assertEqual(rows[0][:2], ["Models", "Models"])

    def test_merged_values_are_not_inherited_without_explicit_topology_request(self) -> None:
        table = group_to_table_ir(TableGroup("table-2", (fragment(3),)))
        _, _, rows, _ = table_grids(table)[0]
        self.assertEqual(rows[0][:2], ["Models", "A"])

    def test_cross_page_rotation_is_preserved(self) -> None:
        table = group_to_table_ir(TableGroup("table-2", (fragment(3, 90), fragment(4, 90, True))))
        self.assertTrue(table.continued)
        self.assertEqual(table.pages, (3, 4))
        self.assertEqual(table.orientations, (90, 90))
        self.assertTrue(all(cell.bbox is not None or cell.inherited_from for cell in table.cells))

    def test_raw_cell_text_can_preserve_function_line_breaks(self) -> None:
        item = fragment(3)
        item = TableFragment(**{
            **item.__dict__,
            "rows": [["Pin", "Functions"], ["PA1", "UART_TX TIM1_CH1"]],
            "grid_rows": (("Pin", "Functions"), ("PA1", "UART_TX\nTIM1_CH1")),
            "cell_bboxes": (
                ((10.0, 20.0, 110.0, 70.0), (110.0, 20.0, 210.0, 70.0)),
                ((10.0, 70.0, 110.0, 120.0), (110.0, 70.0, 210.0, 120.0)),
            ),
        })
        table = group_to_table_ir(TableGroup("table-2", (item,)))
        cell = next(cell for cell in table.cells if cell.row == 1 and cell.column == 1)
        self.assertEqual(cell.raw_text, "UART_TX\nTIM1_CH1")
        self.assertEqual(cell.normalized_text, "uart_txtim1_ch1")


if __name__ == "__main__":
    unittest.main()
