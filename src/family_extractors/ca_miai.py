from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from core.collector import Collector
from core.documents import extract_page_texts
from core.evidence import Evidence
from core.ir import DocumentIR
from core.profile import DocumentProfile, load_profile
from core.table_ir_builder import build_document_ir
from core.table_ir_legacy_view import IRDocumentTableView
from core.table_engine import (
    logical_row_model_table as engine_logical_row_model_table,
    logical_transposed_model_table as engine_logical_transposed_model_table,
    rectangular_rows,
)
from core.text import clean, compact, parse_number, parse_range, scalar_or_none
from core.units import convert_number
from topologies.matrix_observer import table_grids

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "profiles" / "ca_miai_v1.json"
ACTIVE_PROFILE: DocumentProfile = load_profile(DEFAULT_PROFILE_PATH)
MODEL_RE = ACTIVE_PROFILE.model_re
PACKAGE_RE = ACTIVE_PROFILE.package_re
PIN_RE = ACTIVE_PROFILE.pin_re
UNSUPPORTED_MARKS = {"", "-", "--", "---", "×", "", "X", "不支持", "无", "0"}


SUPPORTED_MARKS = {"√", "Y", "YES", "支持", "有", "1"}
RULE_CONFIG: dict[str, Any] = {}


def configure(profile: DocumentProfile, rules: dict[str, Any]) -> None:
    """Bind one CA/MiAi rule package without leaking its logic into the runner."""
    global ACTIVE_PROFILE, MODEL_RE, PACKAGE_RE, PIN_RE, RULE_CONFIG
    ACTIVE_PROFILE = profile
    MODEL_RE = profile.model_re
    PACKAGE_RE = profile.package_re
    PIN_RE = profile.pin_re
    RULE_CONFIG = rules


def model_tokens(text: str, exact_only: bool = False) -> list[str]:
    return ACTIVE_PROFILE.model_tokens(text, exact_only=exact_only)


def is_exact_model(token: str) -> bool:
    return ACTIVE_PROFILE.is_exact_model(token)


def series_for_model(model: str) -> str:
    return ACTIVE_PROFILE.series_for_model(model)


def normalize_function(token: str) -> str:
    return ACTIVE_PROFILE.normalize_function(token)


def parse_memory_kb(value: object) -> int | float | None:
    return convert_number(value, RULE_CONFIG["unit_conversions"]["memory_kb"])


def basic_page_index(texts: list[str]) -> int | None:
    locator = RULE_CONFIG.get("document_locators", {}).get("basic", {})
    window = locator.get("preferred_page_window", [1, 25])
    page_limit = int(window[1])
    scored_terms = locator.get("scored_terms", [])
    minimum_score = int(locator.get("minimum_score", 6))
    best: tuple[int, int] | None = None
    for index, text in enumerate(texts[:page_limit]):
        normalized = compact(text)
        upper = normalized.upper()
        score = sum(
            int(group["weight"])
            for group in scored_terms
            if any(compact(term).upper() in upper for term in group.get("any", []))
        )
        score -= 4 if text.count("....") > 5 else 0
        if score >= minimum_score and (best is None or score > best[0]):
            best = (score, index)
    return best[1] if best else None


