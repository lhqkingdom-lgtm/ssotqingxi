from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from core.mapped_cell import MappedCell
from core.field_dictionary import load_table_recipe
from core.recipe_executor import parse_packages, reduce_values


import json

PROFILE = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
RECIPE = load_table_recipe(
    ROOT / "config" / "table_presets" / "stm32_feature_matrix.json",
    PROFILE["dictionary"]["aliases"],
)
PACKAGE_PATTERN = PROFILE["syntax"]["package_pattern"]


class RecipeExecutorTests(unittest.TestCase):
    def test_model_labelled_frequency_selects_the_target_variant(self) -> None:
        mapped = [
            MappedCell(
                "field:max_freq_mhz",
                "CPU Caches size Frequency Frequency",
                "STM32MP157C: 2 × 650 MHz STM32MP157F: 2 × 800 MHz",
                16,
                3,
                entity_key="STM32MP157CAC3T",
            )
        ]
        reduced = reduce_values(mapped, RECIPE, package_pattern=r"LQFP\d+")
        self.assertEqual(reduced["fields"]["max_freq_mhz"], 650)

    def test_model_conditioned_capacity_selects_matching_density_code(self) -> None:
        value = "1 Mbyte for xxxG devices\n2 Mbyte for xxxI devices"
        mapped_g = [MappedCell("field:flash_kb", "Flash memory", value, 15, 6, entity_key="STM32L4R5ZG")]
        mapped_i = [MappedCell("field:flash_kb", "Flash memory", value, 15, 6, entity_key="STM32L4R5ZI")]
        self.assertEqual(reduce_values(mapped_g, RECIPE, package_pattern=PACKAGE_PATTERN)["fields"]["flash_kb"], 1024)
        self.assertEqual(reduce_values(mapped_i, RECIPE, package_pattern=PACKAGE_PATTERN)["fields"]["flash_kb"], 2048)

    def test_reversed_vertical_package_text_is_restored_only_by_package_pattern(self) -> None:
        pattern = r"(?:LQFP|TSSOP|UFQFPN|VFQFPN|TFBGA|UFBGA|LFBGA|WLCSP|BGA)\s*\d+"
        self.assertEqual(parse_packages("001PFQL 001AGBFT", pattern), ["TFBGA100", "LQFP100"])
        self.assertEqual(parse_packages("UFQFPN48 7 mm x 7 mm", pattern), ["UFQFPN48"])

    def test_adc_channel_count_ignores_adc_instance_rows(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_channel_count",
                    "reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [
            MappedCell("peripheral:ADC", "ADC 12-bit ADC", "1", 20, 8, "12-bit ADC"),
            MappedCell("peripheral:ADC", "ADC 14-bit ADC", "1", 20, 8, "14-bit ADC"),
            MappedCell("peripheral:ADC", "ADC Nbr of channels", "24 / 24", 20, 8, "Nbr of channels"),
        ]
        result = reduce_values(mapped, recipe, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(result["peripherals"]["ADC"], {"count": 24, "resolution": 14})

    def test_combined_adc_instance_and_channel_row_keeps_first_count(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_channel_count",
                    "reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "2 10", 10, 3, "12-bit ADCs Number of channels")]
        result = reduce_values(mapped, recipe, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(result["peripherals"]["ADC"], {"count": 10, "resolution": 12})

    def test_channel_summary_with_explicit_channel_suffix_keeps_leading_instances(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "max",
                    "channel_value_parser": "adc_channel_count",
                    "channel_reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell(
            "peripheral:ADC",
            "12-bit ADC / Number of channels",
            "2\n10 channels",
            10,
            3,
            "Number of channels",
        )]

        result = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(
            result["peripherals"]["ADC"],
            {"count": 2, "channel_count": 10, "resolution": 12},
        )

    def test_adc_value_written_as_channel_count_means_one_instance(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_channel_count",
                    "reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell("peripheral:ADC", "12-bit ADC Number of channels", "13 channels", 13, 2, "12-bit ADC Number of channels")]
        result = reduce_values(mapped, recipe, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(result["peripherals"]["ADC"], {"count": 13, "resolution": 12})

    def test_adc_yes_plus_channel_count_uses_channel_count(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADC Number of channels", "Yes 16", 11, 2, "12-bit ADC Number of channels")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 1, "channel_count": 16, "resolution": 12})

    def test_channel_label_with_single_numeric_value_uses_that_count(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "5", 16, 1, "12-bit ADCs Number of channels")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": None, "channel_count": 5, "resolution": 12})

    def test_plain_adc_instance_row_is_not_used_as_channel_count(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADCs", "3", 16, 1, "12-bit ADCs")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 3, "resolution": 12})

    def test_distinct_adc_resolution_rows_sum_instances_without_duplicate_rows(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "sum_distinct_labels",
                    "channel_value_parser": "adc_channel_count",
                    "channel_reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [
            MappedCell("peripheral:ADC", "ADC / 12-bit ADC", "1", 8, 2, "12-bit ADC"),
            MappedCell("peripheral:ADC", "ADC / 14-bit ADC", "1", 8, 2, "14-bit ADC"),
            MappedCell("peripheral:ADC", "ADC / 14-bit ADC", "1", 9, 2, "14-bit ADC"),
        ]

        result = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(result["peripherals"]["ADC"]["count"], 2)
        self.assertEqual(result["peripherals"]["ADC"]["resolution"], 14)

    def test_adc_summary_does_not_turn_resolution_or_channel_count_into_instances(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "sum",
                    "channel_value_parser": "adc_channel_count",
                    "channel_reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [
            MappedCell("peripheral:ADC", "内置12位ADC", "内置12位ADC", 1, 0, "内置12位ADC"),
            MappedCell("peripheral:ADC", "支持26路12位SAR ADC", "支持26路12位SAR ADC", 8, 0, "支持26路12位SAR ADC"),
        ]
        result = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(
            result["peripherals"]["ADC"],
            {"count": None, "channel_count": 26, "resolution": 12},
        )

    def test_configured_bare_adc_role_moves_unlabelled_matrix_value_to_channels(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "channel_value_parser": "adc_channel_count",
                    "bare_numeric_role": "channel_count",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell("peripheral:ADC", "12位 ADC", "7", 7, 1, "12位 ADC")]
        result = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(
            result["peripherals"]["ADC"],
            {"count": None, "channel_count": 7, "resolution": 12},
        )

    def test_external_plus_internal_adc_channels_keep_external_count(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADC channels (ext. + int.)", "19 + 2", 9, 8, "12-bit ADC channels (ext. + int.)")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": None, "channel_count": 19, "resolution": 12})

    def test_leading_adc_instance_is_kept_before_parenthesized_channel_breakdown(self) -> None:
        mapped = [MappedCell(
            "peripheral:ADC",
            "12-bit ADC (number of channels)",
            "1 (10 ext. + 3 int.)",
            11,
            1,
            "12-bit ADC (number of channels)",
        )]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(
            result["peripherals"]["ADC"],
            {"count": 1, "channel_count": 10, "resolution": 12},
        )

    def test_trailing_numeric_footnote_is_not_an_adc_business_value(self) -> None:
        mapped = [MappedCell(
            "peripheral:ADC",
            "12-bit synchronized ADC Number of channels",
            "1\n16(2)",
            12,
            1,
            "12-bit synchronized ADC Number of channels",
        )]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(
            result["peripherals"]["ADC"],
            {"count": 1, "channel_count": 16, "resolution": 12},
        )

    def test_adc_instances_and_channels_are_distinct_outputs(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "max",
                    "channel_value_parser": "adc_channel_count",
                    "channel_reducer": "max",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [
            MappedCell("peripheral:ADC", "12-bit ADCs", "4", 3, 1, "12-bit ADCs"),
            MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "22 channels", 3, 1, "Number of channels"),
        ]
        result = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 4, "channel_count": 22, "resolution": 12})

    def test_vertically_merged_adc_label_separates_bare_instance_and_qualified_channels(self) -> None:
        mapped = [
            MappedCell("peripheral:ADC", "16-bit ADCs Number of channels", "3", 21, 1, "16-bit ADCs Number of channels"),
            MappedCell("peripheral:ADC", "16-bit ADCs Number of channels", "Up to 36", 21, 1, "16-bit ADCs Number of channels"),
        ]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 3, "channel_count": 36, "resolution": 16})

    def test_package_pin_numbers_are_not_adc_channel_counts(self) -> None:
        mapped = [
            MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "5", 16, 1, "12-bit ADCs Number of channels"),
            MappedCell(
                "peripheral:ADC",
                "12-bit ADCs Number of channels",
                "20 in LQFP48 21 in UFQFPN48",
                16,
                1,
                "12-bit ADCs Number of channels",
            ),
        ]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 5, "channel_count": 21, "resolution": 12})

    def test_two_bare_values_in_one_merged_adc_path_are_instance_and_channels(self) -> None:
        mapped = [
            MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "5", 16, 1, "12-bit ADCs Number of channels"),
            MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "42", 16, 1, "12-bit ADCs Number of channels"),
        ]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 5, "channel_count": 42, "resolution": 12})

    def test_named_adc_channel_classes_preserve_instances_and_sum_channels(self) -> None:
        label = "16-bit ADCs Number of Direct channels Number of Fast channels Number of Slow channels"
        mapped = [
            MappedCell("peripheral:ADC", label, "3", 21, 1, label),
            MappedCell("peripheral:ADC", label, "2\n3\n11", 21, 1, label),
        ]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 3, "channel_count": 16, "resolution": 16})

    def test_shared_adc_label_pairs_instance_row_before_channel_row(self) -> None:
        label = "12-bit ADC Number of channels"
        mapped = [
            MappedCell("peripheral:ADC", label, "3", 18, 1, label),
            MappedCell("peripheral:ADC", label, "24", 18, 1, label),
        ]

        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(result["peripherals"]["ADC"], {"count": 3, "channel_count": 24, "resolution": 12})

    def test_package_alternative_adc_channels_keep_maximum_projection(self) -> None:
        label = "12-bit synchronized ADC Number of channels"
        mapped = [
            MappedCell("peripheral:ADC", label, "1", 11, 2, label),
            MappedCell("peripheral:ADC", label, "20/19 channels(1)", 11, 2, label),
        ]

        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(result["peripherals"]["ADC"], {"count": 1, "channel_count": 20, "resolution": 12})

    def test_split_ram_and_serial_rows_are_aggregated(self) -> None:
        mapped = [
            MappedCell("field:flash_kb", "Flash (Kbytes)", "512", 13, 3),
            MappedCell("field:ram_kb", "SRAM (Kbytes)", "64", 13, 3),
            MappedCell("field:ram_kb", "CCM RAM (Kbytes)", "16", 13, 3),
            MappedCell("peripheral:UART", "USART", "3", 13, 3),
            MappedCell("peripheral:UART", "UART", "2", 13, 3),
            MappedCell("peripheral:ADC", "12-bit ADCs", "4 22 channels", 13, 3, "12-bit ADCs"),
        ]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 80)
        self.assertEqual(reduced["peripherals"]["UART"]["count"], 5)
        self.assertEqual(reduced["peripherals"]["ADC"], {"count": 4, "channel_count": 22, "resolution": 12})

    def test_conditional_ram_uses_largest_explicit_capacity(self) -> None:
        mapped = [MappedCell("field:ram_kb", "SRAM (Kbyte)", "32 (with parity) or 36 (without parity)", 12, 11)]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 36)

    def test_value_cell_unit_overrides_parent_header_unit(self) -> None:
        mapped = [MappedCell("field:ram_kb", "Memory density (bytes) SRAM", "96 K", 14, 12, "SRAM")]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 96)

    def test_trailing_cell_unit_applies_to_aggregate_and_components(self) -> None:
        mapped = [MappedCell("field:ram_kb", "SRAM", "128 (80 + 16 + 32) KB", 15, 2, "SRAM")]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 128)

    def test_long_trailing_unit_alias_applies_to_total_and_components(self) -> None:
        mapped = [MappedCell("field:ram_kb", "SRAM System", "640 (256 + 64 + 320) Kbytes", 15, 2, "System")]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 640)

    def test_explicit_sram_total_is_not_added_again_to_numbered_banks(self) -> None:
        mapped = [
            MappedCell("field:ram_kb", "Memory density SRAM", "128 K", 13, 2, "SRAM"),
            MappedCell("field:ram_kb", "Memory density SRAM1", "64 K", 13, 2, "SRAM1"),
            MappedCell("field:ram_kb", "Memory density SRAM2", "64 K", 13, 2, "SRAM2"),
        ]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 128)

    def test_explicit_sram_total_in_parent_label_wins_over_child_regions(self) -> None:
        mapped = [
            MappedCell("field:ram_kb", "Embedded SRAM (808 Kbytes) CPU system", "384 Kbytes", 7, 3, "CPU system"),
            MappedCell("field:ram_kb", "Embedded SRAM (808 Kbytes) MCU system", "256 Kbytes", 7, 3, "MCU system"),
            MappedCell("field:ram_kb", "Embedded SRAM (808 Kbytes) MCU retention", "128 Kbytes", 7, 3, "MCU retention"),
            MappedCell("field:ram_kb", "Embedded SRAM (808 Kbytes) SmartRun domain", "32 Kbytes", 7, 3, "SmartRun domain"),
        ]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 808)

    def test_slash_separated_uart_components_are_summed(self) -> None:
        mapped = [MappedCell("peripheral:UART", "USART/UART", "4/2", 16, 5)]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["peripherals"]["UART"]["count"], 6)

    def test_additive_peripheral_counts_continue_after_bracketed_footnote(self) -> None:
        recipe = {
            "peripherals": {
                "SPI": {"value_parser": "sum_explicit_addends", "reducer": "max"}
            }
        }
        mapped = [
            MappedCell(
                "peripheral:SPI",
                "Communication interfaces / SPI",
                "2 [1] + 4 extra through USARTs",
                10,
                1,
            )
        ]

        reduced = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(reduced["peripherals"]["SPI"]["count"], 6)

    def test_slash_qualified_mode_count_is_not_added_to_interface_count(self) -> None:
        recipe = {
            "peripherals": {
                "SPI": {"value_parser": "sum_explicit_addends", "reducer": "max"}
            }
        }
        mapped = [
            MappedCell(
                "peripheral:SPI",
                "Communication interfaces / SPI / I2S",
                "6/3 (simplex)(2)",
                17,
                1,
            )
        ]

        reduced = reduce_values(mapped, recipe, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(reduced["peripherals"]["SPI"]["count"], 6)

    def test_merged_serial_subrows_preserve_all_aligned_counts(self) -> None:
        mapped = [
            MappedCell(
                "peripheral:UART",
                "Communication interfaces / USART(1)/UART UART LPUART",
                "3/2 (2)\n2\n1",
                19,
                2,
            )
        ]

        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(reduced["peripherals"]["UART"]["count"], 6)

    def test_package_qualified_uart_alternatives_ignore_package_pin_digits(self) -> None:
        mapped = [
            MappedCell("peripheral:UART", "USART", "3", 15, 2),
            MappedCell(
                "peripheral:UART",
                "UART",
                "0 in QFP48 1 in QFN48",
                15,
                2,
            ),
            MappedCell("peripheral:UART", "LPUART", "1", 15, 2),
        ]

        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)

        self.assertEqual(reduced["peripherals"]["UART"]["count"], 5)


if __name__ == "__main__":
    unittest.main()
