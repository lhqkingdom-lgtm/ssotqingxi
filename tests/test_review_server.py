from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_app.review_server import DocumentCatalog, ReviewBatchCatalog, ReviewStateConflictError, ReviewStateStore, export_payload


class ReviewServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads((ROOT / "config" / "schema.json").read_text(encoding="utf-8"))

    def test_flattens_selected_chips_into_five_reviewed_business_tables(self):
        tables = {sheet: [] for sheet in self.schema["sheets"]}
        metadata = {
            "pdf_source_file": "sample.pdf",
            "evidence_pages": "1",
            "ingested_by": "review-test",
            "ingested_at": "2026-09-04T12:00:00+08:00",
            "is_reviewed": False,
        }
        tables["chips"] = [{"chip_id": "MCU1", "series": "S", **metadata, "not_in_schema": "drop"}]
        tables["peripherals"] = [{"chip_id": "MCU1", "peripheral_type": "ADC", "instance_count": 1, **metadata}]
        payload = export_payload({
            "export_batch_id": "batch-1",
            "chips": [{"chip_id": "MCU1", "tables": tables}],
        }, self.schema)
        self.assertEqual(set(payload["data"]), set(self.schema["sheets"]))
        self.assertTrue(payload["data"]["chips"][0]["is_reviewed"])
        self.assertTrue(payload["data"]["peripherals"][0]["is_reviewed"])
        self.assertNotIn("not_in_schema", payload["data"]["chips"][0])

    def test_rejects_cross_chip_rows(self):
        tables = {sheet: [] for sheet in self.schema["sheets"]}
        tables["chips"] = [{"chip_id": "OTHER"}]
        with self.assertRaisesRegex(ValueError, "does not match"):
            export_payload({
                "export_batch_id": "batch-1",
                "chips": [{"chip_id": "MCU1", "tables": tables}],
            }, self.schema)

    def test_review_state_is_atomically_saved_loaded_and_revision_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review_dir = root / "output" / "batch-a" / "review"
            review_dir.mkdir(parents=True)
            dataset = {
                "run_id": "run-a",
                "base_extracted_sha256": "a" * 64,
                "sheet_order": ["chips", "peripherals"],
                "chips": [{"chip_id": "MCU1"}],
            }
            (review_dir / "review_dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
            state = {
                "state_version": 1,
                "run_id": "run-a",
                "base_extracted_sha256": "a" * 64,
                "updated_at": "2026-09-03T00:00:00Z",
                "chips": {"MCU1": {"decision": "unreviewed"}},
                "record_changes": {},
                "export_batches": [],
                "applied_receipts": [],
            }
            batch = "output/batch-a/review"
            store = ReviewStateStore(root)
            self.assertEqual(store.load(batch)["revision"], 0)
            saved = store.save(batch, 0, state)
            self.assertEqual(saved["revision"], 1)
            target = review_dir / "review_result.json"
            self.assertTrue(target.is_file())
            self.assertEqual(list(review_dir.glob(".review_result.*.tmp")), [])

            restarted_store = ReviewStateStore(root)
            loaded = restarted_store.load(batch)
            self.assertEqual(loaded["revision"], 1)
            self.assertEqual(loaded["state"], state)
            unchanged = restarted_store.save(batch, 1, state)
            self.assertEqual(unchanged["revision"], 1)
            with self.assertRaises(ReviewStateConflictError):
                restarted_store.save(batch, 0, state)

    def test_review_state_rejects_wrong_batch_identity_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review_dir = root / "output" / "batch-a" / "review"
            review_dir.mkdir(parents=True)
            dataset = {
                "run_id": "run-a",
                "base_extracted_sha256": "a" * 64,
                "sheet_order": ["chips"],
                "chips": [{"chip_id": "MCU1"}],
            }
            (review_dir / "review_dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
            invalid = {
                "state_version": 1,
                "run_id": "other-run",
                "base_extracted_sha256": "a" * 64,
                "chips": {"MCU1": {}},
                "record_changes": {},
                "export_batches": [],
                "applied_receipts": [],
            }
            store = ReviewStateStore(root)
            with self.assertRaisesRegex(ValueError, "run_id"):
                store.save("output/batch-a/review", 0, invalid)
            with self.assertRaisesRegex(ValueError, "escapes"):
                store.load("../outside")

    def test_batch_catalog_lists_valid_and_broken_batches_without_cross_contamination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = root / "output" / "batch-good" / "review"
            broken = root / "output" / "batch-broken" / "review"
            good.mkdir(parents=True)
            broken.mkdir(parents=True)
            dataset = {
                "run_id": "run-good",
                "base_extracted_sha256": "b" * 64,
                "sheet_order": ["chips"],
                "summary": {"records": {"chips": 2}},
                "source_issues": [{"kind": "warning"}],
                "chips": [
                    {"chip_id": "MCU1", "documents": ["one.pdf"]},
                    {"chip_id": "MCU2", "documents": ["two.pdf"]},
                ],
            }
            (good / "review_dataset.json").write_text(json.dumps(dataset), encoding="utf-8")
            (broken / "review_dataset.json").write_text("{broken", encoding="utf-8")
            state = {
                "state_version": 1,
                "run_id": "run-good",
                "base_extracted_sha256": "b" * 64,
                "updated_at": "2026-09-03T00:00:00Z",
                "chips": {
                    "MCU1": {"decision": "approved", "review_revision": 1, "exported_revision": 0, "synced_revision": 0},
                    "MCU2": {"decision": "issue", "review_revision": 1, "exported_revision": 0, "synced_revision": 0},
                },
                "record_changes": {},
                "export_batches": [],
                "applied_receipts": [],
            }
            store = ReviewStateStore(root)
            store.save("output/batch-good/review", 0, state)
            payload = ReviewBatchCatalog(root, store).list_batches()
            self.assertEqual(payload["batch_count"], 2)
            by_name = {item["name"]: item for item in payload["batches"]}
            self.assertEqual(by_name["batch-good"]["status"], "ready")
            self.assertEqual(by_name["batch-good"]["chip_count"], 2)
            self.assertEqual(by_name["batch-good"]["document_count"], 2)
            self.assertEqual(by_name["batch-good"]["record_count"], 2)
            self.assertEqual(by_name["batch-good"]["review_url"], "/review/?batch=output%2Fbatch-good%2Freview")
            self.assertEqual(by_name["batch-good"]["review_counts"]["pending_export"], 1)
            self.assertEqual(by_name["batch-good"]["review_counts"]["issue"], 1)
            self.assertEqual(by_name["batch-broken"]["status"], "dataset_error")

    def test_document_catalog_scans_input_links_extraction_and_marks_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            input_dir.mkdir()
            ca_content = b"parsed-ca-pdf"
            (input_dir / "CA51F2.pdf").write_bytes(ca_content)
            (input_dir / "stm32-one.pdf").write_bytes(b"same-stm32")
            (input_dir / "stm32-copy.pdf").write_bytes(b"same-stm32")
            (input_dir / "unknown.pdf").write_bytes(b"unknown")

            config = root / "config"
            profiles = config / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "ca.json").write_text(json.dumps({
                "profile_id": "ca_miai_v1",
                "detection": {"filename_any": ["CA51"]},
            }), encoding="utf-8")
            (profiles / "stm.json").write_text(json.dumps({
                "profile_id": "stm32_v1",
                "detection": {"filename_any": ["stm32"]},
            }), encoding="utf-8")
            registry = config / "document_families.json"
            registry.write_text(json.dumps({"profiles": ["profiles/ca.json", "profiles/stm.json"]}), encoding="utf-8")

            batch = root / "output" / "batch-a"
            (batch / "review").mkdir(parents=True)
            (batch / "review" / "review_dataset.json").write_text("{}", encoding="utf-8")
            (batch / "extracted.json").write_text(json.dumps({
                "documents": [{
                    "sha256": hashlib.sha256(ca_content).hexdigest(),
                    "family_id": "ca_miai_v1",
                    "model_count": 2,
                    "models": ["CA51F251L2", "CA51F251L3"],
                }],
            }), encoding="utf-8")

            payload = DocumentCatalog(root, input_dir, registry).list_documents()
            self.assertEqual(payload["document_count"], 4)
            by_name = {item["filename"]: item for item in payload["documents"]}
            self.assertEqual(by_name["CA51F2.pdf"]["status"], "complete")
            self.assertEqual(by_name["CA51F2.pdf"]["classification_source"], "extraction")
            self.assertEqual(by_name["CA51F2.pdf"]["chip_count"], 2)
            self.assertIn("document=CA51F2.pdf", by_name["CA51F2.pdf"]["review_url"])
            self.assertEqual(by_name["stm32-one.pdf"]["status"], "pending")
            self.assertTrue(by_name["stm32-one.pdf"]["duplicate"])
            self.assertEqual(by_name["stm32-one.pdf"]["duplicate_count"], 2)
            self.assertEqual(by_name["unknown.pdf"]["status"], "unclassified")


if __name__ == "__main__":
    unittest.main()
