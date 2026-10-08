from __future__ import annotations

import re
import json
from contextlib import nullcontext
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable

from core.field_dictionary import load_table_recipe
from core.pin_config import load_pin_recipe
from core.pin_ir import PinDefinitionIR, pin_identity_issues, resolve_pin_mux
from core.collector import Collector
from core.candidate_executor import reduce_candidates, validate_hard_rules
from core.documents import extract_page_texts
from core.evidence import Evidence
from core.field_rules import observations_to_candidates
from core.profile import DocumentProfile
from core.section_runner import run_optional_section
from core.table_ir_builder import build_document_ir
from core.table_index import DocumentTableIndex, TableCandidateQuery, build_table_index
from core.table_ir_legacy_view import IRDocumentTableView
from core.text import clean, compact, parse_number, scalar_or_none
from family_rules.stm32_matrix import (
    discover_device_summary_models,
    discover_model_variants,
    ordering_codes,
    ordering_suffix,
    package_header_observation,
    select_observations,
)
from topologies.matrix_observer import table_grids
from topologies.operating_conditions import (
    extract_operating_conditions,
    is_primary_voltage_row,
)
from topologies.ordering_code_legend import parse_ordering_code_legend
from topologies.package_matrix import find_package_values, parse_package_names
from topologies.pin_af_matrix import observe_pin_af_matrices, unique_pin_af_lookup
from topologies.pin_definition import (
    extract_pin_definitions,
    infer_pin_header_depth as topology_infer_pin_header_depth,
    package_column as topology_package_column,
    split_functions as split_pin_functions,
)


SECTION_RECIPE: dict[str, Any] = {}
FEATURE_RECIPE: dict[str, Any] = {}
PIN_RECIPE: dict[str, Any] = {}
PACKAGE_PATTERN = ""


def configure(profile: DocumentProfile) -> None:
    global SECTION_RECIPE, FEATURE_RECIPE, PIN_RECIPE, PACKAGE_PATTERN
    FEATURE_RECIPE = load_table_recipe(profile.topology_preset("feature_matrix"), profile.field_aliases)
    SECTION_RECIPE = json.loads(profile.topology_preset("operating_conditions").read_text(encoding="utf-8"))
    PIN_RECIPE = load_pin_recipe(profile.topology_preset("pin_relations"))
    PACKAGE_PATTERN = profile.package_re.pattern


def source_ordering_config(profile: DocumentProfile, page_texts: list[str]) -> dict[str, Any]:
    """Resolve ordering-code meanings only from the source PDF legend.

    Non-semantic family settings remain available, but the three code maps are
    replaced rather than merged so a missing source declaration cannot be
    silently supplied by the legacy profile.
    """
    result: dict[str, Any] = {}
    legend = parse_ordering_code_legend(page_texts)
    for name, source_values in (
        ("pin_count_codes", legend.pin_count_codes),
        ("flash_code_kb", legend.flash_code_kb),
        ("package_type_codes", legend.package_type_codes),
        ("pinout_qualifier_codes", legend.pinout_qualifier_codes),
    ):
        result[name] = dict(source_values)
    result["ordering_legend_pages"] = list(legend.evidence_pages)
    result["ordering_code_source"] = "pdf"
    result["ordering_code_missing"] = [
        name
        for name in ("pin_count_codes", "flash_code_kb", "package_type_codes")
        if not result[name]
    ]
    return result


def pin_counts_for_code(ordering_config: dict[str, Any], package_code: str) -> tuple[int, ...]:
    value = ordering_config.get("pin_count_codes", {}).get(package_code)
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    return tuple(dict.fromkeys(int(item) for item in values))


def configured_output_value(section: str, name: str, default: str) -> str:
    """Compatibility wrapper around the fixed public output vocabulary."""
    values = {("memory_type", "flash"): "Flash", ("memory_type", "ram"): "SRAM_Internal", ("electrical_param", "voltage"): "VDD", ("electrical_param", "temperature"): "T_operating"}
    return values.get((section, name), default)


def missing_review_fields(section: str, row: dict[str, Any]) -> list[str]:
    """Return configured business fields that still need source confirmation."""
    fields = ("flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max") if section == "chips" else ()
    return [field for field in fields if row.get(field) in (None, "", [])]


def preferred_model_voltage(
    feature_values: dict[str, Any],
    operating_values: dict[str, Any] | None,
) -> tuple[Any, Any] | None:
    """Prefer a model-applicable range over a document-wide fallback."""
    if feature_values.get("voltage_min") is not None and feature_values.get("voltage_max") is not None:
        return feature_values["voltage_min"], feature_values["voltage_max"]
    if operating_values and operating_values.get("voltage_min") is not None:
        return operating_values.get("voltage_min"), operating_values.get("voltage_max")
    return None


def configured_term_groups(section: str, name: str) -> list[list[str]]:
    return [
        [compact(item).lower() for item in group if compact(item)]
        for group in SECTION_RECIPE.get(section, {}).get(name, [])
        if isinstance(group, list)
    ]


def indexed_tables(
    document: Any,
    table_index: DocumentTableIndex,
    query: TableCandidateQuery,
) -> list[Any]:
    """Resolve ranked index entries back to Table IR without scan fallback."""
    by_id = {table.table_id: table for table in document.tables}
    return [by_id[item.table_id] for item in table_index.rank(query) if item.table_id in by_id]


