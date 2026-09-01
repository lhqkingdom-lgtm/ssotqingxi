from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from core.collector import Collector
from core.candidate_executor import reduce_candidates, validate_hard_rules
from core.documents import extract_page_texts
from core.evidence import Evidence
from core.field_rules import observations_to_candidates
from core.profile import DocumentProfile
from core.table_ir_builder import build_document_ir
from core.table_ir_legacy_view import IRDocumentTableView
from core.text import clean, compact, parse_number, scalar_or_none
from core.validation import valid_pin_designator
from family_rules.stm32_matrix import package_header_observation, select_observations
from topologies.matrix_observer import table_grids, text_direction_variants


RULE_CONFIG: dict[str, Any] = {}
FEATURE_RECIPE: dict[str, Any] = {}


def configure(profile: DocumentProfile, rules: dict[str, Any]) -> None:
    global RULE_CONFIG, FEATURE_RECIPE
    RULE_CONFIG = rules
    preset_path = (profile.path.parent / profile.payload["table_presets"]["feature_matrix"]).resolve()
    FEATURE_RECIPE = json.loads(preset_path.read_text(encoding="utf-8"))


def configured_terms(section: str, name: str) -> list[str]:
    return [compact(item).lower() for item in RULE_CONFIG.get(section, {}).get(name, [])]


def configured_term_groups(section: str, name: str) -> list[list[str]]:
    return [
        [compact(item).lower() for item in group if compact(item)]
        for group in RULE_CONFIG.get(section, {}).get(name, [])
        if isinstance(group, list)
    ]


def header_matches(value: str, terms: Iterable[str] = (), term_groups: Iterable[Iterable[str]] = ()) -> bool:
    normalized = compact(value).lower()
    return any(term in normalized for term in terms) or any(
        all(term in normalized for term in group) for group in term_groups
    )


def label_matches(name: str, value: object) -> bool:
    label = compact(value).lower()
    return any(term in label for term in configured_terms("field_labels", name))


def make_evidence(doc: dict[str, Any], page: int, bbox: Iterable[float], kind: str, confidence: str, raw_text: str, sequence: int) -> Evidence:
    return Evidence(
        evidence_id=f"EV-{doc['doc_id']}-{page:04d}-{kind.upper()}-{sequence:02d}",
        doc_id=doc["doc_id"],
        source_file=str(doc["path"]),
        page=page,
        bbox=[round(float(value), 2) for value in bbox],
        kind=kind,
        confidence=confidence,
        raw_text=clean(raw_text)[:1200],
    )


def target_model(path: Path, profile: DocumentProfile) -> str:
    stem = re.sub(r"(STM32[A-Z]{1,2}\d{2,3})_([A-Z0-9]{2,})", r"\1\2", path.stem, count=1, flags=re.I)
    match = re.search(str(profile.payload["stm32"]["filename_model_pattern"]), stem, re.I)
    if not match:
        raise ValueError(f"STM32 profile cannot derive a target model from filename: {path.name}")
    return match.group(0).upper()


def number_from_cell(value: object) -> float | None:
    text = clean(value).replace("–", "-").replace("−", "-")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def normalize_label(row: list[Any]) -> str:
    return compact(" ".join(clean(cell) for cell in row[:2])).lower()


def extract_feature_table(pdf: pdfplumber.PDF, page_texts: list[str], profile: DocumentProfile) -> dict[str, Any]:
    limit = int(profile.payload["stm32"].get("feature_page_limit", 30))
    locator = RULE_CONFIG["document_locators"]["feature_table"]
    required_all = [compact(item).lower() for item in locator["required_all"]]
    required_any = [compact(item).lower() for item in locator["required_any"]]
    for page_index, page in enumerate(pdf.pages[:limit]):
        for table in page.find_tables():
            data = table.extract()
            joined = compact(" ".join(clean(cell) for row in data for cell in row)).lower()
            if not all(term in joined for term in required_all) or not any(term in joined for term in required_any):
                continue
            return {"page": page_index + 1, "bbox": table.bbox, "rows": data, "text": page_texts[page_index]}
    raise ValueError("STM32 feature/peripheral table not found")


