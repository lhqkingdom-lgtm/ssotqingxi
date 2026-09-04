from __future__ import annotations

import unittest

from family_extractors.ca_miai import (
    associate_pin_number_headers,
    resolve_adjacent_value_column,
    resolve_pin_data_column,
)
from topologies.pin_definition import first_pin_data_row, headers_spanning_value_columns


class CaPinHeaderAssociationTests(unittest.TestCase):
    def test_uses_package_header_in_same_columns(self) -> None:
        packages, variants = associate_pin_number_headers(
            ["LQFP64", "LQFP48", "管脚名称"], [0, 1], 2
        )

        self.assertEqual(packages, {0: ["LQFP64"], 1: ["LQFP48"]})
        self.assertEqual(variants, {0: "", 1: ""})

    def test_pairs_shifted_merged_headers_in_visual_order(self) -> None:
        packages, variants = associate_pin_number_headers(
            ["1", "引脚序号 LQFP64", "1", "LQFP48", "VDD", "管脚名称"],
            [0, 2],
            4,
        )

        self.assertEqual(packages, {0: ["LQFP64"], 2: ["LQFP48"]})
        self.assertEqual(variants, {0: "", 2: ""})

    def test_does_not_guess_when_header_counts_are_ambiguous(self) -> None:
        packages, _ = associate_pin_number_headers(
            ["", "LQFP64", "", "管脚名称"], [0, 2], 3
        )

        self.assertEqual(packages, {0: [], 2: []})

    def test_geometry_can_assign_multiple_packages_to_one_number_column(self) -> None:
        packages, _ = associate_pin_number_headers(
            ["SOP28", "SSOP28", "", "管脚名称"],
            [0, 2],
            3,
            {0: ["SOP28", "SSOP28"]},
        )

        self.assertEqual(packages[0], ["SOP28", "SSOP28"])

    def test_geometry_recovers_grouped_package_headers(self) -> None:
        rows = [
            ["SOP28", "SSOP28", "SOP16", "管脚名称"],
            ["1", "", "5", "VDD"],
        ]
        bboxes = {
            (0, 0): (0.0, 0.0, 10.0, 10.0),
            (0, 1): (10.0, 0.0, 20.0, 10.0),
            (0, 2): (20.0, 0.0, 30.0, 10.0),
            (0, 3): (30.0, 0.0, 50.0, 10.0),
            (1, 0): (0.0, 10.0, 20.0, 20.0),
            (1, 2): (20.0, 10.0, 30.0, 20.0),
            (1, 3): (30.0, 10.0, 50.0, 20.0),
        }

        import re

        result = headers_spanning_value_columns(
            rows, bboxes, [0, 1, 2, 3], 1, [0, 2], re.compile(r"(?:S|SS)OP\d+")
        )

        self.assertEqual(result, {0: ["SOP28", "SSOP28"], 2: ["SOP16"]})

    def test_supply_pin_starts_data_before_first_gpio(self) -> None:
        rows = [
            ["引脚序号", "", "管脚名称", "管脚功能"],
            ["", "", "", ""],
            ["1", "", "VDD", "芯片供电管脚"],
            ["2", "", "P2.0/RESET", "通用双向IO口"],
        ]

        self.assertEqual(first_pin_data_row(rows, ("管脚名称", "引脚名称")), 2)

    def test_resolves_shifted_pin_and_default_value_columns(self) -> None:
        rows = [
            ["", "", "", "", "", "管脚名称", "", "", "", "默认功能"],
            ["2", "", "2", "", "P7.5/RESET", "", "功能说明", "", "硬件复位脚", ""],
            ["3", "", "-", "", "P7.4/XTAL_IN", "", "功能说明", "", "通用双向IO口", ""],
        ]

        self.assertEqual(resolve_pin_data_column(rows, 1, 5), 4)
        self.assertEqual(resolve_adjacent_value_column(rows, 1, 9), 8)


if __name__ == "__main__":
    unittest.main()
