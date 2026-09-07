from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from core.field_rules import semantic_key
from core.field_dictionary import load_table_recipe
from family_rules.stm32_matrix import column_headers, discover_device_summary_models, discover_model_variants, first_model_column, header_model_patterns, map_model_matrix, select_model_column
from core.recipe_executor import reduce_values
from topologies.matrix_observer import text_direction_variants
from core.table_engine import TableFragment, TableGroup, rectangular_rows


PROFILE = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
RECIPE = load_table_recipe(
    ROOT / "config" / "table_presets" / "stm32_feature_matrix.json",
    PROFILE["dictionary"]["aliases"],
)
ORDERING = {
    "flash_code_kb": {
        "4": 16,
        "6": 32,
        "8": 64,
        "B": 128,
        "C": 256,
        "E": 512,
        "G": 1024,
        "I": 2048,
    }
}


def group(*items: tuple[int, list[list[str]]]) -> TableGroup:
    fragments = tuple(
        TableFragment(
            page=page,
            bbox=(0.0, 0.0, 500.0, 700.0),
            rows=rectangular_rows(rows),
            page_text="Table 2",
            title="Table 2. features and peripheral counts" + (" continued" if index else ""),
            table_number="2",
            continued=bool(index),
            rotation=0,
        )
        for index, (page, rows) in enumerate(items)
    )
    return TableGroup("table-2", fragments)


