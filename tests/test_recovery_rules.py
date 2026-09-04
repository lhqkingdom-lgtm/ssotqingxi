from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.recovery_rules import recover_configured_value


class RecoveryRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = [
            {
                "current_value_in": ["PA1"],
                "context_any": ["JTMS-SWDIO"],
                "replacement": "PA13",
            }
        ]

    def test_recovers_value_from_configured_semantic_context(self) -> None:
        self.assertEqual(
            recover_configured_value("PA1", {"functions": ["JTMS-SWDIO", "EVENTOUT"]}, self.rules),
            "PA13",
        )

    def test_does_not_change_a_real_value_without_matching_context(self) -> None:
        self.assertEqual(
            recover_configured_value("PA1", {"functions": ["USART2_RTS", "EVENTOUT"]}, self.rules),
            "PA1",
        )


if __name__ == "__main__":
    unittest.main()
