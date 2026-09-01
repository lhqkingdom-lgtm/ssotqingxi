from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from topologies.model_column_matrix import select_exact_or_wildcard_column


RECIPE = {
    "fields": {
        "program_memory": {"rows": ["programmemory"]},
        "system_memory": {"rows": ["systemram"]},
    }
}


class GenericMatrixTopologyTests(unittest.TestCase):
    def test_unrelated_full_model_headers_use_same_selector(self) -> None:
        rows = [
            ["Feature", "MXA100", "MXA200"],
            ["Program memory", "64 KB", "128 KB"],
            ["System RAM", "16 KB", "32 KB"],
        ]
        predicate = lambda value: value.upper().startswith("MXA")
        self.assertEqual(select_exact_or_wildcard_column(rows, "MXA200", RECIPE, predicate), 2)


if __name__ == "__main__":
    unittest.main()
