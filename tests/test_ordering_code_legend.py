from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from topologies.ordering_code_legend import parse_ordering_code_legend
from core.profile import load_profile
from family_extractors.stm32 import source_ordering_config


class OrderingCodeLegendTests(unittest.TestCase):
    def test_extracts_source_declared_pin_flash_and_package_codes(self) -> None:
        text = """7 Ordering information
Pin count
E = 25
G = 28
Flash memory size
8 = 64 Kbytes
B = 128 Kbytes
Package type
I = UFBGA
Y = WLCSP
Temperature range
6 = -40 to 85°C
"""
        legend = parse_ordering_code_legend(["contents", text])
        self.assertEqual(legend.pin_count_codes, {"E": 25, "G": 28})
        self.assertEqual(legend.flash_code_kb, {"8": 64, "B": 128})
        self.assertEqual(legend.package_type_codes, {"I": "UFBGA", "Y": "WLCSP"})
        self.assertEqual(legend.evidence_pages, (2,))

    def test_does_not_treat_unsectioned_code_examples_as_authoritative(self) -> None:
        legend = parse_ordering_code_legend(["Ordering information\nExample STM32 G 071 K 8 T 6\nE = 25"])
        self.assertEqual(legend.pin_count_codes, {})

    def test_source_values_replace_static_maps_without_legacy_fallback(self) -> None:
        profile = load_profile(SRC.parent / "config" / "profiles" / "stm32_v1.json")
        ordering = source_ordering_config(
            profile,
            ["Ordering information\nPin count\nE = 25\nPackage type\nY = WLCSP"],
        )
        self.assertEqual(ordering["pin_count_codes"]["E"], 25)
        self.assertNotIn("F", ordering["pin_count_codes"])
        self.assertEqual(ordering["package_type_codes"]["Y"], "WLCSP")
        self.assertEqual(ordering["flash_code_kb"], {})
        self.assertEqual(ordering["ordering_code_source"], "pdf")
        self.assertEqual(ordering["ordering_code_missing"], ["flash_code_kb"])
        self.assertEqual(ordering["ordering_legend_pages"], [1])

    def test_code_size_starts_flash_section_without_overwriting_pin_codes(self) -> None:
        legend = parse_ordering_code_legend(
            [
                """Ordering information
Sub-family
474 = STM32G474xB/xC/xE
Pin count
C = 48 pins
M = 80 pins, 81 pins
Code size
B = 128 Kbyte
C = 256 Kbyte
E = 512 Kbyte
Package
T = LQFP
"""
            ]
        )
        self.assertEqual(legend.pin_count_codes, {"C": 48, "M": [80, 81]})
        self.assertEqual(legend.flash_code_kb, {"B": 128, "C": 256, "E": 512})
        self.assertEqual(legend.package_type_codes, {"T": "LQFP"})

    def test_footnoted_code_can_wrap_before_its_equals_value(self) -> None:
        legend = parse_ordering_code_legend(
            [
                """Ordering information
Flash memory size
Y
(2) = 640 Kbytes
G = 1 Mbyte
Package
Y = WLCSP100
"""
            ]
        )

        self.assertEqual(legend.flash_code_kb, {"Y": 640, "G": 1024})
        self.assertEqual(legend.package_type_codes, {"Y": "WLCSP100"})


if __name__ == "__main__":
    unittest.main()