def parse_basic_defaults(text: str) -> dict[str, Any]:
    normalized = clean(text).replace("I²C", "I2C").replace("I 2 C", "I2C")
    normalized = normalized.replace("‐", "-").replace("–", "-").replace("—", "-")
    defaults: dict[str, Any] = {"peripherals": {}}
    if re.search(r"1T\s*8051", normalized, re.I):
        defaults["core"] = "1T 8051"
    elif re.search(r"8051", normalized, re.I):
        defaults["core"] = "8051"
    else:
        cortex = re.search(r"Cortex[- ]?M\d+\+?", normalized, re.I)
        if cortex:
            defaults["core"] = "ARM"
        elif re.search(r"\bARM\b", normalized, re.I):
            defaults["core"] = "ARM"

    frequency_patterns = [
        r"CPU\s*(?:频率|主频)[^\d]{0,20}(?:最高|最大)?[^\d]{0,10}(\d+(?:\.\d+)?)\s*MHz",
        r"最高(?:工作|运行)?频率[^\d]{0,10}(\d+(?:\.\d+)?)\s*MHz",
        r"最高(?:工作|运行)?主频[^\d]{0,10}(\d+(?:\.\d+)?)\s*MHz",
        r"CPU\s*频率[^@\n]{0,40}@\s*(\d+(?:\.\d+)?)\s*MHz",
    ]
    for pattern in frequency_patterns:
        match = re.search(pattern, normalized, re.I)
        if match:
            defaults["max_freq_mhz"] = scalar_or_none(float(match.group(1)))
            break

    voltage_ranges = []
    for match in re.finditer(r"(?:工作|电源)电压[^\d-]{0,40}(\d+(?:\.\d+)?)\s*V?\s*(?:-|~|～|至)\s*(\d+(?:\.\d+)?)\s*V", normalized, re.I):
        voltage_ranges.append((float(match.group(1)), float(match.group(2))))
    if voltage_ranges:
        defaults["voltage_min"] = min(v[0] for v in voltage_ranges)
        defaults["voltage_max"] = max(v[1] for v in voltage_ranges)

    internal_patterns = [
        r"(\d+(?:\.\d+)?)\s*(K|KB|字节|BYTE)?\s*内部\s*RAM",
        r"内部\s*RAM\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(K|KB|字节|BYTE)?",
    ]
    external_patterns = [
        r"(\d+(?:\.\d+)?)\s*(K|KB|字节|BYTE)?\s*外部\s*RAM",
        r"外部\s*RAM\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(K|KB|字节|BYTE)?",
    ]
    for patterns, key in ((internal_patterns, "sram_internal_kb"), (external_patterns, "sram_external_kb")):
        for pattern in patterns:
            match = re.search(pattern, normalized, re.I)
            if match:
                defaults[key] = parse_memory_kb(f"{match.group(1)}{match.group(2) or ''}")
                break

    eeprom = re.search(r"EEPROM\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(K|KB|字节|BYTE)?", normalized, re.I)
    if eeprom:
        defaults["eeprom_kb"] = parse_memory_kb(f"{eeprom.group(1)}{eeprom.group(2) or ''}")

    peripheral_patterns = {
        "UART": [r"(?:支持|内置|最多支持)\s*(\d+)\s*(?:个|路)?\s*(?:全双工)?\s*UART", r"(\d+)\s*路\s*全双工串口"],
        "I2C": [r"(?:支持|内置)\s*(\d+)\s*(?:个|路)?\s*I2C"],
        "SPI": [r"(?:支持|内置)\s*(\d+)\s*(?:个|路)?\s*(?:4线)?\s*SPI"],
        "PWM": [r"(?:支持|最多有|最多支持)\s*(\d+)\s*(?:个|路|通道)?\s*PWM"],
        "ADC": [r"(?:支持|最多支持)\s*(\d+)\s*(?:个|路|通道)?\s*(\d+)\s*位\s*(?:SAR\s*)?ADC"],
        "Touch": [r"(?:最大|最多)?\s*支持\s*(\d+)\s*(?:个|路|通道)?\s*(?:触摸|Touch)"],
    }
    for ptype, patterns in peripheral_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, normalized, re.I)
            if match:
                defaults["peripherals"][ptype] = {"count": int(match.group(1))}
                if ptype == "ADC" and match.lastindex and match.lastindex >= 2:
                    defaults["peripherals"][ptype]["resolution"] = int(match.group(2))
                break
    pwm_res = re.search(r"(\d+)\s*(?:Bit|位)\s*PWM", normalized, re.I)
    if pwm_res:
        defaults["peripherals"].setdefault("PWM", {})["resolution"] = int(pwm_res.group(1))
    adc_res = re.search(r"(\d+)\s*(?:Bit|位)\s*(?:SAR\s*)?ADC", normalized, re.I)
    if adc_res:
        defaults["peripherals"].setdefault("ADC", {})["resolution"] = int(adc_res.group(1))
    return defaults


def logical_model_table(table_obj: Any) -> dict[str, Any] | None:
    parsed = logical_model_rows(rectangular_rows(table_obj.extract()))
    if parsed:
        parsed["bbox"] = [float(value) for value in table_obj.bbox]
    return parsed


