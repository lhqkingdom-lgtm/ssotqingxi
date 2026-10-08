from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.declarative_executor import (
    _aligned_row_labels,
    _model_patterns,
    execute_declarative_plan,
    validate_declarative_plan,
)
from core.ir import CellIR, DocumentIR, PageIR, TableIR
from core.table_index import build_table_index
from core.table_ir_legacy_view import IRDocumentTableView
from topologies.model_column_matrix import intersect_model_patterns
from topologies.pin_definition import extract_multi_package_pin_definitions


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
        series_for_model=lambda model: model.upper()[: len(prefix) + 2],
        field_dictionary_file=ROOT / "config" / "common_field_dictionary.json",
        field_aliases={},
        package_re=SimpleNamespace(pattern=r"(?:LQFP|TSSOP|QFN)\s*\d+"),
        boundary_re=re.compile(r"(?!)"),
        model_re=re.compile(rf"{prefix}[A-Z0-9]+", re.I),
    )


class DeclarativeExecutorTests(unittest.TestCase):
    def test_separate_named_function_column_projects_codes_for_all_declared_packages(self) -> None:
        document = document_from_tables((
            "Pin attributes",
            [
                ["LQFP64", "QFN48", "PIN NAME", "DIGITAL PIN FUNCTION"],
                ["1", "1", "PA0", "UART0_TX [2] / I2C0_SDA [3]"],
                ["2", "2", "PA1", "UART0_RX [2] / I2C0_SCL [3]"],
                ["3", "3", "PA2", "SPI0_CS0 [4] / TIM0_C0 [5]"],
            ],
        ))
        tables = extract_multi_package_pin_definitions(
            IRDocumentTableView(document),
            models=[{"model_id": "GX100", "package_types": ["LQFP64", "QFN48"]}],
            pin_pattern=re.compile(r"P[A-Z]\d{1,2}", re.I),
            package_pattern=re.compile(r"(?:LQFP|QFN)\s*\d+", re.I),
            normalize_function=lambda value: value.strip().upper(),
            recipe={
                "required_any": ["pin attributes", "digital pin function"],
                "minimum_pin_tokens": 3,
                "identity_header_aliases": ["pin name"],
                "default_function_header_aliases": ["pin name"],
                "default_function_fallback": "GPIO",
                "function_header_aliases": ["digital pin function"],
                "function_separator_pattern": r"[/\n]+",
                "function_code_pattern": r"\[\s*(\d+)\s*\]",
            },
            table_index=build_table_index(document),
        )
        rows = [row for table in tables for row in table["rows"]]
        self.assertEqual(len(rows), 12)
        self.assertEqual({row.package_type for row in rows}, {"LQFP64", "QFN48"})
        self.assertIn(
            ("LQFP64", "PA0", 1, "UART0_TX", "2"),
            {
                (row.package_type, row.pin_name, row.pin_number, row.function_name, row.af_code)
                for row in rows
            },
        )

    def test_named_composite_headers_expand_and_repeated_model_rows_merge(self) -> None:
        document = document_from_tables((
            "Device comparison",
            [
                ["Device name", "FLASH / SRAM (KB)", "UART / I2C / SPI", "ADC / CHAN", "Package"],
                ["GX100", "32 / 16", "4 / 2 / 1", "2 / 17", "64 LQFP"],
                ["GX100", "32 / 16", "4 / 2 / 1", "2 / 16", "48 QFN"],
            ],
        ))
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "title_any": ["device comparison"],
                        "model_header_any": ["device name"],
                        "composite_columns": {
                            "separator_pattern": r"\s*/\s*",
                            "part_aliases": {
                                "chan": {
                                    "semantic": "peripheral_adc",
                                    "normalized_label": "channel",
                                },
                            },
                        },
                        "fields": {
                            "flash_kb": {"reducer": "unique", "required": True},
                            "ram_kb": {"reducer": "unique", "required": True},
                            "package_types": {"required": True},
                        },
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "fields": {
                            "peripheral_uart": {"value_parser": "first_integer"},
                            "peripheral_i2c": {"value_parser": "first_integer"},
                            "peripheral_spi": {"value_parser": "first_integer"},
                            "peripheral_adc": {
                                "value_parser": "adc_instance_count",
                                "channel_value_parser": "adc_channel_count",
                            },
                        },
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("GX")
        )
        models = result["models"]["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual(
            models[0]["fields"],
            {"flash_kb": 32, "ram_kb": 16, "package_types": ["LQFP64", "QFN48"]},
        )
        peripherals = {
            row["peripheral_type"]: (row["instance_count"], row["channel_count"])
            for row in result["peripherals"]["records"]
        }
        self.assertEqual(peripherals["UART"], (4, None))
        self.assertEqual(peripherals["I2C"], (2, None))
        self.assertEqual(peripherals["SPI"], (1, None))
        self.assertEqual(peripherals["ADC"], (2, 17))

    def test_composite_header_mismatch_and_repeated_scalar_conflict_fail_closed(self) -> None:
        base_config = {
            "layout": "models_in_rows",
            "title_any": ["device comparison"],
            "model_header_any": ["device name"],
            "composite_columns": {"separator_pattern": r"\s*/\s*", "part_aliases": {}},
            "fields": {
                "flash_kb": {"reducer": "unique", "required": True},
                "ram_kb": {"reducer": "unique", "required": True},
            },
        }
        for rows, message in (
            (
                [["Device name", "FLASH / SRAM (KB)"], ["GX100", "32"]],
                "arity mismatch",
            ),
            (
                [
                    ["Device name", "FLASH / SRAM (KB)"],
                    ["GX100", "32 / 16"],
                    ["GX100", "64 / 16"],
                ],
                "not semantically unique",
            ),
        ):
            document = document_from_tables(("Device comparison", rows))
            plan = {"version": 1, "sections": [{
                "id": "models", "component": "model_matrix", "config": base_config,
            }]}
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, message):
                execute_declarative_plan(
                    plan, document=document, page_texts=[""], profile=profile("GX")
                )

    def test_shared_numeric_infix_model_header_expands_both_families(self) -> None:
        test_profile = profile("QZ")

        self.assertEqual(
            _model_patterns("QZ767/769Bx", test_profile),
            ["QZ767BX", "QZ769BX", "QZ767", "QZ769"],
        )

    def test_reversed_grouped_header_expands_density_suffix_alternatives(self) -> None:
        test_profile = profile("QZ")

        self.assertEqual(
            _model_patterns("I/GA575ZQ", test_profile),
            ["QZ575AG", "QZ575AI"],
        )

    def test_multiline_merged_label_aligns_with_split_value_rows(self) -> None:
        source = CellIR(
            cell_id="label",
            page=1,
            fragment=1,
            row=0,
            column=0,
            raw_text="16-bit converters\nDirect channels\nFast channels\nSlow channels",
            normalized_text="",
            bbox=None,
            row_span=2,
        )
        inherited = CellIR(
            cell_id="label-inherited",
            page=1,
            fragment=1,
            row=1,
            column=0,
            raw_text="",
            normalized_text="",
            bbox=None,
            inherited_from="label",
        )
        value0 = CellIR("value-0", 1, 1, 0, 1, "3", "3", None)
        value1 = CellIR("value-1", 1, 1, 1, 1, "2\n3\n11", "", None)
        rows = [
            [source.raw_text, value0.raw_text],
            [source.raw_text, value1.raw_text],
        ]
        cells = {(0, 0): source, (1, 0): inherited, (0, 1): value0, (1, 1): value1}
        self.assertEqual(_aligned_row_labels(rows, cells, 0, 1, 1), ["16-bit converters"])
        self.assertEqual(
            _aligned_row_labels(rows, cells, 1, 1, 1),
            ["16-bit converters Direct channels Fast channels Slow channels"],
        )

    def test_document_quantities_separates_named_regions_on_one_line(self) -> None:
        document = document_from_tables(
            ("Capabilities", [["Feature", "AB701Q"], ["Flash", "16 KB"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["capabilities"],
                        "fields": {
                            "flash_kb": {"required": True},
                        },
                    },
                },
                {
                    "id": "shared",
                    "component": "document_quantities",
                    "config": {
                        "fields": ["ram_internal_kb", "ram_external_kb"],
                        "scan_page_count": 1,
                        "association_window": 24,
                    },
                },
                {
                    "id": "memory",
                    "component": "memory_projection",
                    "depends_on": ["models", "shared"],
                    "config": {
                        "model_section": "models",
                        "mappings": [
                            {"source_field": "flash_kb", "memory_type": "Flash"},
                            {
                                "source_field": "ram_external_kb",
                                "memory_type": "SRAM_External",
                                "fallback_section": "shared",
                            },
                            {
                                "source_section": "shared",
                                "source_field": "ram_internal_kb",
                                "memory_type": "SRAM_Internal",
                            },
                        ],
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan,
            document=document,
            page_texts=["RAM: 256 BYTE internal RAM, 768 BYTE external RAM"],
            profile=profile("AB"),
        )
        self.assertEqual(result["shared"]["fields"]["ram_internal_kb"], 0.25)
        self.assertEqual(result["shared"]["fields"]["ram_external_kb"], 0.75)
        self.assertEqual(
            [(item["memory_type"], item["size_kb"]) for item in result["memory"]["records"]],
            [("Flash", 16), ("SRAM_External", 0.75), ("SRAM_Internal", 0.25)],
        )

    def test_memory_projection_maps_semantic_fields_and_preserves_sources(self) -> None:
        document = document_from_tables(
            ("Capabilities", [["Feature", "AB701Q"], ["Flash", "64 KB"], ["RAM", "16 KB"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["capabilities"],
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                },
                {
                    "id": "memory",
                    "component": "memory_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "mappings": [
                            {"source_field": "flash_kb", "memory_type": "Flash"},
                            {"source_field": "ram_kb", "memory_type": "SRAM_Internal"},
                        ],
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("AB")
        )
        records = result["memory"]["records"]
        self.assertEqual(
            [(item["chip_id"], item["memory_type"], item["size_kb"]) for item in records],
            [("AB701Q", "Flash", 64), ("AB701Q", "SRAM_Internal", 16)],
        )
        self.assertTrue(all(item["value_sources"] for item in records))

    def test_memory_projection_preserves_explicit_zero_capacity(self) -> None:
        document = document_from_tables(
            ("Capabilities", [["Feature", "AB701Q"], ["Flash", "0 KB"], ["RAM", "16 KB"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["capabilities"],
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                },
                {
                    "id": "memory",
                    "component": "memory_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "mappings": [
                            {"source_field": "flash_kb", "memory_type": "Flash"},
                            {"source_field": "ram_kb", "memory_type": "SRAM_Internal"},
                        ],
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("AB")
        )
        self.assertEqual(
            [(item["memory_type"], item["size_kb"]) for item in result["memory"]["records"]],
            [("Flash", 0), ("SRAM_Internal", 16)],
        )

    def test_memory_projection_rejects_answer_like_or_untraceable_mapping(self) -> None:
        document = document_from_tables(
            ("Capabilities", [["Feature", "AB701Q"], ["Flash", "64 KB"], ["RAM", "16 KB"]])
        )
        bad_plan = {
            "version": 1,
            "sections": [{
                "id": "memory",
                "component": "memory_projection",
                "config": {
                    "model_section": "missing",
                    "mappings": [{"source_field": "flash_kb", "memory_type": "Flash", "size_kb": 64}],
                },
            }],
        }
        with self.assertRaisesRegex(ValueError, "model_section"):
            execute_declarative_plan(
                bad_plan, document=document, page_texts=[""], profile=profile("AB")
            )

    def test_pattern_intersection_uses_both_sources_without_guessing(self) -> None:
        self.assertEqual(
            intersect_model_patterns("AB71?D", "AB7?RD", ("?",)),
            "AB71RD",
        )
        self.assertEqual(
            intersect_model_patterns("AB71?D", "AB7??D", ("?",)),
            "AB71?D",
        )
        self.assertIsNone(intersect_model_patterns("AB71QD", "AB72RD", ("?",)))

    def test_pattern_intersection_uses_shorter_declared_sales_type(self) -> None:
        self.assertEqual(
            intersect_model_patterns("STM32L15XCXXXA", "STM32L151X6", ("X",)),
            "STM32L151C6",
        )

    def test_column_and_title_patterns_expand_merged_density_subcolumns(self) -> None:
        cells = (
            CellIR("h0", 1, 1, 0, 0, "Feature", "feature", (0, 0, 9, 9)),
            CellIR("h1", 1, 1, 0, 1, "AB7C\nX", "ab7c x", (10, 0, 19, 9)),
            CellIR("h2", 1, 1, 0, 2, "", "", (20, 0, 29, 9), inherited_from="h1"),
            CellIR("f0", 1, 1, 1, 0, "Flash", "flash", (0, 10, 9, 19)),
            CellIR("f1", 1, 1, 1, 1, "16 KB", "16 kb", (10, 10, 19, 19)),
            CellIR("f2", 1, 1, 1, 2, "32 KB", "32 kb", (20, 10, 29, 19)),
            CellIR("r0", 1, 1, 2, 0, "RAM", "ram", (0, 20, 9, 29)),
            CellIR("r1", 1, 1, 2, 1, "4 KB", "4 kb", (10, 20, 19, 29)),
            CellIR("r2", 1, 1, 2, 2, "4 KB", "4 kb", (20, 20, 29, 29)),
        )
        table = TableIR(
            "table-patterns", "AB7X1/X2 device features", (1,), False, (0,),
            ((0, 0, 30, 30),), cells,
        )
        document = DocumentIR(
            "unseen.pdf", 1,
            (PageIR(1, 100, 100, 0, "", (table.table_id,)),),
            (table,),
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_columns",
                    "title_any": ["device features"],
                    "locator": {"required_all": ["flash", "ram"]},
                    "fields": {
                        "flash_kb": {"parser": "quantity", "reducer": "max", "required": True},
                        "ram_kb": {"parser": "quantity", "reducer": "max", "required": True},
                    },
                },
            }],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("AB")
        )
        self.assertEqual(
            [(item["model_id"], item["fields"]["flash_kb"]) for item in result["models"]["models"]],
            [("AB7C1", 16), ("AB7C2", 32)],
        )

    def test_column_patterns_expand_only_to_concrete_catalog_models(self) -> None:
        def model_tokens(value: str, exact_only: bool = False) -> list[str]:
            tokens = list(dict.fromkeys(item.upper() for item in re.findall(r"AB[A-Z0-9]+", value, re.I)))
            return [item for item in tokens if "X" not in item] if exact_only else tokens

        synthetic_profile = SimpleNamespace(
            model_tokens=model_tokens,
            wildcard_markers=("X",),
            field_dictionary_file=ROOT / "config" / "common_field_dictionary.json",
            field_aliases={},
            package_re=SimpleNamespace(pattern=r"QFN\s*\d+"),
            boundary_re=re.compile(r"(?!)"),
            model_re=re.compile(r"AB[A-Z0-9]+", re.I),
        )
        document = document_from_tables(
            (
                "Supported devices",
                [["Feature", "AB701Q", "AB702Q"], ["Marker", "yes", "yes"]],
            ),
            (
                "Capabilities",
                [
                    ["Feature", "AB70XQ"],
                    ["Flash memory", "64 KB"],
                    ["SRAM", "16 KB"],
                ],
            ),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "catalog",
                    "component": "model_matrix",
                    "config": {"layout": "models_in_columns", "title_any": ["supported devices"]},
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["catalog"],
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["capabilities"],
                        "catalog_section": "catalog",
                        "locator": {"required_all": ["flash", "sram"]},
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                        },
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=["", ""], profile=synthetic_profile
        )
        models = result["models"]["models"]
        self.assertEqual([item["model_id"] for item in models], ["AB701Q", "AB702Q"])
        self.assertTrue(all(item["fields"]["flash_kb"] == 64 for item in models))
        self.assertTrue(all(item["fields"]["ram_kb"] == 16 for item in models))

    def test_compact_suffix_alternatives_expand_against_catalog(self) -> None:
        document = document_from_tables(
            (
                "Supported devices",
                [["Feature", "AB701Q", "AB701R"], ["Marker", "yes", "yes"]],
            ),
            (
                "Capabilities",
                [["Feature", "AB701Q/R"], ["Flash memory", "64 KB"], ["SRAM", "16 KB"]],
            ),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "catalog",
                    "component": "model_matrix",
                    "config": {"layout": "models_in_columns", "title_any": ["supported devices"]},
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["catalog"],
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["capabilities"],
                        "catalog_section": "catalog",
                        "locator": {"required_all": ["flash", "sram"]},
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=["", ""], profile=profile("AB")
        )
        self.assertEqual(
            [item["model_id"] for item in result["models"]["models"]],
            ["AB701Q", "AB701R"],
        )

    def test_package_can_be_completed_from_same_line_model_relation(self) -> None:
        document = document_from_tables(
            (
                "Capabilities",
                [["Feature", "AB70KB"], ["Flash", "64 KB"], ["RAM", "16 KB"]],
            )
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_columns",
                    "title_any": ["capabilities"],
                    "fields": {
                        "flash_kb": {"required": True},
                        "ram_kb": {"required": True},
                        "package_types": {
                            "parser": "package_names",
                            "fallback": "document_model_relation",
                            "required": True,
                        },
                    },
                },
            }],
        }
        result = execute_declarative_plan(
            plan,
            document=document,
            page_texts=["Figure 3. AB70KxT QFN32 pinout"],
            profile=profile("AB"),
        )
        model = result["models"]["models"][0]
        self.assertEqual(model["fields"]["package_types"], ["QFN32"])
        self.assertEqual(model["field_sources"]["package_types"][0]["page"], 1)

    def test_catalog_matrix_accepts_concrete_and_wildcard_headers_together(self) -> None:
        document = document_from_tables(
            (
                "Device summary",
                [
                    ["Reference", "Part number"],
                    ["ZX70xx", "ZX70A1, ZX70B1"],
                ],
            ),
            (
                "Capabilities inventory",
                [
                    ["Feature", "ZX70A1", "ZX70Bx"],
                    ["Flash memory", "16 KB", "16 KB"],
                    ["SRAM", "4 KB", "8 KB"],
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
                        "title_optional_when_model_header_matches": True,
                        "locator": {"required_all": ["flash", "sram"]},
                        "catalog_section": "catalog",
                        "ordering_section": "codes",
                        "density_relation": {
                            "relation": "flash_code_kb", "offset": -1, "field": "flash_kb"
                        },
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                        },
                    },
                },
            ],
        }
        texts = [
            "Ordering information\nPin count\nA = 20 pins\nB = 32 pins\n"
            "Flash memory size\n1 = 16 Kbytes"
        ]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("ZX")
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(set(models), {"ZX70A1", "ZX70B1"})
        self.assertEqual(models["ZX70A1"]["fields"]["ram_kb"], 4)
        self.assertEqual(models["ZX70B1"]["fields"]["ram_kb"], 8)

    def test_ordering_code_disambiguates_literal_wildcard_character(self) -> None:
        document = document_from_tables(
            (
                "Device summary",
                [["Reference", "Part number"], ["QZ70 family", "QZ70A1"]],
            ),
            (
                "Device features",
                [
                    ["Feature", "QZ70A1", "QZ70X1"],
                    ["Flash memory", "16 KB", "16 KB"],
                    ["SRAM", "4 KB", "8 KB"],
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
                    "depends_on": ["catalog"],
                    "config": {
                        "layout": "models_in_columns",
                        "title_any": ["device features"],
                        "locator": {"required_all": ["flash", "sram"]},
                        "catalog_section": "catalog",
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                        },
                    },
                },
            ],
        }
        texts = [
            "Ordering information\nPin count\nA = 20 pins\nX = 40 pins\n"
            "Flash memory size\n1 = 16 Kbytes"
        ]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("QZ")
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(set(models), {"QZ70A1", "QZ70X1"})
        self.assertEqual(models["QZ70A1"]["fields"]["ram_kb"], 4)
        self.assertEqual(models["QZ70X1"]["fields"]["ram_kb"], 8)

    def test_grouped_header_is_aligned_by_document_relations_and_merges_variants(self) -> None:
        header = "QZ70_\n_B2 _A1 _A1 _B1"
        document = document_from_tables(
            (
                "Device features",
                [
                    ["Feature", header, header, header, header],
                    ["Flash memory", "16 KB", "16 KB", "16 KB", "32 KB"],
                    ["Package", "QFN20", "QFN20", "QFN32", "QFN32"],
                    ["12-bit ADC channels", "9", "10", "11", "12"],
                ],
            )
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
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["codes"],
                    "config": {
                        "layout": "models_in_grouped_columns",
                        "title_any": ["device features"],
                        "locator": {"required_all": ["flash"]},
                        "ordering_section": "codes",
                        "alignment_relations": [
                            {
                                "relation": "flash_code_kb",
                                "code_offset": -1,
                                "field": "flash_kb",
                            },
                            {
                                "relation": "pin_count_codes",
                                "code_offset": -2,
                                "row_any": ["package"],
                            },
                        ],
                        "grouped_header": {
                            "prefix_pattern": "(QZ[0-9]+_)",
                            "suffix_pattern": "_([A-Z][0-9A-Z])",
                            "separator_pattern": "[_\\s]+",
                        },
                        "fields": {
                            "flash_kb": {"required": True},
                            "package_types": {"parser": "package_names", "required": True},
                        },
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "fields": {
                            "peripheral_adc": {
                                "value_parser": "adc_instance_count",
                                "reducer": "max",
                                "channel_value_parser": "adc_channel_count",
                                "channel_reducer": "max",
                                "resolution_from_label": True,
                            }
                        },
                    },
                },
            ],
        }
        texts = [
            "Ordering information\nPin count\nA = 20 pins\nB = 32 pins\n"
            "Flash memory size\n1 = 16 Kbytes\n2 = 32 Kbytes"
        ]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("QZ")
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(set(models), {"QZ70A1", "QZ70B1", "QZ70B2"})
        self.assertEqual(models["QZ70A1"]["fields"]["flash_kb"], 16)
        self.assertEqual(models["QZ70A1"]["fields"]["package_types"], ["QFN20"])
        adc = {
            item["chip_id"]: item
            for item in result["peripherals"]["records"]
            if item["peripheral_type"] == "ADC"
        }
        self.assertEqual(adc["QZ70A1"]["channel_count"], 10)

    def test_grouped_header_maps_multiple_density_models_to_each_physical_column(self) -> None:
        test_profile = profile("QZ")
        test_profile.wildcard_markers = ("X",)
        header = "QZ09x\n_FB / _FC _EB / _EC"
        document = document_from_tables(
            (
                "QZ091xB/xC and QZ092xB/xC device features",
                [
                    ["Feature", header, header],
                    ["Flash memory", "128/256 KB", "128/256 KB"],
                    ["SRAM", "36 (QZ091xx) / 30 (QZ092xx)", "36 (QZ091xx) / 30 (QZ092xx)"],
                    ["Package", "QFN20", "QFN24"],
                ],
            )
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
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["codes"],
                    "config": {
                        "layout": "models_in_grouped_columns",
                        "title_any": ["device features"],
                        "locator": {"required_all": ["flash", "sram"]},
                        "ordering_section": "codes",
                        "alignment_relations": [
                            {"relation": "flash_code_kb", "code_offset": -1, "field": "flash_kb"},
                            {"relation": "pin_count_codes", "code_offset": -2, "row_any": ["package"]},
                        ],
                        "grouped_header": {
                            "prefix_pattern": "(QZ[A-Z0-9]+_?)",
                            "suffix_pattern": "_([A-Z][0-9A-Z])",
                            "separator_pattern": "[_\\s]+",
                        },
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                            "package_types": {"parser": "package_names", "required": True},
                        },
                    },
                },
            ],
        }
        texts = [
            "Ordering information\nPin count\nF = 20 pins\nE = 24 pins\n"
            "Flash memory size\nB = 128 Kbytes\nC = 256 Kbytes"
        ]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=test_profile
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(
            set(models),
            {
                "QZ091FB", "QZ091FC", "QZ091EB", "QZ091EC",
                "QZ092FB", "QZ092FC", "QZ092EB", "QZ092EC",
            },
        )
        self.assertEqual(models["QZ091FB"]["fields"]["flash_kb"], 128)
        self.assertEqual(models["QZ091FC"]["fields"]["flash_kb"], 256)
        self.assertEqual(models["QZ091FB"]["fields"]["ram_kb"], 36)
        self.assertEqual(models["QZ092FB"]["fields"]["ram_kb"], 30)

    def test_grouped_header_normalizes_reversed_ordering_suffix_tokens(self) -> None:
        header = "QZ71_\n/ 8F_ BF_ / Nxx8F_ NxxBF_"
        document = document_from_tables(
            (
                "Device features",
                [
                    ["Feature", header, header],
                    ["Flash memory", "64/128 KB", "64/128 KB"],
                    ["SRAM", "24 KB", "24 KB"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "codes",
                    "component": "ordering_code_legend",
                    "config": {"required_relations": ["flash_code_kb"]},
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["codes"],
                    "config": {
                        "layout": "models_in_grouped_columns",
                        "locator": {"required_all": ["flash", "sram"]},
                        "ordering_section": "codes",
                        "alignment_relations": [
                            {"relation": "flash_code_kb", "code_offset": -1, "field": "flash_kb"}
                        ],
                        "grouped_header": {
                            "prefix_pattern": "(QZ[A-Z0-9]+_?)",
                            "suffix_pattern": "_([A-Z][0-9A-Z])",
                            "separator_pattern": "[_\\s]+",
                        },
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                },
            ],
        }
        texts = ["Ordering information\nFlash memory size\n8 = 64 Kbytes\nB = 128 Kbytes"]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("QZ")
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(set(models), {"QZ71F8", "QZ71FB"})
        self.assertEqual(models["QZ71F8"]["fields"]["flash_kb"], 64)
        self.assertEqual(models["QZ71FB"]["fields"]["flash_kb"], 128)

    def test_catalog_density_uses_ordering_value_when_matrix_declares_only_upper_bound(self) -> None:
        document = document_from_tables(
            (
                "Device summary",
                [["Reference", "Part number"], ["ZX70Ax", "ZX70A1/A2"]],
            ),
            (
                "Device features",
                [
                    ["Feature", "ZX70Ax"],
                    ["Flash memory", "Up to 32 KB"],
                    ["SRAM", "8 KB"],
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
                        "catalog_section": "catalog",
                        "ordering_section": "codes",
                        "density_relation": {
                            "relation": "flash_code_kb", "offset": -1, "field": "flash_kb"
                        },
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                        },
                    },
                },
            ],
        }
        texts = [
            "Ordering information\nPin count\nA = 20 pins\n"
            "Flash memory size\n1 = 16 Kbytes\n2 = 32 Kbytes"
        ]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("ZX")
        )
        models = {item["model_id"]: item for item in result["models"]["models"]}
        self.assertEqual(models["ZX70A1"]["fields"]["flash_kb"], 16)
        self.assertEqual(models["ZX70A2"]["fields"]["flash_kb"], 32)
        self.assertEqual(
            models["ZX70A1"]["field_sources"]["flash_kb"][0]["raw_value"],
            "1 = 16 Kbytes",
        )

    def test_catalog_density_uses_ordering_value_when_matrix_omits_density_row(self) -> None:
        document = document_from_tables(
            (
                "ZX70xx Device features",
                [
                    ["Feature", "ZX70A0"],
                    ["SRAM", "8 KB"],
                    ["CPU frequency", "80 MHz"],
                ],
            ),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "codes",
                    "component": "ordering_code_legend",
                    "config": {"required_relations": ["flash_code_kb"]},
                },
                {
                    "id": "catalog",
                    "component": "model_catalog",
                    "required": False,
                    "depends_on": ["codes"],
                    "config": {
                        "title_any": ["device summary"],
                        "ordering_section": "codes",
                        "code_positions": [
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
                        "locator": {"required_all": ["ram"]},
                        "catalog_section": "catalog",
                        "ordering_section": "codes",
                        "density_relation": {
                            "relation": "flash_code_kb", "offset": -1, "field": "flash_kb"
                        },
                        "fields": {
                            "flash_kb": {"required": True},
                            "ram_kb": {"required": True},
                            "max_freq_mhz": {"required": True},
                        },
                    },
                },
            ],
        }
        texts = ["Ordering information\nFlash memory size\n0 = 0-1 Kbytes"]
        result = execute_declarative_plan(
            plan, document=document, page_texts=texts, profile=profile("ZX")
        )
        model = result["models"]["models"][0]
        self.assertEqual(model["model_id"], "ZX70A0")
        self.assertEqual(
            model["fields"],
            {"flash_kb": 0, "ram_kb": 8, "max_freq_mhz": 80},
        )
        self.assertEqual(
            model["field_sources"]["flash_kb"][0]["raw_value"],
            "0 = 0-1 Kbytes",
        )

    def test_ram_regions_are_summed_without_counting_backup_or_duplicate_rows(self) -> None:
        document = document_from_tables(
            (
                "Capabilities",
                [
                    ["Feature", "QX10"],
                    ["Flash memory", "64 KB"],
                    ["SRAM on data bus", "12 KB"],
                    ["Core coupled memory RAM", "4 KB"],
                    ["SRAM on data bus", "12 KB"],
                    ["Backup SRAM", "2 KB"],
                    ["CPU frequency", "48 MHz"],
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
                    "locator": {"required_all": ["flash", "sram"]},
                    "fields": {
                        "flash_kb": {"required": True},
                        "ram_kb": {"reducer": "sum_distinct_regions", "required": True},
                    },
                },
            }],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("QX")
        )
        self.assertEqual(result["models"]["models"][0]["fields"]["ram_kb"], 16)

    def test_column_matrix_is_scoped_to_document_catalog_when_available(self) -> None:
        document = document_from_tables(
            ("Main features", [["Feature", "QX10"], ["Flash", "32 KB"], ["RAM", "8 KB"]]),
            ("Family comparison", [["Feature", "QX90", "QX91"], ["Flash", "64 KB", "64 KB"], ["RAM", "16 KB", "16 KB"]]),
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_columns",
                        "locator": {"required_all": ["flash", "ram"]},
                        "catalog_section": "catalog",
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "executed section"):
            execute_declarative_plan(plan, document=document, page_texts=["", ""], profile=profile("QX"))

        plan["sections"].insert(0, {
            "id": "catalog",
            "component": "model_matrix",
            "config": {
                "layout": "models_in_columns",
                "title_any": ["main features"],
            },
        })
        plan["sections"][1]["depends_on"] = ["catalog"]
        result = execute_declarative_plan(
            plan, document=document, page_texts=["", ""], profile=profile("QX")
        )
        self.assertEqual(
            [item["model_id"] for item in result["models"]["models"]], ["QX10"]
        )

    def test_catalog_matrix_uses_geometric_lineage_when_continuation_collapses_density_columns(self) -> None:
        def cell(fragment, page, row, column, text, bbox, inherited_from=None):
            return CellIR(
                cell_id=f"f{fragment}-r{row}-c{column}", page=page, fragment=fragment,
                row=row, column=column, raw_text=text, normalized_text=text.casefold(),
                bbox=bbox, inherited_from=inherited_from,
            )

        cells = (
            cell(1, 2, 0, 0, "Feature", (0, 0, 10, 10)),
            cell(1, 2, 0, 1, "ZX90Ax", (10, 0, 30, 10)),
            cell(1, 2, 0, 2, "", None, "f1-r0-c1"),
            cell(1, 2, 1, 0, "Flash memory in Kbytes", (0, 10, 10, 20)),
            cell(1, 2, 1, 1, "16", (10, 10, 20, 20)),
            cell(1, 2, 1, 2, "32", (20, 10, 30, 20)),
            cell(1, 2, 2, 0, "SRAM", (0, 20, 10, 30)),
            cell(1, 2, 2, 1, "4 KB", (10, 20, 20, 30)),
            cell(1, 2, 2, 2, "8 KB", (20, 20, 30, 30)),
            cell(2, 3, 0, 0, "Feature", (0, 0, 10, 10)),
            cell(2, 3, 0, 1, "ZX90Ax", (10, 0, 30, 10)),
            cell(2, 3, 0, 2, "", None, "f2-r0-c1"),
            cell(2, 3, 1, 0, "Packages", (0, 10, 10, 20)),
            cell(2, 3, 1, 1, "LQFP32", (10, 10, 20, 20)),
            cell(2, 3, 1, 2, "QFN32", (20, 10, 30, 20)),
        )
        matrix = TableIR(
            table_id="matrix", title="Device features", pages=(2, 3), continued=True,
            orientations=(0, 0), bboxes=((0, 0, 30, 30), (0, 0, 30, 20)), cells=cells,
        )
        catalog = document_from_tables(
            ("Device summary", [["Reference", "Part number"], ["ZX90xx", "ZX90A1, ZX90A2"]]),
        )
        document = DocumentIR(
            "unseen.pdf", 3,
            (
                catalog.pages[0],
                PageIR(page=2, width=100, height=100, rotation=0, text="", table_ids=("matrix",)),
                PageIR(page=3, width=100, height=100, rotation=0, text="", table_ids=("matrix",)),
            ),
            (catalog.tables[0], matrix),
        )
        plan = {
            "version": 1,
            "sections": [
                {"id": "codes", "component": "ordering_code_legend", "config": {"required_relations": ["pin_count_codes", "flash_code_kb"]}},
                {"id": "catalog", "component": "model_catalog", "depends_on": ["codes"], "config": {"title_any": ["device summary"], "ordering_section": "codes", "code_positions": [{"relation": "pin_count_codes", "offset": -2}, {"relation": "flash_code_kb", "offset": -1}]}},
                {"id": "models", "component": "model_matrix", "depends_on": ["codes", "catalog"], "config": {"layout": "models_from_catalog", "title_any": ["device features"], "locator": {"required_all": ["flash", "sram"]}, "catalog_section": "catalog", "ordering_section": "codes", "density_relation": {"relation": "flash_code_kb", "offset": -1, "field": "flash_kb"}, "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}, "package_types": {"required": True}}}},
            ],
        }
        texts = ["Ordering information\nPin count\nA = 20 pins\nFlash memory size\n1 = 16 Kbytes\n2 = 32 Kbytes", "", ""]
        result = execute_declarative_plan(plan, document=document, page_texts=texts, profile=profile("ZX"))
        models = result["models"]["models"]
        self.assertEqual(models[0]["fields"]["package_types"], ["LQFP32"])
        self.assertEqual(models[1]["fields"]["package_types"], ["QFN32"])

    def test_optional_structural_component_falls_through_without_hiding_config_errors(self) -> None:
        document = document_from_tables(
            ("Device features", [["Feature", "QX10"], ["Flash", "32 KB"], ["SRAM", "8 KB"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "codes",
                    "component": "ordering_code_legend",
                    "required": False,
                    "config": {"required_relations": ["pin_count_codes"]},
                },
                {
                    "id": "models",
                    "component": "model_matrix",
                    "depends_on": ["codes"],
                    "config": {
                        "layout": "models_in_columns",
                        "locator": {"required_all": ["flash", "sram"]},
                        "fields": {"flash_kb": {"required": True}, "ram_kb": {"required": True}},
                    },
                },
            ],
        }
        result = execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("QX"))
        self.assertIsNone(result["codes"])
        self.assertEqual(result["models"]["models"][0]["model_id"], "QX10")

        plan["sections"][0]["config"] = {"required_relations": ["not_registered"]}
        with self.assertRaisesRegex(ValueError, "unknown relations"):
            execute_declarative_plan(plan, document=document, page_texts=[""], profile=profile("QX"))

    def test_missing_optional_catalog_allows_later_model_layout_variant(self) -> None:
        document = document_from_tables()
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "codes",
                    "component": "ordering_code_legend",
                    "required": False,
                    "config": {"required_relations": ["pin_count_codes", "flash_code_kb"]},
                },
                {
                    "id": "catalog",
                    "component": "model_catalog",
                    "required": False,
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
                        "variants": [
                            {
                                "layout": "models_from_catalog",
                                "catalog_section": "catalog",
                                "ordering_section": "codes",
                                "density_relation": {
                                    "relation": "flash_code_kb", "offset": -1, "field": "flash_kb"
                                },
                            },
                            {
                                "layout": "models_in_text_summary",
                                "fields": {
                                    "flash_kb": {"required": True},
                                    "ram_kb": {"required": True},
                                },
                            },
                        ]
                    },
                },
            ],
        }
        text = "QX10 product summary\nFlash memory 32 KB\nSRAM 8 KB"
        result = execute_declarative_plan(
            plan, document=document, page_texts=[text], profile=profile("QX")
        )
        self.assertEqual(result["models"]["models"][0]["model_id"], "QX10")

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

    def test_text_summary_layout_uses_dictionary_semantics_for_unseen_family(self) -> None:
        document = document_from_tables()
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_text_summary",
                    "scan_page_count": 2,
                    "require_single_model": True,
                    "fields": {
                        "flash_kb": {"required": True},
                        "ram_kb": {"required": True},
                        "max_freq_mhz": {"required": True},
                        "operating_voltage": {"required": True},
                        "package_types": {"required": True},
                    },
                },
            }],
        }
        text = (
            "QX10 Product summary\n"
            "Memory options include 32-Kbyte Flash memory\n"
            "64-Kbyte Flash memory, 8-Kbyte SRAM\n"
            "Cortex-M4 CPU frequency\nup to 48 MHz speed\nSupply voltage 1.8 to 3.6 V\n"
            "Package selection chapter\n-40 to 85 C temperature range\nPackage LQFP48"
        )
        result = execute_declarative_plan(plan, document=document, page_texts=[text], profile=profile("QX"))
        model = result["models"]["models"][0]
        self.assertEqual(model["model_id"], "QX10")
        self.assertEqual(
            model["fields"],
            {
                "flash_kb": 64,
                "ram_kb": 8,
                "max_freq_mhz": 48,
                "voltage_min": 1.8,
                "voltage_max": 3.6,
                "package_types": ["LQFP48"],
            },
        )
        self.assertIn("64-Kbyte", model["field_sources"]["flash_kb"][0]["raw_value"])
        self.assertEqual(model["field_sources"]["package_types"][0]["raw_value"], "Package LQFP48")

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

    def test_electrical_projection_maps_normalized_ranges_with_evidence(self) -> None:
        document = document_from_tables(
            (
                "Product selection",
                [
                    ["Part number", "Flash", "Operating voltage", "Operating temperature"],
                    ["VZ10", "16 KB", "1.8 to 3.6 V", "-40 to 85 C"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "locator": {"required_any": ["flash"]},
                        "model_header_any": ["part number"],
                        "fields": {
                            "operating_voltage": {},
                            "operating_temperature": {},
                        },
                    },
                },
                {
                    "id": "electrical",
                    "component": "electrical_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "mappings": [
                            {
                                "semantic": "operating_voltage",
                                "param_name": "VDD",
                                "unit": "V",
                                "evidence_fields": ["operating_voltage"],
                            },
                            {
                                "semantic": "operating_temperature",
                                "param_name": "T_operating",
                                "unit": "°C",
                                "evidence_fields": ["operating_temperature"],
                            },
                        ],
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("VZ")
        )
        records = result["electrical"]["records"]
        self.assertEqual(
            [(row["param_name"], row["min_value"], row["max_value"], row["unit"]) for row in records],
            [("VDD", 1.8, 3.6, "V"), ("T_operating", -40.0, 85.0, "°C")],
        )
        self.assertEqual(records[0]["value_sources"][0]["source_cell_id"], "t1-r1-c2")
        self.assertEqual(records[1]["value_sources"][0]["source_cell_id"], "t1-r1-c3")

    def test_electrical_projection_rejects_answer_shaped_mapping(self) -> None:
        document = document_from_tables(
            ("Product selection", [["Part number", "Operating voltage"], ["VZ10", "1.8 to 3.6 V"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "locator": {"required_any": ["operating voltage"]},
                        "model_header_any": ["part number"],
                        "fields": {"operating_voltage": {}},
                    },
                },
                {
                    "id": "electrical",
                    "component": "electrical_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "mappings": [{
                            "semantic": "operating_voltage",
                            "param_name": "VDD",
                            "unit": "V",
                            "evidence_fields": ["operating_voltage"],
                            "expected_value": 3.6,
                        }],
                    },
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "implementation details or answers"):
            execute_declarative_plan(
                plan, document=document, page_texts=[""], profile=profile("VZ")
            )

    def test_document_ranges_supply_shared_operating_conditions(self) -> None:
        document = document_from_tables(
            ("Product selection", [["Part number", "Flash"], ["VZ10", "16 KB"]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "locator": {"required_any": ["flash"]},
                        "model_header_any": ["part number"],
                    },
                },
                {
                    "id": "conditions",
                    "component": "document_ranges",
                    "config": {
                        "fields": ["operating_voltage", "operating_temperature"],
                        "scan_page_count": 2,
                    },
                },
                {
                    "id": "electrical",
                    "component": "electrical_projection",
                    "depends_on": ["models", "conditions"],
                    "config": {
                        "model_section": "models",
                        "mappings": [
                            {
                                "semantic": "operating_voltage",
                                "param_name": "VDD",
                                "unit": "V",
                                "evidence_fields": ["operating_voltage"],
                                "fallback_section": "conditions",
                            },
                            {
                                "semantic": "operating_temperature",
                                "param_name": "T_operating",
                                "unit": "°C",
                                "evidence_fields": ["operating_temperature"],
                                "fallback_section": "conditions",
                            },
                        ],
                    },
                },
            ],
        }
        text = (
            "工作电压：2.7V ~ 5.5V@8MHz 2.2V ~ 5.5V@4MHz\n"
            "工作温度：-40℃~+85℃"
        )
        result = execute_declarative_plan(
            plan, document=document, page_texts=[text], profile=profile("VZ")
        )
        records = result["electrical"]["records"]
        self.assertEqual(
            [(row["param_name"], row["min_value"], row["max_value"]) for row in records],
            [("VDD", 2.2, 5.5), ("T_operating", -40.0, 85.0)],
        )
        self.assertIn("工作电压", records[0]["value_sources"][0]["raw_value"])

    def test_peripheral_projection_preserves_instance_channel_and_resolution_semantics(self) -> None:
        document = document_from_tables(
            (
                "Product selection",
                [
                    ["Part number", "UART数量", "PWM通道数量", "1 2 b i t ADC通道数量"],
                    ["VZ10", "2", "6", "8"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "locator": {"required_any": ["uart", "pwm", "adc"]},
                        "model_header_any": ["part number"],
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "fields": {
                            "peripheral_uart": {"value_parser": "first_integer"},
                            "peripheral_pwm": {"value_parser": "first_integer"},
                            "peripheral_adc": {
                                "value_parser": "adc_instance_count",
                                "channel_value_parser": "adc_channel_count",
                                "resolution_from_label": True,
                            },
                        },
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("VZ")
        )
        records = {
            row["peripheral_type"]: row
            for row in result["peripherals"]["records"]
        }
        self.assertEqual(records["UART"]["instance_count"], 2)
        self.assertIsNone(records["UART"]["channel_count"])
        self.assertIsNone(records["PWM"]["instance_count"])
        self.assertEqual(records["PWM"]["channel_count"], 6)
        self.assertIsNone(records["ADC"]["instance_count"])
        self.assertEqual(records["ADC"]["channel_count"], 8)
        self.assertEqual(records["ADC"]["max_resolution"], 12)
        self.assertEqual(records["ADC"]["value_sources"][0]["source_cell_id"], "t1-r1-c3")

    def test_models_in_rows_reconstructs_vertically_fragmented_headers(self) -> None:
        document = document_from_tables(
            (
                "Product selection",
                [
                    ["", "触"],
                    ["", "摸"],
                    ["Part number", "按"],
                    ["", "键"],
                    ["", "数"],
                    ["VZ10", "5"],
                ],
            )
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "title_any": ["product selection"],
                        "model_header_any": ["part number"],
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "fields": {"peripheral_touch": {"value_parser": "first_integer"}},
                    },
                },
            ],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=[""], profile=profile("VZ")
        )
        row = result["peripherals"]["records"][0]
        self.assertEqual(row["peripheral_type"], "Touch")
        self.assertEqual(row["channel_count"], 5)
        self.assertEqual(row["value_sources"][0]["source_cell_id"], "t1-r5-c1")

    def test_titled_model_matrix_outranks_larger_titleless_body_match(self) -> None:
        document = document_from_tables(
            (
                "Device features",
                [["Feature", "VZ10"], ["Flash", "16 KB"], ["RAM", "4 KB"]],
            ),
            (
                "Document revision history",
                [
                    ["Date", "Changes"],
                    ["2020", "Added VZ20; updated Flash and RAM"],
                    ["2021", "Added VZ30; updated Flash and RAM"],
                ],
            ),
        )
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_columns",
                    "title_any": ["device features"],
                    "title_optional_when_model_header_matches": True,
                    "locator": {"required_all": ["flash", "ram"]},
                    "fields": {
                        "flash_kb": {"parser": "quantity", "reducer": "max", "required": True},
                        "ram_kb": {"parser": "quantity", "reducer": "max", "required": True},
                    },
                },
            }],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=["", ""], profile=profile("VZ")
        )
        self.assertEqual(result["models"]["table_id"], "table-1")
        self.assertEqual(
            [(item["model_id"], item["fields"]) for item in result["models"]["models"]],
            [("VZ10", {"flash_kb": 16, "ram_kb": 4})],
        )

    def test_filename_target_series_selects_the_matching_peer_matrix(self) -> None:
        base = document_from_tables(
            (
                "ZX60xx Device features",
                [["Feature", "ZX60A1"], ["Flash", "16 KB"], ["RAM", "4 KB"]],
            ),
            (
                "ZX70xx Device features",
                [["Feature", "ZX70A1"], ["Flash", "32 KB"], ["RAM", "8 KB"]],
            ),
        )
        document = DocumentIR("ZX70A1.pdf", base.page_count, base.pages, base.tables)
        plan = {
            "version": 1,
            "sections": [{
                "id": "models",
                "component": "model_matrix",
                "config": {
                    "layout": "models_in_columns",
                    "title_any": ["device features"],
                    "locator": {"required_all": ["flash", "ram"]},
                    "fields": {
                        "flash_kb": {"required": True},
                        "ram_kb": {"required": True},
                    },
                },
            }],
        }
        result = execute_declarative_plan(
            plan, document=document, page_texts=["", ""], profile=profile("ZX")
        )
        self.assertEqual(result["models"]["table_id"], "table-2")
        self.assertEqual(result["models"]["models"][0]["model_id"], "ZX70A1")

    def test_document_values_excludes_model_variable_summary_from_global_fallback(self) -> None:
        plan = {
            "version": 1,
            "sections": [{
                "id": "shared",
                "component": "document_values",
                "config": {
                    "fields": ["peripheral_i2c", "peripheral_touch"],
                    "scan_page_count": 1,
                    "association_window": 24,
                    "statement_any": ["内置", "支持"],
                    "scope_exclude_any": ["不同型号会有不同"],
                },
            }],
        }
        result = execute_declarative_plan(
            plan,
            document=document_from_tables(("irrelevant", [["x"]])),
            page_texts=[
                "内置 1 路 I2C\n"
                "基于 8051 内核并集成 Touch Key\n"
                "最大支持 20 通道触摸（不同型号会有不同）"
            ],
            profile=profile("VZ"),
        )
        self.assertEqual(len(result["shared"]["values"]), 1)
        self.assertIn("i2c", result["shared"]["values"][0]["label"])

    def test_explicit_model_absence_blocks_document_peripheral_fallback(self) -> None:
        document = document_from_tables(
            ("Product selection", [["Part number", "触摸按键数量"], ["VZ10", ""]])
        )
        plan = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "title_any": ["product selection"],
                        "model_header_any": ["part number"],
                    },
                },
                {
                    "id": "shared",
                    "component": "document_values",
                    "config": {
                        "fields": ["peripheral_touch"],
                        "scan_page_count": 1,
                        "association_window": 12,
                        "statement_any": ["支持"],
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models", "shared"],
                    "config": {
                        "model_section": "models",
                        "fallback_section": "shared",
                        "fields": {"peripheral_touch": {"value_parser": "first_integer"}},
                    },
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "produced no records"):
            execute_declarative_plan(
                plan,
                document=document,
                page_texts=["最大支持 16 触摸通道"],
                profile=profile("VZ"),
            )

    def test_peripheral_projection_rejects_unregistered_or_answer_like_fields(self) -> None:
        document = document_from_tables(
            ("Product selection", [["Part number", "UART"], ["VZ10", "2"]])
        )
        base = {
            "version": 1,
            "sections": [
                {
                    "id": "models",
                    "component": "model_matrix",
                    "config": {
                        "layout": "models_in_rows",
                        "locator": {"required_any": ["uart"]},
                        "model_header_any": ["part number"],
                    },
                },
                {
                    "id": "peripherals",
                    "component": "peripheral_projection",
                    "depends_on": ["models"],
                    "config": {
                        "model_section": "models",
                        "fields": {"peripheral_can": {}},
                    },
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "not registered"):
            execute_declarative_plan(
                base, document=document, page_texts=[""], profile=profile("VZ")
            )
        base["sections"][1]["config"]["fields"] = {
            "peripheral_uart": {"expected_value": 2}
        }
        with self.assertRaisesRegex(ValueError, "implementation details or answers"):
            execute_declarative_plan(
                base, document=document, page_texts=[""], profile=profile("VZ")
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
