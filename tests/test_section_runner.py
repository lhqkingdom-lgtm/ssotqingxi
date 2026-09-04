from __future__ import annotations

import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.collector import Collector
from core.section_runner import run_optional_section


SCHEMA = {"sheets": {"chips": {"fields": ["chip_id", "flash_kb"], "key": ["chip_id"]}}}


class SectionRunnerTests(unittest.TestCase):
    def test_failed_section_discards_only_its_rows_and_evidence(self) -> None:
        collector = Collector(SCHEMA)
        collector.add("chips", {"chip_id": "CORE", "flash_kb": 32}, "EV-CORE")
        evidences = ["EV-CORE"]

        def broken() -> None:
            collector.add("chips", {"chip_id": "PARTIAL", "flash_kb": 64}, "EV-PARTIAL")
            evidences.append("EV-PARTIAL")
            raise ValueError("broken AF matrix")

        result = run_optional_section(
            collector,
            evidences,
            name="pin_af",
            source_file="sample.pdf",
            operation=broken,
            pages="30-31",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["CORE"])
        self.assertEqual(evidences, ["EV-CORE"])
        self.assertEqual(result.review_item["section"], "pin_af")

    def test_successful_section_commits_rows(self) -> None:
        collector = Collector(SCHEMA)
        result = run_optional_section(
            collector,
            [],
            name="memory",
            source_file="sample.pdf",
            operation=lambda: collector.add("chips", {"chip_id": "OK", "flash_kb": 32}, "EV-OK"),
        )
        self.assertEqual(result.status, "success")
        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["OK"])


if __name__ == "__main__":
    unittest.main()
