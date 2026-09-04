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

    def test_missing_af_is_kept_as_unresolved_not_guessed(self) -> None:
        resolved = resolve_pin_mux([definition("PA9", 30, "USART1_TX")], {})
        self.assertIsNone(resolved[0].af_code)

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


if __name__ == "__main__":
    unittest.main()
