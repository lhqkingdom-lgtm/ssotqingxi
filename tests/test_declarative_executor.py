from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.declarative_executor import execute_declarative_plan, validate_declarative_plan
from core.ir import CellIR, DocumentIR, PageIR, TableIR


def document_from_tables(*tables: tuple[str, list[list[str]]]) -> DocumentIR:
    built = []
    for table_index, (title, rows) in enumerate(tables, start=1):
        cells = tuple(
            CellIR(
                cell_id=f"t{table_index}-r{row}-c{column}",
                page=table_index,
                fragment=1,
                row=row,
                column=column,
                raw_text=value,
                normalized_text=value.casefold(),
                bbox=(column * 10.0, row * 10.0, column * 10.0 + 9.0, row * 10.0 + 9.0),
            )
            for row, values in enumerate(rows)
            for column, value in enumerate(values)
        )
        built.append(
            TableIR(
                table_id=f"table-{table_index}",
                title=title,
                pages=(table_index,),
                continued=False,
                orientations=(0,),
                bboxes=((0.0, 0.0, 100.0, 100.0),),
                cells=cells,
            )
        )
    pages = tuple(
        PageIR(page=index, width=100, height=100, rotation=0, text="", table_ids=(table.table_id,))
        for index, table in enumerate(built, start=1)
    )
    return DocumentIR("unseen.pdf", len(pages), pages, tuple(built))


def profile(prefix: str) -> SimpleNamespace:
    def model_tokens(value: str, exact_only: bool = False) -> list[str]:
        del exact_only
        return list(dict.fromkeys(item.upper() for item in re.findall(rf"{prefix}[A-Z0-9]+", value, re.I)))

    return SimpleNamespace(
        model_tokens=model_tokens,
        field_dictionary_file=ROOT / "config" / "common_field_dictionary.json",
        field_aliases={},
        package_re=SimpleNamespace(pattern=r"(?:LQFP|TSSOP|QFN)\s*\d+"),
        boundary_re=re.compile(r"(?!)"),
        model_re=re.compile(rf"{prefix}[A-Z0-9]+", re.I),
    )


