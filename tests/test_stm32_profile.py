from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.profile import load_profile
from family_extractors.stm32 import configure, header_matches, is_primary_voltage_row, model_codes, ordering_flash_value, package_column, parse_packages, split_functions, target_model


PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "profiles" / "stm32_v1.json"


class Stm32ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profile = load_profile(PROFILE_PATH)
        rules = __import__("json").loads(Path(profile.rules_file).read_text(encoding="utf-8"))
        configure(profile, rules)

    def test_filename_model_and_series(self) -> None:
        profile = load_profile(PROFILE_PATH)
        self.assertEqual(target_model(Path("stm32f103c8.pdf"), profile), "STM32F103C8")
        self.assertEqual(profile.series_for_model("STM32F103C8"), "STM32F103")
        self.assertEqual(target_model(Path("STM32MP157_CAC3T.pdf"), profile), "STM32MP157CAC3T")
        self.assertEqual(target_model(Path("STM32F072_C8_2.pdf"), profile), "STM32F072C8")

    def test_model_codes(self) -> None:
        self.assertEqual(model_codes("STM32C031C4"), ("C", "4"))
        self.assertEqual(model_codes("STM32F103C8"), ("C", "8"))
        self.assertEqual(model_codes("STM32WB55CC"), ("C", "C"))

    def test_flash_density_is_resolved_from_document_ordering_codes(self) -> None:
        pages = ["unrelated", "Ordering information\nFlash memory size\nG = 1 Mbyte\nI = 2 Mbytes"]
        self.assertEqual(ordering_flash_value(pages, "STM32H562AG"), (1024, 2))
        self.assertEqual(ordering_flash_value(pages, "STM32H562AI"), (2048, 2))

    def test_primary_voltage_symbol_excludes_auxiliary_rails(self) -> None:
        locator = {"primary_voltage_symbols": ["VDD"]}
        self.assertTrue(is_primary_voltage_row(["VDD", "Standard operating voltage"], locator))
        self.assertFalse(is_primary_voltage_row(["VDD12", "Standard operating voltage"], locator))
        self.assertFalse(is_primary_voltage_row(["VDDUSB", "Standard operating voltage"], locator))

    def test_vfbga_is_not_truncated_to_bga(self) -> None:
        self.assertEqual(parse_packages("VFBGA169"), ["VFBGA169"])

    def test_package_column_prefers_exact_extracted_package(self) -> None:
        headers = ["751AGBFT", "163AGBFT", "844AGBFL"]
        self.assertEqual(package_column(headers, package_types=["TFBGA361"]), 1)

    def test_package_column_respects_configured_preference_order(self) -> None:
        headers = ["Pin Number", "UFQFPN48", "LQFP48"]
        self.assertEqual(package_column(headers, package_types=["LQFP48", "UFQFPN48"]), 2)

    def test_duplicate_package_variants_use_configured_unqualified_default(self) -> None:
        self.assertEqual(
            package_column(
                ["SMPS UFBGA169", "UFBGA169"],
                package_types=["UFBGA169"],
                qualifier_preferences=["unqualified", "smps"],
            ),
            1,
        )

    def test_rotated_pin_name_header_can_match_term_conjunction(self) -> None:
        self.assertTrue(header_matches("retfa...pin(functionreset)nameafter", ["pinname"], [["pin", "name"]]))
        self.assertFalse(header_matches("pinnumber", ["pinname"], [["pin", "name"]]))

    def test_legacy_pin_cell_line_breaks(self) -> None:
        profile = load_profile(PROFILE_PATH)
        value = "SPI2_MISO/\nUSART3_RTS(9)\nTIM1_CH2N (9)"
        self.assertEqual(
            split_functions(value, profile, "PB14"),
            ["SPI2_MISO", "USART3_RTS", "TIM1_CH2N"],
        )
        self.assertEqual(
            split_functions("TIM2_CH1_\nETR(9)/\nPA0", profile, "PA0"),
            ["TIM2_CH1_ETR"],
        )


if __name__ == "__main__":
    unittest.main()
