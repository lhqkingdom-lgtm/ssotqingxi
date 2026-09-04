from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.pipeline import run_extraction


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PipelinePartialTests(unittest.TestCase):
    def test_partial_and_failed_documents_are_written_to_review_queue(self) -> None:
        partial_doc = {
            "doc_id": "partial-doc",
            "sha256": "a" * 64,
            "version": "1",
            "path": Path("partial.pdf"),
            "duplicates": ["partial.pdf"],
        }
        failed_doc = {
            "doc_id": "failed-doc",
            "sha256": "b" * 64,
            "version": "1",
            "path": Path("failed.pdf"),
            "duplicates": ["failed.pdf"],
        }
        zero_model_doc = {
            "doc_id": "zero-model-doc",
            "sha256": "c" * 64,
            "version": "1",
            "path": Path("zero-model.pdf"),
            "duplicates": ["zero-model.pdf"],
        }

        class Extractor:
            @staticmethod
            def process_document(doc, collector, aliases, profile):
                if doc["doc_id"] == "failed-doc":
                    raise ValueError("injected document failure")
                if doc["doc_id"] == "zero-model-doc":
                    return ({
                        "doc_id": doc["doc_id"],
                        "sha256": doc["sha256"],
                        "version": doc["version"],
                        "source_file": str(doc["path"]),
                        "duplicate_paths": doc["duplicates"],
                        "page_count": len(doc["page_texts"]),
                        "model_count": 0,
                        "models": [],
                        "evidence_count": 0,
                    }, [])
                return ({
                    "doc_id": doc["doc_id"],
                    "sha256": doc["sha256"],
                    "version": doc["version"],
                    "source_file": str(doc["path"]),
                    "duplicate_paths": doc["duplicates"],
                    "page_count": len(doc["page_texts"]),
                    "model_count": 1,
                    "models": ["TEST123"],
                    "evidence_count": 0,
                    "status": "partial",
                    "sections": {"pin_af": "failed"},
                    "review_items": [{
                        "section": "pin_af",
                        "status": "failed",
                        "source_file": str(doc["path"]),
                        "pages": [3],
                        "error_type": "ValueError",
                        "reason": "injected section failure",
                    }],
                }, [])

        profile = SimpleNamespace(profile_id="test_family")
        family = SimpleNamespace(
            profile=profile,
            rules={"rule_version": "test-1"},
            aliases={},
            extractor=Extractor,
            rules_path=Path("rules.json"),
        )

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            with (
                patch("core.pipeline.choose_pdfs", return_value=[partial_doc, failed_doc, zero_model_doc]),
                patch("core.pipeline.extract_page_texts", return_value=["one page"]),
                patch("core.pipeline.load_family", return_value=family),
                patch("core.pipeline.select_family", return_value=family),
            ):
                payload = run_extraction(
                    input_dir=Path("input"),
                    output_dir=output_dir,
                    schema_path=PROJECT_ROOT / "config" / "schema.json",
                    profile_paths=[Path("profile.json")],
                    ingested_by="test",
                    ingested_at="2026-09-02T00:00:00+08:00",
                )

            queue = [json.loads(line) for line in (output_dir / "review_queue.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(payload["review_queue_count"], 3)
            self.assertEqual({item["doc_id"] for item in queue}, {"partial-doc", "failed-doc", "zero-model-doc"})
            self.assertEqual(payload["documents"][0]["status"], "partial")
            self.assertEqual(payload["documents"][1]["status"], "failed")
            self.assertEqual(payload["documents"][2]["status"], "failed")
            self.assertEqual(len(payload["errors"]), 2)
            self.assertNotIn("page_texts", partial_doc)
            self.assertNotIn("page_texts", failed_doc)
            self.assertNotIn("page_texts", zero_model_doc)


if __name__ == "__main__":
    unittest.main()
