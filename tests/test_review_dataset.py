from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from review_app.build_dataset import build_review_dataset, pin_record_sort_key, validate_review_dataset


SCHEMA = {
    "sheets": {
        "chips": {"fields": ["chip_id", "pdf_source_file"], "key": ["chip_id"]},
        "pin_mux": {"fields": ["chip_id", "pin_name", "function_name", "pdf_source_file"], "key": ["chip_id", "pin_name", "function_name"]},
        "peripherals": {"fields": ["chip_id", "peripheral_type", "pdf_source_file"], "key": ["chip_id", "peripheral_type"]},
        "electrical": {"fields": ["chip_id", "param_name", "pdf_source_file"], "key": ["chip_id", "param_name"]},
        "memory": {"fields": ["chip_id", "memory_type", "pdf_source_file"], "key": ["chip_id", "memory_type"]},
    }
}


def row(chip_id: str, evidence_id: str, **values):
    return {
        "chip_id": chip_id,
        "pdf_source_file": "sample.pdf",
        "_evidence_ids": [evidence_id],
        "_field_evidence": {"chip_id": [evidence_id]},
        **values,
    }


class ReviewDatasetTests(unittest.TestCase):
    def fixture(self):
        extracted = {
            "schema_version": 2,
            "data": {
                "chips": [row("MCU1", "EV1")],
                "pin_mux": [row("MCU1", "EV2", pin_name="PA0", function_name="UART_TX")],
                "peripherals": [],
                "electrical": [],
                "memory": [row("MCU1", "EV1", memory_type="Flash")],
            },
            "conflicts": [],
            "validation_issues": [],
            "errors": [],
        }
        evidence = [
            {"evidence_id": "EV1", "source_file": "sample.pdf", "page": 1, "bbox": [0, 0, 1, 1]},
            {"evidence_id": "EV2", "source_file": "sample.pdf", "page": 2, "bbox": [0, 0, 1, 1]},
        ]
        return extracted, evidence

    def test_groups_all_business_rows_by_chip(self):
        extracted, evidence = self.fixture()
        dataset = build_review_dataset(extracted, evidence, SCHEMA, extracted_sha256="a" * 64)
        self.assertEqual(dataset["summary"]["records"], {"chips": 1, "pin_mux": 1, "peripherals": 0, "electrical": 0, "memory": 1})
        self.assertEqual(dataset["chips"][0]["counts"]["pin_mux"], 1)
        self.assertEqual(dataset["chips"][0]["empty_sections"], ["peripherals", "electrical"])
        self.assertEqual(validate_review_dataset(dataset), [])

    def test_rejects_unknown_chip_ownership(self):
        extracted, evidence = self.fixture()
        extracted["data"]["memory"][0]["chip_id"] = "UNKNOWN"
        with self.assertRaisesRegex(ValueError, "unknown chip_id"):
            build_review_dataset(extracted, evidence, SCHEMA, extracted_sha256="b" * 64)

    def test_rejects_missing_evidence(self):
        extracted, evidence = self.fixture()
        evidence.pop()
        with self.assertRaisesRegex(ValueError, "missing evidence"):
            build_review_dataset(extracted, evidence, SCHEMA, extracted_sha256="c" * 64)

    def test_field_evidence_is_included_in_record_evidence(self):
        extracted, evidence = self.fixture()
        extracted["data"]["pin_mux"][0]["_field_evidence"]["function_name"] = ["EV1"]
        dataset = build_review_dataset(extracted, evidence, SCHEMA, extracted_sha256="d" * 64)
        record = dataset["chips"][0]["tables"]["pin_mux"][0]
        self.assertEqual(record["evidence_ids"], ["EV2", "EV1"])

    def test_validator_rejects_invalid_evidence_location(self):
        extracted, evidence = self.fixture()
        dataset = build_review_dataset(extracted, evidence, SCHEMA, extracted_sha256="e" * 64)
        dataset["evidence"][0]["page"] = 0
        self.assertIn("EV1: page must be a positive integer", validate_review_dataset(dataset))

    def test_review_queue_is_preserved_as_source_issue(self):
        extracted, evidence = self.fixture()
        dataset = build_review_dataset(
            extracted,
            evidence,
            SCHEMA,
            extracted_sha256="f" * 64,
            review_queue=[{"section": "pin_af", "status": "failed"}],
        )
        self.assertEqual(dataset["summary"]["source_issues"], 1)
        self.assertEqual(dataset["source_issues"][0]["kind"], "review_queue")

    def test_pin_records_sort_by_numeric_lead_then_pin_and_function(self):
        records = [
            {"row_id": "d", "values": {"pin_number": 10, "pin_name": "P3.2", "function_name": "TK6"}},
            {"row_id": "b", "values": {"pin_number": 7, "pin_name": "P3.7", "function_name": "TK3"}},
            {"row_id": "a", "values": {"pin_number": 7, "pin_name": "P3.7", "function_name": "I2C_SCL"}},
            {"row_id": "c", "values": {"pin_number": 9, "pin_name": "P3.3", "function_name": "PWM6"}},
        ]

        ordered = sorted(records, key=pin_record_sort_key)

        self.assertEqual([item["row_id"] for item in ordered], ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
