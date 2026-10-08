from __future__ import annotations

import unittest

from validate_workbook_stream import column_index, equivalent, expected_value


class WorkbookStreamValidationTests(unittest.TestCase):
    def test_column_index_crosses_excel_boundaries(self) -> None:
        self.assertEqual([column_index(value) for value in ("A1", "Z9", "AA2", "ZZ3")], [0, 25, 26, 701])

    def test_package_lists_use_writer_representation(self) -> None:
        self.assertEqual(expected_value("package_types", ["LQFP48", "QFN48"]), '["LQFP48","QFN48"]')

    def test_numeric_cells_compare_without_type_noise(self) -> None:
        self.assertTrue(equivalent(32, 32.0))
        self.assertFalse(equivalent(32, 33))


if __name__ == "__main__":
    unittest.main()
