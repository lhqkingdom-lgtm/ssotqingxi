from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.ir import CellIR, DocumentIR, TableIR
from core.table_index import TableCandidateQuery, build_table_index


def table(table_id: str, title: str, rows: list[list[str]], *, orientation: int = 0) -> TableIR:
    cells = tuple(
        CellIR(
            cell_id=f"{table_id}-r{row}-c{column}",
            page=3,
            fragment=1,
            row=row,
            column=column,
            raw_text=value,
            normalized_text=value.casefold(),
            bbox=None,
        )
        for row, values in enumerate(rows)
        for column, value in enumerate(values)
    )
    return TableIR(
        table_id=table_id,
        title=title,
        pages=(3,),
        continued=False,
        orientations=(orientation,),
        bboxes=((0.0, 0.0, 100.0, 100.0),),
        cells=cells,
        metadata={"table_number": "12"},
    )


class TableIndexTests(unittest.TestCase):
    def test_required_feature_alone_returns_candidates_without_text_hint(self) -> None:
        pin_table = table("pin", "Pin assignment", [["Pin name"], ["PA0"]])
        index = build_table_index(DocumentIR("sample.pdf", 10, (), (pin_table,)))
        ranked = index.rank(
            TableCandidateQuery(required_features=frozenset({"pin_like"}))
        )
        self.assertTrue(ranked)

    def test_indexes_structure_without_family_configuration(self) -> None:
        pin_table = table(
            "table-12",
            "Table 12. Pin assignment and description",
            [
                ["LQFP64", "LQFP100", "Pin name", "AF0", "AF1"],
                ["1", "1", "PA0", "TIM2_CH1", "USART2_CTS"],
            ],
            orientation=90,
        )
        index = build_table_index(DocumentIR("sample.pdf", 10, (), (pin_table,)))
        entry = index.entries[0]
        self.assertEqual(entry.pages, (3,))
        self.assertEqual(entry.table_number, "12")
        self.assertEqual(entry.package_headers, ("LQFP64", "LQFP100"))
        self.assertEqual(entry.af_headers, ("AF0", "AF1"))
        self.assertEqual(entry.pin_like_cells, 1)
        self.assertTrue({"rotated", "multi_package", "pin_like", "af_matrix"}.issubset(entry.features))

    def test_ranking_prefers_title_and_required_structure(self) -> None:
        feature = table(
            "table-2",
            "Table 2. Device features and peripheral counts",
            [["Peripheral", "STM32X"], ["Flash memory", "128"]],
        )
        unrelated = table("table-7", "Table 7. Revision history", [["Date", "Description"]])
        index = build_table_index(DocumentIR("sample.pdf", 10, (), (unrelated, feature)))
        ranked = index.rank(
            TableCandidateQuery(
                title_any=("device features",),
                text_any=("flash memory",),
            )
        )
        self.assertEqual(ranked[0].table_id, "table-2")
        self.assertGreaterEqual(ranked[0].score, 10)

    def test_required_features_exclude_semantically_similar_wrong_table(self) -> None:
        pin_table = table(
            "pin",
            "Pin assignment",
            [["LQFP48", "Pin name"], ["1", "PA0"]],
        )
        text_only = table("text", "Pin notes", [["Pin name", "Description"]])
        index = build_table_index(DocumentIR("sample.pdf", 10, (), (text_only, pin_table)))
        ranked = index.rank(
            TableCandidateQuery(
                title_any=("pin",),
                required_features=frozenset({"pin_like"}),
            )
        )
        self.assertEqual(tuple(item.table_id for item in ranked), ("pin",))

    def test_ca_style_pin_name_with_inline_functions_is_pin_like(self) -> None:
        pin_table = table(
            "ca-pin",
            "管脚描述",
            [["TSSOP20", "管脚名称"], ["1", "P0.0/ADC[0]/TK[0]/INT2"]],
        )
        entry = build_table_index(DocumentIR("ca.pdf", 20, (), (pin_table,))).entries[0]
        self.assertEqual(entry.pin_like_cells, 1)
        self.assertIn("pin_like", entry.features)

    def test_reversed_vertical_headers_remain_searchable(self) -> None:
        pin_table = table(
            "vertical-pin",
            "Pin definition",
            [
                ["84PFQL", "retfa\neman\n)1()teser\nnoitcnuf(\nniP", "Alternate\nfunctions"],
                ["1", "0AP", "TIM2_CH1"],
            ],
        )
        entry = build_table_index(DocumentIR("vertical.pdf", 20, (), (pin_table,))).entries[0]
        self.assertIn("LQFP48", entry.package_headers)
        self.assertEqual(entry.pin_like_cells, 1)
        self.assertIn("pin_like", entry.features)
        self.assertIn("pin", entry.normalized_text)
        self.assertIn("name", entry.normalized_text)


if __name__ == "__main__":
    unittest.main()
