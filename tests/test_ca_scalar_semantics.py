from __future__ import annotations

import unittest

from family_extractors.ca_miai import parse_basic_defaults
from topologies.operating_conditions import is_absolute_maximum_context


class CaScalarSemanticsTests(unittest.TestCase):
    def test_cpu_clock_conditions_supply_the_maximum_supported_frequency(self) -> None:
        text = "CPU 时钟为 8MHz 时，工作电压 2.2~5.5V；CPU 时钟为 16MHz 时，工作电压 2.7~5.5V"
        self.assertEqual(parse_basic_defaults(text)["max_freq_mhz"], 16)

    def test_absolute_maximum_section_is_not_an_operating_context(self) -> None:
        self.assertTrue(is_absolute_maximum_context("23.1 极限参数 直流供电电压 -0.3 6 V"))
        self.assertFalse(is_absolute_maximum_context("工作条件 标准工作电压 2.2 5.5 V"))


if __name__ == "__main__":
    unittest.main()