def logical_model_rows(rows: list[list[str]]) -> dict[str, Any] | None:
    return engine_logical_row_model_table(
        rows,
        model_tokens=lambda value: model_tokens(value),
        package_re=PACKAGE_RE,
    )


def logical_transposed_model_table(table_obj: Any) -> dict[str, Any] | None:
    parsed = logical_transposed_model_rows(rectangular_rows(table_obj.extract()))
    if parsed:
        parsed["bbox"] = [float(value) for value in table_obj.bbox]
    return parsed


def logical_transposed_model_rows(rows: list[list[str]]) -> dict[str, Any] | None:
    return engine_logical_transposed_model_table(
        rows,
        exact_model_tokens=lambda value: model_tokens(value, exact_only=True),
        model_header_terms=("器件型号", "芯片型号", "产品型号"),
    )


def semantic_columns(headers: dict[int, str]) -> dict[str, int]:
    mappings: dict[str, int] = {}
    aliases = {
        key: [compact(item).upper() for item in values]
        for key, values in RULE_CONFIG.get("header_aliases", {}).items()
    }

    def configured_match(name: str, header: str) -> bool:
        for alias in aliases.get(name, []):
            if alias == "型号":
                if header == alias:
                    return True
            elif alias in header:
                return True
        return False

    for column, header in headers.items():
        h = header.upper()
        if configured_match("chip_id", h) or "芯片型号" in h or "器件型号" in h or "产品型号" in h or h == "型号":
            mappings.setdefault("chip_id", column)
        elif configured_match("mtp", h) or "MTP" in h:
            mappings.setdefault("mtp", column)
        elif configured_match("eeprom", h) or "EEPROM" in h:
            mappings.setdefault("eeprom", column)
        elif configured_match("flash", h) or "FLASH" in h:
            mappings.setdefault("flash", column)
        elif configured_match("external_ram", h) or ("外部" in h and ("RAM" in h or "SRAM" in h)) or h.startswith("SRAM"):
            mappings.setdefault("external_ram", column)
        elif configured_match("voltage", h) or "工作电压" in h or "电压范围" in h:
            mappings.setdefault("voltage", column)
        elif configured_match("package", h) or "封装" in h:
            mappings.setdefault("package", column)
        elif configured_match("UART", h) or "UART" in h:
            mappings.setdefault("UART", column)
        elif configured_match("I2C", h) or "I2C" in h or "I²C" in h:
            mappings.setdefault("I2C", column)
        elif configured_match("SPI", h) or "SPI" in h:
            mappings.setdefault("SPI", column)
        elif configured_match("PWM", h) or ("PWM" in h and ("数量" in h or "通道" in h or h == "PWM")):
            mappings.setdefault("PWM", column)
        elif configured_match("ADC", h) or "ADC" in h:
            mappings.setdefault("ADC", column)
        elif configured_match("Touch", h) or "触摸" in h or "TOUCH" in h:
            mappings.setdefault("Touch", column)
    return mappings


def extract_models_from_pdf(pdf: pdfplumber.PDF, texts: list[str]) -> list[dict[str, Any]]:
    locator = RULE_CONFIG.get("document_locators", {}).get("model_table", {})
    required_any = [compact(item).upper() for item in locator.get("required_any", ["芯片型号", "器件型号", "产品型号"])]
    supporting_any = [compact(item).upper() for item in locator.get("supporting_any", ["封装", "FLASH", "MTP"])]
    result = []
    for page_index, text in enumerate(texts):
        ntext = compact(text).upper()
        exact = model_tokens(text, exact_only=True)
        has_model_header = any(label in ntext for label in required_any)
        if not exact or not has_model_header or not any(label in ntext for label in supporting_any):
            continue
        page = pdf.pages[page_index]
        for table_obj in page.find_tables():
            parsed = logical_model_table(table_obj) or logical_transposed_model_table(table_obj)
            if not parsed:
                continue
            columns = semantic_columns(parsed["headers"])
            if "chip_id" not in columns:
                continue
            expanded_records = []
            for record in parsed["records"]:
                raw_ids = model_tokens(table_value(record, columns["chip_id"]), exact_only=True)
                if len(raw_ids) <= 1:
                    expanded_records.append(record)
                    continue
                for raw_id in raw_ids:
                    cloned = {"values": dict(record["values"])}
                    cloned["values"][columns["chip_id"]] = raw_id
                    expanded_records.append(cloned)
            parsed["records"] = expanded_records
            parsed.update({"page": page_index + 1, "columns": columns})
            result.append(parsed)
    return result