def feature_candidate_tables(document: Any, table_index: DocumentTableIndex) -> list[Any]:
    locator = FEATURE_RECIPE.get("locator", {})
    candidates = indexed_tables(
        document,
        table_index,
        TableCandidateQuery(
            title_any=tuple(FEATURE_RECIPE.get("title_any", [])),
            text_any=tuple(locator.get("required_any", [])),
            preferred_features=frozenset({"continued", "merged_cells"}),
        ),
    )
    # A concrete ordering-code table can contain the model plus words such as
    # RAM/package and therefore outrank a wildcard feature matrix.  When an
    # explicitly titled feature matrix exists, constrain this extraction stage
    # to that semantic table class.  Caption-damaged documents retain the
    # structural fallback.
    title_terms = [compact(item).casefold() for item in FEATURE_RECIPE.get("title_any", [])]
    titled = [
        table
        for table in candidates
        if any(term in compact(table_index.get(table.table_id).title).casefold() for term in title_terms)
    ]
    return titled or candidates


def header_matches(value: str, terms: Iterable[str] = (), term_groups: Iterable[Iterable[str]] = ()) -> bool:
    normalized = compact(value).lower()
    return any(term in normalized for term in terms) or any(
        all(term in normalized for term in group) for group in term_groups
    )


def make_evidence(doc: dict[str, Any], page: int, bbox: Iterable[float], kind: str, confidence: str, raw_text: str, sequence: int) -> Evidence:
    evidence_namespace = str(doc.get("_evidence_namespace") or doc["doc_id"])
    return Evidence(
        evidence_id=f"EV-{evidence_namespace}-{page:04d}-{kind.upper()}-{sequence:02d}",
        doc_id=doc["doc_id"],
        source_file=str(doc["path"]),
        page=page,
        bbox=[round(float(value), 2) for value in bbox],
        kind=kind,
        confidence=confidence,
        raw_text=clean(raw_text)[:1200],
    )


def target_model(path: Path, profile: DocumentProfile) -> str:
    # Search downloads commonly insert an underscore before the model suffix.
    # Accept a one-letter product variant (STM32MP157_A) as well as concrete
    # package/density pairs (STM32C031_C4), but do not absorb numeric copy
    # suffixes such as ``_2``.
    stem = re.sub(r"(STM32[A-Z]{1,2}\d{2,3})_([A-Z][A-Z0-9]*)", r"\1\2", path.stem, count=1, flags=re.I)
    for rule in profile.payload["model"].get("filename_rules", []):
        candidate = re.search(str(rule["pattern"]), stem, re.I)
        if candidate:
            model = candidate.group(int(rule.get("group", 1))).upper()
            option = re.search(rf"{re.escape(model)}-([A-Z])(?:$|[^A-Z0-9])", stem, re.I)
            return f"{model}-{option.group(1).upper()}" if option else model
    match = profile.model_re.search(stem)
    if not match:
        raise ValueError(f"STM32 profile cannot derive a target model from filename: {path.name}")
    return match.group(0).upper()


def model_codes(model: str, ordering_config: dict[str, Any] | None = None) -> tuple[str, str]:
    # A concrete STM32 ordering code ends in a letter package code followed by
    # a density code.  Generic family names such as STM32MP157C must not be
    # backtracked into the false pair ("7", "C").
    try:
        if ordering_config:
            return ordering_codes(model, ordering_config)
        return ordering_suffix(model)
    except ValueError as error:
        raise ValueError(f"unsupported STM32 model code: {model}") from error


def ordering_flash_value(
    page_texts: list[str], model: str, ordering_config: dict[str, Any] | None = None
) -> tuple[float | int, int] | None:
    locator = SECTION_RECIPE.get("ordering_legend", {})
    page_terms = [compact(item).lower() for item in locator.get("page_any", [])]
    label_terms = [compact(item).lower() for item in locator.get("flash_label_any", [])]
    try:
        _, flash_code = model_codes(model, ordering_config)
    except ValueError:
        return None
    if ordering_config and flash_code in ordering_config.get("flash_code_kb", {}):
        pages = ordering_config.get("ordering_legend_pages", [])
        if pages:
            return ordering_config["flash_code_kb"][flash_code], int(pages[0])
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


def parse_packages(text: str) -> list[str]:
    if not PACKAGE_PATTERN:
        raise RuntimeError("STM32 adapter is not configured")
    return parse_package_names(text, PACKAGE_PATTERN)


def package_values_from_ir(
    document: Any,
    model: str,
    profile: DocumentProfile,
    ordering_config: dict[str, Any] | None = None,
    candidate_table_ids: Iterable[str] = (),
) -> tuple[list[str], int | None, tuple[float, ...] | None]:
    ordering = ordering_config or {}
    try:
        package_code, _ = model_codes(model, ordering)
        pin_counts = pin_counts_for_code(ordering, package_code)
    except (KeyError, ValueError):
        return [], None, None
    definition = PIN_RECIPE["topologies"]["pin_definition"]
    preferred_package = ordering.get("package_type_codes", {}).get(package_code)
    return find_package_values(
        document,
        pin_counts=pin_counts,
        package_pattern=profile.package_re.pattern,
        package_label_terms=FEATURE_RECIPE["fields"]["package_types"]["rows"],
        pin_header_terms=definition["pin_name_aliases"],
        preferred_package_kinds=(preferred_package,) if preferred_package else (),
        qualifier_preferences=definition["column_qualifier_preferences"],
        candidate_table_ids=candidate_table_ids,
    )


def operating_values_from_ir(
    document: Any,
    page_texts: list[str],
    table_index: DocumentTableIndex,
) -> dict[str, Any] | None:
    return extract_operating_conditions(document, table_index, SECTION_RECIPE)


def package_column(
    headers: list[Any],
    pin_count: int | Iterable[int] | None = None,
    package_types: Iterable[str] = (),
    qualifier_preferences: Iterable[str] = (),
) -> int | None:
    return topology_package_column(headers, pin_count, package_types, qualifier_preferences)


