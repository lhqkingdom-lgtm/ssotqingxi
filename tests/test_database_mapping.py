from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.database_mapping import (
    DatabaseMappingError,
    database_workbook_payload,
    load_database_mapping,
    map_data_to_database,
)


class DatabaseMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mapping = load_database_mapping(ROOT / "config" / "database_mapping.json")
        schema = json.loads((ROOT / "config" / "schema.json").read_text(encoding="utf-8"))

        def row(sheet: str, **values):
            result = {field: None for field in schema["sheets"][sheet]["fields"]}
            result.update({
                "pdf_source_file": "sample.pdf",
                "evidence_pages": "2-3",
                "ingested_by": "extractor",
                "ingested_at": "2026-09-08T10:00:00+08:00",
                "is_reviewed": False,
            })
            result.update(values)
            return result

        cls.data = {
            "chips": [row("chips", chip_id="MCU1", series="MCU", core="Arm", package_types=["LQFP48"])],
            "pin_mux": [row("pin_mux", chip_id="MCU1", package_type="LQFP48", pin_name="PA0", pin_number=1, function_name="GPIO", is_supported=True)],
            "peripherals": [row("peripherals", chip_id="MCU1", peripheral_type="UART", instance_count=2)],
            "electrical": [row("electrical", chip_id="MCU1", param_name="VDD", min_value=2.0, max_value=3.6, unit="V")],
            "memory": [
                row("memory", chip_id="MCU1", memory_type="Flash", size_kb=128),
                row("memory", chip_id="MCU1", memory_type="SRAM_External", size_kb=16),
            ],
        }

    def test_unreviewed_rows_map_to_draft_without_reviewer(self) -> None:
        result = map_data_to_database(copy.deepcopy(self.data), self.mapping)
        chip = result["tables"]["ssot_mcu_chips"][0]
        self.assertEqual(chip["source_doc_name"], "sample.pdf")
        self.assertEqual(chip["source_page"], "2-3")
        self.assertEqual(chip["submitter"], "extractor")
        self.assertEqual(chip["package_types"], ["LQFP48"])
        self.assertEqual(chip["ram_kb"], 16)
        self.assertIsNone(chip["updated_at"])
        self.assertEqual(
            list(chip),
            ["chip_id", "series", "core", "flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max", "updated_at", "source_doc_name", "source_page", "submitter", "reviewer", "audit_status"],
        )
        self.assertEqual(chip["audit_status"], "draft")
        self.assertIsNone(chip["reviewer"])
        self.assertNotIn("is_reviewed", chip)
        self.assertNotIn("ingested_at", chip)
        self.assertEqual(result["source_ingested_at"], ["2026-09-08T10:00:00+08:00"])

    def test_reviewed_rows_require_and_store_reviewer(self) -> None:
        data = copy.deepcopy(self.data)
        for rows in data.values():
            rows[0]["is_reviewed"] = True
        with self.assertRaisesRegex(DatabaseMappingError, "reviewer is required"):
            map_data_to_database(data, self.mapping)
        result = map_data_to_database(data, self.mapping, reviewer="Reviewer A")
        for rows in result["tables"].values():
            self.assertEqual(rows[0]["reviewer"], "Reviewer A")
            self.assertEqual(rows[0]["audit_status"], "approved")

    def test_channel_count_is_losslessly_embedded_in_features_jsonb(self) -> None:
        data = copy.deepcopy(self.data)
        data["peripherals"][0]["channel_count"] = 16
        result = map_data_to_database(data, self.mapping)
        self.assertEqual(
            result["tables"]["ssot_mcu_peripherals"][0]["features"],
            {"channel_count": 16},
        )

    def test_jsonb_merge_preserves_existing_feature_values(self) -> None:
        data = copy.deepcopy(self.data)
        data["peripherals"][0]["features"] = ["DMA"]
        data["peripherals"][0]["channel_count"] = 16
        result = map_data_to_database(data, self.mapping)
        self.assertEqual(
            result["tables"]["ssot_mcu_peripherals"][0]["features"],
            {"values": ["DMA"], "channel_count": 16},
        )

    def test_json_text_is_converted_for_jsonb_columns(self) -> None:
        data = copy.deepcopy(self.data)
        data["memory"][0]["features"] = '["ECC"]'
        result = map_data_to_database(data, self.mapping)
        self.assertEqual(result["tables"]["ssot_mcu_memory"][0]["features"], ["ECC"])

    def test_fractional_ram_is_omitted_from_integer_summary_but_preserved_in_memory(self) -> None:
        data = copy.deepcopy(self.data)
        data["memory"][1]["size_kb"] = 0.75
        data["chips"][0]["ram_kb"] = 0.75
        result = map_data_to_database(data, self.mapping)
        self.assertIsNone(result["tables"]["ssot_mcu_chips"][0]["ram_kb"])
        self.assertEqual(result["tables"]["ssot_mcu_memory"][1]["size_kb"], 0.75)

    def test_non_integer_pin_identifiers_and_multi_af_codes_are_losslessly_embedded(self) -> None:
        data = copy.deepcopy(self.data)
        data["pin_mux"][0].update(pin_number="A2", af_code="AF1,AF12")
        result = map_data_to_database(data, self.mapping)
        pin = result["tables"]["ssot_mcu_pin_mux"][0]
        self.assertIsNone(pin["pin_number"])
        self.assertIsNone(pin["af_code"])
        self.assertEqual(pin["constraints"]["physical_pin_designator"], "A2")
        self.assertEqual(pin["constraints"]["source_af_code"], "AF1,AF12")
        self.assertEqual(pin["constraints"]["package_type"], "LQFP48")
        self.assertEqual(
            list(pin),
            ["chip_id", "pin_name", "pin_number", "function_name", "is_supported", "af_code", "default_function", "constraints", "source_doc_name", "source_page", "submitter", "reviewer", "audit_status"],
        )

    def test_database_workbook_keeps_stable_tab_names_with_database_columns(self) -> None:
        mapped = map_data_to_database(copy.deepcopy(self.data), self.mapping)
        workbook = database_workbook_payload(mapped, self.mapping)
        self.assertEqual(
            list(workbook["data"]),
            ["chips", "pin_mux", "peripherals", "electrical", "memory"],
        )
        self.assertEqual(
            list(workbook["data"]["chips"][0]),
            self.mapping["tables"]["chips"]["column_order"],
        )
        self.assertEqual(workbook["data"]["chips"][0]["audit_status"], "draft")


if __name__ == "__main__":
    unittest.main()
