from __future__ import annotations

import unittest

from core.semantic_validation import validate_data_semantics


class SemanticValidationTests(unittest.TestCase):
    def test_total_and_components_can_agree(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "ram_kb": 0.75}],
            "memory": [
                {"chip_id": "DEVICE", "memory_type": "SRAM_Total", "size_kb": 0.75},
                {"chip_id": "DEVICE", "memory_type": "SRAM_Internal", "size_kb": 0.25},
                {"chip_id": "DEVICE", "memory_type": "SRAM_External", "size_kb": 0.5},
            ],
        }

        self.assertEqual(validate_data_semantics(data), [])

    def test_total_is_not_added_to_components(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "ram_kb": 0.75}],
            "memory": [
                {"chip_id": "DEVICE", "memory_type": "SRAM_Total", "size_kb": 0.75},
                {"chip_id": "DEVICE", "memory_type": "SRAM_Internal", "size_kb": 0.25},
                {"chip_id": "DEVICE", "memory_type": "SRAM_External", "size_kb": 0.5},
            ],
        }

        issues = validate_data_semantics(data)
        self.assertFalse(any("1.5" in issue["message"] for issue in issues))

    def test_component_contradiction_is_reported_without_mutation(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "ram_kb": 0.75}],
            "memory": [
                {"chip_id": "DEVICE", "memory_type": "SRAM_Internal", "size_kb": 0.25},
                {"chip_id": "DEVICE", "memory_type": "SRAM_External", "size_kb": 0.75},
            ],
        }

        issues = validate_data_semantics(data)
        self.assertEqual(len(issues), 1)
        self.assertIn("internal+external", issues[0]["message"])
        self.assertEqual(data["memory"][1]["size_kb"], 0.75)


if __name__ == "__main__":
    unittest.main()
