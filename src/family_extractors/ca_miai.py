from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from core.collector import Collector
from core.documents import extract_page_texts
from core.evidence import Evidence
from core.field_dictionary import load_field_dictionary, match_common_field, peripheral_count_field
from core.ir import DocumentIR
from core.profile import DocumentProfile, load_profile
from core.table_ir_builder import build_document_ir
from core.table_index import DocumentTableIndex, TableCandidateQuery, build_table_index
from core.table_ir_legacy_view import IRDocumentTableView
from core.table_engine import (
    logical_row_model_table as engine_logical_row_model_table,
    logical_transposed_model_table as engine_logical_transposed_model_table,
    rectangular_rows,
)
from core.text import clean, compact, parse_number, parse_range, scalar_or_none
from core.units import convert_number
from topologies.matrix_observer import table_grids
from topologies.operating_conditions import is_absolute_maximum_context, parse_compound_operating_conditions
from topologies.pin_definition import first_pin_data_row, headers_spanning_value_columns

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "config" / "profiles" / "ca_miai_v1.json"
ACTIVE_PROFILE: DocumentProfile = load_profile(DEFAULT_PROFILE_PATH)
MODEL_RE = ACTIVE_PROFILE.model_re
PACKAGE_RE = ACTIVE_PROFILE.package_re
PIN_RE = ACTIVE_PROFILE.pin_re
UNSUPPORTED_MARKS = {"", "-", "--", "---", "×", "", "X", "不支持", "无", "0"}


SUPPORTED_MARKS = {"√", "Y", "YES", "支持", "有", "1"}
TABLE_RECIPE: dict[str, Any] = json.loads(ACTIVE_PROFILE.topology_preset("family_tables").read_text(encoding="utf-8"))
FIELD_DICTIONARY = load_field_dictionary(
    ACTIVE_PROFILE.field_dictionary_file,
    ACTIVE_PROFILE.field_aliases,
)


def configure(profile: DocumentProfile) -> None:
    """Bind one CA/MiAi rule package without leaking its logic into the runner."""
    global ACTIVE_PROFILE, MODEL_RE, PACKAGE_RE, PIN_RE, TABLE_RECIPE, FIELD_DICTIONARY
    ACTIVE_PROFILE = profile
    MODEL_RE = profile.model_re
    PACKAGE_RE = profile.package_re
    PIN_RE = profile.pin_re
    TABLE_RECIPE = json.loads(profile.topology_preset("family_tables").read_text(encoding="utf-8"))
    FIELD_DICTIONARY = load_field_dictionary(
        profile.field_dictionary_file,
        profile.field_aliases,
    )


def model_tokens(text: str, exact_only: bool = False) -> list[str]:
    return ACTIVE_PROFILE.model_tokens(text, exact_only=exact_only)


def is_exact_model(token: str) -> bool:
    return ACTIVE_PROFILE.is_exact_model(token)


def series_for_model(model: str) -> str:
    return ACTIVE_PROFILE.series_for_model(model)


def normalize_function(token: str) -> str:
    return ACTIVE_PROFILE.normalize_function(token)


def parse_memory_kb(value: object) -> int | float | None:
    return convert_number(value, FIELD_DICTIONARY["units"]["memory_kb"])


def parse_frequency_mhz(value: object) -> int | float | None:
    return convert_number(value, FIELD_DICTIONARY["units"]["frequency_mhz"])


