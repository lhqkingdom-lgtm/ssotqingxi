from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from core.mapped_cell import MappedCell
from core.recipe_executor import parse_packages, reduce_values


RECIPE = json.loads((ROOT / "config" / "table_presets" / "stm32_feature_matrix.json").read_text(encoding="utf-8"))
PACKAGE_PATTERN = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))["patterns"]["package"]


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
        recipe = {"fields": {"max_freq_mhz": {"value_parser": "frequency_mhz", "reducer": "max"}}}
        reduced = reduce_values(mapped, recipe, package_pattern=r"LQFP\d+")
        self.assertEqual(reduced["fields"]["max_freq_mhz"], 650)

    def test_reversed_vertical_package_text_is_restored_only_by_package_pattern(self) -> None:
        pattern = r"(?:LQFP|TSSOP|UFQFPN|VFQFPN|TFBGA|UFBGA|LFBGA|WLCSP|BGA)\s*\d+"
        self.assertEqual(parse_packages("001PFQL 001AGBFT", pattern), ["TFBGA100", "LQFP100"])
        self.assertEqual(parse_packages("UFQFPN48 7 mm x 7 mm", pattern), ["UFQFPN48"])

    def test_adc_channel_count_is_not_adc_instance_count(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "sum_distinct_labels",
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
        self.assertEqual(result["peripherals"]["ADC"], {"count": 2, "resolution": 14})

    def test_combined_adc_instance_and_channel_row_keeps_first_count(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "sum_distinct_labels",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "2 10", 10, 3, "12-bit ADCs Number of channels")]
        result = reduce_values(mapped, recipe, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(result["peripherals"]["ADC"], {"count": 2, "resolution": 12})

    def test_adc_value_written_as_channel_count_means_one_instance(self) -> None:
        recipe = {
            "peripherals": {
                "ADC": {
                    "value_parser": "adc_instance_count",
                    "reducer": "sum_distinct_labels",
                    "resolution_from_label": True,
                }
            }
        }
        mapped = [MappedCell("peripheral:ADC", "12-bit ADC Number of channels", "13 channels", 13, 2, "12-bit ADC Number of channels")]
        result = reduce_values(mapped, recipe, package_pattern=r"LQFP\s*\d+")
        self.assertEqual(result["peripherals"]["ADC"], {"count": 1, "resolution": 12})

    def test_adc_yes_plus_channel_count_means_one_instance(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADC Number of channels", "Yes 16", 11, 2, "12-bit ADC Number of channels")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 1, "resolution": 12})

    def test_plural_adc_numeric_value_is_instance_count(self) -> None:
        mapped = [MappedCell("peripheral:ADC", "12-bit ADCs Number of channels", "5", 16, 1, "12-bit ADCs Number of channels")]
        result = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(result["peripherals"]["ADC"], {"count": 5, "resolution": 12})

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
        self.assertEqual(reduced["peripherals"]["ADC"], {"count": 4, "resolution": 12})

    def test_conditional_ram_uses_largest_explicit_capacity(self) -> None:
        mapped = [MappedCell("field:ram_kb", "SRAM (Kbyte)", "32 (with parity) or 36 (without parity)", 12, 11)]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 36)

    def test_explicit_sram_total_is_not_added_again_to_numbered_banks(self) -> None:
        mapped = [
            MappedCell("field:ram_kb", "Memory density SRAM", "128 K", 13, 2, "SRAM"),
            MappedCell("field:ram_kb", "Memory density SRAM1", "64 K", 13, 2, "SRAM1"),
            MappedCell("field:ram_kb", "Memory density SRAM2", "64 K", 13, 2, "SRAM2"),
        ]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 128)

    def test_slash_separated_uart_components_are_summed(self) -> None:
        mapped = [MappedCell("peripheral:UART", "USART/UART", "4/2", 16, 5)]
        reduced = reduce_values(mapped, RECIPE, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["peripherals"]["UART"]["count"], 6)


if __name__ == "__main__":
    unittest.main()
