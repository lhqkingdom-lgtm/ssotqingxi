from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from topologies.package_matrix import parse_package_names


PATTERN = r"(?:LQFP|TSSOP|SSOP|SOP|QFN)\s*\d+"


class PackageNameTests(unittest.TestCase):
    def test_pin_count_split_across_line_is_joined(self) -> None:
        self.assertEqual(parse_package_names("SSOP2\n8", PATTERN), ["SSOP28"])
        self.assertEqual(parse_package_names("TSSOP\n20", PATTERN), ["TSSOP20"])

    def test_physical_dimension_is_not_appended_to_pin_count(self) -> None:
        self.assertEqual(parse_package_names("LQFP48\n7 mm x 7 mm", PATTERN), ["LQFP48"])

    def test_multiple_packages_remain_separate(self) -> None:
        self.assertEqual(
            parse_package_names("LQFP32 / UFQFPN32", r"(?:LQFP|UFQFPN)\s*\d+"),
            ["LQFP32", "UFQFPN32"],
        )

    def test_legitimate_prefix_package_names_are_not_collapsed(self) -> None:
        self.assertEqual(
            parse_package_names("BGA1 / BGA100", r"BGA\s*\d+"),
            ["BGA1", "BGA100"],
        )


if __name__ == "__main__":
    unittest.main()
