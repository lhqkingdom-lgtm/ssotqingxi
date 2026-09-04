from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.evidence import Evidence
from core.metadata import attach_ingestion_metadata, compact_page_ranges


class MetadataTests(unittest.TestCase):
    def test_compact_page_ranges(self) -> None:
        self.assertEqual(compact_page_ranges([9, 10, 11, 15, 17, 18]), "9-11,15,17-18")

    def test_metadata_is_derived_from_evidence(self) -> None:
        data = {"chips": [{"chip_id": "X1", "_evidence_ids": ["EV1", "EV2"]}]}
        evidences = [
            Evidence("EV1", "D", r"D:\docs\a.pdf", 9, [], "model", "A", ""),
            Evidence("EV2", "D", r"D:\docs\a.pdf", 10, [], "basic", "B", ""),
        ]
        attach_ingestion_metadata(data, evidences, "tester", "2026-08-31T12:00:00+08:00")
        row = data["chips"][0]
        self.assertEqual(row["pdf_source_file"], "a.pdf")
        self.assertEqual(row["evidence_pages"], "9-10")
        self.assertEqual(row["ingested_by"], "tester")
        self.assertFalse(row["is_reviewed"])


if __name__ == "__main__":
    unittest.main()