def model_codes(model: str) -> tuple[str, str]:
    match = re.match(r"STM32[A-Z]{1,2}\d{2,3}([A-Z0-9])([A-Z0-9])$", model, re.I)
    if not match:
        raise ValueError(f"unsupported STM32 model code: {model}")
    return match.group(1).upper(), match.group(2).upper()


def ordering_flash_value(page_texts: list[str], model: str) -> tuple[float | int, int] | None:
    locator = RULE_CONFIG.get("document_locators", {}).get("ordering_information", {})
    page_terms = [compact(item).lower() for item in locator.get("page_any", [])]
    label_terms = [compact(item).lower() for item in locator.get("flash_label_any", [])]
    _, flash_code = model_codes(model)
    unit_factors = {"KBYTE": 1, "KBYTES": 1, "MBYTE": 1024, "MBYTES": 1024}
    for page, text in enumerate(page_texts, 1):
        normalized = compact(text).lower()
        if page_terms and not any(term in normalized for term in page_terms):
            continue
        if label_terms and not any(term in normalized for term in label_terms):
            continue
        for code, number, unit in re.findall(
            r"\b([A-Z0-9])\s*=\s*(\d+(?:\.\d+)?)\s*(KBYTES?|MBYTES?)\b",
            text,
            re.I,
        ):
            if code.upper() != flash_code:
                continue
            value = float(number) * unit_factors[unit.upper()]
            return scalar_or_none(value), page
    return None


def feature_column(rows: list[list[Any]], model: str, flash_code_kb: dict[str, int]) -> int:
    package_code, flash_code = model_codes(model)
    expected_flash = float(flash_code_kb[flash_code])
    header = rows[0]
    combined = compact(" ".join(clean(cell) for cell in header)).upper()
    suffixes = re.findall(r"_([A-Z0-9])([A-Z0-9])", combined)
    if suffixes:
        candidates = [index + 2 for index, suffix in enumerate(suffixes) if suffix == (package_code, flash_code)]
        if candidates:
            return candidates[0]
    group_starts = []
    for index, cell in enumerate(header):
        value = compact(cell).upper()
        if re.search(rf"STM32[A-Z]\d{{3}}{re.escape(package_code)}X", value):
            group_starts.append(index)
    flash_row = next((row for row in rows if "flash" in normalize_label(row)), None)
    for start in group_starts:
        next_group = next((index for index in range(start + 1, len(header)) if clean(header[index])), len(header))
        for column in range(start, next_group):
            if flash_row and column < len(flash_row) and number_from_cell(flash_row[column]) == expected_flash:
                return column
    if flash_row:
        for column, cell in enumerate(flash_row):
            if number_from_cell(cell) == expected_flash:
                return column
    raise ValueError(f"cannot select STM32 feature column for {model}")


def carried_value(row: list[Any], column: int) -> str:
    if column < len(row) and clean(row[column]):
        return clean(row[column])
    for index in range(min(column, len(row) - 1), 1, -1):
        if clean(row[index]):
            return clean(row[index])
    return ""


def parse_packages(text: str) -> list[str]:
    values = []
    for match in re.finditer(r"(?:LQFP|TSSOP|UFQFPN|VFQFPN|TFBGA|UFBGA|LFBGA|VFBGA|WLCSP|BGA)\s*\d+", text, re.I):
        value = compact(match.group(0)).upper()
        if value not in values:
            values.append(value)
    return values