def split_functions(
    value: object,
    profile: DocumentProfile,
    pin_name: str,
    *,
    wrapped_cell: bool = False,
) -> list[str]:
    return split_pin_functions(value, profile.normalize_function, pin_name, wrapped_cell=wrapped_cell)


def infer_pin_header_depth(
    rows: list[list[Any]],
    *,
    header_terms: Iterable[str],
    header_term_groups: Iterable[Iterable[str]],
    package_targets: Iterable[str | None],
    pin_count: int | Iterable[int] | None,
    qualifier_preferences: Iterable[str] = (),
) -> int | None:
    return topology_infer_pin_header_depth(
        rows,
        header_terms=header_terms,
        header_term_groups=header_term_groups,
        package_targets=package_targets,
        pin_count=pin_count,
        qualifier_preferences=qualifier_preferences,
    )


def summary_values(page_texts: list[str], profile: DocumentProfile) -> tuple[dict[str, Any], int]:
    """Read explicitly labelled cover/summary facts without a matrix layout."""
    text = "\n".join(page_texts[:8])
    values: dict[str, Any] = {}

    def memory_value(label: str) -> float | int | None:
        patterns = {
            "flash": r"(\d+(?:\.\d+)?)\s*[- ]?(Mbytes?|Kbytes?)\s+(?:of\s+)?(?:embedded\s+)?flash\s+memory|(?:flash\s+memory|flash)[^\n,;]{0,30}?(\d+(?:\.\d+)?)\s*[- ]?(Mbytes?|Kbytes?)",
            "ram": r"(\d+(?:\.\d+)?)\s*[- ]?Kbytes?\s+(?:of\s+)?(?:internal\s+)?SRAM|(?:embedded\s+)?SRAM\s*\(?\s*(\d+(?:\.\d+)?)\s*[- ]?Kbytes?",
        }
        found = re.search(patterns[label], text, re.I)
        if not found:
            return None
        groups = [item for item in found.groups() if item]
        number = float(groups[0])
        if label == "flash" and len(groups) > 1 and groups[1].lower().startswith("m"):
            number *= 1024
        return scalar_or_none(number)

    flash = memory_value("flash")
    ram = memory_value("ram")
    if flash is not None:
        values["flash_kb"] = flash
    elif re.search(r"external\s+DDR\s+memory", text, re.I) and re.search(r"(?:Quad|Octo)-?SPI\s+(?:flash\s+)?memory\s+interface", text, re.I):
        values["flash_kb"] = 0
    if ram is not None:
        values["ram_kb"] = ram
    frequency = re.search(r"(?:up\s+to\s+|at\s+)(\d+(?:\.\d+)?)\s*(GHz|MHz)\b", text, re.I)
    if frequency:
        values["max_freq_mhz"] = scalar_or_none(float(frequency.group(1)) * (1000 if frequency.group(2).lower() == "ghz" else 1))
    voltage_ranges = [
        (float(low), float(high))
        for low, high in re.findall(r"(\d+(?:\.\d+)?)\s*(?:V\s*)?to\s+(\d+(?:\.\d+)?)\s*V", text, re.I)
    ]
    if voltage_ranges:
        values["voltage_min"] = scalar_or_none(min(item[0] for item in voltage_ranges))
        values["voltage_max"] = scalar_or_none(max(item[1] for item in voltage_ranges))
    source_page = next((index for index, value in enumerate(page_texts[:8], 1) if re.search(r"\bFeatures\b|Product summary", value, re.I)), 1)
    packages = parse_package_names(page_texts[source_page - 1], profile.package_re.pattern)
    packages = [item for item in packages if not any(other != item and other.startswith(item) for other in packages)]
    if packages:
        values["package_types"] = packages
    return values, source_page


def summary_peripheral_counts(page_texts: list[str]) -> tuple[dict[str, int], int | None]:
    """Count only explicitly numbered feature-list bullets."""
    for page, source in enumerate(page_texts[:10], 1):
        if not re.search(r"peripherals|communication", source, re.I):
            continue
        counts: dict[str, int] = {}
        for kind, aliases in {"SPI": ("SPI",), "I2C": ("I2C",), "UART": ("USART", "UART", "LPUART"), "ADC": ("ADC",)}.items():
            total = 0
            for alias in aliases:
                total += sum(int(value) for value in re.findall(rf"(?:•|–)\s*(\d+)\s*[×x]\s*{alias}s?\b", source, re.I))
                total += sum({"one": 1, "two": 2, "three": 3, "four": 4}.get(value.lower(), 0) for value in re.findall(rf"(?:•|–)\s*(one|two|three|four)\s+{alias}s?\b", source, re.I))
            if total:
                counts[kind] = total
        if counts:
            return counts, page
    return {}, None


