from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.profile import load_profile
from family_extractors.stm32 import configure, configured_output_value, header_matches, infer_pin_header_depth, is_primary_voltage_row, missing_review_fields, model_codes, ordering_flash_value, package_column, parse_packages, split_functions, target_model


PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "profiles" / "stm32_v1.json"


class Stm32ProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        profile = load_profile(PROFILE_PATH)
        configure(profile)

    def test_filename_model_and_series(self) -> None:
        profile = load_profile(PROFILE_PATH)
        self.assertEqual(target_model(Path("stm32f103c8.pdf"), profile), "STM32F103C8")
        self.assertEqual(profile.series_for_model("STM32F103C8"), "STM32F103")
        self.assertEqual(target_model(Path("STM32MP157_CAC3T.pdf"), profile), "STM32MP157CAC3T")
        self.assertEqual(target_model(Path("STM32MP157_A.pdf"), profile), "STM32MP157A")
        self.assertEqual(target_model(Path("STM32MP257_A.pdf"), profile), "STM32MP257A")
        self.assertEqual(target_model(Path("STM32F072_C8_2.pdf"), profile), "STM32F072C8")
        self.assertEqual(target_model(Path("STM32L552_CET3QTR.pdf"), profile), "STM32L552CE")
        self.assertEqual(target_model(Path("STM32WB55_CEQ7XXX.pdf"), profile), "STM32WB55CE")

    def test_model_codes(self) -> None:
        self.assertEqual(model_codes("STM32C031C4"), ("C", "4"))
        self.assertEqual(model_codes("STM32F103C8"), ("C", "8"))
        self.assertEqual(model_codes("STM32WB55CC"), ("C", "C"))

    def test_generic_mpu_family_name_is_not_misread_as_ordering_codes(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported STM32 model code"):
            model_codes("STM32MP157C")
        self.assertIsNone(ordering_flash_value(["Ordering information\nC = 256 Kbytes"], "STM32MP157C"))

    def test_flash_density_is_resolved_from_document_ordering_codes(self) -> None:
        pages = ["unrelated", "Ordering information\nFlash memory size\nG = 1 Mbyte\nI = 2 Mbytes"]
        self.assertEqual(ordering_flash_value(pages, "STM32H562AG"), (1024, 2))
        self.assertEqual(ordering_flash_value(pages, "STM32H562AI"), (2048, 2))

    def test_primary_voltage_symbol_excludes_auxiliary_rails(self) -> None:
        locator = {"primary_voltage_symbols": ["VDD"]}
        self.assertTrue(is_primary_voltage_row(["VDD", "Standard operating voltage"], locator))
        self.assertFalse(is_primary_voltage_row(["VDD12", "Standard operating voltage"], locator))
        self.assertFalse(is_primary_voltage_row(["VDDUSB", "Standard operating voltage"], locator))

    def test_output_vocabulary_matches_template(self) -> None:
        self.assertEqual(configured_output_value("memory_type", "ram", "SRAM"), "SRAM_Internal")
        self.assertEqual(configured_output_value("electrical_param", "voltage", "VDD_operating"), "VDD")
        self.assertEqual(configured_output_value("electrical_param", "temperature", "T_ambient"), "T_operating")

    def test_missing_chip_fields_are_configured_as_nonblocking_review_items(self) -> None:
        row = {
            "flash_kb": None,
            "ram_kb": 704,
            "max_freq_mhz": 650,
            "package_types": [],
            "voltage_min": None,
            "voltage_max": None,
        }
        self.assertEqual(
            missing_review_fields("chips", row),
            ["flash_kb", "package_types", "voltage_min", "voltage_max"],
        )

    def test_vfbga_is_not_truncated_to_bga(self) -> None:
        self.assertEqual(parse_packages("VFBGA169"), ["VFBGA169"])

    def test_package_column_prefers_exact_extracted_package(self) -> None:
        headers = ["751AGBFT", "163AGBFT", "844AGBFL"]
        self.assertEqual(package_column(headers, package_types=["TFBGA361"]), 1)

    def test_package_column_respects_configured_preference_order(self) -> None:
        headers = ["Pin Number", "UFQFPN48", "LQFP48"]
        self.assertEqual(package_column(headers, package_types=["LQFP48", "UFQFPN48"]), 2)

    def test_package_column_accepts_one_adjacent_transposition_from_vertical_text(self) -> None:
        headers = ["Pin Number", "001FQPL", "821FQPL"]
        self.assertEqual(package_column(headers, package_types=["LQFP100"]), 1)
        self.assertEqual(package_column(headers, package_types=["LQFP128"]), 2)

    def test_package_column_does_not_use_broad_fuzzy_matching(self) -> None:
        self.assertIsNone(package_column(["LPFQ100"], package_types=["LQFP100"]))

    def test_pin_header_depth_is_inferred_from_roles_and_package_column(self) -> None:
        rows = [
            ["Pin", "Pin", "Pin name", "Alternate functions"],
            ["LQFP64", "LQFP100", "", ""],
            ["1", "2", "PA0", "TIM2_CH1"],
        ]
        self.assertEqual(
            infer_pin_header_depth(
                rows,
                header_terms=("pin",),
                header_term_groups=(("name",),),
                package_targets=("LQFP100",),
                pin_count=None,
            ),
            2,
        )

    def test_pin_header_depth_has_no_fixed_row_fallback(self) -> None:
        rows = [
            ["Pin", "Pin name", "Alternate functions"],
            ["number", "", ""],
            ["1", "PA0", "TIM2_CH1"],
        ]
        self.assertIsNone(
            infer_pin_header_depth(
                rows,
                header_terms=("pin",),
                header_term_groups=(("name",),),
                package_targets=("LQFP100",),
                pin_count=None,
            )
        )

    def test_duplicate_package_variants_use_configured_unqualified_default(self) -> None:
        self.assertEqual(
            package_column(
                ["SMPS UFBGA169", "UFBGA169"],
                package_types=["UFBGA169"],
                qualifier_preferences=["unqualified", "smps"],
            ),
            1,
        )

    def test_ambiguous_package_columns_have_no_first_column_fallback(self) -> None:
        self.assertIsNone(
            package_column(
                ["UFBGA169 option A", "UFBGA169 option B"],
                package_types=["UFBGA169"],
                qualifier_preferences=["unqualified", "smps"],
            )
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
        self.assertEqual(
            split_functions("USART2_CTS/USART\n2_NSS", profile, "PA0"),
            ["USART2_CTS", "USART2_NSS"],
        )
        self.assertEqual(
            split_functions("TIM8_BKIN\n_COMP12", profile, "PA6"),
            ["TIM8_BKIN_COMP12"],
        )
        self.assertEqual(
            split_functions("RTC_TAMP3/W\nKUP6", profile, "PC1"),
            ["RTC_TAMP3", "WKUP6"],
        )
        self.assertEqual(
            split_functions("OPAMP1_VOU\nT, COMP1_INM", profile, "PC4"),
            ["OPAMP1_VOUT", "COMP1_INM"],
        )

    def test_af_matrix_line_breaks_are_visual_wrapping(self) -> None:
        profile = load_profile(PROFILE_PATH)
        self.assertEqual(
            split_functions("EVENT\nOUT", profile, "PE2", wrapped_cell=True),
            ["EVENTOUT"],
        )
        self.assertEqual(
            split_functions("SAI2_MCL\nK_B", profile, "PE6", wrapped_cell=True),
            ["SAI2_MCLK_B"],
        )

if __name__ == "__main__":
    unittest.main()
