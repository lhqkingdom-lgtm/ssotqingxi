from __future__ import annotations

import unittest

from core.validation import valid_pin_designator


class PinDesignatorTests(unittest.TestCase):
    def test_accepts_positive_lead_numbers_and_bga_coordinates(self) -> None:
        for value in (1, 48, "1", "M8", "AA12"):
            with self.subTest(value=value):
                self.assertTrue(valid_pin_designator(value))

    def test_rejects_nonpositive_and_malformed_designators(self) -> None:
        for value in (None, True, 0, -1, "0", "A0", "-", "P1.2", "1A"):
            with self.subTest(value=value):
                self.assertFalse(valid_pin_designator(value))


if __name__ == "__main__":
    unittest.main()
