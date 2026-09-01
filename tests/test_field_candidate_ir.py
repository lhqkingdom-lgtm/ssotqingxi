from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.field_rules import observations_to_candidates
from topologies.matrix_observer import MatrixObservation


class FieldCandidateIRTests(unittest.TestCase):
    def test_observation_has_no_business_key_until_rules_are_applied(self) -> None:
        observation = MatrixObservation(
            observation_id="OBS-table-2-f1-r1-c2",
            entity_key="MXA200",
            source_table_id="table-2",
            page=3,
            row=1,
            column=2,
            label_path=("Program memory",),
            row_label="Programmemory",
            raw_value="128 KB",
            source_cell_id="table-2-f1-p3-r1-c2",
            bbox=(10.0, 20.0, 30.0, 40.0),
        )
        rules = {"fields": {"flash_kb": {"rows": ["program memory"]}}}
        candidates = observations_to_candidates([observation], rules, entity_type="chip")
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual((candidate.field_name, candidate.raw_value), ("flash_kb", "128 KB"))
        self.assertEqual(candidate.source_cell_ids, ("table-2-f1-p3-r1-c2",))
        self.assertEqual(candidate.evidence_pages, (3,))


if __name__ == "__main__":
    unittest.main()
