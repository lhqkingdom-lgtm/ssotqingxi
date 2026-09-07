from __future__ import annotations

import unittest

from core.collector import Collector


SCHEMA = {
    "sheets": {
        "chips": {
            "fields": ["chip_id", "flash_kb"],
            "key": ["chip_id"],
        }
    }
}


class CollectorTransactionTests(unittest.TestCase):
    def test_restore_discards_partial_document_rows_and_conflicts(self) -> None:
        collector = Collector(SCHEMA)
        collector.add("chips", {"chip_id": "KEPT", "flash_kb": 32}, "EV-KEPT")
        checkpoint = collector.checkpoint()

        collector.add("chips", {"chip_id": "PARTIAL", "flash_kb": 64}, "EV-PARTIAL")
        collector.add("chips", {"chip_id": "KEPT", "flash_kb": 128}, "EV-CONFLICT")
        collector.current_source_rank = 9
        collector.restore(checkpoint)

        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["KEPT"])
        self.assertEqual(set(collector.index["chips"]), {("KEPT",)})
        self.assertEqual(collector.conflicts, [])
        self.assertEqual(collector.current_source_rank, 0)

        collector.add("chips", {"chip_id": "AFTER", "flash_kb": 256}, "EV-AFTER")
        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["KEPT", "AFTER"])

    def test_commit_keeps_rows_without_copying_accumulated_data(self) -> None:
        collector = Collector(SCHEMA)
        collector.add("chips", {"chip_id": "KEPT", "flash_kb": 32}, "EV-KEPT")
        checkpoint = collector.checkpoint()
        collector.add("chips", {"chip_id": "NEW", "flash_kb": 64}, "EV-NEW")
        self.assertNotIn("data", checkpoint)
        self.assertNotIn("index", checkpoint)
        collector.commit(checkpoint)
        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["KEPT", "NEW"])

    def test_nested_section_restore_keeps_earlier_document_rows(self) -> None:
        collector = Collector(SCHEMA)
        document = collector.checkpoint()
        collector.add("chips", {"chip_id": "CORE", "flash_kb": 32}, "EV-CORE")
        section = collector.checkpoint()
        collector.add("chips", {"chip_id": "BROKEN", "flash_kb": 64}, "EV-BROKEN")
        collector.restore(section)
        collector.commit(document)
        self.assertEqual([row["chip_id"] for row in collector.data["chips"]], ["CORE"])

    def test_same_revision_conflict_retains_both_complete_candidates(self) -> None:
        collector = Collector(SCHEMA)
        collector.add("chips", {"chip_id": "DEVICE", "flash_kb": 32}, "EV-OLD")
        collector.add("chips", {"chip_id": "DEVICE", "flash_kb": 64}, "EV-NEW")

        self.assertTrue(collector.data["chips"][0]["_ambiguous"])
        self.assertEqual(len(collector.conflicts), 1)
        conflict = collector.conflicts[0]
        self.assertEqual(conflict["resolution"], "ambiguous_same_revision")
        self.assertEqual(conflict["existing_candidate"]["flash_kb"], 32)
        self.assertEqual(conflict["incoming_candidate"]["flash_kb"], 64)
        self.assertEqual(conflict["existing_candidate"]["_evidence_ids"], ["EV-OLD"])
        self.assertEqual(conflict["incoming_candidate"]["_evidence_ids"], ["EV-NEW"])


if __name__ == "__main__":
    unittest.main()