def process_summary_document(
    doc: dict[str, Any], collector: Collector, profile: DocumentProfile, document_ir: Any
) -> tuple[dict[str, Any], list[Evidence]]:
    """Extract a single-product summary document with a definition-only pin map."""
    path: Path = doc["path"]
    texts = doc["page_texts"]
    model = target_model(path, profile)
    values, page = summary_values(texts, profile)
    required = ("flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max")
    missing = [item for item in required if values.get(item) in (None, [], "")]
    if missing:
        raise ValueError(f"summary document lacks explicit required fields: {', '.join(missing)}")
    page_ir = document_ir.pages[page - 1]
    evidence = make_evidence(doc, page, (0, 0, page_ir.width, page_ir.height), "summary", "A", texts[page - 1], 1)
    evidences = [evidence]
    chip = {
        "chip_id": model,
        "series": profile.series_for_model(model),
        "core": "ARM",
        **{field: values[field] for field in required},
    }
    collector.add("chips", chip, evidence.evidence_id, "A")
    collector.add("memory", {"chip_id": model, "memory_type": "Flash", "size_kb": values["flash_kb"], "organization": None, "features": None}, evidence.evidence_id, "A")
    collector.add("memory", {"chip_id": model, "memory_type": "SRAM_Internal", "size_kb": values["ram_kb"], "organization": None, "features": None}, evidence.evidence_id, "A")
    collector.add("electrical", {"chip_id": model, "param_name": "VDD", "min_value": values["voltage_min"], "max_value": values["voltage_max"], "unit": "V", "conditions": None}, evidence.evidence_id, "A")

    counts, peripheral_page = summary_peripheral_counts(texts)
    peripheral_count = 0
    if peripheral_page:
        source = texts[peripheral_page - 1]
        p = document_ir.pages[peripheral_page - 1]
        peripheral_evidence = make_evidence(doc, peripheral_page, (0, 0, p.width, p.height), "peripheral", "A", source, 1)
        evidences.append(peripheral_evidence)
        for kind, count in counts.items():
            collector.add("peripherals", {"chip_id": model, "peripheral_type": kind, "instance_count": count, "channel_count": None, "max_resolution": None, "max_frequency_mhz": None, "features": None}, peripheral_evidence.evidence_id, "A")
            peripheral_count += 1

    pin_rows = 0
    for table in document_ir.tables:
        grid = next(iter(table_grids(table)), None)
        if grid is None:
            continue
        _, table_page, rows, _ = grid
        header = compact(" ".join(str(cell) for row in rows[:3] for cell in row)).lower()
        if "pinname" not in header or "functionafterreset" not in header:
            continue
        pin_page = document_ir.pages[table_page - 1]
        pin_evidence = make_evidence(doc, table_page, table.bboxes[0], "pin", "A", texts[table_page - 1], len(evidences) + 1)
        added_here = 0
        for row in rows:
            if not row or not re.fullmatch(r"\d+", compact(row[0])):
                continue
            pin_name = next((match.group(0).upper() for cell in row[1:] for match in [profile.pin_re.search(clean(cell))] if match), None)
            if not pin_name:
                continue
            collector.add("pin_mux", {"chip_id": model, "pin_name": pin_name, "pin_number": int(compact(row[0])), "function_name": pin_name, "is_supported": True, "af_code": None, "default_function": pin_name, "constraints": None}, pin_evidence.evidence_id, "A")
            pin_rows += 1
            added_here += 1
        if added_here:
            evidences.append(pin_evidence)
    sections = {"chips": "success", "memory": "success", "electrical": "success", "peripherals": "success" if peripheral_count else "not_applicable", "pin_mux": "success" if pin_rows else "not_applicable", "pin_af": "not_applicable"}
    return {"doc_id": doc["doc_id"], "sha256": doc["sha256"], "version": doc.get("version"), "source_file": str(path), "duplicate_paths": doc["duplicates"], "page_count": len(texts), "basic_page": page, "model_count": 1, "models": [model], "evidence_count": len(evidences), "status": "complete", "sections": sections, "review_items": []}, evidences


def pin_tables(
    pdf: Any,
    page_texts: list[str],
    model: str,
    profile: DocumentProfile,
    package_types: Iterable[str] = (),
    ordering_config: dict[str, Any] | None = None,
    table_index: Any | None = None,
) -> list[dict[str, Any]]:
    ordering = ordering_config or {}
    packages = list(package_types)
    try:
        package_code, _ = model_codes(model, ordering)
    except ValueError:
        package_code = None
    definition_recipe = PIN_RECIPE["topologies"]["pin_definition"]
    pin_variant_preferences = definition_recipe["column_qualifier_preferences"]
    selected_pinout_qualifier = next(
        (
            qualifier
            for code, qualifier in sorted(
                ordering.get("pinout_qualifier_codes", {}).items(),
                key=lambda item: len(str(item[0])),
                reverse=True,
            )
            if model.endswith(str(code).upper())
        ),
        None,
    )
    if selected_pinout_qualifier:
        pin_variant_preferences = [
            selected_pinout_qualifier,
            *[item for item in pin_variant_preferences if item != selected_pinout_qualifier],
        ]
    pin_counts = pin_counts_for_code(ordering, package_code)
    if not packages and not pin_counts:
        return []
    pin_count = pin_counts or None
    return extract_pin_definitions(
        pdf,
        model=model,
        pin_pattern=profile.pin_re,
        package_pattern=profile.package_re,
        normalize_function=profile.normalize_function,
        # The merged pin-table header may repeat the same package under sibling
        # product groups.  The ordering-code stem (everything before package
        # and density) is the narrow identity needed to bind the right column;
        # the broader profile series can match both groups.
        series_identity=(
            model[: model.find(package_code)]
            if package_code and model.find(package_code) >= len(profile.series_for_model(model))
            else profile.series_for_model(model)
        ),
        recipe={**definition_recipe, "column_qualifier_preferences": pin_variant_preferences},
        package_types=packages,
        pin_count=pin_count,
        table_index=table_index,
    )


