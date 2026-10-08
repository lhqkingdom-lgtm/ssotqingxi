from __future__ import annotations

import io
import unittest
import zipfile

from write_workbook_stream import cell_xml, column_name, english_sort_text, excel_date_serial, public_value


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

    def test_empty_cells_keep_explicit_stripe_style(self) -> None:
        self.assertEqual(cell_xml("B2", None, 3), '<c r="B2" s="3"/>')

    def test_database_json_values_are_written_as_valid_json(self) -> None:
        self.assertEqual(public_value("features", {"channel_count": 16}), '{"channel_count":16}')
        self.assertEqual(public_value("package_types", ["LQFP48"]), '["LQFP48"]')

    def test_streamed_workbook_keeps_real_excel_tables(self) -> None:
        from write_workbook_stream import table_parts

        parts = table_parts(1, "peripherals", ["chip_id", "channel_count"], 2)
        self.assertIn('name="peripheralsTable"', parts["xl/tables/table1.xml"])
        self.assertIn('ref="A1:B3"', parts["xl/tables/table1.xml"])
        self.assertIn('name="channel_count"', parts["xl/tables/table1.xml"])

    def test_explicit_row_styles_do_not_depend_on_an_office_theme(self) -> None:
        from write_workbook_stream import static_parts

        parts = static_parts(["chips"])
        styles = parts["xl/styles.xml"]
        self.assertIn('fgColor rgb="FFD9E7F5"', styles)
        self.assertIn('<cellXfs count="5">', styles)
        self.assertNotIn('/xl/theme/theme1.xml', parts["[Content_Types].xml"])
        self.assertNotIn('Target="theme/theme1.xml"', parts["xl/_rels/workbook.xml.rels"])

        themed = static_parts(["chips"], include_theme=True)
        self.assertIn('/xl/theme/theme1.xml', themed["[Content_Types].xml"])
        self.assertIn('Target="theme/theme1.xml"', themed["xl/_rels/workbook.xml.rels"])
        self.assertRegex(themed["docProps/core.xml"], r"<dcterms:created[^>]*>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z</dcterms:created>")

    def test_table_does_not_reapply_theme_dependent_stripes(self) -> None:
        from write_workbook_stream import table_parts

        table = table_parts(1, "chips", ["chip_id"], 1)["xl/tables/table1.xml"]
        self.assertIn('</tableColumns></table>', table)
        self.assertNotIn("TableStyleMedium2", table)
        self.assertNotIn("tableStyleInfo", table)

    def test_worksheet_does_not_duplicate_table_auto_filter(self) -> None:
        from collections import defaultdict
        from write_workbook_stream import write_sheet

        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            write_sheet(archive, 1, "chips", ["chip_id"], [{"chip_id": "X"}], defaultdict(lambda: defaultdict(list)))
        with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertNotIn("<autoFilter", sheet)


if __name__ == "__main__":
    unittest.main()
