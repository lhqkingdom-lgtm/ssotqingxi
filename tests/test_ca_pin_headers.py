from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from family_extractors.ca_miai import (
    associate_pin_number_headers,
    inherit_pin_column_metadata,
    pin_number_column_positions,
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

    def test_geometry_fragments_do_not_replace_complete_multiline_headers(self) -> None:
        packages, variants = associate_pin_number_headers(
            ["SOP28(B)", "SOP20TSSOP20(A)", "SOP16(A)", "管脚名称"],
            [0, 1, 2],
            3,
            {0: ["SOP2"], 1: ["SOP20", "TSSOP2"], 2: ["SOP1"]},
        )

        self.assertEqual(packages, {
            0: ["SOP28"],
            1: ["SOP20", "TSSOP20"],
            2: ["SOP16"],
        })
        self.assertEqual(variants, {0: "B", 1: "A", 2: "A"})

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

    def test_continuation_metadata_is_inherited_by_physical_column(self) -> None:
        packages, variants = inherit_pin_column_metadata(
            {0: [], 2: []},
            {0: "", 2: ""},
            {0: 0.1, 2: 0.5},
            {
                0.1: (["PKG_ALPHA"], "A"),
                0.3: (["PKG_BETA"], "B"),
                0.5: (["PKG_GAMMA"], "C"),
            },
        )

        self.assertEqual(packages, {0: ["PKG_ALPHA"], 2: ["PKG_GAMMA"]})
        self.assertEqual(variants, {0: "A", 2: "C"})

    def test_visible_continuation_metadata_is_not_overwritten(self) -> None:
        packages, variants = inherit_pin_column_metadata(
            {0: ["PKG_NEW"], 2: []},
            {0: "N", 2: ""},
            {0: 0.1, 2: 0.5},
            {0.1: (["PKG_OLD"], "O"), 0.5: (["PKG_RIGHT"], "R")},
        )

        self.assertEqual(packages, {0: ["PKG_NEW"], 2: ["PKG_RIGHT"]})
        self.assertEqual(variants, {0: "N", 2: "R"})

    def test_grid_compression_does_not_change_physical_column_identity(self) -> None:
        packages, variants = inherit_pin_column_metadata(
            {0: [], 1: []},
            {0: "", 1: ""},
            {0: 0.1, 1: 0.3},
            {0.1: (["PKG_LEFT"], "L"), 0.3: (["PKG_RIGHT"], "R")},
        )

        self.assertEqual(packages, {0: ["PKG_LEFT"], 1: ["PKG_RIGHT"]})
        self.assertEqual(variants, {0: "L", 1: "R"})

    def test_column_positions_come_from_cell_geometry_not_dense_indexes(self) -> None:
        rows = [
            ["PKG_LEFT", "", "PKG_RIGHT", "管脚名称"],
            ["1", "", "1", "VDD"],
        ]
        bboxes = {
            (1, 0): (0.0, 10.0, 20.0, 20.0),
            (1, 2): (40.0, 10.0, 60.0, 20.0),
        }

        positions = pin_number_column_positions(
            rows, bboxes, [0, 2, 3], 1, [0, 1], (0.0, 0.0, 100.0, 20.0)
        )

        self.assertEqual(positions, {0: 0.1, 1: 0.5})

    def test_supply_pin_starts_data_before_first_gpio(self) -> None:
        rows = [
            ["引脚序号", "", "管脚名称", "管脚功能"],
            ["", "", "", ""],
            ["1", "", "VDD", "芯片供电管脚"],
            ["2", "", "P2.0/RESET", "通用双向IO口"],
        ]

        self.assertEqual(first_pin_data_row(rows, ("管脚名称", "引脚名称")), 2)

    def test_package_header_fragments_do_not_start_pin_data(self) -> None:
        rows = [
            ["引脚序号", "", "管脚名称", "管脚功能"],
            ["28", "8", "", ""],
            ["28", "(A)", "", ""],
            ["1", "1", "VDD", "芯片供电管脚"],
        ]

        self.assertEqual(first_pin_data_row(rows, ("管脚名称", "引脚名称")), 3)

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
