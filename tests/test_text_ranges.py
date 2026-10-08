from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.text import parse_range, parse_ranges


class TextRangeTests(unittest.TestCase):
    def test_english_to_range_with_trailing_unit(self) -> None:
        self.assertEqual(parse_range("2.0 to 3.6 V"), (2.0, 3.6))

    def test_unit_before_english_separator_is_supported(self) -> None:
        self.assertEqual(parse_range("1.8 V to 5.5 V"), (1.8, 5.5))

    def test_chinese_and_symbol_ranges_remain_supported(self) -> None:
        self.assertEqual(parse_range("2.0～5.5V"), (2.0, 5.5))
        self.assertEqual(parse_range("-40至85℃"), (-40.0, 85.0))

    def test_single_value_is_not_invented_as_a_range(self) -> None:
        self.assertIsNone(parse_range("3.3 V typical"))

    def test_multiple_conditional_ranges_are_preserved(self) -> None:
        self.assertEqual(
            parse_ranges("1.8 V to 3.6 V with BOR; 1.65 to 3.6 V without BOR"),
            [(1.8, 3.6), (1.65, 3.6)],
        )

    def test_percentage_tolerance_is_converted_to_bounds(self) -> None:
        low, high = parse_range("1.8 V ± 8%")
        self.assertAlmostEqual(low, 1.656)
        self.assertAlmostEqual(high, 1.944)

    def test_plus_slash_minus_tolerance_is_supported(self) -> None:
        self.assertEqual(parse_range("100 MHz +/- 5%"), (95.0, 105.0))


if __name__ == "__main__":
    unittest.main()
