from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.output_contract import validate_data_contract, validate_schema


class OutputContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "config" / "schema.json").read_text(encoding="utf-8"))
        metadata = {
            "pdf_source_file": "sample.pdf", "evidence_pages": "1", "ingested_by": "test",
            "ingested_at": "2026-09-04T12:00:00+08:00", "is_reviewed": False,
        }

        def row(sheet: str, **values):
            result = {field: None for field in cls.schema["sheets"][sheet]["fields"]}
            result.update(metadata)
            result.update(values)
            return result

        cls.sample = {
            "chips": [row("chips", chip_id="MCU1", series="TEST", core="ARM", flash_kb=16, ram_kb=4,
                          max_freq_mhz=48, package_types=["LQFP48"], voltage_min=2.0, voltage_max=3.6)],
            "pin_mux": [row("pin_mux", chip_id="MCU1", pin_name="PA0", pin_number=1,
                            function_name="UART_TX", is_supported=True)],
            "peripherals": [row("peripherals", chip_id="MCU1", peripheral_type="UART", instance_count=1)],
            "electrical": [row("electrical", chip_id="MCU1", param_name="VDD_operating",
                               min_value=2.0, max_value=3.6, unit="V")],
            "memory": [row("memory", chip_id="MCU1", memory_type="Flash", size_kb=16)],
        }

    def test_schema_defines_exact_five_sheet_contract(self) -> None:
        self.assertEqual(validate_schema(self.schema), [])

    def test_representative_extraction_satisfies_contract(self) -> None:
        self.assertEqual(validate_data_contract(self.sample, self.schema), [])

    def test_rejects_unknown_public_field(self) -> None:
        data = copy.deepcopy(self.sample)
        data["chips"][0]["vendor_private_field"] = "must not leak"
        issues = validate_data_contract(data, self.schema)
        self.assertTrue(any("unknown public fields" in item["message"] for item in issues))

    def test_rejects_wrong_value_type(self) -> None:
        data = copy.deepcopy(self.sample)
        data["chips"][0]["flash_kb"] = "16 KB"
        issues = validate_data_contract(data, self.schema)
        self.assertTrue(any("flash_kb has invalid type" in item["message"] for item in issues))

    def test_rejects_metadata_not_at_end(self) -> None:
        schema = copy.deepcopy(self.schema)
        fields = schema["sheets"]["chips"]["fields"]
        fields[-1], fields[-2] = fields[-2], fields[-1]
        issues = validate_schema(schema)
        self.assertTrue(any("metadata fields must be the final columns" in item["message"] for item in issues))

    def test_rejects_orphan_child_row(self) -> None:
        data = copy.deepcopy(self.sample)
        data["memory"][0]["chip_id"] = "UNKNOWN"
        issues = validate_data_contract(data, self.schema)
        self.assertTrue(any("unknown chip_id" in item["message"] for item in issues))

    def test_rejects_reversed_numeric_pair(self) -> None:
        data = copy.deepcopy(self.sample)
        data["chips"][0]["voltage_min"] = 5.0
        data["chips"][0]["voltage_max"] = 1.0
        issues = validate_data_contract(data, self.schema)
        self.assertTrue(any("voltage_min > voltage_max" in item["message"] for item in issues))


if __name__ == "__main__":
    unittest.main()
