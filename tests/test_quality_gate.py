from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from quality_gate import evaluate_blind, freeze_manifest, score, wilson_lower_bound


class QualityGateTests(unittest.TestCase):
    def test_wilson_lower_bound(self) -> None:
        self.assertAlmostEqual(wilson_lower_bound(500, 500), 0.992375, places=5)
        self.assertAlmostEqual(wilson_lower_bound(495, 500), 0.976807, places=5)
        self.assertEqual(wilson_lower_bound(0, 0), 0.0)

    def test_severe_error_fails_even_when_rates_pass(self) -> None:
        manifest = {"sets": {"blind": {"files": [{"name": "a.pdf", "family": "F"}]}}}
        fields = [
            {"id": f"n{i}", "risk": "normal", "expected": "x", "actual": "x", "evidence_checked": True, "evidence_correct": True}
            for i in range(1000)
        ]
        fields += [
            {"id": "h", "risk": "high", "expected": "x", "actual": "x", "severe_kind": "wrong_model_or_package", "evidence_checked": True, "evidence_correct": True}
        ]
        annotations = {"documents": [{"name": "a.pdf", "extraction_success": True, "models": [{"found": True}], "records": [{"found": True}], "fields": fields}]}
        result = score(manifest, annotations, "blind")
        passed, failures = evaluate_blind(result)
        self.assertFalse(passed)
        self.assertIn("severe_errors=1 > 0", failures)

    def test_each_family_must_pass(self) -> None:
        manifest = {"sets": {"blind": {"files": [{"name": "a.pdf", "family": "large"}, {"name": "b.pdf", "family": "small"}]}}}
        docs = []
        for name, correct in (("a.pdf", True), ("b.pdf", False)):
            docs.append({
                "name": name,
                "extraction_success": True,
                "models": [{"found": True}],
                "records": [{"found": True}],
                "fields": [
                    {"id": "h", "risk": "high", "expected": 1, "actual": 1, "evidence_checked": True, "evidence_correct": True},
                    *[
                        {"id": f"n{i}", "risk": "normal", "expected": 1, "actual": 1 if correct else 0, "evidence_checked": True, "evidence_correct": correct}
                        for i in range(1000)
                    ],
                ],
            })
        result = score(manifest, {"documents": docs}, "blind")
        passed, failures = evaluate_blind(result)
        self.assertFalse(passed)
        self.assertTrue(any(item.startswith("family[small]") for item in failures))

    def test_freeze_rejects_hash_used_by_another_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "x.pdf"
            pdf.write_bytes(b"pdf")
            metadata = root / "metadata.json"
            metadata.write_text(json.dumps({"files": [{"name": "x.pdf", "vendor": "V", "family": "F", "layout": "L"}]}), encoding="utf-8")
            excluded = root / "old.json"
            from quality_gate import sha256
            excluded.write_text(json.dumps({"old": {"files": [{"sha256": sha256(pdf)}]}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "another set"):
                freeze_manifest(root, metadata, root / "out.json", "blind", [excluded])


if __name__ == "__main__":
    unittest.main()
