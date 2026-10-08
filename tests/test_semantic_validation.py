from __future__ import annotations

import unittest

from core.semantic_validation import validate_data_semantics


class SemanticValidationTests(unittest.TestCase):
    def test_package_summary_and_physical_pin_headers_must_agree(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "package_types": ["LQFP48", "UFBGA48"]}],
            "memory": [],
            "pin_mux": [
                {"chip_id": "DEVICE", "package_type": "LQFP48"},
                {"chip_id": "DEVICE", "package_type": "UFQFPN48"},
            ],
        }
        issues = validate_data_semantics(data)
        self.assertEqual(len(issues), 1)
        self.assertIn("package sources disagree", issues[0]["message"])

    def test_matching_package_sources_are_not_reported(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "package_types": ["LQFP48", "UFQFPN48"]}],
            "memory": [],
            "pin_mux": [
                {"chip_id": "DEVICE", "package_type": "UFQFPN48"},
                {"chip_id": "DEVICE", "package_type": "LQFP48"},
            ],
        }
        self.assertEqual(validate_data_semantics(data), [])

    def test_auxiliary_ball_suffix_is_preserved_but_compatible_with_base_summary(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "package_types": ["LQFP176", "UFBGA176"]}],
            "memory": [],
            "pin_mux": [
                {"chip_id": "DEVICE", "package_type": "LQFP176"},
                {"chip_id": "DEVICE", "package_type": "UFBGA176"},
                {"chip_id": "DEVICE", "package_type": "UFBGA176+25"},
            ],
        }

        self.assertEqual(validate_data_semantics(data), [])
        self.assertEqual(data["pin_mux"][2]["package_type"], "UFBGA176+25")

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

    def test_explicit_total_does_not_assume_distinct_address_spaces_are_additive(self) -> None:
        data = {
            "chips": [{"chip_id": "DEVICE", "ram_kb": 2}],
            "memory": [
                {"chip_id": "DEVICE", "memory_type": "SRAM_Total", "size_kb": 2},
                {"chip_id": "DEVICE", "memory_type": "SRAM_Internal", "size_kb": 0.25},
                {"chip_id": "DEVICE", "memory_type": "SRAM_External", "size_kb": 2},
            ],
        }

        self.assertEqual(validate_data_semantics(data), [])


if __name__ == "__main__":
    unittest.main()