class DeclarativeExecutorTests(unittest.TestCase):
    def test_cross_table_catalog_ordering_and_wildcard_matrix_are_joined(self) -> None:
        document = document_from_tables(
            (
                "Device summary",
                [["Reference", "Part number"], ["ZX90x1", "X90A1, ZX90B2"]],
            ),
            (
                "Device features",
                [
                    ["Feature", "ZX90Ax", "", "ZX90Bx", ""],
                    ["Flash memory", "16 KB", "32 KB", "16 KB", "32 KB"],
                    ["SRAM", "4 KB", "4 KB", "8 KB", "8 KB"],
                ],
            ),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "codes",
                    "component": "ordering_code_legend",
                    "config": {"required_relations": ["pin_count_codes", "flash_code_kb"]},
                },
                {
                    "id": "catalog",
                    "component": "model_catalog",
                    "depends_on": ["codes"],
                    "config": {
                        "title_any": ["device summary"],
                        "ordering_section": "codes",
                        "code_positions": [
                            {"relation": "pin_count_codes", "offset": -2},
                            {"relation": "flash_code_kb", "offset": -1},
                        ],
                    },
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["codes", "catalog"],
                    "config": {
                        "layout": "models_from_catalog",
                        "title_any": ["device features"],
                        "locator": {"required_all": ["flash", "sram"]},
                        "catalog_section": "catalog",
                        "ordering_section": "codes",
                        "density_relation": {
                            "relation": "flash_code_kb", "offset": -1, "field": "flash_kb"
                        },
                        "fields": {
                            "flash_kb": {"parser": "quantity", "required": True},
                            "ram_kb": {"parser": "quantity", "required": True},
                        },
                    },
                },
            ],
        }
        texts = [
            "Device summary\nZX90A1, ZX90B2\nOrdering information\n"
            "Pin count\nA = 20 pins\nB = 32 pins\n"
            "Flash memory size\n1 = 16 Kbytes\n2 = 32 Kbytes"
        ]
        result = execute_declarative_plan(plan, document=document, page_texts=texts, profile=profile("ZX"))
        models = result["models"]["models"]
        self.assertEqual([item["model_id"] for item in models], ["ZX90A1", "ZX90B2"])
        self.assertEqual(models[0]["fields"], {"flash_kb": 16, "ram_kb": 4})
        self.assertEqual(models[1]["fields"], {"flash_kb": 32, "ram_kb": 8})

    def test_same_component_reads_models_in_columns_with_evidence(self) -> None:
        document = document_from_tables(
            (
                "Device features",
                [
                    ["Feature", "AX100", "AX200"],
                    ["Flash", "64 KB", "128 KB"],
                    ["SRAM", "8 KB", "16 KB"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_columns",
                    "title_any": ["device features"],
                    "locator": {"required_all": ["flash", "sram"]},
                    "fields": {
                        "flash_kb": {"reducer": "max"},
                        "ram_kb": {"reducer": "max"},
                    },
                },
            }],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("AX"))
        models = result["models"]["models"]
        self.assertEqual([item["model_id"] for item in models], ["AX100", "AX200"])
        self.assertEqual(models[1]["values"][0]["raw_value"], "128 KB")
        self.assertEqual(models[1]["values"][0]["source_cell_id"], "t1-r1-c2")
        self.assertEqual(models[1]["fields"], {"flash_kb": 128, "ram_kb": 16})

    def test_same_component_reads_models_in_rows_for_other_family(self) -> None:
        document = document_from_tables(
            (
                "Product selection",
                [
                    ["Part number", "Program memory", "RAM"],
                    ["BY10", "32 KB", "4 KB"],
                    ["BY20", "64 KB", "8 KB"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_rows",
                    "title_any": ["product selection"],
                    "locator": {"required_any": ["program memory", "ram"]},
                    "model_header_any": ["part number", "device"],
                },
            }],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("BY"))
        models = result["models"]["models"]
        self.assertEqual([item["model_id"] for item in models], ["BY10", "BY20"])
        self.assertEqual(models[0]["values"][0]["label"], "Program memory")

    def test_configured_variants_select_structure_without_filename_branch(self) -> None:
        document = document_from_tables(
            (
                "Product selection",
                [["Device", "Flash"], ["VZ10", "16 KB"], ["VZ20", "32 KB"]],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "variants": [
                        {
                            "layout": "models_in_columns",
                            "locator": {"required_any": ["flash"]},
                        },
                        {
                            "layout": "models_in_rows",
                            "locator": {"required_any": ["flash"]},
                            "model_header_any": ["device"],
                        },
                    ]
                },
            }],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("VZ"))
        self.assertEqual(
            [item["model_id"] for item in result["models"]["models"]],
            ["VZ10", "VZ20"],
        )

    def test_operating_conditions_are_dependency_ordered_and_exclude_stress_table(self) -> None:
        document = document_from_tables(
            ("Absolute maximum ratings", [["Symbol", "Min", "Max"], ["VDD", "-0.3", "6"]]),
            (
                "General operating conditions",
                [
                    ["Symbol", "Parameter", "Min", "Max"],
                    ["VDD", "Supply voltage", "1.8", "3.6"],
                    ["TA", "Ambient temperature", "-40", "85"],
                ],
            ),
            ("Product selection", [["Device", "Flash"], ["CZ1", "16 KB"]]),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "conditions",
                    "component": "operating_conditions",
                    "depends_on": ["models"],
                    "config": {
                        "scope": "document",
                        "table_any": ["supplyvoltage", "operatingconditions"],
                        "primary_voltage_symbols": ["VDD"],
                        "temperature_any": ["ambienttemperature"],
                        "temperature_exclude_any": ["suffix"],
                    },
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "title_any": ["product selection"],
                        "locator": {"required_any": ["flash"]},
                        "model_header_any": ["device"],
                    },
                },
            ],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=["", "", ""], profile=profile("CZ"))
        self.assertEqual(result["conditions"]["voltage_min"], 1.8)
        self.assertEqual(result["conditions"]["voltage_max"], 3.6)
        self.assertEqual(result["conditions"]["temperature_min"], -40)

    def test_grouped_prefix_suffix_header_expands_physical_columns(self) -> None:
        document = document_from_tables(
            (
                "Family device features",
                [
                    ["Peripheral", "DX10_\n_A1 _A2 _B1"],
                    ["Flash", "16 KB", "32 KB", "64 KB"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_grouped_columns",
                    "title_any": ["device features"],
                    "locator": {"required_any": ["flash"]},
                    "grouped_header": {
                        "prefix_pattern": "(DX10_)",
                        "suffix_pattern": "_([A-Z][0-9])",
                        "separator_pattern": "[_\\s]+",
                    },
                },
            }],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("DX"))
        self.assertEqual(
            [item["model_id"] for item in result["models"]["models"]],
            ["DX10A1", "DX10A2", "DX10B1"],
        )

    def test_standard_conditions_component_rejects_feature_matrix_shape(self) -> None:
        document = document_from_tables(
            (
                "Device features",
                [
                    ["Feature", "EX1"],
                    ["Operating voltage", "2.0 to 3.6 V"],
                    ["Operating temperature", "-40 to 85 C"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "conditions",
                "component": "operating_conditions",
                "required": False,
                "config": {
                    "scope": "document",
                    "table_any": ["operatingvoltage"],
                    "primary_voltage_symbols": ["VDD"],
                    "temperature_any": ["operatingtemperature"],
                    "temperature_exclude_any": [],
                },
            }],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("EX"))
        self.assertIsNone(result["conditions"])

    def test_unknown_components_cycles_and_embedded_answers_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown declarative component"):
            validate_declarative_plan(
                {"version": 1, "sections": [{"id": "x", "component": "vendor_parser", "config": {}}]},
                {"model_matrix"},
            )

    def test_unknown_parser_qualifier_and_missing_required_value_fail(self) -> None:
        document = document_from_tables(
            ("Device features", [["Feature", "RX1"], ["Flash", "16 KB"]])
        )
        base = {
            "layout": "models_in_columns",
            "locator": {"required_any": ["flash"]},
        }
        for fields, message in (
            ({"flash_kb": {"parser": "vendor_magic"}}, "unsupported parser"),
            ({"flash_kb": {"qualifiers": ["answer_is_16"]}}, "unknown qualifiers"),
            ({"ram_kb": {"required": True}}, "required field ram_kb missing"),
        ):
            plan = {
                "version": 1,
                "sections": [{
                    "id": "models",
                    "component": "model_matrix",
                    "config": {**base, "fields": fields},
                }],
            }
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("RX"))
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            validate_declarative_plan(
                {
                    "version": 1,
                    "sections": [
                        {"id": "a", "component": "model_matrix", "depends_on": ["b"], "config": {}},
                        {"id": "b", "component": "model_matrix", "depends_on": ["a"], "config": {}},
                    ],
                },
                {"model_matrix"},
            )
        with self.assertRaisesRegex(ValueError, "answers"):
            validate_declarative_plan(
                {
                    "version": 1,
                    "sections": [{
                        "id": "models",
                        "component": "model_matrix",
                        "config": {"expected_value": "AX100"},
                    }],
                },
                {"model_matrix"},
            )


if __name__ == "__main__":
    unittest.main()
