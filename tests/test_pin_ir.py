from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.pin_ir import PinDefinitionIR, pin_identity_issues, resolve_pin_mux


def definition(pin_name: str, pin_number: int, function: str) -> PinDefinitionIR:
    return PinDefinitionIR(
        chip_id="MCU1", package_type="LQFP64", pin_name=pin_name,
        pin_number=pin_number, function_name=function, default_function=pin_name,
        source_table_id="pin-table", source_cell_id="pin-cell", page=10, bbox=None,
    )


class PinIRTests(unittest.TestCase):
    def test_definition_and_af_are_joined_by_normalized_business_identity(self) -> None:
        af = SimpleNamespace(
            af_code="AF7", source_table_id="af-table", source_cell_id="af-cell", page=20
        )
        resolved = resolve_pin_mux([definition("PA9", 30, "USART1_TX")], {("PA9", "USART1_TX"): af})
        self.assertEqual(resolved[0].af_code, "AF7")
        self.assertEqual(resolved[0].af_source_cell_id, "af-cell")
        self.assertEqual(resolved[0].output_row()["function_name"], "USART1_TX")
        self.assertEqual(resolved[0].output_row()["package_type"], "LQFP64")

    def test_missing_af_is_kept_as_unresolved_not_guessed(self) -> None:
        resolved = resolve_pin_mux([definition("PA9", 30, "USART1_TX")], {})
        self.assertIsNone(resolved[0].af_code)

    def test_explicit_function_code_survives_without_separate_af_matrix(self) -> None:
        item = PinDefinitionIR(**{**definition("PA0", 1, "UART0_TX").__dict__, "af_code": "2"})
        resolved = resolve_pin_mux([item], {})
        self.assertEqual(resolved[0].af_code, "2")

    def test_wrapped_fragments_join_only_when_af_proves_the_identity(self) -> None:
        definitions = [
            definition("PA7", 31, "ETH_R"),
            definition("PA7", 31, "MII_CRS_DV"),
            definition("PA7", 31, "EVENTOUT"),
        ]
        lookup = {
            ("PA7", "ETH_RMII_CRS_DV"): SimpleNamespace(af_code="AF11"),
            ("PA7", "EVENTOUT"): SimpleNamespace(af_code="AF15"),
        }
        resolved = resolve_pin_mux(definitions, lookup)
        self.assertEqual(
            [item.definition.function_name for item in resolved],
            ["ETH_RMII_CRS_DV", "EVENTOUT"],
        )

    def test_contradictory_pin_identity_is_reported_not_rewritten(self) -> None:
        definitions = [definition("PA1", 10, "A"), definition("PA1", 11, "B")]
        issues = pin_identity_issues(definitions)
        self.assertEqual(issues[0]["kind"], "pin_name_multiple_numbers")
        self.assertEqual(definitions[0].pin_name, "PA1")

    def test_documented_logical_aliases_may_share_one_physical_lead(self) -> None:
        definitions = [definition("PA1", 9, "ADC_IN1"), definition("PA2", 9, "ADC_IN2")]
        self.assertEqual(pin_identity_issues(definitions), [])

    def test_package_dimension_prevents_false_cross_package_conflict(self) -> None:
        first = definition("PA1", 9, "ADC_IN1")
        second = PinDefinitionIR(**{**first.__dict__, "package_type": "LQFP48", "pin_number": 15})
        self.assertEqual(pin_identity_issues([first, second]), [])


if __name__ == "__main__":
    unittest.main()