def associate_pin_number_headers(
    header_by_col: list[str],
    number_cols: list[int],
    pin_name_col: int,
    spanned_packages: dict[int, list[str]] | None = None,
) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Associate pin-number columns with package/variant headers.

    PDF table recovery can place the text of a merged package header in the
    placeholder column immediately beside the numeric column it labels. Use
    exact-column matches first, then pair the remaining package headers and
    number columns in visual left-to-right order when their cardinality agrees.
    """
    header_meta: dict[int, tuple[list[str], str]] = {}
    for column, column_header in enumerate(header_by_col[:pin_name_col]):
        packages: list[str] = []
        for match in PACKAGE_RE.finditer(compact(column_header)):
            package = re.sub(r"\([^)]*\)", "", compact(match.group(0))).upper()
            if package and package not in packages:
                packages.append(package)
        variant_match = re.search(r"\(\s*([A-Z0-9]+)\s*\)", column_header, re.I)
        header_meta[column] = (packages, variant_match.group(1).upper() if variant_match else "")

    packages_for_col = {column: list(header_meta.get(column, ([], ""))[0]) for column in number_cols}
    variant_for_col = {column: header_meta.get(column, ([], ""))[1] for column in number_cols}

    # A physical pin-number cell can span several narrower package header
    # cells (for example SOP28 and SSOP28 share one pin-number column).  Use
    # geometry to supplement the reconstructed multi-row header.  Individual
    # vertical header cells may contain only fragments such as ``SOP2`` or
    # ``SOP1``; those fragments must not replace a complete ``SOP28`` or
    # ``SOP16`` recovered by joining the header rows.
    for column, packages in (spanned_packages or {}).items():
        if column in packages_for_col and packages:
            merged = list(packages_for_col[column])
            for package in packages:
                if any(existing != package and existing.startswith(package) for existing in merged):
                    continue
                merged = [
                    existing
                    for existing in merged
                    if not (existing != package and package.startswith(existing))
                ]
                if package not in merged:
                    merged.append(package)
            packages_for_col[column] = merged

    unmapped_numbers = [column for column in number_cols if not packages_for_col[column]]
    package_header_cols = [
        column
        for column, (packages, _) in header_meta.items()
        if packages and column not in number_cols
    ]
    if unmapped_numbers and len(unmapped_numbers) == len(package_header_cols):
        for number_column, header_column in zip(sorted(unmapped_numbers), sorted(package_header_cols)):
            packages_for_col[number_column] = list(header_meta[header_column][0])
            variant_for_col[number_column] = header_meta[header_column][1]

    return packages_for_col, variant_for_col


def inherit_pin_column_metadata(
    packages_for_col: dict[int, list[str]],
    variant_for_col: dict[int, str],
    column_positions: dict[int, float],
    active_columns: dict[float, tuple[list[str], str]],
    *,
    position_tolerance: float = 0.035,
) -> tuple[dict[int, list[str]], dict[int, str]]:
    """Fill continuation metadata by normalized horizontal column position.

    PDF grid recovery may include empty placeholder columns on the header page
    and compress them away on continuation pages.  Neither the dense array
    position nor the raw grid index is therefore a stable identity.  Cell
    geometry remains stable, so match only a nearby normalized x position.
    """
    resolved_packages = {column: list(packages) for column, packages in packages_for_col.items()}
    resolved_variants = dict(variant_for_col)
    for column in packages_for_col:
        if resolved_packages[column] and resolved_variants.get(column):
            continue
        position = column_positions.get(column)
        if position is None or not active_columns:
            continue
        nearest_position = min(active_columns, key=lambda active: abs(active - position))
        if abs(nearest_position - position) > position_tolerance:
            continue
        active_packages, active_variant = active_columns[nearest_position]
        if not resolved_packages[column]:
            resolved_packages[column] = list(active_packages)
        if not resolved_variants.get(column):
            resolved_variants[column] = active_variant
    return resolved_packages, resolved_variants


def pin_number_column_positions(
    raw_rows: list[list[Any]],
    cell_bboxes: dict[tuple[int, int], tuple[float, float, float, float] | None],
    keep_cols: list[int],
    header_end: int,
    number_cols: list[int],
    table_bbox: Iterable[float],
) -> dict[int, float]:
    """Return normalized x centers for populated physical pin-number columns."""
    x0, _, x1, _ = (float(value) for value in table_bbox)
    width = x1 - x0
    if width <= 0:
        return {}
    positions: dict[int, float] = {}
    for column in number_cols:
        raw_col = keep_cols[column]
        for row_index in range(header_end, len(raw_rows)):
            if raw_col >= len(raw_rows[row_index]):
                continue
            if not re.fullmatch(r"\d+", compact(raw_rows[row_index][raw_col])):
                continue
            bbox = cell_bboxes.get((row_index, raw_col))
            if bbox is not None:
                positions[column] = ((float(bbox[0]) + float(bbox[2])) / 2.0 - x0) / width
                break
    return positions


def resolve_pin_data_column(rows: list[list[str]], header_end: int, declared_col: int | None) -> int | None:
    """Resolve a semantic pin-name header to the column containing pin values."""
    scores = {
        column: sum(bool(PIN_RE.search(row[column])) for row in rows[header_end:] if column < len(row))
        for column in range(max((len(row) for row in rows), default=0))
    }
    if declared_col is not None and scores.get(declared_col, 0):
        return declared_col
    candidates = [column for column, score in scores.items() if score == max(scores.values(), default=0) and score]
    if not candidates:
        return None
    return min(candidates, key=lambda column: (abs(column - declared_col), column)) if declared_col is not None else candidates[0]


def resolve_adjacent_value_column(rows: list[list[str]], header_end: int, declared_col: int | None) -> int | None:
    """Move a merged semantic header to an adjacent populated value column."""
    if declared_col is None:
        return None

    def populated(column: int) -> int:
        return sum(column < len(row) and bool(compact(row[column])) for row in rows[header_end:])

    if populated(declared_col):
        return declared_col
    adjacent = [column for column in (declared_col - 1, declared_col + 1) if column >= 0 and populated(column)]
    return min(adjacent, key=lambda column: (abs(column - declared_col), column)) if adjacent else declared_col


def basic_page_index(texts: list[str]) -> int | None:
    locator = TABLE_RECIPE["topologies"]["basic_text"]
    scored_terms = locator.get("scored_terms", [])
    minimum_score = int(locator.get("minimum_score", 6))
    best: tuple[int, int] | None = None
    for index, text in enumerate(texts):
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
        r"CPU\s*(?:工作)?\s*(?:频率|主频)[^\d]{0,20}(?:最高|最大)?[^\d]{0,10}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
        r"最高(?:工作|运行)?频率[^\d]{0,10}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
        r"最高(?:工作|运行)?主频[^\d]{0,10}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
        r"CPU\s*频率[^@\n]{0,40}@\s*(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
    ]
    frequency_patterns.append(
        r"CPU\s*(?:时钟|频率|主频)[^\d\n。]{0,20}(?:为|是|:|：)?\s*(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)"
    )
    frequencies = [
        value
        for pattern in frequency_patterns
        for match in re.finditer(pattern, normalized, re.I)
        if (value := parse_frequency_mhz(f"{match.group(1)}{match.group(2)}")) is not None
    ]
    if frequencies:
        defaults["max_freq_mhz"] = scalar_or_none(max(frequencies))

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
        "SPI": [r"(?:支持|内置)\s*(\d+)\s*(?:个|路)?\s*(?:4\s*线)?\s*SPI"],
        "PWM": [r"(?:支持|最多有|最多支持)\s*(\d+)\s*(?:个|路|通道)?\s*PWM"],
        "ADC": [r"(?:支持|最多支持)\s*(\d+)\s*(?:个|路|通道)?\s*(\d+)\s*位\s*(?:SAR\s*)?ADC"],
        "Touch": [r"(?:最大|最多)?\s*支持\s*(\d+)\s*(?:个|路|通道)?\s*(?:触摸|Touch)"],
    }
    for ptype, patterns in peripheral_patterns.items():
        for pattern in patterns:
            match = re.search(pattern, normalized, re.I)
            if match:
                count_semantic = peripheral_count_field(FIELD_DICTIONARY, ptype)
                output_field = "channel_count" if count_semantic in {"channel_count", "mixed"} else "count"
                defaults["peripherals"][ptype] = {output_field: int(match.group(1))}
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


def max_cpu_frequency_by_page(texts: list[str]) -> tuple[int | float, int] | None:
    """Find the highest explicitly CPU/system-clock-labelled frequency.

    Oscillator-only frequencies are deliberately excluded: an oscillator rate
    is not automatically the maximum supported CPU clock.  Returning the page
    with the value lets the caller retain field-specific provenance.
    """
    patterns = (
        (3, r"CPU\s*工作\s*(?:频率|主频)[^\d]{0,30}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)"),
        (3, r"(?:CPU|系统)\s*(?:最高|最大)?\s*(?:工作|运行)?\s*(?:频率|主频)[^\d]{0,20}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)"),
        (2, r"(?:支持|可达|高达)\s*(?:的)?\s*(?:系统|CPU)?\s*(?:时钟|频率|主频)[^\d]{0,20}(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)"),
        (1, r"CPU\s*(?:时钟|频率|主频)[^\d\n。]{0,20}(?:为|是|:|：)?\s*(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)"),
    )
    candidates: list[tuple[int, float, int]] = []
    for page_index, text in enumerate(texts):
        normalized = clean(text).replace("‐", "-").replace("–", "-").replace("—", "-")
        for score, pattern in patterns:
            for match in re.finditer(pattern, normalized, re.I):
                value = parse_frequency_mhz(f"{match.group(1)}{match.group(2)}")
                if value is not None:
                    candidates.append((score, float(value), page_index))
    if not candidates:
        return None
    _, value, page_index = max(candidates, key=lambda item: (item[0], item[1]))
    return scalar_or_none(value), page_index


def peripheral_defaults_by_page(
    texts: list[str], start_index: int, page_count: int = 3
) -> dict[str, tuple[int, dict[str, Any]]]:
    """Locate each basic-feature peripheral statement on its actual PDF page."""
    located: dict[str, tuple[int, dict[str, Any]]] = {}
    for page_index in range(start_index, min(len(texts), start_index + page_count)):
        page_defaults = parse_basic_defaults(texts[page_index]).get("peripherals", {})
        for peripheral_type, values in page_defaults.items():
            located.setdefault(peripheral_type, (page_index, values))
    return located


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


def semantic_columns(
    headers: dict[int, str],
    field_dictionary: dict[str, Any] | None = None,
) -> dict[str, int | list[int]]:
    field_dictionary = field_dictionary or FIELD_DICTIONARY
    mappings: dict[str, int | list[int]] = {}

    def add(role: str, column: int) -> None:
        existing = mappings.get(role)
        if existing is None:
            mappings[role] = column
        elif isinstance(existing, list):
            if column not in existing:
                existing.append(column)
        elif existing != column:
            mappings[role] = [existing, column]
    roles = {
        "chip_id": "chip_id",
        "flash_kb": "flash",
        "ram_kb": "ram_total",
        "max_freq_mhz": "max_freq_mhz",
        "mtp_kb": "mtp",
        "eeprom_kb": "eeprom",
        "operating_voltage": "voltage",
        "operating_conditions": "operating_conditions",
        "package_types": "package",
    }

    for column, header in headers.items():
        common_field = match_common_field(header, field_dictionary)
        if common_field == "ram_kb":
            normalized_header = compact(header).casefold()
            if "外部" in normalized_header or "external" in normalized_header or "xram" in normalized_header:
                mappings.setdefault("external_ram", column)
            elif "内部" in normalized_header or "internal" in normalized_header or "iram" in normalized_header:
                mappings.setdefault("internal_ram", column)
            else:
                mappings.setdefault("ram_total", column)
            continue
        definition = field_dictionary.get("fields", {}).get(common_field or "", {})
        if (common_field or "").startswith("peripheral_") and definition.get("canonical"):
            add(f"peripheral:{definition['canonical']}", column)
            continue
        role = roles.get(common_field or "")
        if role:
            mappings.setdefault(role, column)
    return mappings


def semantic_column_indices(columns: dict[str, int | list[int]], role: str) -> list[int]:
    value = columns.get(role)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def discovered_peripheral_types(
    columns: dict[str, int],
    defaults: dict[str, Any],
) -> list[str]:
    """Return configured table peripherals plus text-derived fallbacks."""
    table_types = {
        role.partition(":")[2]
        for role in columns
        if role.startswith("peripheral:") and role.partition(":")[2]
    }
    fallback_types = set(defaults.get("peripherals", {}))
    return sorted(table_types | fallback_types)


def resolve_ram_total_kb(
    table_total_kb: int | float | None,
    internal_kb: int | float | None,
    external_kb: int | float | None,
) -> int | float | None:
    """Resolve total RAM without relabelling a total as a component."""
    if table_total_kb is not None:
        return scalar_or_none(table_total_kb)
    components = [value for value in (internal_kb, external_kb) if value is not None]
    return scalar_or_none(sum(components)) if components else None


def extract_models_from_pdf(pdf: pdfplumber.PDF, texts: list[str]) -> list[dict[str, Any]]:
    locator = TABLE_RECIPE["topologies"]["model_matrix"]
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


def extract_models_from_ir(
    document: DocumentIR,
    texts: list[str],
    table_index: DocumentTableIndex,
) -> list[dict[str, Any]]:
    locator = TABLE_RECIPE["topologies"]["model_matrix"]
    required_any = [compact(item).upper() for item in locator.get("required_any", ["芯片型号", "器件型号", "产品型号"])]
    supporting_any = [compact(item).upper() for item in locator.get("supporting_any", ["封装", "FLASH", "MTP"])]
    ranked = table_index.rank(
        TableCandidateQuery(text_any=tuple(required_any + supporting_any))
    )
    candidate_ids = {item.table_id for item in ranked}
    result = []
    for table in document.tables:
        if table.table_id not in candidate_ids:
            continue
        for fragment, page, rows, _ in table_grids(table, inherit_merged_values=True):
            table_text = " ".join(cell for row in rows for cell in row)
            ntext = compact(table_text).upper()
            if not model_tokens(table_text, exact_only=True):
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


def adc_instance_value(value: str, header: str) -> int | None:
    text = compact(value).upper()
    explicit = re.search(r"(\d+)\s*[X×]\s*\d+\s*(?:BIT|位)", text, re.I)
    if explicit:
        return int(explicit.group(1))
    # In CA/MiAi feature matrices a lone number under a resolution-labelled
    # ADC column is a channel count.  Only an explicit multiplier establishes
    # how many converter instances exist.
    return None


def adc_channel_value(value: str, header: str) -> int | None:
    text = compact(value).upper()
    labelled = [int(item) for item in re.findall(r"(\d+)\s*(?:CHANNELS?|通道)", text, re.I)]
    if labelled:
        return labelled[-1]
    normalized_header = compact(header).upper()
    if (
        "CHANNEL" in normalized_header
        or "通道" in normalized_header
        or ("ADC" in normalized_header and ("BIT" in normalized_header or "位" in normalized_header))
    ):
        number = parse_number(text)
        return int(number) if number is not None else None
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


def table_frequency_mhz(table: dict[str, Any], record: dict[str, Any], column: int | None) -> float | None:
    if column is None:
        return None
    value = table_value(record, column)
    header = table["headers"].get(column, "")
    return parse_frequency_mhz(f"{value} {header}")


def extract_pin_tables(
    pdf: Any,
    texts: list[str],
    models: list[dict[str, Any]],
    table_index: DocumentTableIndex,
) -> list[dict[str, Any]]:
    by_package: dict[str, list[str]] = defaultdict(list)
    for model in models:
        package = compact((model.get("package_types") or [""])[0]).upper()
        if package:
            by_package[package].append(model["chip_id"])
    extracted = []
    active_columns: dict[float, tuple[list[str], str]] = {}
    locator = TABLE_RECIPE["topologies"]["pin_definition"]
    required_any = [compact(item).casefold() for item in locator.get("required_any", [])]
    minimum_gpio_tokens = int(locator.get("minimum_pin_tokens", 3))
    candidate_pages = {
        page
        for entry in table_index.entries
        if entry.pin_like_cells >= minimum_gpio_tokens
        and (not required_any or any(term in entry.normalized_text for term in required_any))
        for page in entry.pages
    }
    for page_index, text in enumerate(texts):
        if page_index + 1 not in candidate_pages:
            continue
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
            header_end = first_pin_data_row(rows, ("管脚名称", "引脚名称", "管脚名", "PINNAME"))
            if header_end is None:
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
            pin_name_col = resolve_pin_data_column(rows, header_end, pin_name_col)
            if pin_name_col is None:
                continue
            if default_col is None:
                default_col = len(keep_cols) - 1
            else:
                default_col = resolve_adjacent_value_column(rows, header_end, default_col)
            number_cols = [i for i in range(pin_name_col) if any(re.fullmatch(r"\d+", compact(row[i])) for row in rows[header_end:])]
            spanned_packages = headers_spanning_value_columns(
                table,
                table_obj.cell_bboxes(),
                keep_cols,
                header_end,
                number_cols,
                PACKAGE_RE,
                lambda value: re.sub(r"\([^)]*\)", "", compact(value)).upper(),
            )
            packages_for_col, variant_for_col = associate_pin_number_headers(
                header_by_col, number_cols, pin_name_col, spanned_packages
            )
            column_positions = pin_number_column_positions(
                table,
                table_obj.cell_bboxes(),
                keep_cols,
                header_end,
                number_cols,
                table_obj.bbox,
            )
            has_visible_column_metadata = any(packages_for_col.values()) or any(variant_for_col.values())
            if has_visible_column_metadata:
                active_columns.clear()
            packages_for_col, variant_for_col = inherit_pin_column_metadata(
                packages_for_col,
                variant_for_col,
                column_positions,
                active_columns,
            )
            for column in number_cols:
                position = column_positions.get(column)
                if position is not None and (packages_for_col.get(column) or variant_for_col.get(column)):
                    active_columns[position] = (
                        list(packages_for_col.get(column, [])),
                        variant_for_col.get(column, ""),
                    )

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
    peripheral_basic_evidence: dict[str, str] = {}
    defaults: dict[str, Any] = {"peripherals": {}}
    if basic_index is not None:
        basic_text = "\n".join(texts[basic_index : min(len(texts), basic_index + 3)])
        defaults = parse_basic_defaults(basic_text)
        page = basic_index + 1
        basic_evidence = make_evidence(doc, page, [0, 0, 595, 842], "basic", "B", texts[basic_index], 1)
        evidences.append(basic_evidence)
        basic_evidence_id = basic_evidence.evidence_id
        page_evidence_ids = {basic_index: basic_evidence_id}
        for peripheral_type, (source_index, _) in peripheral_defaults_by_page(texts, basic_index).items():
            if source_index not in page_evidence_ids:
                source_evidence = make_evidence(
                    doc,
                    source_index + 1,
                    [0, 0, 595, 842],
                    "basic",
                    "B",
                    texts[source_index],
                    source_index - basic_index + 1,
                )
                evidences.append(source_evidence)
                page_evidence_ids[source_index] = source_evidence.evidence_id
            peripheral_basic_evidence[peripheral_type] = page_evidence_ids[source_index]
    document_frequency = max_cpu_frequency_by_page(texts)
    frequency_evidence_id: str | None = None
    if document_frequency is not None:
        frequency_value, frequency_page_index = document_frequency
        defaults["max_freq_mhz"] = frequency_value
        if frequency_page_index == basic_index and basic_evidence_id:
            frequency_evidence_id = basic_evidence_id
        else:
            frequency_evidence = make_evidence(
                doc,
                frequency_page_index + 1,
                [0, 0, 595, 842],
                "frequency",
                "A",
                texts[frequency_page_index],
                1,
            )
            evidences.append(frequency_evidence)
            frequency_evidence_id = frequency_evidence.evidence_id
    document_ir = build_document_ir(path, texts, progress=doc.get("table_progress"))
    table_index = build_table_index(document_ir)
    doc["_table_index"] = table_index
    with pdfplumber.open(path) as pdf:
        tables = extract_models_from_ir(document_ir, texts, table_index)
        models: list[dict[str, Any]] = []
        for model_table_index, table in enumerate(tables, start=1):
            ev = make_evidence(
                doc,
                table["page"],
                table["bbox"],
                "model",
                "A",
                texts[table["page"] - 1],
                model_table_index,
            )
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
                compound_conditions = parse_compound_operating_conditions(
                    table_value(record, columns.get("operating_conditions"))
                ) if "operating_conditions" in columns else {}
                voltage = parse_range(table_value(record, columns.get("voltage")))
                if voltage is None and "voltage_min" in compound_conditions:
                    voltage = (compound_conditions["voltage_min"], compound_conditions["voltage_max"])
                flash_kb = table_size_kb(table, record, columns.get("flash")) if "flash" in columns else None
                mtp_kb = table_size_kb(table, record, columns.get("mtp")) if "mtp" in columns else None
                table_ram_total_kb = table_size_kb(table, record, columns.get("ram_total")) if "ram_total" in columns else None
                table_ram_internal_kb = table_size_kb(table, record, columns.get("internal_ram")) if "internal_ram" in columns else None
                table_ram_external_kb = table_size_kb(table, record, columns.get("external_ram")) if "external_ram" in columns else None
                ram_internal_kb = table_ram_internal_kb if table_ram_internal_kb is not None else defaults.get("sram_internal_kb")
                ram_external_kb = table_ram_external_kb if table_ram_external_kb is not None else defaults.get("sram_external_kb")
                ram_kb = resolve_ram_total_kb(
                    table_ram_total_kb,
                    ram_internal_kb,
                    ram_external_kb,
                )
                table_frequency = table_frequency_mhz(table, record, columns.get("max_freq_mhz")) if "max_freq_mhz" in columns else None
                model_row = {
                    "chip_id": model_id,
                    "series": series_for_model(model_id),
                    "core": defaults.get("core"),
                    "flash_kb": scalar_or_none(flash_kb),
                    "ram_kb": scalar_or_none(ram_kb),
                    "max_freq_mhz": table_frequency if table_frequency is not None else defaults.get("max_freq_mhz"),
                    "package_types": [package.group(0).replace(" ", "")] if package else [],
                    "voltage_min": voltage[0] if voltage else defaults.get("voltage_min"),
                    "voltage_max": voltage[1] if voltage else defaults.get("voltage_max"),
                    "_evidence_id": ev.evidence_id,
                    "_voltage_evidence_id": ev.evidence_id if voltage else basic_evidence_id or ev.evidence_id,
                    "_mtp_kb": mtp_kb,
                    "_eeprom_kb": table_size_kb(table, record, columns.get("eeprom")) if "eeprom" in columns else defaults.get("eeprom_kb"),
                    "_temperature_min": compound_conditions.get("temperature_min"),
                    "_temperature_max": compound_conditions.get("temperature_max"),
                }
                models.append(model_row)
                table_fields = {"chip_id", "series"}
                if flash_kb is not None:
                    table_fields.add("flash_kb")
                if any(value is not None for value in (table_ram_total_kb, table_ram_internal_kb, table_ram_external_kb)):
                    table_fields.add("ram_kb")
                if package:
                    table_fields.add("package_types")
                if table_frequency is not None:
                    table_fields.add("max_freq_mhz")
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
                    all(value is None for value in (table_ram_total_kb, table_ram_internal_kb, table_ram_external_kb)) and ram_kb is not None,
                    table_frequency is None and defaults.get("max_freq_mhz") is not None and frequency_evidence_id is None,
                    voltage is None and defaults.get("voltage_min") is not None,
                    voltage is None and defaults.get("voltage_max") is not None,
                ))
                if basic_evidence_id and uses_basic_defaults:
                    basic_fields = set()
                    if defaults.get("core") is not None:
                        basic_fields.add("core")
                    if all(value is None for value in (table_ram_total_kb, table_ram_internal_kb, table_ram_external_kb)) and ram_kb is not None:
                        basic_fields.add("ram_kb")
                    if table_frequency is None and defaults.get("max_freq_mhz") is not None and frequency_evidence_id is None:
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
                if table_frequency is None and frequency_evidence_id is not None:
                    collector.add(
                        "chips",
                        model_row,
                        frequency_evidence_id,
                        "A",
                        {"max_freq_mhz": frequency_evidence_id},
                    )

                if flash_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "Flash", "size_kb": scalar_or_none(flash_kb), "organization": None, "features": None}, ev.evidence_id, "A")
                if mtp_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "MTP", "size_kb": scalar_or_none(mtp_kb), "organization": None, "features": None}, ev.evidence_id, "A")
                if table_ram_total_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "SRAM_Total", "size_kb": scalar_or_none(table_ram_total_kb), "organization": None, "features": None}, ev.evidence_id, "A")
                if ram_external_kb is not None:
                    external_evidence_id = ev.evidence_id if table_ram_external_kb is not None else basic_evidence_id or ev.evidence_id
                    collector.add("memory", {"chip_id": model_id, "memory_type": "SRAM_External", "size_kb": scalar_or_none(ram_external_kb), "organization": None, "features": None}, external_evidence_id, "A" if table_ram_external_kb is not None else "B")
                if ram_internal_kb is not None:
                    internal_evidence_id = ev.evidence_id if table_ram_internal_kb is not None else basic_evidence_id or ev.evidence_id
                    collector.add("memory", {"chip_id": model_id, "memory_type": "SRAM_Internal", "size_kb": scalar_or_none(ram_internal_kb), "organization": None, "features": None}, internal_evidence_id, "A" if table_ram_internal_kb is not None else "B")
                eeprom_kb = model_row["_eeprom_kb"]
                if eeprom_kb is not None:
                    collector.add("memory", {"chip_id": model_id, "memory_type": "EEPROM", "size_kb": scalar_or_none(eeprom_kb), "organization": None, "features": None}, ev.evidence_id, "A")

                for ptype in discovered_peripheral_types(columns, defaults):
                    column_role = f"peripheral:{ptype}"
                    peripheral_columns = semantic_column_indices(columns, column_role)
                    values = [table_value(record, column) for column in peripheral_columns]
                    fallback = defaults.get("peripherals", {}).get(ptype)
                    headers = [table["headers"].get(column, "") for column in peripheral_columns]
                    if ptype == "ADC":
                        parsed_counts = [
                            count
                            for value, header in zip(values, headers)
                            if (count := adc_instance_value(value, header)) is not None
                        ]
                        parsed_channels = [
                            count
                            for value, header in zip(values, headers)
                            if (count := adc_channel_value(value, header)) is not None
                        ]
                        count = scalar_or_none(sum(parsed_counts)) if parsed_counts else (fallback or {}).get("count")
                        channel_count = max(parsed_channels) if parsed_channels else (fallback or {}).get("channel_count")
                    else:
                        parsed_counts = [
                            count
                            for value in values
                            if (count := peripheral_value(value, None)) is not None
                        ]
                        count_semantic = peripheral_count_field(FIELD_DICTIONARY, ptype)
                        parsed_value = scalar_or_none(sum(parsed_counts)) if parsed_counts else None
                        if count_semantic == "channel_count":
                            count = (fallback or {}).get("count")
                            channel_count = parsed_value if parsed_value is not None else (fallback or {}).get("channel_count")
                        else:
                            count = parsed_value if parsed_value is not None else peripheral_value("", fallback)
                            channel_count = (fallback or {}).get("channel_count")
                    if count is None and channel_count is None:
                        continue
                    resolution = None
                    resolution_values = [
                        int(match.group(1))
                        for column in peripheral_columns
                        if (match := re.search(r"(\d+)(?:BIT|位)", table["headers"].get(column, ""), re.I))
                    ]
                    if resolution_values:
                        resolution = max(resolution_values)
                    elif fallback:
                        resolution = fallback.get("resolution")
                    peripheral_row = {
                        "chip_id": model_id,
                        "peripheral_type": ptype,
                        "instance_count": count,
                        "channel_count": channel_count,
                        "max_resolution": resolution,
                        "max_frequency_mhz": None,
                        "features": None,
                    }
                    collector.add("peripherals", peripheral_row, ev.evidence_id, "A")
                    fallback_evidence_id = peripheral_basic_evidence.get(ptype, basic_evidence_id)
                    if fallback_evidence_id and fallback and (not parsed_counts or not resolution_values):
                        collector.add("peripherals", peripheral_row, fallback_evidence_id, "B")

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
                    "ram_kb": resolve_ram_total_kb(
                        None,
                        defaults.get("sram_internal_kb"),
                        defaults.get("sram_external_kb"),
                    ),
                    "max_freq_mhz": defaults.get("max_freq_mhz"),
                    "package_types": [],
                    "voltage_min": defaults.get("voltage_min"),
                    "voltage_max": defaults.get("voltage_max"),
                    "_evidence_id": ev.evidence_id,
                    "_voltage_evidence_id": basic_evidence_id or ev.evidence_id,
                }]
                collector.add("chips", models[0], ev.evidence_id, "B")

        ir_table_view = IRDocumentTableView(document_ir)
        models_with_voltage: set[str] = set()
        models_with_temperature: set[str] = set()
        for model in models:
            if model.get("voltage_min") is not None and model.get("voltage_max") is not None:
                voltage_evidence_id = model.get("_voltage_evidence_id") or model["_evidence_id"]
                collector.add(
                    "electrical",
                    {
                        "chip_id": model["chip_id"],
                        "param_name": "VDD",
                        "min_value": model["voltage_min"],
                        "max_value": model["voltage_max"],
                        "unit": "V",
                        "conditions": None,
                    },
                    voltage_evidence_id,
                    "A" if voltage_evidence_id == model["_evidence_id"] else "B",
                )
                models_with_voltage.add(model["chip_id"])
            if model.get("_temperature_min") is not None and model.get("_temperature_max") is not None:
                collector.add(
                    "electrical",
                    {
                        "chip_id": model["chip_id"],
                        "param_name": "T_operating",
                        "min_value": model["_temperature_min"],
                        "max_value": model["_temperature_max"],
                        "unit": "°C",
                        "conditions": None,
                    },
                    model["_evidence_id"],
                    "A",
                )
                models_with_temperature.add(model["chip_id"])

        temperature_added = len(models_with_temperature) == len(models)
        for electrical_index, text in enumerate(texts):
            ntext = clean(text)
            supply = re.search(r"直流供电电压\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*V", ntext, re.I)
            if supply and is_absolute_maximum_context(ntext):
                supply = None
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
                    if model["chip_id"] not in models_with_voltage:
                        collector.add("electrical", {"chip_id": model["chip_id"], "param_name": "VDD", "min_value": float(supply.group(1)), "max_value": float(supply.group(2)), "unit": "V", "conditions": None}, ev.evidence_id, "A")
                        models_with_voltage.add(model["chip_id"])
                if temperature and model["chip_id"] not in models_with_temperature:
                    collector.add("electrical", {"chip_id": model["chip_id"], "param_name": "T_operating", "min_value": float(temperature.group(1)), "max_value": float(temperature.group(2)), "unit": "°C", "conditions": None}, ev.evidence_id, "A")
                    models_with_temperature.add(model["chip_id"])
            temperature_added = temperature_added or bool(temperature)
            if len(models_with_voltage) == len(models) and len(models_with_temperature) == len(models):
                break

        for pin_index, pin_table in enumerate(
            extract_pin_tables(ir_table_view, texts, models, table_index),
            start=1,
        ):
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
