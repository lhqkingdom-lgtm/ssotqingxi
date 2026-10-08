from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from regression import business_row


class RegressionHarnessTests(unittest.TestCase):
    def test_business_projection_drops_provenance_and_selected_additive_fields(self) -> None:
        row = {
            "chip_id": "MCU1",
            "package_type": "LQFP48",
            "pin_name": "PA0",
            "_evidence_ids": ["EV-new"],
            "_confidence": "A",
            "pdf_source_file": "source.pdf",
        }

        self.assertEqual(
            business_row(row, {"package_type"}),
            {"chip_id": "MCU1", "pin_name": "PA0"},
        )


if __name__ == "__main__":
    unittest.main()