def feature_values(feature: dict[str, Any], model: str, profile: DocumentProfile) -> dict[str, Any]:
    rows = feature["rows"]
    config = profile.payload["stm32"]
    column = feature_column(rows, model, config["flash_code_kb"])
    result: dict[str, Any] = {"peripherals": {}}
    parent = ""
    for row in rows:
        first = clean(row[0]) if row else ""
        second = clean(row[1]) if len(row) > 1 else ""
        if first:
            parent = first
        label = compact(f"{parent}{second}").lower()
        value = carried_value(row, column)
        if label_matches("flash", label) and "kbyte" in label:
            result["flash_kb"] = scalar_or_none(number_from_cell(value))
        elif label_matches("ram", label) and "kbyte" in label:
            result["ram_kb"] = scalar_or_none(number_from_cell(value))
        elif label_matches("cpu_frequency", label):
            result["max_freq_mhz"] = scalar_or_none(number_from_cell(value))
        elif label_matches("operating_voltage", label):
            numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", value)]
            if len(numbers) >= 2:
                result["voltage_min"], result["voltage_max"] = numbers[0], numbers[1]
        elif label_matches("package", label):
            result["package_types"] = parse_packages(value)
        elif label_matches("SPI", label):
            result["peripherals"]["SPI"] = {"count": int(number_from_cell(value) or 0)}
        elif label_matches("I2C", label):
            result["peripherals"]["I2C"] = {"count": int(number_from_cell(value) or 0)}
        elif label_matches("UART", label):
            result["peripherals"]["UART"] = {"count": int(number_from_cell(value) or 0)}
        elif label_matches("ADC", label) and label_matches("channel", label):
            numbers = [int(item) for item in re.findall(r"\d+", value)]
            if numbers:
                count = numbers[0] if "numberofchannels" in label and len(numbers) >= 2 else 1
                result["peripherals"]["ADC"] = {"count": count, "resolution": 12}
    return result


def package_values(pdf: pdfplumber.PDF, model: str, profile: DocumentProfile) -> tuple[list[str], int | None, tuple[float, ...] | None]:
    package_code, _ = model_codes(model)
    pin_count = int(profile.payload["stm32"]["pin_count_codes"][package_code])
    for page_index, page in enumerate(pdf.pages[: int(profile.payload["stm32"].get("feature_page_limit", 30))]):
        for table in page.find_tables():
            for row in table.extract():
                label = compact(clean(row[0])).lower()
                if not label_matches("package", label):
                    continue
                matches = []
                for cell in row[1:]:
                    for package in parse_packages(clean(cell)):
                        if str(pin_count) in package and package not in matches:
                            matches.append(package)
                if matches:
                    return matches, page_index + 1, table.bbox
    return [], None, None


def package_values_from_ir(document: Any, model: str, profile: DocumentProfile) -> tuple[list[str], int | None, tuple[float, ...] | None]:
    package_code, _ = model_codes(model)
    pin_count = int(profile.payload["stm32"]["pin_count_codes"][package_code])
    limit = int(profile.payload["stm32"].get("feature_page_limit", 30))
    for table in document.tables:
        for fragment, page, rows, _ in table_grids(table):
            if page > limit:
                continue
            for row in rows:
                label = compact(clean(row[0])).lower()
                if not label_matches("package", label):
                    continue
                matches = []
                for cell in row[1:]:
                    for package in parse_packages(clean(cell)):
                        if str(pin_count) in package and package not in matches:
                            matches.append(package)
                if matches:
                    bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                    return matches, page, bbox
    # Some feature matrices expose only a pin count. In that layout the
    # package names are authoritative column headers in the pin table.
    for table in document.tables:
        for fragment, page, rows, _ in table_grids(table):
            if page > limit or not rows:
                continue
            header_text = compact(
                " ".join(
                    variant
                    for row in rows[:2]
                    for cell in row
                    for variant in text_direction_variants(clean(cell))
                )
            ).lower()
            if "pinname" not in header_text:
                continue
            matches = []
            for cell in (cell for row in rows[:2] for cell in row):
                for variant in text_direction_variants(clean(cell)):
                    for package in parse_packages(variant):
                        count_match = re.search(r"(\d+)$", package)
                        if count_match and int(count_match.group(1)) == pin_count and package not in matches:
                            matches.append(package)
            if matches:
                bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
                return matches, page, bbox
    return [], None, None


