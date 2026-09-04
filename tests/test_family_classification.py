from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.pipeline import load_family, select_family


ROOT = Path(__file__).resolve().parents[1]


class FamilyClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ca = load_family(ROOT / "config" / "profiles" / "ca_miai_v1.json")
        cls.stm32 = load_family(ROOT / "config" / "profiles" / "stm32_v1.json")
        cls.families = [cls.ca, cls.stm32]

    def test_filename_and_text_select_ca(self) -> None:
        selected = select_family(Path("MiAi-6099A1.pdf"), ["基本特性 芯片型号"], self.families)
        self.assertEqual(selected.profile.profile_id, "ca_miai_v1")

    def test_ca_family_accepts_8051_and_arm_pin_designators(self) -> None:
        self.assertIsNotNone(self.ca.profile.pin_re.fullmatch("P0.0"))
        self.assertIsNotNone(self.ca.profile.pin_re.fullmatch("PA0"))

    def test_filename_and_text_select_stm32(self) -> None:
        selected = select_family(Path("stm32f103c8.pdf"), ["STM32 device features and peripheral counts"], self.families)
        self.assertEqual(selected.profile.profile_id, "stm32_v1")

    def test_stm32_evaluation_board_schematic_is_not_a_chip_datasheet(self) -> None:
        pages = [
            "STM32H563ZI evaluation platform",
            "STEVAL-MKI109D schematic diagrams",
        ]
        with self.assertRaisesRegex(ValueError, "unknown"):
            select_family(Path("STM32H563_ZI.pdf"), pages, self.families)

    def test_unknown_document_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            select_family(Path("unknown.pdf"), ["unrelated manual"], self.families)

    def test_ambiguous_document_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            select_family(Path("MiAi-6099A1.pdf"), ["基本特性"], [self.ca, self.ca])


if __name__ == "__main__":
    unittest.main()
