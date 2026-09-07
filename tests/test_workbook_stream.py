from __future__ import annotations

import unittest

from write_workbook_stream import column_name, english_sort_text, excel_date_serial


class WorkbookStreamTests(unittest.TestCase):
    def test_column_names_cross_excel_boundaries(self) -> None:
        self.assertEqual([column_name(i) for i in (0, 25, 26, 701)], ["A", "Z", "AA", "ZZ"])

    def test_sort_key_matches_legacy_identifier_order(self) -> None:
        values = ["ADC0_8", "ADC_VREF", "TIM16_CH1", "TIM1_CH1"]
        self.assertEqual(
            sorted(values, key=english_sort_text),
            ["ADC_VREF", "ADC0_8", "TIM1_CH1", "TIM16_CH1"],
        )

    def test_excel_date_uses_wall_clock_components(self) -> None:
        self.assertEqual(excel_date_serial("1899-12-31T00:00:00+08:00"), 1)

    def test_streamed_workbook_keeps_real_excel_tables(self) -> None:
        from write_workbook_stream import table_parts

        parts = table_parts(1, "peripherals", ["chip_id", "channel_count"], 2)
        self.assertIn('name="peripheralsTable"', parts["xl/tables/table1.xml"])
        self.assertIn('ref="A1:B3"', parts["xl/tables/table1.xml"])
        self.assertIn('name="channel_count"', parts["xl/tables/table1.xml"])


if __name__ == "__main__":
    unittest.main()
