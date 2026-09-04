from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.document_semantics import detect_arm_cores, resolve_target_model


CONFIG = {
    "filename_model_pattern": r"STM32[A-Z]{1,2}[0-9A-Z]{4,12}",
    "series_pattern": r"^(STM32[A-Z]{1,2}[0-9]{2,3})",
    "verification_scan_pages": 10,
    "core_scan_pages": 10,
    "core_pattern": r"Cortex\s*[-–]?\s*([AMR]\d{1,2}\+?)",
}


class DocumentSemanticsTests(unittest.TestCase):
    def test_exact_filename_model_is_verified_in_text(self) -> None:
        value = resolve_target_model(Path("stm32u585ai.pdf"), ["STM32U585AI product data"], CONFIG)
        self.assertEqual((value.value, value.pages), ("STM32U585AI", (1,)))

    def test_family_wildcard_text_can_verify_specific_filename_model(self) -> None:
        value = resolve_target_model(Path("stm32h743vi.pdf"), ["STM32H742xI/G and STM32H743xI/G"], CONFIG)
        self.assertEqual(value.value, "STM32H743VI")

    def test_missing_or_mismatched_filename_model_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot derive"):
            resolve_target_model(Path("datasheet.pdf"), ["STM32U585xx"], CONFIG)
        with self.assertRaisesRegex(ValueError, "not supported"):
            resolve_target_model(Path("stm32u585ai.pdf"), ["STM32F777xx"], CONFIG)

    def test_multiple_filename_models_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_target_model(Path("stm32u585ai_stm32h743vi.pdf"), ["STM32"], CONFIG)

    def test_single_and_multiple_arm_cores_keep_evidence_pages(self) -> None:
        single = detect_arm_cores(["Arm Cortex-M7 core"], CONFIG)
        self.assertEqual((single.value, single.pages), ("ARM Cortex-M7", (1,)))
        dual = detect_arm_cores(["Cortex-M4 application core", "Cortex-M0+ network core"], CONFIG)
        self.assertEqual((dual.value, dual.pages), ("ARM Cortex-M4 + Cortex-M0+", (1, 2)))


if __name__ == "__main__":
    unittest.main()
