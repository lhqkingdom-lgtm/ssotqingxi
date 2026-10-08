from __future__ import annotations

import sys
import unittest
import re
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.profile import load_profile
from family_extractors.stm32 import configure, configured_output_value, header_matches, infer_pin_header_depth, is_primary_voltage_row, missing_review_fields, model_codes, ordering_flash_value, package_column, parse_packages, preferred_model_voltage, split_functions, target_model
from topologies.pin_definition import extract_pin_definitions


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
        self.assertEqual(target_model(Path("stm32l151c6-a.pdf"), profile), "STM32L151C6-A")

    def test_wildcard_headers_are_not_concrete_models(self) -> None:
        profile = load_profile(PROFILE_PATH)
        self.assertEqual(profile.model_tokens("STM32F102C4", exact_only=True), ["STM32F102C4"])
        self.assertEqual(profile.model_tokens("STM32F102Cx", exact_only=True), [])

    def test_model_codes(self) -> None:
        self.assertEqual(model_codes("STM32C031C4"), ("C", "4"))
        self.assertEqual(model_codes("STM32F103C8"), ("C", "8"))
        self.assertEqual(model_codes("STM32WB55CC"), ("C", "C"))
        self.assertEqual(model_codes("STM32G0B1KB"), ("K", "B"))
        self.assertEqual(model_codes("STM32L4R5ZI"), ("Z", "I"))
        self.assertEqual(
            model_codes(
                "STM32N657X0H3Q",
                {"pin_count_codes": {"X": 264}, "flash_code_kb": {"0": 0}},
            ),
            ("X", "0"),
        )

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

    def test_model_specific_voltage_precedes_document_wide_operating_range(self) -> None:
        self.assertEqual(
            preferred_model_voltage(
                {"voltage_min": 1.71, "voltage_max": 3.6},
                {"voltage_min": 1.62, "voltage_max": 3.6},
            ),
            (1.71, 3.6),
        )

    def test_document_wide_voltage_is_used_only_as_fallback(self) -> None:
        self.assertEqual(
            preferred_model_voltage({}, {"voltage_min": 1.62, "voltage_max": 3.6}),
            (1.62, 3.6),
        )

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

    def test_package_column_accepts_one_missing_vertical_prefix_glyph_only_with_exact_count(self) -> None:
        self.assertEqual(package_column(["001GBFU"], package_types=["UFBGA100"]), 0)
        self.assertIsNone(package_column(["441GBFU"], package_types=["UFBGA100"]))

    def test_bga_family_qualifiers_are_not_conflated(self) -> None:
        self.assertIsNone(package_column(["LFBGA100"], package_types=["UFBGA100"]))
        self.assertEqual(package_column(["BGA100"], package_types=["UFBGA100"]), 0)

    def test_package_column_prefers_simple_qualifier_over_compound_variant(self) -> None:
        self.assertEqual(
            package_column(
                ["WLCSP81_ext-smps", "WLCSP81_smps"],
                package_types=["WLCSP81"],
                qualifier_preferences=["unqualified", "gp", "smps", "n"],
            ),
            1,
        )

    def test_package_column_does_not_use_broad_fuzzy_matching(self) -> None:
        self.assertIsNone(package_column(["LPFQ100"], package_types=["LQFP100"]))

    def test_bga_header_can_use_generic_kind_with_exact_ball_count(self) -> None:
        self.assertEqual(
            package_column(
                ["UBGA132_SMPS", "BGA132", "UFBGA169"],
                package_types=["UFBGA132"],
                qualifier_preferences=["unqualified", "smps"],
            ),
            1,
        )

    def test_bga_header_does_not_cross_ball_counts(self) -> None:
        self.assertIsNone(package_column(["BGA144"], package_types=["UFBGA132"]))

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

    def test_pin_header_depth_accepts_number_name_header_without_repeated_pin_caption(self) -> None:
        rows = [
            ["Number", None, "Name (function after reset)", "Alternate functions"],
            ["84PFQL", "001AGBFU", None, None],
            ["1", "C1", "PE2", "TRACECLK"],
        ]
        self.assertEqual(
            infer_pin_header_depth(
                rows,
                header_terms=("alternatefunctions",),
                header_term_groups=(),
                package_targets=("LQFP48",),
                pin_count=48,
            ),
            2,
        )

    def test_package_pattern_keeps_power_variant_outside_physical_package_identity(self) -> None:
        profile = load_profile(PROFILE_PATH)
        match = profile.package_re.search("UFQFPN48 SMPS")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0), "UFQFPN48")

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

    def test_short_variant_qualifier_is_tokenized_not_matched_inside_package_name(self) -> None:
        self.assertEqual(
            package_column(
                ["LQFP64 - GP", "LQFP64 - N", "UFBGA64 - N"],
                package_types=["LQFP64"],
                qualifier_preferences=["unqualified", "gp", "smps", "n"],
            ),
            0,
        )

    def test_short_variant_qualifier_can_follow_numeric_package_count(self) -> None:
        self.assertEqual(
            package_column(
                ["VFBGA169", "VFBGA169GP"],
                package_types=["VFBGA169"],
                qualifier_preferences=["gp", "unqualified"],
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

    def test_full_header_path_binds_repeated_package_to_target_product_group(self) -> None:
        rows = [
            ["Pin number", "", "", "", "Pin name", "Alternate functions"],
            ["STM32L4R5xxx", "", "STM32L4R9xxx", "", "", ""],
            ["LQFP100", "", "LQFP100", "", "", ""],
            ["10", "", "90", "", "PA0", "TIM2_CH1"],
        ]

        class FakeTable:
            bbox = (0, 0, 100, 100)
            table_id = "pin-table"

            def extract(self):
                return rows

            def column_header_paths(self, depth):
                paths = {
                    0: ("Pin number", "STM32L4R5xxx", "LQFP100"),
                    2: ("Pin number", "STM32L4R9xxx", "LQFP100"),
                    4: ("Pin name",),
                    5: ("Alternate functions",),
                }
                return {column: path[:depth] for column, path in paths.items()}

            def cell_id(self, row, column):
                return f"cell-{row}-{column}"

        recipe = {
            "required_header_terms": ["alternatefunctions"],
            "required_header_groups": [["pin", "name"]],
            "pin_name_aliases": ["pinname"],
            "pin_name_alias_groups": [["pin", "name"]],
            "pin_name_exclude": ["pinnumber"],
            "pin_name_preference": "rightmost",
            "function_aliases": ["alternatefunctions"],
            "default_function_aliases": ["mainfunction"],
            "column_qualifier_preferences": ["unqualified", "smps"],
        }
        pdf = SimpleNamespace(pages=[SimpleNamespace(find_tables=lambda: [FakeTable()])])
        extracted = extract_pin_definitions(
            pdf,
            model="STM32L4R5VI",
            pin_pattern=re.compile(r"P[A-Z]\d+"),
            package_pattern=re.compile(r"(?:LQFP|UFBGA)\d+(?:\+\d+)?"),
            normalize_function=lambda value: value,
            series_identity="STM32L4R5",
            recipe=recipe,
            package_types=["LQFP100"],
        )
        self.assertEqual(extracted[0]["rows"][0].pin_number, 10)

    def test_split_parent_pin_and_child_name_header_uses_complete_depth(self) -> None:
        rows = [
            ["Pin (UFQFPN48)", "", "Alternate functions"],
            ["Number", "Name (function after reset)", "AF0"],
            ["1", "PA0", "TIM2_CH1"],
        ]

        class FakeTable:
            bbox = (0, 0, 100, 100)
            table_id = "split-pin-table"

            def extract(self):
                return rows

            def column_header_paths(self, depth):
                paths = {
                    0: ("Pin (UFQFPN48)", "Number"),
                    1: ("Pin (UFQFPN48)", "Name (function after reset)"),
                    2: ("Alternate functions", "AF0"),
                }
                return {column: path[:depth] for column, path in paths.items()}

            def cell_id(self, row, column):
                return f"cell-{row}-{column}"

        recipe = {
            "required_header_terms": ["alternatefunctions"],
            "required_header_groups": [["pin", "name"]],
            "pin_name_aliases": ["pinname", "name(functionafterreset)"],
            "pin_name_alias_groups": [["pin", "name"]],
            "pin_name_exclude": ["pinnumber", "ufqfpn"],
            "pin_name_preference": "rightmost",
            "function_aliases": ["alternatefunctions"],
            "default_function_aliases": ["mainfunction"],
            "column_qualifier_preferences": ["unqualified"],
        }
        pdf = SimpleNamespace(pages=[SimpleNamespace(find_tables=lambda: [FakeTable()])])
        extracted = extract_pin_definitions(
            pdf,
            model="STM32WB50CG",
            pin_pattern=re.compile(r"P[A-Z]\d+"),
            package_pattern=re.compile(r"(?:UFQFPN|UFBGA)\d+(?:\+\d+)?"),
            normalize_function=lambda value: value,
            series_identity="STM32WB50",
            recipe=recipe,
            package_types=["UFQFPN48"],
        )
        self.assertEqual(extracted[0]["rows"][0].pin_name, "PA0")

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