def _process_single_model(doc: dict[str, Any], collector: Collector, aliases: dict[str, str], profile: DocumentProfile) -> tuple[dict[str, Any], list[Evidence]]:
    path: Path = doc["path"]
    collector.current_source_rank = int(doc.get("version_rank") or 0)
    filename_model = target_model(path, profile)
    model = str(doc.get("_target_model") or aliases.get(filename_model, filename_model))
    texts = doc.get("page_texts") or extract_page_texts(path)
    evidences: list[Evidence] = []
    sections: dict[str, str] = {}
    review_items: list[dict[str, Any]] = []
    document_ir = doc.get("_document_ir") or build_document_ir(path, texts, progress=doc.get("table_progress"))
    table_index = doc.get("_table_index") or build_table_index(document_ir)
    ordering_config = doc.get("_ordering_config") or source_ordering_config(profile, texts)
    # Table IR already carries every page size needed for full-page ordering
    # evidence. Reopening the complete PDF once per expanded model retained a
    # second page tree and caused long-family documents to peak unnecessarily.
    with nullcontext(document_ir) as pdf:
        feature_tables = feature_candidate_tables(document_ir, table_index)
        if not feature_tables:
            raise ValueError("full-document index found no STM32 feature-matrix candidate")
        feature_table, observations = select_observations(
            feature_tables, model, FEATURE_RECIPE, ordering_config
        )
        header_package = package_header_observation(
            feature_table,
            observations,
            model,
            profile.package_re.pattern,
        )
        if header_package is not None:
            observations.append(header_package)
        candidates = observations_to_candidates(observations, FEATURE_RECIPE, entity_type="chip")
        reduced, aggregates = reduce_candidates(
            candidates, FEATURE_RECIPE, package_pattern=profile.package_re.pattern
        )
        hard_issues = validate_hard_rules(candidates, aggregates, FEATURE_RECIPE)
        if hard_issues:
            raise ValueError("; ".join(issue.message for issue in hard_issues))
        values = {**reduced["fields"], "peripherals": reduced["peripherals"]}
        cover, cover_page = summary_values(texts, profile)
        summary_overrides: set[str] = set()
        try:
            model_codes(model, ordering_config)
            generic_model = False
        except ValueError:
            generic_model = True
        for field_name in ("flash_kb", "max_freq_mhz", "voltage_min", "voltage_max"):
            if values.get(field_name) is None and cover.get(field_name) is not None:
                values[field_name] = cover[field_name]
                summary_overrides.add(field_name)
        if cover.get("ram_kb") is not None and (
            values.get("ram_kb") is None or cover["ram_kb"] > values["ram_kb"]
        ):
            values["ram_kb"] = cover["ram_kb"]
            summary_overrides.add("ram_kb")
        if generic_model and cover.get("package_types"):
            values["package_types"] = cover["package_types"]
            summary_overrides.add("package_types")
        if not values.get("peripherals"):
            summary_counts, _ = summary_peripheral_counts(texts)
            if summary_counts:
                values["peripherals"] = {
                    kind: {"count": count, "channel_count": None, "resolution": None}
                    for kind, count in summary_counts.items()
                }
                summary_overrides.add("peripherals")
        aggregate_index = {
            (item.field_group, item.field_name): item
            for item in aggregates
        }
        feature_page = feature_table.pages[0]
        feature = {
            "page": feature_page,
            "bbox": feature_table.bboxes[0],
            "text": texts[feature_page - 1],
        }
        evidence_pages = {feature_page}
        for aggregate in aggregates:
            evidence_pages.update(aggregate.evidence_pages)
        feature_evidence_by_page: dict[int, Evidence] = {}
        for sequence, page in enumerate(sorted(evidence_pages), 1):
            position = feature_table.pages.index(page)
            evidence = make_evidence(
                doc,
                page,
                feature_table.bboxes[position],
                "model",
                "A",
                texts[page - 1],
                sequence,
            )
            evidences.append(evidence)
            feature_evidence_by_page[page] = evidence
        feature_evidence = feature_evidence_by_page[feature_page]
        summary_evidence = None
        if summary_overrides:
            cover_ir = document_ir.pages[cover_page - 1]
            summary_evidence = make_evidence(
                doc,
                cover_page,
                (0, 0, cover_ir.width, cover_ir.height),
                "summary",
                "A",
                texts[cover_page - 1],
                1,
            )
            evidences.append(summary_evidence)

        def aggregate_evidence_ids(group: str, name: str) -> list[str]:
            aggregate = aggregate_index.get((group, name))
            if aggregate is None:
                return []
            return [
                feature_evidence_by_page[page].evidence_id
                for page in aggregate.evidence_pages
                if page in feature_evidence_by_page
            ]
        ordering_evidence = None
        ordering_flash = ordering_flash_value(texts, model, ordering_config)
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
        # Cross-check matrix package cells against the ordering-code pin count.
        # Merged cells on continuation pages can otherwise shift a package one
        # or more model columns to the right.
        package_source_tables = feature_tables + indexed_tables(
            document_ir,
            table_index,
            TableCandidateQuery(
                required_features=frozenset({"pin_like"}),
                preferred_features=frozenset({"multi_package"}),
            ),
        )
        packages, package_page, package_bbox = package_values_from_ir(
            document_ir,
            model,
            profile,
            ordering_config,
            (table.table_id for table in package_source_tables),
        )
        if packages:
            values["package_types"] = packages
            if package_page and package_bbox and package_page != feature["page"]:
                package_evidence = make_evidence(doc, package_page, package_bbox, "package", "A", texts[package_page - 1], 1)
                evidences.append(package_evidence)
        else:
            package_aggregate = next((item for item in aggregates if item.field_name == "package_types"), None)
            if package_aggregate and package_aggregate.evidence_pages and package_aggregate.evidence_pages[0] != feature["page"]:
                package_page = package_aggregate.evidence_pages[0]
                package_position = feature_table.pages.index(package_page)
                package_bbox = feature_table.bboxes[package_position]
                package_evidence = make_evidence(doc, package_page, package_bbox, "package", "A", texts[package_page - 1], 1)
                evidences.append(package_evidence)
        operating_result = run_optional_section(
            collector,
            evidences,
            name="electrical",
            source_file=str(path),
            operation=lambda: operating_values_from_ir(document_ir, texts, table_index),
        )
        operating = operating_result.value if operating_result.status == "success" else None
        if operating_result.review_item:
            review_items.append(operating_result.review_item)
        matrix_voltage = preferred_model_voltage(values, None)
        selected_voltage = preferred_model_voltage(values, operating)
        chip_row = {
            "chip_id": model,
            "series": profile.series_for_model(model),
            "core": "ARM",
            "flash_kb": values.get("flash_kb"),
            "ram_kb": values.get("ram_kb"),
            "max_freq_mhz": values.get("max_freq_mhz"),
            "package_types": values.get("package_types", []),
            "voltage_min": selected_voltage[0] if selected_voltage else None,
            "voltage_max": selected_voltage[1] if selected_voltage else None,
        }
        chip_field_evidence: dict[str, str | list[str]] = {
            "chip_id": feature_evidence.evidence_id,
            "series": feature_evidence.evidence_id,
            "core": feature_evidence.evidence_id,
        }
        if summary_evidence is not None:
            for field_name in summary_overrides:
                chip_field_evidence[field_name] = summary_evidence.evidence_id
        for field_name in ("ram_kb", "max_freq_mhz"):
            if field_name in summary_overrides:
                continue
            ids = aggregate_evidence_ids("fields", field_name)
            if ids:
                chip_field_evidence[field_name] = ids
        if ordering_evidence is None and "flash_kb" not in summary_overrides:
            ids = aggregate_evidence_ids("fields", "flash_kb")
            if ids:
                chip_field_evidence["flash_kb"] = ids
        if values.get("package_types") and package_evidence is None and "package_types" not in summary_overrides:
            ids = aggregate_evidence_ids("fields", "package_types")
            if ids:
                chip_field_evidence["package_types"] = ids
        if matrix_voltage is not None:
            for field_name in ("voltage_min", "voltage_max"):
                ids = aggregate_evidence_ids("fields", field_name)
                if ids:
                    chip_field_evidence[field_name] = ids
        collector.add(
            "chips",
            chip_row,
            feature_evidence.evidence_id,
            "A",
            chip_field_evidence,
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
            flash_evidence = ordering_evidence or (summary_evidence if "flash_kb" in summary_overrides else feature_evidence)
            flash_ids = (
                [ordering_evidence.evidence_id]
                if ordering_evidence
                else ([summary_evidence.evidence_id] if summary_evidence and "flash_kb" in summary_overrides else aggregate_evidence_ids("fields", "flash_kb"))
            )
            collector.add("memory", {"chip_id": model, "memory_type": configured_output_value("memory_type", "flash", "Flash"), "size_kb": values["flash_kb"], "organization": None, "features": None}, flash_evidence.evidence_id, "A", {field: flash_ids or [flash_evidence.evidence_id] for field in ("chip_id", "memory_type", "size_kb")})
        if values.get("ram_kb") is not None:
            ram_ids = ([summary_evidence.evidence_id] if summary_evidence and "ram_kb" in summary_overrides else aggregate_evidence_ids("fields", "ram_kb")) or [feature_evidence.evidence_id]
            collector.add("memory", {"chip_id": model, "memory_type": configured_output_value("memory_type", "ram", "SRAM_Internal"), "size_kb": values["ram_kb"], "organization": None, "features": None}, ram_ids[0], "A", {field: ram_ids for field in ("chip_id", "memory_type", "size_kb")})
        for peripheral_type, item in values.get("peripherals", {}).items():
            peripheral_ids = ([summary_evidence.evidence_id] if summary_evidence and "peripherals" in summary_overrides else aggregate_evidence_ids("peripherals", peripheral_type)) or [feature_evidence.evidence_id]
            peripheral_row = {"chip_id": model, "peripheral_type": peripheral_type, "instance_count": item.get("count"), "channel_count": item.get("channel_count"), "max_resolution": item.get("resolution"), "max_frequency_mhz": None, "features": None}
            evidenced_fields = ["chip_id", "peripheral_type"] + [field for field in ("instance_count", "channel_count", "max_resolution") if peripheral_row.get(field) is not None]
            collector.add("peripherals", peripheral_row, peripheral_ids[0], "A", {field: peripheral_ids for field in evidenced_fields})

        electrical_voltage_added = False
        if operating:
            operating_evidence = make_evidence(doc, operating["page"], operating["bbox"], "electrical", "A", texts[operating["page"] - 1], 1)
            evidences.append(operating_evidence)
            if operating.get("voltage_min") is not None and matrix_voltage is None:
                collector.add(
                    "chips",
                    chip_row,
                    operating_evidence.evidence_id,
                    "A",
                    {"voltage_min": operating_evidence.evidence_id, "voltage_max": operating_evidence.evidence_id},
                )
                collector.add("electrical", {"chip_id": model, "param_name": configured_output_value("electrical_param", "voltage", "VDD"), "min_value": operating["voltage_min"], "max_value": operating["voltage_max"], "unit": "V", "conditions": None}, operating_evidence.evidence_id, "A")
                electrical_voltage_added = True
            if operating.get("temperature_min") is not None:
                collector.add("electrical", {"chip_id": model, "param_name": configured_output_value("electrical_param", "temperature", "T_operating"), "min_value": operating["temperature_min"], "max_value": operating["temperature_max"], "unit": "°C", "conditions": None}, operating_evidence.evidence_id, "A")

        if not electrical_voltage_added and chip_row.get("voltage_min") is not None:
            voltage_ids = ([summary_evidence.evidence_id] if summary_evidence and "voltage_min" in summary_overrides else aggregate_evidence_ids("fields", "voltage_min")) or [feature_evidence.evidence_id]
            collector.add(
                "electrical",
                {
                    "chip_id": model,
                    "param_name": configured_output_value("electrical_param", "voltage", "VDD"),
                    "min_value": chip_row["voltage_min"],
                    "max_value": chip_row["voltage_max"],
                    "unit": "V",
                    "conditions": None,
                },
                voltage_ids[0],
                "A",
                {field: voltage_ids for field in ("chip_id", "param_name", "min_value", "max_value", "unit")},
            )

        missing_chip_fields = missing_review_fields("chips", chip_row)
        sections["chips"] = "partial" if missing_chip_fields else "success"
        if missing_chip_fields:
            review_items.append({
                "section": "chips",
                "status": "partial",
                "chip_id": model,
                "source_file": str(path),
                "pages": str(feature["page"]),
                "error_type": None,
                "reason": f"关键字段未从原文确认: {', '.join(missing_chip_fields)}",
                "missing_fields": missing_chip_fields,
            })
        sections["memory"] = "success" if values.get("flash_kb") is not None or values.get("ram_kb") is not None else "missing"
        sections["peripherals"] = "success" if values.get("peripherals") else "missing"
        if operating_result.status == "failed":
            sections["electrical"] = "partial" if chip_row.get("voltage_min") is not None else "failed"
        elif electrical_voltage_added or chip_row.get("voltage_min") is not None:
            sections["electrical"] = "success"
        else:
            sections["electrical"] = "missing"
            review_items.append({
                "section": "electrical",
                "status": "missing",
                "source_file": str(path),
                "pages": None,
                "error_type": None,
                "reason": "未定位到可确认的工作电压或温度参数",
            })
        for section_name, reason in (
            ("memory", "未提取到存储参数"),
            ("peripherals", "未提取到配置范围内的外设参数"),
        ):
            if sections[section_name] == "missing":
                review_items.append({
                    "section": section_name,
                    "status": "missing",
                    "source_file": str(path),
                    "pages": str(feature["page"]),
                    "error_type": None,
                    "reason": reason,
                })

        ir_table_view = IRDocumentTableView(document_ir)
        def extract_af_section() -> tuple[list[Any], dict[tuple[str, str], Any], dict[tuple[str, int], str]]:
            af_recipe = PIN_RECIPE["topologies"]["af_matrix"]
            af_tables = indexed_tables(
                document_ir,
                table_index,
                TableCandidateQuery(
                    title_any=tuple(
                        af_recipe["title_any"]
                    ),
                    required_features=frozenset({"af_matrix"}),
                ),
            )
            af_observations = observe_pin_af_matrices(
                af_tables,
                locator=af_recipe,
                pin_pattern=profile.pin_re,
                split_functions=lambda value, pin_name: split_functions(
                    value, profile, pin_name, wrapped_cell=True
                ),
                identity_terms=(profile.series_for_model(model),),
            )
            af_lookup = unique_pin_af_lookup(af_observations)
            af_evidence_ids: dict[tuple[str, int], str] = {}
            for sequence, (table_page, items) in enumerate(
                groupby(
                    sorted(af_observations, key=lambda item: (item.source_table_id, item.page)),
                    key=lambda item: (item.source_table_id, item.page),
                ),
                start=1,
            ):
                grouped = list(items)
                table_id, page = table_page
                table = next(item for item in document_ir.tables if item.table_id == table_id)
                fragment_index = table.pages.index(page)
                bbox = table.bboxes[fragment_index]
                af_evidence = make_evidence(doc, page, bbox, "pin_af", "A", texts[page - 1], sequence)
                evidences.append(af_evidence)
                af_evidence_ids[(table_id, page)] = af_evidence.evidence_id
            return af_observations, af_lookup, af_evidence_ids

        af_result = run_optional_section(
            collector,
            evidences,
            name="pin_af",
            source_file=str(path),
            operation=extract_af_section,
        )
        if af_result.status == "success":
            af_observations, af_lookup, af_evidence_ids = af_result.value
            if af_observations:
                sections["pin_af"] = "success"
            else:
                # Legacy STM32 families often publish complete alternate-function
                # names in the pin-definition table and have no numeric AF matrix.
                # Absence is therefore not a failed extraction by itself.
                sections["pin_af"] = "not_applicable"
        else:
            af_observations, af_lookup, af_evidence_ids = [], {}, {}
            sections["pin_af"] = "failed"
            if af_result.review_item:
                review_items.append(af_result.review_item)

        def extract_pin_section() -> tuple[int, list[dict[str, Any]]]:
            row_count = 0
            all_definitions: list[PinDefinitionIR] = []
            definition_pages: set[int] = set()
            for index, table in enumerate(
                pin_tables(
                    ir_table_view,
                    texts,
                    model,
                    profile,
                    values.get("package_types", []),
                    ordering_config,
                    table_index,
                ),
                start=1,
            ):
                evidence = make_evidence(doc, table["page"], table["bbox"], "pin", "B", texts[table["page"] - 1], index)
                evidences.append(evidence)
                definitions = table["rows"]
                definition_pages.add(table["page"])
                all_definitions.extend(definitions)
                for resolved in resolve_pin_mux(definitions, af_lookup):
                    row = resolved.output_row()
                    af_observation = af_lookup.get((row["pin_name"], row["function_name"]))
                    field_evidence = {
                        field: evidence.evidence_id
                        for field, value in row.items()
                        if field != "af_code" and value not in (None, "", [])
                    }
                    if af_observation is not None:
                        field_evidence["af_code"] = af_evidence_ids[(af_observation.source_table_id, af_observation.page)]
                    collector.add("pin_mux", row, evidence.evidence_id, "B", field_evidence)
                    row_count += 1
            identity_issues = pin_identity_issues(all_definitions)
            if identity_issues:
                review_items.append({
                    "section": "pin_definition",
                    "status": "partial",
                    "source_file": str(path),
                    "pages": sorted(definition_pages),
                    "error_type": "ambiguous_pin_identity",
                    "reason": "管脚名称和脚号不是一一对应，已保留原始提取值等待审核",
                    "details": identity_issues[:20],
                })
            return row_count, identity_issues

        pin_result = run_optional_section(
            collector,
            evidences,
            name="pin_definition",
            source_file=str(path),
            operation=extract_pin_section,
        )
        if pin_result.status == "success" and pin_result.value[0]:
            sections["pin_mux"] = "partial" if sections["pin_af"] == "failed" or pin_result.value[1] else "success"
        elif pin_result.status == "success":
            sections["pin_mux"] = "missing"
            review_items.append({
                "section": "pin_definition",
                "status": "missing",
                "source_file": str(path),
                "pages": None,
                "error_type": None,
                "reason": "未提取到管脚定义记录",
            })
        else:
            sections["pin_mux"] = "failed"
            if pin_result.review_item:
                review_items.append(pin_result.review_item)

    document_status = "partial" if any(value in {"partial", "failed", "missing"} for value in sections.values()) else "complete"
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
        "status": document_status,
        "sections": sections,
        "review_items": review_items,
    }, evidences


