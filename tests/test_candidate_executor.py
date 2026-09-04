from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.candidate_executor import reduce_candidates, validate_hard_rules
from core.field_dictionary import load_table_recipe
from core.ir import AggregatedFieldIR, FieldCandidateIR


PROFILE = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
RULES = load_table_recipe(
    ROOT / "config" / "table_presets" / "stm32_feature_matrix.json",
    PROFILE["dictionary"]["aliases"],
)
PACKAGE_PATTERN = PROFILE["syntax"]["package_pattern"]


def candidate(field_group: str, field_name: str, value: str, cell: str, label: str | None = None) -> FieldCandidateIR:
    return FieldCandidateIR(
        candidate_id=f"FC-{cell}", entity_type="chip", entity_key="MCU1", field_group=field_group,
        field_name=field_name, label_path=(label or field_name,), row_label=label or field_name,
        raw_value=value, parsed_value=value, unit=None, condition=None, source_table_id="table-2",
        source_cell_ids=(cell,), evidence_pages=(3,), bboxes=((1.0, 2.0, 3.0, 4.0),),
    )


class CandidateExecutorTests(unittest.TestCase):
    def test_aggregate_keeps_all_contributing_cells(self) -> None:
        items = [
            candidate("fields", "ram_kb", "64", "c1", "SRAM (Kbytes)"),
            candidate("fields", "ram_kb", "16", "c2", "CCM RAM (Kbytes)"),
            candidate("peripherals", "UART", "3", "c3", "USART"),
            candidate("peripherals", "UART", "2", "c4", "UART"),
        ]
        reduced, aggregates = reduce_candidates(items, RULES, package_pattern=PACKAGE_PATTERN)
        self.assertEqual(reduced["fields"]["ram_kb"], 80)
        ram = next(item for item in aggregates if item.field_name == "ram_kb")
        uart = next(item for item in aggregates if item.field_name == "UART")
        self.assertEqual(ram.source_cell_ids, ("c1", "c2"))
        self.assertEqual(uart.source_cell_ids, ("c3", "c4"))

    def test_hard_rules_report_without_removing_candidates(self) -> None:
        bad = candidate("fields", "flash_kb", "unknown", "c1")
        bad = FieldCandidateIR(**{**bad.__dict__, "entity_key": ""})
        _, aggregates = reduce_candidates([bad], RULES, package_pattern=PACKAGE_PATTERN)
        issues = validate_hard_rules([bad], aggregates, RULES)
        self.assertEqual({item.rule_id for item in issues}, {"primary_key_empty", "numeric_unparseable"})
        self.assertEqual(bad.raw_value, "unknown")

    def test_nonpositive_pin_number_is_a_hard_issue(self) -> None:
        bad = candidate("fields", "pin_number", "0", "c1")
        issues = validate_hard_rules([bad], [], RULES)
        self.assertEqual([item.rule_id for item in issues], ["pin_number_nonpositive"])

    def test_minimum_greater_than_maximum_is_a_hard_issue(self) -> None:
        base = dict(
            entity_type="chip", entity_key="MCU1", field_group="fields", unit="V",
            candidate_ids=("c1",), source_cell_ids=("cell1",), evidence_pages=(3,),
            bboxes=((1.0, 2.0, 3.0, 4.0),), raw_values=("bad range",),
        )
        minimum = AggregatedFieldIR(aggregate_id="min", field_name="voltage_min", value=5, **base)
        maximum = AggregatedFieldIR(aggregate_id="max", field_name="voltage_max", value=3, **base)
        issues = validate_hard_rules([], [minimum, maximum], RULES)
        self.assertEqual([item.rule_id for item in issues], ["minimum_gt_maximum"])

    def test_contradictory_single_value_in_same_table_is_a_hard_issue(self) -> None:
        first = candidate("fields", "flash_kb", "16", "c1")
        second = candidate("fields", "flash_kb", "32", "c2")
        issues = validate_hard_rules([first, second], [], RULES)
        self.assertIn("contradictory_same_key", {item.rule_id for item in issues})


if __name__ == "__main__":
    unittest.main()