class ModelMatrixTests(unittest.TestCase):
    def test_device_summary_supplies_models_missing_from_generic_matrix(self) -> None:
        pages = [
            "Table 1. Device summary\n"
            "Reference Part number\n"
            "STM32L151CB, STM32L151C8, STM32L152R6\n"
            "Example STM32L152ZZ"
        ]
        ordering = {
            "pin_count_codes": {"C": 48, "R": 64},
            "flash_code_kb": {"6": 32, "8": 64, "B": 128},
        }

        self.assertEqual(
            discover_device_summary_models(pages, ordering),
            ["STM32L151CB", "STM32L151C8", "STM32L152R6"],
        )

    def test_wildcard_family_header_spans_empty_continuation_columns(self) -> None:
        rows = [
            ["Peripherals", "", "STM32F207Vx", "", "", "", "STM32F207Zx", "", "", ""],
            ["Flash memory in Kbytes", "", "256", "512", "768", "1024", "256", "512", "768", "1024"],
        ]
        ordering = {"flash_code_kb": {"C": 256, "E": 512, "F": 768, "G": 1024}}

        self.assertEqual(
            discover_model_variants(rows, "STM32F207VB", RECIPE, ordering),
            [
                "STM32F207VC",
                "STM32F207VE",
                "STM32F207VF",
                "STM32F207VG",
                "STM32F207ZC",
                "STM32F207ZE",
                "STM32F207ZF",
                "STM32F207ZG",
            ],
        )

    def test_model_discovery_normalizes_local_density_units_before_code_matching(self) -> None:
        rows = [
            ["Feature", "", "STM32WB55Cx", "", "", "STM32WB55Rx", "", "", "STM32WB55Vx", "", "", "", "STM32WB35Cx", ""],
            ["Memory density (bytes)", "Flash", "256 K", "512 K", "1 M", "256 K", "512 K", "1 M", "256 K", "512 K", "1 M", "640 K", "256 K", "512 K"],
        ]
        ordering = {"flash_code_kb": {"C": 256, "E": 512, "G": 1024, "Y": 640}}

        self.assertEqual(
            discover_model_variants(rows, "STM32WB55CC", RECIPE, ordering),
            [
                "STM32WB55CC", "STM32WB55CE", "STM32WB55CG",
                "STM32WB55RC", "STM32WB55RE", "STM32WB55RG",
                "STM32WB55VC", "STM32WB55VE", "STM32WB55VG", "STM32WB55VY",
                "STM32WB35CC", "STM32WB35CE",
            ],
        )

    def test_compact_final_ordering_code_alternatives_are_expanded(self) -> None:
        self.assertEqual(
            header_model_patterns("STM32H562AI/G"),
            ["STM32H562AI/G", "STM32H562AI", "STM32H562AG"],
        )

    def test_compact_family_number_alternatives_are_expanded(self) -> None:
        self.assertEqual(
            header_model_patterns("STM32F767/769Bx"),
            ["STM32F767/769BX", "STM32F767BX", "STM32F769BX"],
        )

    def test_wildcard_model_group_uses_flash_code_to_select_subcolumn(self) -> None:
        rows = [
            ["Peripherals", "", "STM32F767/769Bx", ""],
            ["Flash memory in Kbytes", "", "1024", "2048"],
            ["SRAM", "", "512", "512"],
        ]
        ordering = {"flash_code_kb": {"G": 1024, "I": 2048}}
        self.assertEqual(select_model_column(rows, "STM32F767BI", RECIPE, ordering), 3)

    def test_vertical_model_header_can_be_read_in_reverse_character_order(self) -> None:
        rows = [
            ["Peripherals", "GV247 H23MTS", "IV347H23MTS"],
            ["Flash memory in Kbytes", "2 x 512 Kbytes", "2 x 1 Mbyte"],
            ["SRAM in Kbytes", "384", "512"],
        ]

        self.assertEqual(text_direction_variants("IV347H23MTS"), ("IV347H23MTS", "STM32H743VI"))
        self.assertEqual(select_model_column(rows, "STM32H743VI", RECIPE, ORDERING), 2)
        self.assertEqual(first_model_column(rows, RECIPE), 1)

    def test_spi_exclusion_prevents_quad_spi_from_becoming_spi_count(self) -> None:
        recipe = {"peripherals": {"SPI": {"rows": ["spi"], "exclude_rows": ["quadspi", "quad-spi", "octospi", "octo-spi"]}}}
        self.assertIsNone(semantic_key("Quad-SPI interface", recipe))
        self.assertEqual(semantic_key("Communication interfaces SPI / I2S", recipe), "peripheral:SPI")

    def test_longest_alias_actually_present_wins_for_mixed_interface_row(self) -> None:
        self.assertEqual(
            semantic_key("USART (smartcard, SPI, IrDA, LIN) + UART (IrDA, LIN)", RECIPE),
            "peripheral:UART",
        )

    def test_internal_supply_is_not_the_main_operating_voltage(self) -> None:
        recipe = {
            "fields": {
                "operating_voltage": {
                    "rows": ["operatingvoltage"],
                    "exclude_rows": ["vdd12", "dd12", "corevoltage", "vcap"],
                }
            }
        }
        self.assertEqual(semantic_key("Operating voltage (V)DD", recipe), "field:operating_voltage")
        self.assertIsNone(semantic_key("Operating voltage (V)DD12", recipe))

    def test_wildcards_inside_family_and_density_codes_select_by_flash(self) -> None:
        rows = [
            ["Peripherals", "", "STM32F77xBx", ""],
            ["Flash memory in Kbytes", "", "1024", "2048"],
            ["SRAM in Kbytes", "System", "512", ""],
        ]
        ordering = {"flash_code_kb": {**ORDERING["flash_code_kb"], "I": 2048}}
        self.assertEqual(select_model_column(rows, "STM32F777BI", RECIPE, ordering), 3)

    def test_continuation_data_rows_do_not_extend_model_headers(self) -> None:
        rows = [
            ["Peripherals", "STM32F77xVx", "STM32F77xZx", "STM32F77xBx"],
            ["GPIOs", "82", "114", "159"],
            ["DFSDM1", "Yes", "", ""],
            ["12-bit ADC Number of channels", "3", "", ""],
        ]
        self.assertEqual(column_headers(rows, RECIPE)[3], "STM32F77XBX")
        self.assertEqual(select_model_column(rows, "STM32F777BI", RECIPE, ORDERING), 3)

    def test_suffix_matrix_column(self) -> None:
        rows = rectangular_rows(
            [
                ["Peripheral", "", "STM32C031_ _F4 _F6 _G4 _G6 _K4 _K6 _C4 _C6", "", "", "", "", "", "", ""],
                ["Flash memory (Kbyte)", "", "16", "32", "16", "32", "16", "32", "16", "32"],
                ["SRAM (Kbyte)", "", "12", "", "", "", "", "", "", ""],
            ]
        )
        self.assertEqual(select_model_column(rows, "STM32C031C4", RECIPE, ORDERING), 8)
        self.assertEqual(
            discover_model_variants(rows, "STM32C031C4", RECIPE, ORDERING),
            [
                "STM32C031F4", "STM32C031F6", "STM32C031G4", "STM32C031G6",
                "STM32C031K4", "STM32C031K6", "STM32C031C4", "STM32C031C6",
            ],
        )

    def test_full_model_header_column(self) -> None:
        rows = rectangular_rows(
            [
                ["Peripherals", "STM32F405RG", "STM32F405VG", "STM32F405ZG"],
                ["Flash memory in Kbytes", "1024", "1024", "1024"],
                ["SRAM in Kbytes", "192", "192", "192"],
            ]
        )
        self.assertEqual(select_model_column(rows, "STM32F405ZG", RECIPE, ORDERING), 3)
        self.assertEqual(
            discover_model_variants(rows, "STM32F405ZG", RECIPE, ORDERING),
            ["STM32F405RG", "STM32F405VG", "STM32F405ZG"],
        )

    def test_multiple_model_patterns_in_one_physical_column(self) -> None:
        rows = rectangular_rows(
            [
                ["Features", "XXCAC751PM23MTSXXCAF751PM23MTS"],
                ["Embedded SRAM", "708 Kbytes"],
                ["Package", "TFBGA361"],
            ]
        )
        self.assertEqual(select_model_column(rows, "STM32MP157CAC3T", RECIPE, ORDERING), 1)

    def test_wildcard_family_header_matches_longer_ordering_code(self) -> None:
        rows = rectangular_rows(
            [
                ["Features", "STM32MP25xC/F"],
                ["Embedded SRAM", "808 Kbytes"],
                ["Package", "TFBGA361"],
            ]
        )
        self.assertEqual(select_model_column(rows, "STM32MP257CA", RECIPE, ORDERING), 1)

    def test_interleaved_family_stem_uses_final_package_and_density_codes(self) -> None:
        rows = rectangular_rows(
            [
                ["Peripheral", "", "STM32L4R5xx", ""],
                ["Memory", "Flash memory in Kbytes", "1024", "2048"],
                ["", "SRAM in Kbytes", "640", "640"],
            ]
        )
        ordering = {"flash_code_kb": {"G": 1024, "I": 2048}}
        self.assertEqual(select_model_column(rows, "STM32L4R5ZI", RECIPE, ordering), 3)

    def test_short_matrix_headers_inherit_family_prefix_from_seed(self) -> None:
        rows = rectangular_rows(
            [
                ["Peripheral", "", "R5VI\nR5VG", "R7VI", "R5ZI\nR5ZG"],
                ["Memory", "Flash memory", "1 Mbyte for xxxG devices\n2 Mbyte for xxxI devices", "", ""],
                ["", "Max. CPU frequency", "120 MHz", "", ""],
            ]
        )
        self.assertEqual(select_model_column(rows, "STM32L4R5ZI", RECIPE, ORDERING), 4)
        self.assertEqual(
            discover_model_variants(rows, "STM32L4R5ZI", RECIPE, ORDERING),
            ["STM32L4R5VI", "STM32L4R5VG", "STM32L4R7VI", "STM32L4R5ZI", "STM32L4R5ZG"],
        )

    def test_wrapped_caption_can_fall_back_to_structural_entity_column(self) -> None:
        matrix = TableGroup(
            "table-2",
            (
                TableFragment(
                    page=15,
                    bbox=(0.0, 0.0, 500.0, 700.0),
                    rows=rectangular_rows([
                        ["Peripheral", "", "R5VI\nR5VG", "R5ZI\nR5ZG"],
                        ["Memory", "Flash memory", "1 Mbyte", "2 Mbyte"],
                        ["", "SRAM", "640 Kbyte", "640 Kbyte"],
                    ]),
                    page_text="Table 2. family features and peripheral counts",
                    title="Table 2. STM32L4R5xx, STM32L4R7xx and STM32L4R9xx",
                    table_number="2",
                    continued=False,
                    rotation=0,
                ),
            ),
        )
        mapped = map_model_matrix(matrix, "STM32L4R5ZI", RECIPE, ORDERING)
        self.assertEqual({item.key for item in mapped}, {"field:flash_kb", "field:ram_kb"})

    def test_suffix_only_matrix_supports_interleaved_family_stem(self) -> None:
        rows = rectangular_rows(
            [
                ["Peripheral", "", "STM32G0B1_ _KB _KC _KE _RB _RC _RE", "", "", "", "", ""],
                ["Flash memory in Kbytes", "", "128", "256", "512", "128", "256", "512"],
                ["SRAM in Kbytes", "", "144", "144", "144", "144", "144", "144"],
            ]
        )
        ordering = {"flash_code_kb": {"B": 128, "C": 256, "E": 512}}
        self.assertEqual(select_model_column(rows, "STM32G0B1RE", RECIPE, ordering), 7)

    def test_continued_pages_share_target_column_and_map_fields(self) -> None:
        matrix = group(
            (15, [["Peripherals", "STM32F405RG", "STM32F405ZG"], ["Flash memory (Kbyte)", "1024", "1024"], ["SRAM (Kbyte)", "192", "192"]]),
            (16, [["Peripherals", "STM32F405RG", "STM32F405ZG"], ["SPI", "3", "3"], ["USART", "4", "4"], ["UART", "2", "2"]]),
        )
        mapped = map_model_matrix(matrix, "STM32F405ZG", RECIPE, ORDERING)
        self.assertEqual(
            [(item.key, item.value, item.page) for item in mapped],
            [
                ("field:flash_kb", "1024", 15),
                ("field:ram_kb", "192", 15),
                ("peripheral:SPI", "3", 16),
                ("peripheral:UART", "4", 16),
                ("peripheral:UART", "2", 16),
            ],
        )

    def test_leaf_row_label_wins_over_merged_group_heading(self) -> None:
        matrix = group(
            (
                17,
                [
                    ["Features", "", "STM32MP157CACXX"],
                    ["SPI I2C USART UART communication peripherals", "SPI Having I2S", "6"],
                    ["", "USART + UART", "4 + 4"],
                ],
            )
        )
        mapped = map_model_matrix(matrix, "STM32MP157CAC3T", RECIPE, ORDERING)
        self.assertEqual([(item.key, item.value) for item in mapped], [("peripheral:SPI", "6"), ("peripheral:UART", "4 + 4")])

    def test_vertically_merged_leaf_label_applies_to_continuation_value_rows(self) -> None:
        matrix = group(
            (
                16,
                [
                    ["Peripheral", "STM32F405ZG"],
                    ["12-bit ADC Number of channels", "3"],
                    ["", "24"],
                ],
            )
        )
        mapped = map_model_matrix(matrix, "STM32F405ZG", RECIPE, ORDERING)
        self.assertEqual(
            [(item.key, item.value, item.row_label) for item in mapped],
            [
                ("peripheral:ADC", "3", "12-bitADCNumberofchannels"),
                ("peripheral:ADC", "24", "12-bitADCNumberofchannels"),
            ],
        )

    def test_hierarchical_adc_rows_use_parent_path_for_channel_count(self) -> None:
        matrix = group(
            (
                20,
                [
                    ["Features", "", "STM32U585AI"],
                    ["ADC", "12-bit ADC", "1"],
                    ["", "14-bit ADC", "1"],
                    ["", "Nbr of channels", "24 / 22"],
                ],
            )
        )
        mapped = map_model_matrix(matrix, "STM32U585AI", RECIPE, ORDERING)
        reduced = reduce_values(mapped, RECIPE, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(reduced["peripherals"]["ADC"], {"count": 1, "channel_count": 24, "resolution": 14})


if __name__ == "__main__":
    unittest.main()