def is_primary_voltage_row(row: list[Any], locator: dict[str, Any]) -> bool:
    symbols = {compact(item).upper() for item in locator.get("primary_voltage_symbols", ["VDD"])}
    return bool(row) and compact(row[0]).upper() in symbols


def operating_values(pdf: pdfplumber.PDF, page_texts: list[str]) -> dict[str, Any] | None:
    locator = RULE_CONFIG["document_locators"]["operating_conditions"]
    page_terms = [compact(item).lower() for item in locator["page_any"]]
    table_terms = [compact(item).lower() for item in locator["table_any"]]
    temperature_terms = [compact(item).lower() for item in locator["temperature_any"]]
    temperature_exclusions = [compact(item).lower() for item in locator["temperature_exclude_any"]]
    for page_index, page in enumerate(pdf.pages):
        text = compact(page_texts[page_index]).lower()
        if not any(term in text for term in page_terms):
            continue
        for table in page.find_tables():
            rows = table.extract()
            joined = compact(" ".join(clean(cell) for row in rows for cell in row)).lower()
            if not any(term in joined for term in table_terms):
                continue
            result: dict[str, Any] = {"page": page_index + 1, "bbox": table.bbox, "rows": rows}
            for row in rows:
                label = compact(" ".join(clean(cell) for cell in row[:2])).lower()
                if any(term in label for term in table_terms) and is_primary_voltage_row(row, locator):
                    numbers = [number_from_cell(cell) for cell in row]
                    values = [number for number in numbers if number is not None]
                    if len(values) >= 2:
                        result["voltage_min"], result["voltage_max"] = values[-2], values[-1]
                full_row = compact(" ".join(clean(cell) for cell in row)).lower()
                if any(term in label for term in temperature_terms) and not any(term in full_row for term in temperature_exclusions):
                    values = [number_from_cell(cell) for cell in row]
                    values = [number for number in values if number is not None]
                    if len(values) >= 2:
                        result["temperature_min"], result["temperature_max"] = values[-2], values[-1]
            return result
    return None


def operating_values_from_ir(document: Any, page_texts: list[str]) -> dict[str, Any] | None:
    locator = RULE_CONFIG["document_locators"]["operating_conditions"]
    page_terms = [compact(item).lower() for item in locator["page_any"]]
    table_terms = [compact(item).lower() for item in locator["table_any"]]
    temperature_terms = [compact(item).lower() for item in locator["temperature_any"]]
    temperature_exclusions = [compact(item).lower() for item in locator["temperature_exclude_any"]]
    for table in document.tables:
        for fragment, page, rows, _ in table_grids(table):
            text = compact(page_texts[page - 1]).lower()
            if not any(term in text for term in page_terms):
                continue
            joined = compact(" ".join(clean(cell) for row in rows for cell in row)).lower()
            if not any(term in joined for term in table_terms):
                continue
            bbox = table.bboxes[fragment - 1] if fragment - 1 < len(table.bboxes) else (0, 0, 0, 0)
            result: dict[str, Any] = {"page": page, "bbox": bbox, "rows": rows}
            for row in rows:
                label = compact(" ".join(clean(cell) for cell in row[:2])).lower()
                if any(term in label for term in table_terms) and is_primary_voltage_row(row, locator):
                    values = [number for cell in row if (number := number_from_cell(cell)) is not None]
                    if len(values) >= 2:
                        result["voltage_min"], result["voltage_max"] = values[-2], values[-1]
                full_row = compact(" ".join(clean(cell) for cell in row)).lower()
                if any(term in label for term in temperature_terms) and not any(term in full_row for term in temperature_exclusions):
                    values = [number for cell in row if (number := number_from_cell(cell)) is not None]
                    if len(values) >= 2:
                        result["temperature_min"], result["temperature_max"] = values[-2], values[-1]
            return result
    return None