def process_document(doc: dict[str, Any], collector: Collector, aliases: dict[str, str], profile: DocumentProfile) -> tuple[dict[str, Any], list[Evidence]]:
    """Expand every concrete model represented by the selected feature matrix."""
    path: Path = doc["path"]
    texts = doc.get("page_texts") or extract_page_texts(path)
    ordering_config = source_ordering_config(profile, texts)
    document_ir = build_document_ir(path, texts, progress=doc.get("table_progress"))
    table_index = build_table_index(document_ir)
    filename_model = target_model(path, profile)
    seed_model = aliases.get(filename_model, filename_model)
    product_option = re.search(r"(-[A-Z])$", seed_model)
    structural_seed = seed_model[: -len(product_option.group(1))] if product_option else seed_model
    feature_tables = feature_candidate_tables(document_ir, table_index)
    if not feature_tables:
        return process_summary_document(doc, collector, profile, document_ir)
    try:
        feature_table, _ = select_observations(
            feature_tables, structural_seed, FEATURE_RECIPE, ordering_config
        )
    except ValueError:
        summary, _ = summary_values(texts, profile)
        if all(summary.get(field) not in (None, [], "") for field in ("flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max")):
            return process_summary_document(doc, collector, profile, document_ir)
        raise
    first_grid = next(iter(table_grids(feature_table)), None)
    if first_grid is None:
        raise ValueError("STM32 feature matrix has no readable grid")
    models = [
        aliases.get(model, model)
        for model in discover_model_variants(first_grid[2], structural_seed, FEATURE_RECIPE, ordering_config)
    ]
    if product_option:
        models = [model if model.endswith(product_option.group(1)) else model + product_option.group(1) for model in models]
    models.extend(
        (
            aliases.get(model, model) + product_option.group(1)
            if product_option and not aliases.get(model, model).endswith(product_option.group(1))
            else aliases.get(model, model)
        )
        for model in discover_device_summary_models(
            texts,
            ordering_config,
            models[0] if models else seed_model,
        )
    )
    models = list(dict.fromkeys(models))

    successful: list[dict[str, Any]] = []
    all_evidences: list[Evidence] = []
    failed_models: list[dict[str, Any]] = []
    for model_index, model in enumerate(models, start=1):
        checkpoint = collector.checkpoint()
        model_doc = {
            **doc,
            "page_texts": texts,
            "_document_ir": document_ir,
            "_table_index": table_index,
            "_target_model": model,
            "_ordering_config": ordering_config,
            "_evidence_namespace": doc["doc_id"] if len(models) == 1 else f"{doc['doc_id']}-{model_index:02d}",
        }
        try:
            result, evidences = _process_single_model(model_doc, collector, aliases, profile)
        except Exception as error:
            collector.restore(checkpoint)
            failed_models.append({
                "section": "model_expansion",
                "status": "failed",
                "chip_id": model,
                "source_file": str(path),
                "pages": str(feature_table.pages[0]),
                "error_type": type(error).__name__,
                "reason": str(error),
            })
            continue
        collector.commit(checkpoint)
        successful.append(result)
        all_evidences.extend(evidences)

    if not successful:
        details = "; ".join(f"{item['chip_id']}: {item['reason']}" for item in failed_models)
        raise ValueError(f"all STM32 matrix models failed: {details}")

    section_names = set().union(*(result["sections"] for result in successful))
    sections: dict[str, str] = {}
    for section in section_names:
        statuses = [result["sections"].get(section, "missing") for result in successful]
        sections[section] = statuses[0] if len(set(statuses)) == 1 else "partial"
    review_items = [item for result in successful for item in result.get("review_items", [])]
    review_items.extend(failed_models)
    status = "partial" if failed_models or any(result["status"] != "complete" for result in successful) else "complete"
    return {
        "doc_id": doc["doc_id"],
        "sha256": doc["sha256"],
        "version": doc.get("version"),
        "source_file": str(path),
        "duplicate_paths": doc["duplicates"],
        "page_count": len(texts),
        "basic_page": feature_table.pages[0],
        "model_count": len(successful),
        "models": [result["models"][0] for result in successful],
        "evidence_count": len(all_evidences),
        "status": status,
        "sections": sections,
        "review_items": review_items,
    }, all_evidences
