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


if __name__ == "__main__":
    unittest.main()
