from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.ir import DocumentIR, PageIR, TableIR
from core.table_ir_legacy_view import IRDocumentTableView
from core.table_ir_validation import build_evidence_manifest, table_shape, validate_special_structures
from input_adapters.mineru import html_to_cells, load_mineru_document


def make_table(table_id: str, page: int, title: str, html: str, orientation: int = 0, continued: bool = False) -> TableIR:
    return TableIR(
        table_id=table_id,
        title=title,
        pages=(page,),
        continued=continued,
        orientations=(orientation,),
        bboxes=((0.0, 0.0, 100.0, 100.0),),
        cells=html_to_cells(table_id, page, 1, html),
        metadata={"image_exists": True, "resolved_image_path": f"{table_id}.jpg"},
    )


class MinerUAdapterTests(unittest.TestCase):
    def test_html_rowspan_and_colspan_create_full_logical_grid(self) -> None:
        cells = html_to_cells(
            "table-1",
            10,
            1,
            "<table><tr><th rowspan='2'>Pin</th><th colspan='2'>Package</th></tr>"
            "<tr><th>LQFP48</th><th>QFN32</th></tr>"
            "<tr><td>PA0</td><td>1</td><td>2</td></tr></table>",
        )
        table = TableIR("table-1", "Pins", (10,), False, (0,), ((0, 0, 1, 1),), cells)
        self.assertEqual(table_shape(table), (3, 3))
        by_position = {(cell.row, cell.column): cell for cell in cells}
        self.assertEqual(by_position[(0, 0)].row_span, 2)
        self.assertEqual(by_position[(0, 1)].column_span, 2)
        self.assertEqual(by_position[(1, 0)].inherited_from, by_position[(0, 0)].cell_id)
        self.assertEqual(by_position[(0, 2)].inherited_from, by_position[(0, 1)].cell_id)

    def test_realistic_nested_table_nodes_are_deduplicated(self) -> None:
        html = "<table><tr><td>Model</td><td>Flash</td></tr><tr><td>CA51F2</td><td>16K</td></tr></table>"
        nested = {
            "type": "table",
            "bbox": [10, 20, 90, 80],
            "blocks": [
                {"type": "table_caption", "lines": [{"spans": [{"type": "text", "content": "Table 1. Features"}]}]},
                {"type": "table_body", "lines": [{"spans": [{"type": "table", "html": html, "image_path": "table.jpg"}]}]},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            (root / "images" / "table.jpg").write_bytes(b"image")
            layout = root / "layout.json"
            layout.write_text(json.dumps({"pdf_info": [{
                "page_idx": 0,
                "page_size": [100, 100],
                "preproc_blocks": [nested],
            }]}), encoding="utf-8")
            document = load_mineru_document(layout)
        self.assertEqual(len(document.tables), 1)
        self.assertEqual(document.tables[0].title, "Table1.Features")
        self.assertTrue(document.tables[0].metadata["image_exists"])
        self.assertEqual(table_shape(document.tables[0]), (2, 2))
        legacy_rows = IRDocumentTableView(document).pages[0].find_tables()[0].extract()
        self.assertEqual(legacy_rows, [["Model", "Flash"], ["CA51F2", "16K"]])

    def test_table_inherits_nearest_preceding_caption(self) -> None:
        html = "<table><tr><td>PA0</td><td>1</td></tr></table>"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images").mkdir()
            (root / "images" / "pins.jpg").write_bytes(b"image")
            layout = root / "layout.json"
            layout.write_text(json.dumps({"pdf_info": [{
                "page_idx": 30,
                "page_size": [600, 800],
                "preproc_blocks": [
                    {"type": "table_caption", "bbox": [100, 400, 300, 425], "lines": [{"spans": [{"type": "text", "content": "Table 12. Pin assignment"}]}]},
                    {"type": "table", "bbox": [50, 426, 550, 700], "html": html, "image_path": "pins.jpg"},
                ],
            }]}), encoding="utf-8")
            document = load_mineru_document(layout)
        self.assertEqual(document.tables[0].title, "Table12.Pinassignment")

    def test_special_structure_detector_covers_all_requested_topologies(self) -> None:
        pins = make_table(
            "pins",
            4,
            "Table 4. Pin assignment",
            "<table><tr><th rowspan='2'>Pin name</th><th colspan='2'>Pin</th></tr>"
            "<tr><th>LQFP48</th><th>UFQFPN32</th></tr><tr><td>PA0</td><td>1</td><td>2</td></tr></table>",
            orientation=90,
        )
        af = make_table(
            "af",
            5,
            "Table 5. Alternate function mapping",
            "<table><tr><th>Port</th><th>AF0</th><th>AF1</th></tr>"
            "<tr><td>PA0</td><td>USART1_TX</td><td>TIM2_CH1</td></tr></table>",
        )
        matrix1 = make_table("matrix-1", 7, "Table 8<sub>.</sub> Device features", "<table><tr><td>Feature</td><td>A</td></tr></table>")
        matrix2 = make_table(
            "matrix-2", 8, "Table 8. Device features (continued)",
            "<table><tr><td>Feature</td><td>B</td></tr></table>", continued=True,
        )
        document = DocumentIR(
            source_file="sample.pdf",
            page_count=8,
            pages=tuple(PageIR(page, 100, 100, 0, "") for page in range(1, 9)),
            tables=(pins, af, matrix1, matrix2),
        )
        structures = validate_special_structures(document)["special_structures"]
        self.assertEqual(structures["rotated_tables"], ["pins"])
        self.assertEqual(structures["multi_level_headers"], ["pins"])
        self.assertEqual(structures["multi_package_pin_tables"], ["pins"])
        self.assertEqual(structures["af_matrices"], ["af"])
        self.assertEqual(len(structures["cross_page_candidates"]), 1)
        report = validate_special_structures(document)
        manifest = build_evidence_manifest(document, report)
        pin_evidence = next(item for item in manifest["tables"] if item["table_id"] == "pins")
        self.assertIn("rotated_tables", pin_evidence["structure_tags"])
        self.assertIn("multi_package_pin_tables", pin_evidence["structure_tags"])
        self.assertEqual(len(manifest["groups"]), 1)


if __name__ == "__main__":
    unittest.main()
