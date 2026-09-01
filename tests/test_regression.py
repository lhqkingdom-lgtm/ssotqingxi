from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from regression import first_difference, source_name


class RegressionTests(unittest.TestCase):
    def test_source_name_accepts_windows_and_posix_paths(self) -> None:
        self.assertEqual(source_name(r"D:\docs\manual.pdf"), "manual.pdf")
        self.assertEqual(source_name("/docs/manual.pdf"), "manual.pdf")

    def test_first_difference_reports_nested_field(self) -> None:
        message = first_difference({"data": [{"value": 1}]}, {"data": [{"value": 2}]})
        self.assertEqual(message, "root.data[0].value: 1 != 2")

    def test_first_difference_accepts_equal_payload(self) -> None:
        self.assertIsNone(first_difference({"a": [1, 2]}, {"a": [1, 2]}))


if __name__ == "__main__":
    unittest.main()
