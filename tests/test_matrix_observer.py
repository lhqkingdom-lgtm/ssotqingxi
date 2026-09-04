from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from topologies.matrix_observer import structural_header_row_count


class MatrixObserverTests(unittest.TestCase):
    def test_capacity_values_are_not_mistaken_for_ordering_code_headers(self) -> None:
        rows = [
            ["Feature", "", "MCU55Cx", "", "MCU55Rx"],
            ["Memory density", "Flash", "256 K", "512 K", "1 M"],
            ["", "SRAM", "128 K", "256 K", "256 K"],
        ]
        self.assertEqual(structural_header_row_count(rows, lambda value: value.startswith("MCU")), 1)

    def test_short_letter_first_ordering_codes_can_extend_multilevel_header(self) -> None:
        rows = [
            ["Feature", "", "MCU031_", "", ""],
            ["", "", "_F4", "_F6", "_G4"],
            ["Flash", "", "16", "32", "16"],
        ]
        self.assertEqual(structural_header_row_count(rows, lambda value: value.startswith("MCU")), 2)


if __name__ == "__main__":
    unittest.main()
