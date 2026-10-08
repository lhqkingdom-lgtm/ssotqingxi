from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from family_extractors.declarative import _section_completion


class DeclarativeAdapterStatusTests(unittest.TestCase):
    def test_missing_optional_section_is_visible_without_downgrading_document(self) -> None:
        plan = {
            "sections": [
                {"id": "catalog", "required": False},
                {"id": "models"},
                {"id": "peripherals"},
            ]
        }

        statuses, document_status = _section_completion(
            plan,
            {"catalog": None, "models": {"models": ["QZ1"]}, "peripherals": {"records": []}},
        )

        self.assertEqual(statuses["catalog"], "not_found")
        self.assertEqual(document_status, "complete")

    def test_missing_required_section_still_downgrades_document(self) -> None:
        plan = {"sections": [{"id": "models"}, {"id": "peripherals"}]}

        statuses, document_status = _section_completion(
            plan,
            {"models": {"models": ["QZ1"]}, "peripherals": None},
        )

        self.assertEqual(statuses["peripherals"], "not_found")
        self.assertEqual(document_status, "partial")


if __name__ == "__main__":
    unittest.main()
