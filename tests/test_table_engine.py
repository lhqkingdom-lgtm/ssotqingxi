from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.table_engine import (
    TableFragment,
    group_continued_tables,
    group_matches,
    logical_row_model_table,
    logical_transposed_model_table,
    rectangular_rows,
    table_identity,
)


def fragment(page: int, title: str, rows: list[list[str]]) -> TableFragment:
    return TableFragment(
        page=page,
        bbox=(10.0, 20.0, 500.0, 700.0),
        rows=rectangular_rows(rows),
        page_text=title,
        title=title,
        table_number="2",
        continued="continued" in title.lower(),
        rotation=0,
    )


def unnumbered_fragment(page: int, title: str, rows: list[list[str]]) -> TableFragment:
    value = fragment(page, title, rows)
    return TableFragment(**{**value.__dict__, "table_number": None, "continued": False})


class TableEngineTests(unittest.TestCase):
    def test_table_identity_tolerates_pdf_letter_spacing(self) -> None:
        number, base, continued = table_identity("T able 2. Device features (continued)")
        self.assertEqual(number, "2")
        self.assertEqual(base, "devicefeatures")
        self.assertTrue(continued)

    def test_rectangular_rows_removes_only_globally_empty_columns(self) -> None:
        rows = rectangular_rows([["A", None, "B"], ["C", None, "D", None]])
        self.assertEqual(rows, [["A", "B"], ["C", "D"]])

    def test_continued_fragments_form_one_logical_group(self) -> None:
        groups = group_continued_tables(
            [
                fragment(15, "Table 2. Features and peripheral counts", [["Flash", "64"]]),
                fragment(16, "Table 2. Features and peripheral counts (continued)", [["USART", "3"]]),
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].pages, [15, 16])

    def test_repeated_entity_headers_recover_captionless_matrix_continuation(self) -> None:
        groups = group_continued_tables(
            [
                fragment(
                    4,
                    "Table 1. Device features",
                    [["Peripheral", "ZX10A1", "ZX10B2", "ZX10C3"], ["Flash", "32", "64", "128"]],
                ),
                unnumbered_fragment(
                    5,
                    "Description",
                    [["Peripheral", "ZX10A1", "ZX10B2", "ZX10C3"], ["CPU frequency", "56", "", ""]],
                ),
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].pages, [4, 5])

    def test_generic_headers_do_not_merge_unrelated_next_page_tables(self) -> None:
        groups = group_continued_tables(
            [
                fragment(20, "Table 2. ADC characteristics", [["Symbol", "Parameter", "Min", "Typ", "Max", "Unit"]]),
                unnumbered_fragment(21, "Electrical characteristics", [["Symbol", "Parameter", "Min", "Typ", "Max", "Unit"]]),
            ]
        )
        self.assertEqual(len(groups), 2)

    def test_repeated_table_numbers_receive_document_unique_keys(self) -> None:
        groups = group_continued_tables(
            [
                fragment(19, "Table 2. Device features", [["Flash", "64"]]),
                fragment(315, "Table 2. Dynamics characteristics", [["Clock", "25"]]),
            ]
        )
        self.assertEqual([group.key for group in groups], ["table-2", "table-2-p315-2"])

    def test_group_terms_can_be_satisfied_across_pages(self) -> None:
        group = group_continued_tables(
            [
                fragment(15, "Table 2. Device features", [["Flash memory", "64"]]),
                fragment(16, "Table 2. Device features (continued)", [["SRAM", "20"]]),
            ]
        )[0]
        self.assertTrue(
            group_matches(
                group,
                title_any=["device features"],
                required_all=["flash"],
                required_any=["sram", "ram"],
            )
        )

    def test_row_model_table_converter_is_structure_driven(self) -> None:
        rows = rectangular_rows(
            [
                ["芯片型号", "Flash容量", "封装"],
                ["ZX12A", "64K", "LQFP48"],
                ["ZX12B", "128K", "LQFP64"],
            ]
        )
        parsed = logical_row_model_table(
            rows,
            model_tokens=lambda text: re.findall(r"ZX\d{2}[A-Z]", text),
            package_re=re.compile(r"LQFP\d+"),
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(len(parsed["records"]), 2)

    def test_transposed_converter_expands_model_columns(self) -> None:
        rows = rectangular_rows(
            [
                ["器件型号", "ZX12A", "ZX12B"],
                ["存储器", "Flash", "64", "128"],
                ["", "SRAM", "8", "16"],
            ]
        )
        parsed = logical_transposed_model_table(
            rows,
            exact_model_tokens=lambda text: re.findall(r"ZX\d{2}[A-Z]", text),
            model_header_terms=["器件型号"],
        )
        self.assertIsNotNone(parsed)
        self.assertEqual([item["values"][0] for item in parsed["records"]], ["ZX12A", "ZX12B"])


if __name__ == "__main__":
    unittest.main()