def package_column(
    headers: list[Any],
    pin_count: int | None = None,
    package_types: Iterable[str] = (),
    qualifier_preferences: Iterable[str] = (),
) -> int | None:
    expected_packages = list(dict.fromkeys(compact(item).upper() for item in package_types if compact(item)))
    # The caller orders packages by the model's package-code preference. Search
    # expected packages first so a multi-package table does not silently select
    # whichever compatible column happens to appear first.
    for expected_package in expected_packages:
        matches = []
        for index, cell in enumerate(headers):
            for value in text_direction_variants(clean(cell)):
                if expected_package == value or expected_package in value:
                    matches.append(index)
                    break
        matches = list(dict.fromkeys(matches))
        if len(matches) > 1:
            preferences = [compact(item).lower() for item in qualifier_preferences if compact(item)]
            qualifiers = [item for item in preferences if item != "unqualified"]
            for preference in preferences:
                preferred = []
                for index in matches:
                    header = compact(" ".join(text_direction_variants(clean(headers[index])))).lower()
                    if preference == "unqualified" and not any(term in header for term in qualifiers):
                        preferred.append(index)
                    elif preference != "unqualified" and preference in header:
                        preferred.append(index)
                if preferred:
                    return preferred[0]
            # Best-effort mode: preserve output for review instead of rolling
            # back the whole document. Evidence still points to the source page.
            return matches[0]
        if matches:
            return matches[0]
    for index, cell in enumerate(headers):
        for value in text_direction_variants(clean(cell)):
            if pin_count is not None and str(pin_count) in value and any(package in value for package in ("LQFP", "UFQFPN", "TSSOP", "VFQFPN", "BGA")):
                return index
    return None


def split_functions(value: object, profile: DocumentProfile, pin_name: str) -> list[str]:
    # Newlines in older ST tables normally separate functions.  The exception is
    # a wrapped identifier such as ``TIM2_CH1_\nETR``; join only that case.
    text = str(value or "").replace("\r", "")
    text = re.sub(r"(?<=_)\n\s*", "", text)
    text = re.sub(r"\(\d+\)", "", text)
    values = []
    for part in re.split(r"[,/\n]", text):
        function = profile.normalize_function(part)
        if (
            function
            and function not in {"-", "--", pin_name, f"({pin_name})"}
            and not function.endswith("-")
            and not re.fullmatch(r"\(?\d+\)?", function)
            and function not in values
        ):
            values.append(function)
    return values


