from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_app.build_readonly import build_readonly_review, image_meets_resolution


class ReadonlyReviewTests(unittest.TestCase):
    def test_low_resolution_evidence_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "evidence.png"
            Image.new("RGB", (1200, 800), "white").save(target)
            self.assertFalse(image_meets_resolution(target, crop_width_points=600, resolution=216))
            self.assertTrue(image_meets_resolution(target, crop_width_points=400, resolution=216))

    def test_builds_centralized_review_data_without_duplicate_static_assets(self):
        dataset = {
            "dataset_version": 1,
            "source_schema_version": 2,
            "base_extracted_sha256": "a" * 64,
            "run_id": "a" * 16,
            "sheet_order": ["chips"],
            "sheet_contracts": {"chips": {"fields": ["chip_id"], "key": ["chip_id"]}},
            "summary": {"chips": 1, "records": {"chips": 1}, "referenced_evidence": 1, "empty_sections": 0, "source_issues": 0},
            "chips": [{
                "chip_id": "MCU1",
                "documents": ["sample.pdf"],
                "counts": {"chips": 1},
                "empty_sections": [],
                "evidence_ids": ["EV1"],
                "tables": {"chips": [{
                    "row_id": "row-1",
                    "key": ["MCU1"],
                    "values": {"chip_id": "MCU1"},
                    "evidence_ids": ["EV1"],
                    "field_evidence": {"chip_id": ["EV1"]},
                    "confidence": "A",
                }]},
            }],
            "evidence": [{"evidence_id": "EV1", "source_file": "sample.pdf", "source_name": "sample.pdf", "page": 1, "bbox": [0, 0, 1, 1]}],
            "source_issues": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "dataset.json"
            source.write_text(json.dumps(dataset), encoding="utf-8")
            result = build_readonly_review(source, root / "review")
            self.assertEqual(result["chips"], 1)
            self.assertTrue((root / "review" / "review_dataset.json").is_file())
            self.assertEqual(result["output"], str(root / "review" / "review_dataset.json"))
            for name in ("index.html", "review_state.js", "app.js", "styles.css"):
                self.assertFalse((root / "review" / name).exists())
            self.assertTrue((root / "review" / "evidence").is_dir())

    def test_central_review_can_filter_to_a_document_from_the_url(self):
        script = (ROOT / "src" / "review_app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('searchParams.get("document")', script)
        self.assertIn('state.activeChipId = visibleChips()[0]?.chip_id', script)

    def test_review_ui_uses_export_only_statuses_and_has_real_chip_filter(self):
        static = ROOT / "src" / "review_app" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        script = (static / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="chipStatusFilter"', html)
        self.assertIn('id="exportScope"', html)
        self.assertIn("本次待导出", html)
        self.assertNotIn("导入数据库回执", html)
        self.assertNotIn("本次待同步", html)
        self.assertIn('state.reviewFilter !== "all"', script)
        self.assertNotIn("importDatabaseReceipt", script)


if __name__ == "__main__":
    unittest.main()
