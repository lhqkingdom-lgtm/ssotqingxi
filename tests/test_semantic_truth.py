from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from verify_semantic_truth import verify


class SemanticTruthVerifierTests(unittest.TestCase):
    def test_matching_value_with_wrong_evidence_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "extracted.json").write_text(
                json.dumps({
                    "data": {
                        "chips": [{
                            "chip_id": "X1",
                            "flash_kb": 32,
                            "pdf_source_file": "x.pdf",
                            "_field_evidence": {"flash_kb": ["E1"]},
                        }]
                    }
                }),
                encoding="utf-8",
            )
            (output / "evidence.jsonl").write_text(
                json.dumps({"evidence_id": "E1", "page": 8}) + "\n",
                encoding="utf-8",
            )
            truth = output / "truth.json"
            truth.write_text(
                json.dumps({
                    "records": [{
                        "sheet": "chips",
                        "key": {"chip_id": "X1"},
                        "source_file_contains": "x.pdf",
                        "expected": {"flash_kb": 32},
                        "evidence_page": 7,
                    }]
                }),
                encoding="utf-8",
            )
            errors = verify(output, truth)
            self.assertEqual(len(errors), 1)
            self.assertIn("value matched but evidence page", errors[0])


if __name__ == "__main__":
    unittest.main()