def pin_tables(
    pdf: Any,
    page_texts: list[str],
    model: str,
    profile: DocumentProfile,
    package_types: Iterable[str] = (),
) -> list[dict[str, Any]]:
    packages = list(package_types)
    package_code, _ = model_codes(model)
    package_preferences = profile.payload["stm32"].get("package_code_preferences", {}).get(package_code, [])
    pin_variant_preferences = profile.payload["stm32"].get("pin_column_variant_preferences", [])
    if packages and package_preferences:
        packages = sorted(
            packages,
            key=lambda package: next(
                (index for index, prefix in enumerate(package_preferences) if compact(package).upper().startswith(compact(prefix).upper())),
                len(package_preferences),
            ),
        )
    pin_count = None
    if not packages:
        pin_count = int(profile.payload["stm32"]["pin_count_codes"][package_code])
    result = []
    locator = RULE_CONFIG["document_locators"]["pin_table"]
    header_scan_rows = int(locator.get("header_scan_rows", 2))
    page_terms = [compact(item).lower() for item in locator["page_any"]]
    header_terms = [compact(item).lower() for item in locator["header_all"]]
    header_term_groups = [
        [compact(item).lower() for item in group]
        for group in locator.get("header_term_groups", [])
    ]
    pin_name_terms = configured_terms("pin_header_aliases", "pin_name")
    pin_name_term_groups = configured_term_groups("pin_header_aliases", "pin_name_all")
    alternate_terms = configured_terms("pin_header_aliases", "alternate_functions")
    main_terms = configured_terms("pin_header_aliases", "main_function")
    for page_index, page in enumerate(pdf.pages):
        if not any(term in compact(page_texts[page_index]).lower() for term in page_terms):
            continue
        for table in page.find_tables():
            rows = table.extract()
            if len(rows) < 3:
                continue
            # Pin tables commonly split the semantic header across two physical
            # rows: package columns on the second row, and pin/function labels
            # spanning both.  Match the normalized header band, not only row 0.
            header_rows = rows[:header_scan_rows]
            header_text = compact(
                " ".join(
                    variant
                    for row in header_rows
                    for cell in row
                    for variant in text_direction_variants(clean(cell))
                )
            ).lower()
            if not all(term in header_text for term in header_terms) or not all(
                all(term in header_text for term in group) for group in header_term_groups
            ):
                continue
            package_header_index = None
            column = None
            for candidate_index, package_headers in enumerate(header_rows):
                candidate_column = package_column(package_headers, pin_count, packages, pin_variant_preferences)
                if candidate_column is not None:
                    package_header_index = candidate_index
                    column = candidate_column
                    break
            if column is None or package_header_index is None:
                continue
            semantic_header_rows = rows[: package_header_index + 1]
            header_band = [
                compact(
                    " ".join(
                        variant
                        for row in semantic_header_rows
                        if index < len(row)
                        for variant in text_direction_variants(clean(row[index]))
                    )
                ).lower()
                for index in range(max(len(row) for row in semantic_header_rows))
            ]
            pin_name_column = next(
                (
                    index
                    for index, cell in enumerate(header_band)
                    if header_matches(cell, pin_name_terms, pin_name_term_groups)
                ),
                None,
            )
            if pin_name_column is None:
                continue
            alt_columns = [index for index, cell in enumerate(header_band) if any(term in cell for term in alternate_terms)]
            function_columns = list(range(alt_columns[0], len(rows[0]))) if alt_columns else []
            default_column = next((index for index, cell in enumerate(header_band) if any(term in cell for term in main_terms)), None)
            page_rows = []
            for row in rows[package_header_index + 1 :]:
                if column >= len(row) or pin_name_column >= len(row):
                    continue
                pin_number = clean(row[column])
                pin_match = re.search(str(profile.payload["patterns"]["pin"]), clean(row[pin_name_column]), re.I)
                if not pin_match or not valid_pin_designator(compact(pin_number)):
                    continue
                pin_name = pin_match.group(0).upper()
                normalized_pin_number: int | str = int(pin_number) if re.fullmatch(r"\d+", compact(pin_number)) else compact(pin_number).upper()
                functions = []
                if default_column is not None and default_column < len(row):
                    functions.extend(split_functions(row[default_column], profile, pin_name))
                for function_column in function_columns:
                    if function_column < len(row):
                        functions.extend(split_functions(row[function_column], profile, pin_name))
                for function in dict.fromkeys(functions):
                    page_rows.append({
                        "chip_id": model,
                        "pin_name": pin_name,
                        "pin_number": normalized_pin_number,
                        "function_name": function,
                        "is_supported": True,
                        "af_code": None,
                        "default_function": pin_name,
                        "constraints": None,
                    })
            if page_rows:
                result.append({"page": page_index + 1, "bbox": table.bbox, "rows": page_rows})
    return result


