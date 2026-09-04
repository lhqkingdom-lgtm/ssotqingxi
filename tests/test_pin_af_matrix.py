from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.ir import CellIR, TableIR
from topologies.pin_af_matrix import PinAFObservation, observe_pin_af_matrices, unique_pin_af_lookup


class PinAFMatrixTests(unittest.TestCase):
    def test_observes_pin_function_and_af_code_without_vendor_logic(self) -> None:
        rows = [
            ["Port", "Pin", "AF0", "AF1", "AF2"],
            ["A", "PA0", "-", "TIM2_CH1/TIM2_ETR", "UART4_TX"],
        ]
        cells = tuple(
            CellIR(
                cell_id=f"c-{row}-{column}", page=9, fragment=1, row=row, column=column,
                raw_text=value, normalized_text=value.lower(), bbox=(column * 10, row * 10, column * 10 + 10, row * 10 + 10),
            )
            for row, values in enumerate(rows)
            for column, value in enumerate(values)
        )
        table = TableIR(
            table_id="table-10", title="Table 10. Port A alternate functions", pages=(9,),
            continued=False, orientations=(0,), bboxes=((0, 0, 50, 20),), cells=cells,
        )
        observations = observe_pin_af_matrices(
            [table],
            locator={"title_any": ["alternate function"], "header_pattern": "^AF\\d+$", "minimum_af_columns": 2},
            pin_pattern=re.compile(r"P[A-Z]\d{1,2}"),
            split_functions=lambda value, _: [item for item in str(value).split("/") if item and item != "-"],
        )
        lookup = unique_pin_af_lookup(observations)
        self.assertEqual(lookup[("PA0", "TIM2_CH1")].af_code, "AF1")
        self.assertEqual(lookup[("PA0", "TIM2_ETR")].af_code, "AF1")
        self.assertEqual(lookup[("PA0", "UART4_TX")].af_code, "AF2")

    def test_named_sibling_family_matrix_is_excluded(self) -> None:
        def table(table_id: str, title: str, af_code: str) -> TableIR:
            rows = [["Pin", "AF0", "AF1"], ["PA6", "-", "TIM1_BKIN"]]
            if af_code == "AF12":
                rows = [["Pin", "AF0", "AF12"], ["PA6", "-", "TIM1_BKIN"]]
            cells = tuple(
                CellIR(
                    cell_id=f"{table_id}-{row}-{column}", page=9, fragment=1, row=row, column=column,
                    raw_text=value, normalized_text=value.lower(), bbox=None,
                )
                for row, values in enumerate(rows)
                for column, value in enumerate(values)
            )
            return TableIR(
                table_id=table_id, title=title, pages=(9,), continued=False,
                orientations=(0,), bboxes=(None,), cells=cells,
            )

        observations = observe_pin_af_matrices(
            [
                table("target", "Alternate functions (ACME55xx)", "AF1"),
                table("sibling", "Alternate functions (ACME35xx)", "AF12"),
            ],
            locator={
                "title_any": ["alternate function"],
                "header_pattern": r"^AF\d+$",
                "minimum_af_columns": 2,
                "identity_pattern": r"ACME\d+",
            },
            pin_pattern=re.compile(r"P[A-Z]\d{1,2}"),
            split_functions=lambda value, _: [str(value)],
            identity_terms=("ACME55",),
        )
        lookup = unique_pin_af_lookup(observations)
        self.assertEqual(lookup[("PA6", "TIM1_BKIN")].af_code, "AF1")

    def test_multiple_af_codes_for_same_pin_function_are_aggregated(self) -> None:
        observations = [
            PinAFObservation("PA6", "TIM1_BKIN", "AF12", "t", "c12", 73, None),
            PinAFObservation("PA6", "TIM1_BKIN", "AF1", "t", "c1", 73, None),
        ]
        lookup = unique_pin_af_lookup(observations)
        self.assertEqual(lookup[("PA6", "TIM1_BKIN")].af_code, "AF1,AF12")


if __name__ == "__main__":
    unittest.main()
