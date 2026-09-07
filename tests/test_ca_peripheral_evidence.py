from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from family_extractors.ca_miai import peripheral_defaults_by_page


class CaPeripheralEvidenceTests(unittest.TestCase):
    def test_locates_peripheral_counts_on_their_actual_pages(self) -> None:
        texts = [
            "基本特性 CPU 频率最高支持 27MHz",
            "支持 8 通道 PWM。支持 8 通道 12 位 SAR ADC。最大支持 24 触摸通道。",
            "最多支持 3 个 UART 接口。内置 1 个 4 线 SPI 接口。内置 1 路 I2C 接口。",
        ]

        located = peripheral_defaults_by_page(texts, 0)

        self.assertEqual(located["PWM"][0], 1)
        self.assertEqual(located["ADC"][0], 1)
        self.assertEqual(located["Touch"][0], 1)
        self.assertEqual(located["UART"][0], 2)
        self.assertEqual(located["SPI"][0], 2)
        self.assertEqual(located["I2C"][0], 2)