def process_document(doc: dict[str, Any], collector: Collector, aliases: dict[str, str], profile: DocumentProfile) -> tuple[dict[str, Any], list[Evidence]]:
    path: Path = doc["path"]
    collector.current_source_rank = int(doc.get("version_rank") or 0)
    model = aliases.get(target_model(path, profile), target_model(path, profile))
    texts = doc.get("page_texts") or extract_page_texts(path)
    evidences: list[Evidence] = []
    document_ir = build_document_ir(path, texts, progress=doc.get("table_progress"))
    with pdfplumber.open(path) as pdf:
        feature_table, observations = select_observations(
            list(document_ir.tables), model, FEATURE_RECIPE, profile.payload["stm32"]
        )
        header_package = package_header_observation(
            feature_table,
            observations,
            model,
            profile.payload["patterns"]["package"],
        )
        if header_package is not None:
            observations.append(header_package)
        candidates = observations_to_candidates(observations, FEATURE_RECIPE, entity_type="chip")
        reduced, aggregates = reduce_candidates(
            candidates, FEATURE_RECIPE, package_pattern=profile.payload["patterns"]["package"]
        )
        hard_issues = validate_hard_rules(candidates, aggregates, FEATURE_RECIPE)
        if hard_issues:
            raise ValueError("; ".join(issue.message for issue in hard_issues))
        values = {**reduced["fields"], "peripherals": reduced["peripherals"]}
        feature_page = feature_table.pages[0]
        feature = {
            "page": feature_page,
            "bbox": feature_table.bboxes[0],
            "text": texts[feature_page - 1],
        }
        feature_evidence = make_evidence(doc, feature["page"], feature["bbox"], "model", "A", feature["text"], 1)
        evidences.append(feature_evidence)
        ordering_evidence = None
        ordering_flash = ordering_flash_value(texts, model)
        if ordering_flash is not None and ordering_flash[0] != values.get("flash_kb"):
            flash_value, ordering_page = ordering_flash
            values["flash_kb"] = flash_value
            ordering_pdf_page = pdf.pages[ordering_page - 1]
            ordering_evidence = make_evidence(
                doc,
                ordering_page,
                (0, 0, ordering_pdf_page.width, ordering_pdf_page.height),
                "ordering",
                "A",
                texts[ordering_page - 1],
                1,
            )
            evidences.append(ordering_evidence)
        package_evidence = None
        package_aggregate = next((item for item in aggregates if item.field_name == "package_types"), None)
        if package_aggregate and package_aggregate.evidence_pages and package_aggregate.evidence_pages[0] != feature["page"]:
            package_page = package_aggregate.evidence_pages[0]
            package_position = feature_table.pages.index(package_page)
            package_bbox = feature_table.bboxes[package_position]
            package_evidence = make_evidence(doc, package_page, package_bbox, "package", "A", texts[package_page - 1], 1)
            evidences.append(package_evidence)
        elif not values.get("package_types"):
            packages, package_page, package_bbox = package_values_from_ir(document_ir, model, profile)
            values["package_types"] = packages
            if package_page and package_bbox and package_page != feature["page"]:
                package_evidence = make_evidence(doc, package_page, package_bbox, "package", "A", texts[package_page - 1], 1)
                evidences.append(package_evidence)
        chip_row = {
            "chip_id": model,
            "series": profile.series_for_model(model),
            "core": "ARM",
            "flash_kb": values.get("flash_kb"),
            "ram_kb": values.get("ram_kb"),
            "max_freq_mhz": values.get("max_freq_mhz"),
            "package_types": values.get("package_types", []),
            "voltage_min": values.get("voltage_min"),
            "voltage_max": values.get("voltage_max"),
        }
        feature_fields = {"chip_id", "series", "core", "ram_kb", "max_freq_mhz"}
        if ordering_evidence is None:
            feature_fields.add("flash_kb")
        if values.get("package_types") and package_evidence is None:
            feature_fields.add("package_types")
        collector.add(
            "chips",
            chip_row,
            feature_evidence.evidence_id,
            "A",
            {field: feature_evidence.evidence_id for field in feature_fields},
        )
        if ordering_evidence is not None:
            collector.add(
                "chips",
                chip_row,
                ordering_evidence.evidence_id,
                "A",
                {"flash_kb": ordering_evidence.evidence_id},
            )
        if package_evidence is not None:
            collector.add(
                "chips",
                chip_row,
                package_evidence.evidence_id,
                "A",
                {"package_types": package_evidence.evidence_id},
            )
        if values.get("flash_kb") is not None:
            flash_evidence = ordering_evidence or feature_evidence
            collector.add("memory", {"chip_id": model, "memory_type": "Flash", "size_kb": values["flash_kb"], "organization": None, "features": None}, flash_evidence.evidence_id, "A")
        if values.get("ram_kb") is not None:
            collector.add("memory", {"chip_id": model, "memory_type": "SRAM", "size_kb": values["ram_kb"], "organization": None, "features": None}, feature_evidence.evidence_id, "A")
        for peripheral_type, item in values.get("peripherals", {}).items():
            collector.add("peripherals", {"chip_id": model, "peripheral_type": peripheral_type, "instance_count": item.get("count"), "max_resolution": item.get("resolution"), "max_frequency_mhz": None, "features": None}, feature_evidence.evidence_id, "A")

        operating = operating_values_from_ir(document_ir, texts)
        electrical_voltage_added = False
        if operating:
            operating_evidence = make_evidence(doc, operating["page"], operating["bbox"], "electrical", "A", texts[operating["page"] - 1], 1)
            evidences.append(operating_evidence)
            if operating.get("voltage_min") is not None:
                feature_voltage = (chip_row.get("voltage_min"), chip_row.get("voltage_max"))
                operating_voltage = (operating["voltage_min"], operating["voltage_max"])
                if feature_voltage == (None, None) or feature_voltage == operating_voltage:
                    collector.add(
                        "chips",
                        {**chip_row, "voltage_min": operating["voltage_min"], "voltage_max": operating["voltage_max"]},
                        operating_evidence.evidence_id,
                        "A",
                        {"voltage_min": operating_evidence.evidence_id, "voltage_max": operating_evidence.evidence_id},
                    )
                collector.add("electrical", {"chip_id": model, "param_name": "VDD_operating", "min_value": operating["voltage_min"], "max_value": operating["voltage_max"], "unit": "V", "conditions": None}, operating_evidence.evidence_id, "A")
                electrical_voltage_added = True
            if operating.get("temperature_min") is not None:
                collector.add("electrical", {"chip_id": model, "param_name": "T_ambient", "min_value": operating["temperature_min"], "max_value": operating["temperature_max"], "unit": "°C", "conditions": None}, operating_evidence.evidence_id, "A")

        if not electrical_voltage_added and chip_row.get("voltage_min") is not None:
            collector.add(
                "electrical",
                {
                    "chip_id": model,
                    "param_name": "VDD_operating",
                    "min_value": chip_row["voltage_min"],
                    "max_value": chip_row["voltage_max"],
                    "unit": "V",
                    "conditions": None,
                },
                feature_evidence.evidence_id,
                "A",
            )

        ir_table_view = IRDocumentTableView(document_ir)
        for index, table in enumerate(
            pin_tables(ir_table_view, texts, model, profile, values.get("package_types", [])),
            start=1,
        ):
            evidence = make_evidence(doc, table["page"], table["bbox"], "pin", "B", texts[table["page"] - 1], index)
            evidences.append(evidence)
            for row in table["rows"]:
                collector.add("pin_mux", row, evidence.evidence_id, "B")

    return {
        "doc_id": doc["doc_id"],
        "sha256": doc["sha256"],
        "version": doc.get("version"),
        "source_file": str(path),
        "duplicate_paths": doc["duplicates"],
        "page_count": len(texts),
        "basic_page": feature["page"],
        "model_count": 1,
        "models": [model],
        "evidence_count": len(evidences),
    }, evidences