def extract_models_from_ir(document: DocumentIR, texts: list[str]) -> list[dict[str, Any]]:
    locator = RULE_CONFIG.get("document_locators", {}).get("model_table", {})
    required_any = [compact(item).upper() for item in locator.get("required_any", ["芯片型号", "器件型号", "产品型号"])]
    supporting_any = [compact(item).upper() for item in locator.get("supporting_any", ["封装", "FLASH", "MTP"])]
    result = []
    for table in document.tables:
        for fragment, page, rows, _ in table_grids(table):
            text = texts[page - 1] if page - 1 < len(texts) else ""
            ntext = compact(text).upper()
            if not model_tokens(text, exact_only=True):
                continue
            if not any(label in ntext for label in required_any) or not any(label in ntext for label in supporting_any):
                continue
            parsed = logical_model_rows(rows) or logical_transposed_model_rows(rows)
            if not parsed:
                continue
            columns = semantic_columns(parsed["headers"])
            if "chip_id" not in columns:
                continue
            expanded_records = []
            for record in parsed["records"]:
                raw_ids = model_tokens(table_value(record, columns["chip_id"]), exact_only=True)
                if len(raw_ids) <= 1:
                    expanded_records.append(record)
                    continue
                for raw_id in raw_ids:
                    cloned = {"values": dict(record["values"])}
                    cloned["values"][columns["chip_id"]] = raw_id
                    expanded_records.append(cloned)
            parsed["records"] = expanded_records
            parsed.update({
                "page": page,
                "columns": columns,
                "bbox": list(table.bboxes[fragment - 1]) if fragment - 1 < len(table.bboxes) else [0, 0, 0, 0],
            })
            result.append(parsed)
    return result


def peripheral_value(value: str, fallback: dict[str, Any] | None) -> int | None:
    text = compact(value).upper()
    if text in UNSUPPORTED_MARKS:
        return None
    number = parse_number(text)
    if number is not None:
        return int(number)
    if text in SUPPORTED_MARKS and fallback and fallback.get("count"):
        return int(fallback["count"])
    if text in SUPPORTED_MARKS:
        return 1
    return None


def table_value(record: dict[str, Any], column: int | None) -> str:
    if column is None:
        return ""
    return record["values"].get(column, "")


def table_size_kb(table: dict[str, Any], record: dict[str, Any], column: int | None) -> float | None:
    if column is None:
        return None
    value = table_value(record, column)
    normalized_value = compact(value).upper()
    header = table["headers"].get(column, "").upper()
    if not re.search(r"(?:K|KB|BYTE|字节|M|MB)", normalized_value):
        if "KB" in header or "KBYTE" in header:
            value = f"{value}KB"
        elif "BYTE" in header or "字节" in header:
            value = f"{value}BYTE"
    return parse_memory_kb(value)


