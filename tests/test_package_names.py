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

    def test_adjacent_packages_do_not_form_a_false_suffix(self) -> None:
        pattern = r"(?:LQFP|UFBGA|BGA|WLCSP)\s*\d+[A-Z]?"
        self.assertEqual(
            parse_package_names("LQFP64\nWLCSP64+2", pattern),
            ["LQFP64", "WLCSP64"],
        )
        self.assertEqual(
            parse_package_names("LQFP144\nUFBGA144", pattern),
            ["LQFP144", "UFBGA144"],
        )

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

    def test_rotated_reverse_direction_package_names_are_recognized(self) -> None:
        pattern = r"(?:LQFP|TFBGA)\s*\d+"
        self.assertEqual(
            parse_package_names("001PFQL 001AGBFT", pattern),
            ["TFBGA100", "LQFP100"],
        )
        self.assertEqual(
            parse_package_names("52+671AGBFU 671PFQL", r"(?:LQFP|UFBGA|BGA)\s*\d+[A-Z]?"),
            ["LQFP176", "UFBGA176"],
        )

    def test_count_before_package_kind_is_normalized_by_declared_grammar(self) -> None:
        self.assertEqual(
            parse_package_names("64 LQFP (0.5 mm pitch) [12 mm x 12 mm]", PATTERN),
            ["LQFP64"],
        )
        self.assertEqual(parse_package_names("12 mm x 12 mm", PATTERN), [])
        self.assertEqual(
            parse_package_names("PIN NUMBER NFQV ,PFQL 84", r"(?:LQFP|VQFN)\s*\d+"),
            ["LQFP48", "VQFN48"],
        )


if __name__ == "__main__":
    unittest.main()