def extract_pin_tables(pdf: Any, texts: list[str], models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_package: dict[str, list[str]] = defaultdict(list)
    for model in models:
        package = compact((model.get("package_types") or [""])[0]).upper()
        if package:
            by_package[package].append(model["chip_id"])
    extracted = []
    active_packages: list[list[str]] = []
    active_variants: list[str] = []
    for page_index, text in enumerate(texts):
        if page_index > 45:
            break
        if len(PIN_RE.findall(text)) < 3:
            continue
        if not any(word in text for word in ("默认功能", "管脚功能", "引脚功能", "通用双向")):
            continue
        page = pdf.pages[page_index]
        for table_obj in page.find_tables():
            table = table_obj.extract()
            if not table or len(table) < 3:
                continue
            max_cols = max(len(row) for row in table)
            keep_cols = [c for c in range(max_cols) if any(c < len(row) and clean(row[c]) for row in table)]
            if len(keep_cols) < 4:
                continue
            rows = [[clean(row[c]) if c < len(row) else "" for c in keep_cols] for row in table]
            header_end = None
            for ri, row in enumerate(rows):
                if any(PIN_RE.search(cell) for cell in row) and any(re.fullmatch(r"\d+", compact(cell)) for cell in row):
                    header_end = ri
                    break
            if header_end is None:
                continue
            headers = rows[:header_end]
            header_by_col = [compact(" ".join(row[c] for row in headers)).upper() for c in range(len(keep_cols))]
            pin_name_col = next((i for i, h in enumerate(header_by_col) if "管脚名称" in h or "引脚名称" in h or "管脚名" in h), None)
            default_col = next((i for i, h in enumerate(header_by_col) if "默认功能" in h), None)
            if pin_name_col is None:
                pin_name_col = next((i for i in range(len(keep_cols)) if any(PIN_RE.search(row[i]) for row in rows[header_end:])), None)
            if pin_name_col is None:
                continue
            if default_col is None:
                default_col = len(keep_cols) - 1
            number_cols = [i for i in range(pin_name_col) if any(re.fullmatch(r"\d+", compact(row[i])) for row in rows[header_end:])]
            packages_for_col: dict[int, list[str]] = {}
            variant_for_col: dict[int, str] = {}
            for column in number_cols:
                column_header = " ".join(row[column] for row in headers)
                packages = []
                for match in PACKAGE_RE.finditer(compact(column_header)):
                    package = re.sub(r"\([^)]*\)", "", compact(match.group(0))).upper()
                    if package and package not in packages:
                        packages.append(package)
                packages_for_col[column] = packages
                variant_match = re.search(r"\(\s*([A-Z0-9]+)\s*\)", column_header, re.I)
                variant_for_col[column] = variant_match.group(1).upper() if variant_match else ""
            visible_packages = [packages_for_col[column] for column in number_cols]
            visible_variants = [variant_for_col[column] for column in number_cols]
            if any(visible_packages):
                active_packages = visible_packages
            elif active_packages:
                for position, column in enumerate(number_cols):
                    if position < len(active_packages):
                        packages_for_col[column] = active_packages[position]
            if any(visible_variants):
                active_variants = visible_variants
            elif active_variants:
                for position, column in enumerate(number_cols):
                    if position < len(active_variants):
                        variant_for_col[column] = active_variants[position]

            # When one package has multiple explicitly labelled pinout variants,
            # pair the sorted model variants with the sorted table variants. This
            # is used only when the cardinalities match, so ordinary shared-pinout
            # package columns continue to fan out to every matching model.
            inferred_variant_models: dict[tuple[int, str], list[str]] = {}
            visible_package_names = {package for column in number_cols for package in packages_for_col.get(column, [])}
            for package in visible_package_names:
                package_columns = [column for column in number_cols if package in packages_for_col.get(column, [])]
                package_models = sorted(by_package.get(package, []), key=str.upper)
                labelled_columns = [(variant_for_col.get(column, ""), column) for column in package_columns]
                if (
                    len(package_models) == len(package_columns)
                    and len(package_columns) > 1
                    and all(variant for variant, _ in labelled_columns)
                    and len({variant for variant, _ in labelled_columns}) == len(labelled_columns)
                ):
                    for chip_id, (_, column) in zip(package_models, sorted(labelled_columns)):
                        inferred_variant_models[(column, package)] = [chip_id]

            single_model_variant_columns: list[int] = []
            if len(models) == 1:
                model_package = compact((models[0].get("package_types") or [""])[0]).upper()
                if model_package:
                    single_model_variant_columns = [
                        column for column in number_cols
                        if model_package in packages_for_col.get(column, []) and variant_for_col.get(column, "")
                    ]
            page_rows = []
            for row in rows[header_end:]:
                pin_cell = row[pin_name_col]
                base_match = PIN_RE.search(pin_cell)
                if not base_match:
                    continue
                pin_name = base_match.group(0).upper()
                # Pin functions are slash/comma delimited. PDF table extraction
                # often inserts whitespace in the middle of tokens (TK\n3,
                # UART1_R\nX), so remove whitespace before splitting instead of
                # treating it as a delimiter.
                parts = [normalize_function(part) for part in re.split(r"[/,，]+", compact(pin_cell)) if compact(part)]
                functions = [part for part in parts if part and part != pin_name and not PIN_RE.fullmatch(part)]
                functions = list(dict.fromkeys(functions))
                if not functions:
                    continue
                default_function = row[default_col] or "通用双向I/O口"
                for column in number_cols:
                    number_text = compact(row[column])
                    if not re.fullmatch(r"\d+", number_text):
                        continue
                    if len(single_model_variant_columns) > 1 and column in single_model_variant_columns and column != single_model_variant_columns[0]:
                        # A single marketed model can document alternative A/B
                        # lead arrangements without identifying the variant in
                        # the model name. Use the primary (leftmost) arrangement
                        # deterministically and keep the other visible in source
                        # evidence for later review.
                        continue
                    packages = packages_for_col.get(column, [])
                    variant = variant_for_col.get(column, "")
                    chip_ids = [
                        m["chip_id"] for m in models
                        if variant
                        and m["chip_id"].upper().endswith(variant)
                        and (not packages or compact((m.get("package_types") or [""])[0]).upper() in packages)
                    ]
                    if not chip_ids:
                        chip_ids = []
                        for package in packages:
                            chip_ids.extend(inferred_variant_models.get((column, package), by_package.get(package, [])))
                        chip_ids = list(dict.fromkeys(chip_ids))
                    if not chip_ids and len(models) == 1:
                        model_package = compact((models[0].get("package_types") or [""])[0]).upper()
                        if model_package:
                            if not packages or any(model_package.startswith(package) for package in packages):
                                chip_ids = [models[0]["chip_id"]]
                        elif column == number_cols[0]:
                            # Single-model manuals often show a second compatible package column;
                            # without an explicit model/package table, use the primary (first) column only.
                            chip_ids = [models[0]["chip_id"]]
                    if not chip_ids and packages:
                        chip_ids = [
                            m["chip_id"] for m in models
                            if any(compact((m.get("package_types") or [""])[0]).upper().startswith(package) for package in packages)
                        ]
                    for chip_id in chip_ids:
                        for function in functions:
                            page_rows.append({
                                "chip_id": chip_id,
                                "pin_name": pin_name,
                                "pin_number": int(number_text),
                                "function_name": function,
                                "is_supported": True,
                                "af_code": None,
                                "default_function": default_function,
                                "constraints": None,
                            })
            if page_rows:
                extracted.append({"page": page_index + 1, "bbox": [float(x) for x in table_obj.bbox], "rows": page_rows})
    return extracted


def make_evidence(doc: dict[str, Any], page: int, bbox: Iterable[float], kind: str, confidence: str, raw_text: str, sequence: int) -> Evidence:
    return Evidence(
        evidence_id=f"EV-{doc['doc_id']}-{page:04d}-{kind.upper()}-{sequence:02d}",
        doc_id=doc["doc_id"],
        source_file=str(doc["path"]),
        page=page,
        bbox=[round(float(x), 2) for x in bbox],
        kind=kind,
        confidence=confidence,
        raw_text=clean(raw_text)[:1200],
    )


def process_document(
    doc: dict[str, Any],
    collector: Collector,
    aliases: dict[str, str],
    profile: DocumentProfile,
) -> tuple[dict[str, Any], list[Evidence]]:
    path: Path = doc["path"]
    collector.current_source_rank = int(doc.get("version_rank") or 0)
    texts = doc.get("page_texts") or extract_page_texts(path)
    evidences: list[Evidence] = []
    basic_index = basic_page_index(texts)
    basic_text = ""
    basic_evidence_id: str | None = None
    defaults: dict[str, Any] = {"peripherals": {}}
    if basic_index is not None:
        basic_text = "\n".join(texts[basic_index : min(len(texts), basic_index + 3)])
        defaults = parse_basic_defaults(basic_text)
        page = basic_index + 1
        basic_evidence = make_evidence(doc, page, [0, 0, 595, 842], "basic", "B", texts[basic_index], 1)
        evidences.append(basic_evidence)
        basic_evidence_id = basic_evidence.evidence_id
    document_ir = build_document_ir(path, texts, progress=doc.get("table_progress"))
    with pdfplumber.open(path) as pdf:
        tables = extract_models_from_ir(document_ir, texts)
        models: list[dict[str, Any]] = []
        for table_index, table in enumerate(tables, start=1):
            ev = make_evidence(doc, table["page"], table["bbox"], "model", "A", texts[table["page"] - 1], table_index)
            evidences.append(ev)
            columns = table["columns"]
            for record in table["records"]:
                raw_model = table_value(record, columns.get("chip_id"))
                ids = model_tokens(raw_model, exact_only=True)
                if not ids:
                    continue
                model_id = aliases.get(ids[0], ids[0])
                package_text = table_value(record, columns.get("package"))
                package = PACKAGE_RE.search(package_text)
                voltage = parse_range(table_value(record, columns.get("voltage")))
                flash_kb = table_size_kb(table, record, columns.get("flash")) if "flash" in columns else None
                mtp_kb = table_size_kb(table, record, columns.get("mtp")) if "mtp" in columns else None
                ram_kb = table_size_kb(table, record, columns.get("external_ram")) if "external_ram" in columns else defaults.get("sram_external_kb")
                model_row = {
                    "chip_id": model_id,
                    "series": series_for_model(model_id),
                    "core": defaults.get("core"),
                    "flash_kb": scalar_or_none(flash_kb),
                    "ram_kb": scalar_or_none(ram_kb),
                    "max_freq_mhz": defaults.get("max_freq_mhz"),
                    "package_types": [package.group(0).replace(" ", "")] if package else [],
                    "voltage_min": voltage[0] if voltage else defaults.get("voltage_min"),
                    "voltage_max": voltage[1] if voltage else defaults.get("voltage_max"),
                    "_evidence_id": ev.evidence_id,
                    "_mtp_kb": mtp_kb,
                    "_eeprom_kb": table_size_kb(table, record, columns.get("eeprom")) if "eeprom" in columns else defaults.get("eeprom_kb"),
                }
                models.append(model_row)
                table_fields = {"chip_id", "series"}
                if flash_kb is not None:
                    table_fields.add("flash_kb")
                if "external_ram" in columns and ram_kb is not None:
                    table_fields.add("ram_kb")
                if package:
                    table_fields.add("package_types")
                if voltage:
                    table_fields.update(("voltage_min", "voltage_max"))
                collector.add(
                    "chips",
                    model_row,
                    ev.evidence_id,
                    "A",
                    {field: ev.evidence_id for field in table_fields},
                )
                uses_basic_defaults = any((
                    defaults.get("core") is not None,
                    "external_ram" not in columns and defaults.get("sram_external_kb") is not None,
                    defaults.get("max_freq_mhz") is not None,
                    voltage is None and defaults.get("voltage_min") is not None,
                    voltage is None and defaults.get("voltage_max") is not None,
                ))
                if basic_evidence_id and uses_basic_defaults:
                    basic_fields = set()
                    if defaults.get("core") is not None:
                        basic_fields.add("core")
                    if "external_ram" not in columns and defaults.get("sram_external_kb") is not None:
                        basic_fields.add("ram_kb")
                    if defaults.get("max_freq_mhz") is not None:
                        basic_fields.add("max_freq_mhz")
                    if voltage is None and defaults.get("voltage_min") is not None:
                        basic_fields.update(("voltage_min", "voltage_max"))
                    collector.add(
                        "chips",
                        model_row,
                        basic_evidence_id,
                        "B",
                        {field: basic_evidence_id for field in basic_fields},
                    )

                if flash_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "Flash", "size_kb": scalar_or_none(flash_kb), "organization": None, "features": None}, ev.evidence_id, "A")
                if mtp_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "MTP", "size_kb": scalar_or_none(mtp_kb), "organization": None, "features": None}, ev.evidence_id, "A")
                if ram_kb is not None:
                    ram_evidence_id = ev.evidence_id if "external_ram" in columns else basic_evidence_id or ev.evidence_id
                    collector.add("memory", {"chip_id": model_id, "memory_type": "SRAM_External", "size_kb": scalar_or_none(ram_kb), "organization": None, "features": None}, ram_evidence_id, "A" if "external_ram" in columns else "B")
                if defaults.get("sram_internal_kb") is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "SRAM_Internal", "size_kb": scalar_or_none(defaults["sram_internal_kb"]), "organization": None, "features": None}, basic_evidence_id or ev.evidence_id, "B")
                eeprom_kb = model_row["_eeprom_kb"]
                if eeprom_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "EEPROM", "size_kb": scalar_or_none(eeprom_kb), "organization": None, "features": None}, ev.evidence_id, "A")

                for ptype in ("PWM", "ADC", "UART", "I2C", "SPI", "Touch"):
                    value = table_value(record, columns.get(ptype)) if ptype in columns else ""
                    fallback = defaults.get("peripherals", {}).get(ptype)
                    count = peripheral_value(value, fallback)
                    if count is None:
                        continue
                    resolution = None
                    header = table["headers"].get(columns.get(ptype, -1), "")
                    resolution_match = re.search(r"(\d+)(?:BIT|位)", header, re.I)
                    if resolution_match:
                        resolution = int(resolution_match.group(1))
                    elif fallback:
                        resolution = fallback.get("resolution")
                    peripheral_row = {
                        "chip_id": model_id,
                        "peripheral_type": ptype,
                        "instance_count": count,
                        "max_resolution": resolution,
                        "max_frequency_mhz": None,
                        "features": None,
                    }
                    collector.add("peripherals", peripheral_row, ev.evidence_id, "A")
                    if basic_evidence_id and fallback and (not value or resolution_match is None):
                        collector.add("peripherals", peripheral_row, basic_evidence_id, "B")

        if not models:
            ids = model_tokens(path.stem, exact_only=True)
            if len(ids) == 1 and basic_index is not None:
                model_id = aliases.get(ids[0], ids[0])
                ev = evidences[0]
                models = [{
                    "chip_id": model_id,
                    "series": series_for_model(model_id),
                    "core": defaults.get("core"),
                    "flash_kb": None,
                    "ram_kb": scalar_or_none(defaults.get("sram_external_kb")),
                    "max_freq_mhz": defaults.get("max_freq_mhz"),
                    "package_types": [],
                    "voltage_min": defaults.get("voltage_min"),
                    "voltage_max": defaults.get("voltage_max"),
                    "_evidence_id": ev.evidence_id,
                }]
                collector.add("chips", models[0], ev.evidence_id, "B")

        ir_table_view = IRDocumentTableView(document_ir)
        for electrical_index, text in enumerate(texts):
            ntext = clean(text)
            supply = re.search(r"直流供电电压\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*V", ntext, re.I)
            temperature = re.search(r"工作(?:环境)?温度\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*(?:℃|°C|C)", ntext, re.I)
            if not supply and not temperature:
                continue
            page = document_ir.pages[electrical_index]
            tables_on_page = ir_table_view.pages[electrical_index].find_tables()
            bbox = tables_on_page[0].bbox if tables_on_page else (0, 0, page.width, page.height)
            ev = make_evidence(doc, electrical_index + 1, bbox, "electrical", "A", text, 1)
            evidences.append(ev)
            for model in models:
                if supply:
                    collector.add("electrical", {"chip_id": model["chip_id"], "param_name": "VDD", "min_value": float(supply.group(1)), "max_value": float(supply.group(2)), "unit": "V", "conditions": None}, ev.evidence_id, "A")
                if temperature:
                    collector.add("electrical", {"chip_id": model["chip_id"], "param_name": "T_operating", "min_value": float(temperature.group(1)), "max_value": float(temperature.group(2)), "unit": "°C", "conditions": None}, ev.evidence_id, "A")
            break

        for pin_index, pin_table in enumerate(extract_pin_tables(ir_table_view, texts, models), start=1):
            ev = make_evidence(doc, pin_table["page"], pin_table["bbox"], "pin", "B", texts[pin_table["page"] - 1], pin_index)
            evidences.append(ev)
            for row in pin_table["rows"]:
                collector.add("pin_mux", row, ev.evidence_id, "B")

    return {
        "doc_id": doc["doc_id"],
        "sha256": doc["sha256"],
        "version": doc.get("version"),
        "source_file": str(path),
        "duplicate_paths": doc["duplicates"],
        "page_count": len(texts),
        "basic_page": basic_index + 1 if basic_index is not None else None,
        "model_count": len(models),
        "models": [model["chip_id"] for model in models],
        "evidence_count": len(evidences),
    }, evidences
