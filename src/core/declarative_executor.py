from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Any, Callable

from core.field_dictionary import load_field_dictionary, match_common_field
from core.ir import DocumentIR, TableIR
from core.table_index import DocumentTableIndex, build_table_index
from core.text import clean, compact, parse_range, scalar_or_none
from core.units import convert_values
from topologies.matrix_observer import table_grids, table_matches, text_direction_variants
from topologies.operating_conditions import (
    extract_operating_conditions,
    is_absolute_maximum_context,
    parse_compound_operating_conditions,
)
from topologies.package_matrix import parse_package_names
from topologies.model_column_matrix import wildcard_match
from topologies.ordering_code_legend import parse_ordering_code_legend


FORBIDDEN_PLAN_KEYS = {
    "script",
    "python",
    "eval",
    "expression",
    "expected",
    "expected_value",
    "page",
    "pages",
    "bbox",
    "coordinates",
    "row_index",
    "column_index",
    "filename",
    "model_whitelist",
}
SECTION_KEYS = {"id", "component", "depends_on", "required", "preset", "config"}
PLAN_KEYS = {"version", "sections"}


@dataclass(frozen=True)
class DeclarativeContext:
    document: DocumentIR
    table_index: DocumentTableIndex
    page_texts: list[str]
    profile: Any


Component = Callable[[DeclarativeContext, dict[str, Any], dict[str, Any]], Any]


def _forbidden_paths(value: Any, path: str = "declarative") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_PLAN_KEYS:
                result.append(child_path)
            result.extend(_forbidden_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_forbidden_paths(child, f"{path}[{index}]"))
    return result


def _execution_order(sections: list[dict[str, Any]]) -> list[str]:
    by_id = {section["id"]: section for section in sections}
    unknown = {
        dependency
        for section in sections
        for dependency in section.get("depends_on", [])
        if dependency not in by_id
    }
    if unknown:
        raise ValueError(f"declarative plan has unknown dependencies: {sorted(unknown)}")
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(section_id: str) -> None:
        if section_id in visiting:
            raise ValueError(f"declarative plan dependency cycle includes {section_id}")
        if section_id in visited:
            return
        visiting.add(section_id)
        for dependency in by_id[section_id].get("depends_on", []):
            visit(dependency)
        visiting.remove(section_id)
        visited.add(section_id)
        order.append(section_id)

    for section in sections:
        visit(section["id"])
    return order


def validate_declarative_plan(plan: Any, component_names: set[str] | None = None) -> list[str]:
    if not isinstance(plan, dict):
        raise ValueError("profile declarative plan must be an object")
    unknown_plan_keys = set(plan) - PLAN_KEYS
    if unknown_plan_keys:
        raise ValueError(f"declarative plan has unknown keys: {sorted(unknown_plan_keys)}")
    if plan.get("version") != 1:
        raise ValueError("only declarative plan version 1 is supported")
    sections = plan.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("declarative plan must contain at least one section")
    forbidden = _forbidden_paths(plan)
    if forbidden:
        raise ValueError(f"declarative plan contains implementation details or answers: {forbidden}")
    identifiers: list[str] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ValueError(f"declarative section {index} must be an object")
        unknown_section_keys = set(section) - SECTION_KEYS
        if unknown_section_keys:
            raise ValueError(
                f"declarative section {index} has unknown keys: {sorted(unknown_section_keys)}"
            )
        missing = {"id", "component"} - set(section)
        if missing:
            raise ValueError(f"declarative section {index} missing keys: {sorted(missing)}")
        section_id = str(section["id"])
        if not re.fullmatch(r"[a-z][a-z0-9_]*", section_id):
            raise ValueError(f"invalid declarative section id: {section_id}")
        identifiers.append(section_id)
        component = str(section["component"])
        if component_names is not None and component not in component_names:
            raise ValueError(f"unknown declarative component: {component}")
        dependencies = section.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise ValueError(f"declarative section {section_id} depends_on must be a string array")
        if "preset" in section and "config" in section:
            raise ValueError(f"declarative section {section_id} cannot declare both preset and config")
        if "preset" not in section and "config" not in section:
            raise ValueError(f"declarative section {section_id} must declare preset or config")
        if "config" in section and not isinstance(section["config"], dict):
            raise ValueError(f"declarative section {section_id} config must be an object")
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise ValueError(f"declarative plan has duplicate section ids: {duplicates}")
    _execution_order(sections)
    return identifiers


def _cell(cells: dict[tuple[int, int], Any], row: int, column: int) -> dict[str, Any]:
    value = cells.get((row, column))
    if value is not None and value.inherited_from:
        value = next(
            (candidate for candidate in cells.values() if candidate.cell_id == value.inherited_from),
            value,
        )
    return {
        "source_cell_id": value.cell_id if value else None,
        "page": value.page if value else None,
        "bbox": value.bbox if value else None,
        "raw_value": value.raw_text if value else "",
    }


def _models_in_columns(
    table: TableIR,
    profile: Any,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for _, page, rows, cells in table_grids(table, inherit_merged_values=True):
        candidates: list[tuple[int, list[tuple[int, str]]]] = []
        for row_index, row in enumerate(rows):
            found = [
                (column, model)
                for column, value in enumerate(row)
                for model in profile.model_tokens(value, exact_only=True)
            ]
            if found:
                candidates.append((row_index, found))
        if not candidates:
            continue
        header_row, model_columns = max(candidates, key=lambda item: len(item[1]))
        first_model_column = min(column for column, _ in model_columns)
        # A model token in the first column identifies a row-oriented matrix,
        # not a model value column. Reject that shape instead of accepting the
        # first data row as a one-model header.
        if first_model_column == 0:
            continue
        for column, model in model_columns:
            values = []
            for row_index, row in enumerate(rows):
                if row_index == header_row or column >= len(row) or not compact(row[column]):
                    continue
                labels = [clean(value) for value in row[:first_model_column] if compact(value)]
                if not labels:
                    continue
                values.append({"label": " / ".join(labels), **_cell(cells, row_index, column)})
            result.append(
                {
                    "model_id": model,
                    "page": page,
                    "source_table_id": table.table_id,
                    "source_table_title": table.title,
                    "model_source": _cell(cells, header_row, column),
                    "values": values,
                }
            )
    return result


def _models_in_rows(
    table: TableIR,
    profile: Any,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    aliases = [compact(item).casefold() for item in config.get("model_header_any", [])]
    if not aliases:
        raise ValueError("models_in_rows requires model_header_any")
    result: list[dict[str, Any]] = []
    for _, page, rows, cells in table_grids(table, inherit_merged_values=True):
        header_candidates = [
            (row_index, column)
            for row_index, row in enumerate(rows)
            for column, value in enumerate(row)
            if any(alias in compact(value).casefold() for alias in aliases)
        ]
        if not header_candidates:
            continue
        header_row, model_column = header_candidates[0]
        headers = {
            column: clean(value)
            for column, value in enumerate(rows[header_row])
            if compact(value)
        }
        for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
            if model_column >= len(row):
                continue
            models = profile.model_tokens(row[model_column], exact_only=True)
            for model in models:
                values = [
                    {"label": label, **_cell(cells, row_index, column)}
                    for column, label in headers.items()
                    if column != model_column and column < len(row) and compact(row[column])
                ]
                result.append(
                    {
                        "model_id": model,
                        "page": page,
                        "source_table_id": table.table_id,
                        "source_table_title": table.title,
                        "model_source": _cell(cells, row_index, model_column),
                        "values": values,
                    }
                )
    return result


def _models_in_grouped_columns(
    table: TableIR,
    profile: Any,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped = config.get("grouped_header")
    if not isinstance(grouped, dict):
        raise ValueError("models_in_grouped_columns requires grouped_header")
    unknown = set(grouped) - {"prefix_pattern", "suffix_pattern", "separator_pattern"}
    if unknown:
        raise ValueError(f"grouped_header has unknown keys: {sorted(unknown)}")
    missing = {"prefix_pattern", "suffix_pattern"} - set(grouped)
    if missing:
        raise ValueError(f"grouped_header missing keys: {sorted(missing)}")
    try:
        prefix_re = re.compile(str(grouped["prefix_pattern"]), re.I)
        suffix_re = re.compile(str(grouped["suffix_pattern"]), re.I)
        separator_re = re.compile(str(grouped.get("separator_pattern", r"[_\s]+")))
    except re.error as exc:
        raise ValueError(f"invalid grouped model header regex: {exc}") from exc
    result: list[dict[str, Any]] = []
    incompatible_fragments: list[tuple[int, int, int]] = []
    for _, page, rows, cells in table_grids(table, inherit_merged_values=True):
        for header_row, row in enumerate(rows):
            unique_headers = list(dict.fromkeys(value for value in row if compact(value)))
            match = next(
                (
                    (value, candidate)
                    for value in unique_headers
                    if (candidate := prefix_re.search(value)) is not None
                ),
                None,
            )
            if match is None:
                continue
            header_text, prefix_match = match
            prefix = prefix_match.group(1) if prefix_match.lastindex else prefix_match.group(0)
            suffixes = [
                item.group(1) if item.lastindex else item.group(0)
                for item in suffix_re.finditer(header_text[prefix_match.end() :])
            ]
            first_model_column = next(
                (column for column, value in enumerate(row) if value == header_text),
                None,
            )
            if first_model_column is None or not suffixes:
                continue
            available_columns = len(row) - first_model_column
            if len(suffixes) != available_columns:
                # A continuation fragment can group several model variants
                # into one physical package column. It is not an independent
                # source for model discovery, so retain the mismatch and use a
                # different fragment only when that fragment maps one suffix
                # to one physical value column. Relationship expansion is a
                # separate topology and must not guess here.
                incompatible_fragments.append((page, len(suffixes), available_columns))
                continue
            for offset, suffix in enumerate(suffixes):
                column = first_model_column + offset
                model = separator_re.sub("", f"{prefix}{suffix}").upper()
                if not profile.model_tokens(model, exact_only=True):
                    raise ValueError(f"grouped model header produced a non-concrete model: {model}")
                values = []
                for row_index, value_row in enumerate(rows):
                    if row_index == header_row or column >= len(value_row) or not compact(value_row[column]):
                        continue
                    labels = [clean(value) for value in value_row[:first_model_column] if compact(value)]
                    if labels:
                        values.append({"label": " / ".join(labels), **_cell(cells, row_index, column)})
                result.append(
                    {
                        "model_id": model,
                        "page": page,
                        "source_table_id": table.table_id,
                        "source_table_title": table.title,
                        "model_source": _cell(cells, header_row, column),
                        "values": values,
                    }
                )
            break
    if not result and incompatible_fragments:
        raise ValueError(
            "grouped model header has no one-to-one suffix/value fragment: "
            f"{incompatible_fragments}"
        )
    return result


def ordering_code_legend_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    _: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"required_relations"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"ordering_code_legend config has unknown keys: {sorted(unknown)}")
    required = config.get("required_relations", [])
    supported = {"pin_count_codes", "flash_code_kb", "package_type_codes"}
    if not isinstance(required, list) or any(item not in supported for item in required):
        raise ValueError("ordering_code_legend required_relations contains unknown relations")
    legend = parse_ordering_code_legend(context.page_texts)
    result = {
        "pin_count_codes": legend.pin_count_codes,
        "flash_code_kb": legend.flash_code_kb,
        "package_type_codes": legend.package_type_codes,
        "evidence_pages": list(legend.evidence_pages),
    }
    missing = [name for name in required if not result[name]]
    if missing:
        raise ValueError(f"ordering code legend missing required relations: {missing}")
    return result


def _position_relation_accepts(
    model: str,
    rule: dict[str, Any],
    legend: dict[str, Any],
) -> bool:
    if set(rule) != {"relation", "offset"}:
        raise ValueError("model catalog code position requires relation and offset")
    relation = str(rule["relation"])
    if relation not in {"pin_count_codes", "flash_code_kb", "package_type_codes"}:
        raise ValueError(f"model catalog references unknown ordering relation: {relation}")
    offset = rule["offset"]
    if not isinstance(offset, int) or offset >= 0 or abs(offset) > len(model):
        raise ValueError("model catalog code offset must be a valid negative integer")
    return model[offset].upper() in legend.get(relation, {})


def model_catalog_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"title_any", "ordering_section", "code_positions"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"model_catalog config has unknown keys: {sorted(unknown)}")
    title_any = config.get("title_any")
    ordering_section = config.get("ordering_section")
    code_positions = config.get("code_positions", [])
    if not isinstance(title_any, list) or not title_any:
        raise ValueError("model_catalog requires title_any")
    if not isinstance(ordering_section, str) or ordering_section not in outputs:
        raise ValueError("model_catalog requires an executed ordering_section")
    if not isinstance(code_positions, list) or not code_positions:
        raise ValueError("model_catalog requires code_positions")
    legend = outputs[ordering_section]
    candidates: list[dict[str, Any]] = []
    for table in context.document.tables:
        if not table_matches(table, {"title_any": title_any}):
            continue
        for _, page, rows, cells in table_grids(table):
            sources = [
                (cell.raw_text, _cell(cells, cell.row, cell.column))
                for cell in cells.values()
            ]
            if 1 <= page <= len(context.page_texts):
                sources.append(
                    (
                        context.page_texts[page - 1],
                        {
                            "source_cell_id": None,
                            "page": page,
                            "bbox": table.bboxes[0] if table.bboxes else None,
                            "raw_value": context.page_texts[page - 1],
                        },
                    )
                )
            for row_index, row in enumerate(rows):
                row_text = "".join(clean(value) for value in row)
                if not compact(row_text):
                    continue
                first_column = next((index for index, value in enumerate(row) if compact(value)), 0)
                row_source = _cell(cells, row_index, first_column)
                row_source = {**row_source, "page": page, "raw_value": row_text}
                sources.append((row_text, row_source))
            for raw_text, source in sources:
                for model in context.profile.model_tokens(raw_text, exact_only=True):
                    upper = model.upper()
                    if not all(
                        _position_relation_accepts(upper, rule, legend)
                        for rule in code_positions
                    ):
                        continue
                    if upper not in {item["model_id"] for item in candidates}:
                        candidates.append({"model_id": upper, "model_source": source})
    if not candidates:
        raise ValueError("model_catalog found no concrete models satisfying ordering relations")
    return {"models": sorted(candidates, key=lambda item: item["model_id"])}


def _raw_model_patterns(text: str, context: DeclarativeContext) -> list[str]:
    result: list[str] = []
    for variant in text_direction_variants(text):
        prepared = context.profile.boundary_re.sub(" ", variant)
        result.extend(compact(match.group(0)).upper() for match in context.profile.model_re.finditer(prepared))
    return list(dict.fromkeys(result))


def _models_from_catalog(
    table: TableIR,
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> list[dict[str, Any]]:
    catalog_section = config.get("catalog_section")
    ordering_section = config.get("ordering_section")
    density = config.get("density_relation")
    if not isinstance(catalog_section, str) or catalog_section not in outputs:
        raise ValueError("models_from_catalog requires an executed catalog_section")
    if not isinstance(ordering_section, str) or ordering_section not in outputs:
        raise ValueError("models_from_catalog requires an executed ordering_section")
    if not isinstance(density, dict) or set(density) != {"relation", "offset", "field"}:
        raise ValueError("models_from_catalog requires density_relation relation, offset and field")
    if density["relation"] not in {"flash_code_kb"} or density["field"] != "flash_kb":
        raise ValueError("models_from_catalog density_relation must use registered flash semantics")
    offset = density["offset"]
    if not isinstance(offset, int) or offset >= 0:
        raise ValueError("models_from_catalog density offset must be negative")
    legend = outputs[ordering_section]
    density_map = legend[density["relation"]]
    catalog = outputs[catalog_section]["models"]
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file, context.profile.field_aliases
    )
    unit_config = dictionary["units"]["memory_kb"]
    accumulated: dict[str, dict[str, Any]] = {}
    for _, page, rows, cells in table_grids(table, inherit_merged_values=True):
        if not rows:
            continue
        headers: list[tuple[int, int, str]] = []
        for row_index, row in enumerate(rows[:6]):
            for column, value in enumerate(row):
                source_cell = cells.get((row_index, column))
                if source_cell is not None and source_cell.inherited_from:
                    continue
                for pattern in _raw_model_patterns(value, context):
                    if "X" in pattern:
                        headers.append((row_index, column, pattern))
        if not headers:
            continue
        first_model_column = min(column for _, column, _ in headers)
        starts = sorted({column for _, column, _ in headers})
        for catalog_item in catalog:
            model = catalog_item["model_id"]
            matches = [(row, column, pattern) for row, column, pattern in headers if wildcard_match(pattern, model)]
            if not matches:
                continue
            header_row, start, _ = matches[0]
            end = next((column for column in starts if column > start), len(rows[header_row]))
            expected = density_map.get(model[offset].upper()) if abs(offset) <= len(model) else None
            if expected is None:
                raise ValueError(f"no ordering density relation for model {model}")
            density_columns: list[int] = []
            has_density_row = False
            for row_index, row in enumerate(rows):
                labels = [clean(value) for value in row[:first_model_column] if compact(value)]
                if match_common_field(" / ".join(labels), dictionary) != density["field"]:
                    continue
                has_density_row = True
                for column in range(start, min(end, len(row))):
                    values = convert_values(row[column], unit_config, " ".join(labels))
                    if any(float(value) == float(expected) for value in values):
                        density_columns.append(column)
            density_columns = list(dict.fromkeys(density_columns))
            if not has_density_row and end - start == 1:
                # A continued fragment can collapse density subcolumns when
                # every value on that page applies to the whole package group.
                density_columns = [start]
            if len(density_columns) != 1:
                raise ValueError(
                    f"model {model} density relation selected {len(density_columns)} columns"
                )
            column = density_columns[0]
            item = accumulated.setdefault(
                model,
                {
                    "model_id": model,
                    "page": page,
                    "source_table_id": table.table_id,
                    "source_table_title": table.title,
                    "model_source": catalog_item["model_source"],
                    "values": [],
                },
            )
            for row_index, row in enumerate(rows):
                if row_index <= header_row or column >= len(row) or not compact(row[column]):
                    continue
                labels = [clean(value) for value in row[:first_model_column] if compact(value)]
                if labels:
                    item["values"].append({"label": " / ".join(labels), **_cell(cells, row_index, column)})
    return list(accumulated.values())


def _reduce_numeric(values: list[int | float], reducer: str) -> int | float | None:
    if not values:
        return None
    if reducer == "sum":
        return scalar_or_none(sum(values))
    if reducer == "max":
        return scalar_or_none(max(values))
    if reducer == "first":
        return values[0]
    raise ValueError(f"unsupported declarative numeric reducer: {reducer}")


FIELD_PARSERS = {
    "flash_kb": {"quantity"},
    "ram_kb": {"quantity"},
    "max_freq_mhz": {"quantity"},
    "operating_voltage": {"range"},
    "operating_conditions": {"compound_ranges"},
    "package_types": {"package_names"},
}
DEFAULT_FIELD_PARSER = {name: next(iter(values)) for name, values in FIELD_PARSERS.items()}
QUALIFIERS = {"nonempty", "not_absolute_maximum"}


def _qualifier_accepts(
    qualifier: str,
    item: dict[str, Any],
    model: dict[str, Any],
) -> bool:
    if qualifier == "nonempty":
        return bool(compact(item.get("raw_value")))
    if qualifier == "not_absolute_maximum":
        context = f"{model.get('source_table_title', '')} {item.get('label', '')}"
        return not is_absolute_maximum_context(context)
    raise ValueError(f"unknown declarative qualifier: {qualifier}")


def _map_model_fields(
    models: list[dict[str, Any]],
    field_plan: Any,
    context: DeclarativeContext,
) -> None:
    if not isinstance(field_plan, dict) or not field_plan:
        raise ValueError("model_matrix fields must be a non-empty object")
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    allowed_fields = {
        "flash_kb",
        "ram_kb",
        "max_freq_mhz",
        "operating_voltage",
        "operating_conditions",
        "package_types",
    }
    unknown_fields = set(field_plan) - allowed_fields
    if unknown_fields:
        raise ValueError(f"model_matrix fields contain unsupported targets: {sorted(unknown_fields)}")
    for field_name, settings in field_plan.items():
        if not isinstance(settings, dict):
            raise ValueError(f"model_matrix field {field_name} must be an object")
        unknown_settings = set(settings) - {"parser", "reducer", "qualifiers", "required"}
        if unknown_settings:
            raise ValueError(
                f"model_matrix field {field_name} has unknown settings: {sorted(unknown_settings)}"
            )
        parser = settings.get("parser", DEFAULT_FIELD_PARSER[field_name])
        if parser not in FIELD_PARSERS[field_name]:
            raise ValueError(f"unsupported parser {parser!r} for model_matrix field {field_name}")
        qualifiers = settings.get("qualifiers", [])
        if not isinstance(qualifiers, list) or any(item not in QUALIFIERS for item in qualifiers):
            raise ValueError(f"model_matrix field {field_name} has unknown qualifiers")
        if "required" in settings and not isinstance(settings["required"], bool):
            raise ValueError(f"model_matrix field {field_name} required must be boolean")
    for model in models:
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in field_plan}
        for value in model["values"]:
            field_name = match_common_field(value["label"], dictionary)
            if field_name in grouped:
                grouped[field_name].append(value)
        fields: dict[str, Any] = {}
        sources: dict[str, list[dict[str, Any]]] = {}
        for field_name, items in grouped.items():
            settings = field_plan[field_name]
            parser = settings.get("parser", DEFAULT_FIELD_PARSER[field_name])
            qualifiers = settings.get("qualifiers", [])
            items = [
                item
                for item in items
                if all(_qualifier_accepts(qualifier, item, model) for qualifier in qualifiers)
            ]
            if not items:
                if settings.get("required"):
                    raise ValueError(
                        f"required field {field_name} missing for model {model['model_id']}"
                    )
                continue
            sources[field_name] = items
            if parser == "quantity":
                unit_name = dictionary["fields"][field_name].get("unit")
                unit_config = dictionary.get("units", {}).get(unit_name)
                if not unit_config:
                    raise ValueError(f"field {field_name} has no registered unit conversion")
                values = [
                    number
                    for item in items
                    for number in convert_values(item["raw_value"], unit_config, item["label"])
                ]
                value = _reduce_numeric(values, settings.get("reducer", "max"))
                if value is not None:
                    fields[field_name] = value
            elif parser == "range":
                ranges = [
                    value
                    for item in items
                    if (value := parse_range(item["raw_value"])) is not None
                ]
                if ranges:
                    fields["voltage_min"] = min(item[0] for item in ranges)
                    fields["voltage_max"] = max(item[1] for item in ranges)
            elif parser == "compound_ranges":
                compound = [
                    parse_compound_operating_conditions(item["raw_value"])
                    for item in items
                ]
                for target in (
                    "voltage_min",
                    "voltage_max",
                    "temperature_min",
                    "temperature_max",
                ):
                    values = [item[target] for item in compound if target in item]
                    if values:
                        fields[target] = min(values) if target.endswith("_min") else max(values)
            elif parser == "package_names":
                packages = list(
                    dict.fromkeys(
                        package
                        for item in items
                        for package in parse_package_names(
                            item["raw_value"], context.profile.package_re.pattern
                        )
                    )
                )
                if packages:
                    fields[field_name] = packages
            if settings.get("required"):
                targets = {
                    "range": ("voltage_min", "voltage_max"),
                    "compound_ranges": ("voltage_min", "voltage_max"),
                }.get(parser, (field_name,))
                missing_targets = [target for target in targets if fields.get(target) is None]
                if missing_targets:
                    raise ValueError(
                        f"required field {field_name} incomplete for model {model['model_id']}: "
                        f"{missing_targets}"
                    )
        model["fields"] = fields
        model["field_sources"] = sources


def model_matrix_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    if "variants" in config:
        if set(config) != {"variants"}:
            raise ValueError("model_matrix variants cannot be mixed with single-layout keys")
        variants = config["variants"]
        if not isinstance(variants, list) or not variants:
            raise ValueError("model_matrix variants must be a non-empty array")
        matches: list[dict[str, Any]] = []
        failures: list[str] = []
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                raise ValueError(f"model_matrix variant {index} must be an object")
            try:
                matches.append(model_matrix_component(context, variant, outputs))
            except ValueError as exc:
                failures.append(f"variant {index}: {exc}")
        if not matches:
            raise ValueError(f"no model_matrix variant matched: {failures}")
        signatures = {
            (item["table_id"], tuple(model["model_id"] for model in item["models"]))
            for item in matches
        }
        if len(signatures) != 1 and len(matches) > 1:
            raise ValueError(f"model_matrix variants are ambiguous: {sorted(signatures)}")
        return matches[0]
    allowed = {
        "layout",
        "title_any",
        "locator",
        "model_header_any",
        "grouped_header",
        "catalog_section",
        "ordering_section",
        "density_relation",
        "fields",
        "variants",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"model_matrix config has unknown keys: {sorted(unknown)}")
    layout = config.get("layout")
    if layout not in {
        "models_in_columns",
        "models_in_rows",
        "models_in_grouped_columns",
        "models_from_catalog",
    }:
        raise ValueError("model_matrix layout must explicitly describe its model header structure")
    locator = {"title_any": config.get("title_any", []), **config.get("locator", {})}
    candidates = [table for table in context.document.tables if table_matches(table, locator)]
    parsed = []
    for table in candidates:
        if layout == "models_in_columns":
            models = _models_in_columns(table, context.profile)
        elif layout == "models_in_rows":
            models = _models_in_rows(table, context.profile, config)
        elif layout == "models_in_grouped_columns":
            models = _models_in_grouped_columns(table, context.profile, config)
        else:
            models = _models_from_catalog(table, context, config, outputs)
        if models:
            parsed.append((len(models), table, models))
    if not parsed:
        raise ValueError("no configured model matrix produced concrete models")
    _, table, models = max(parsed, key=lambda item: (item[0], -item[1].pages[0]))
    if "fields" in config:
        _map_model_fields(models, config["fields"], context)
    return {"layout": layout, "table_id": table.table_id, "models": models}


def operating_conditions_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    _: dict[str, Any],
) -> dict[str, Any] | None:
    allowed = {
        "table_any",
        "primary_voltage_symbols",
        "temperature_any",
        "temperature_exclude_any",
        "scope",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"operating_conditions config has unknown keys: {sorted(unknown)}")
    if config.get("scope") != "document":
        raise ValueError("operating_conditions must declare document scope")
    required = {"table_any", "temperature_any", "temperature_exclude_any"}
    missing = required - set(config)
    if missing:
        raise ValueError(f"operating_conditions config missing keys: {sorted(missing)}")
    return extract_operating_conditions(
        context.document,
        context.table_index,
        {**config, "require_min_typ_max": True},
    )


COMPONENTS: dict[str, Component] = {
    "ordering_code_legend": ordering_code_legend_component,
    "model_catalog": model_catalog_component,
    "model_matrix": model_matrix_component,
    "operating_conditions": operating_conditions_component,
}


def _section_config(section: dict[str, Any], profile: Any) -> dict[str, Any]:
    if "config" in section:
        return dict(section["config"])
    preset_name = str(section["preset"])
    preset_path: Path = profile.topology_preset(preset_name)
    payload = json.loads(preset_path.read_text(encoding="utf-8"))
    return payload


def execute_declarative_plan(
    plan: dict[str, Any],
    *,
    document: DocumentIR,
    page_texts: list[str],
    profile: Any,
) -> dict[str, Any]:
    validate_declarative_plan(plan, set(COMPONENTS))
    sections = {section["id"]: section for section in plan["sections"]}
    context = DeclarativeContext(document, build_table_index(document), page_texts, profile)
    outputs: dict[str, Any] = {}
    for section_id in _execution_order(plan["sections"]):
        section = sections[section_id]
        component = COMPONENTS[section["component"]]
        value = component(context, _section_config(section, profile), outputs)
        if value is None and section.get("required", True):
            raise ValueError(f"required declarative section produced no result: {section_id}")
        outputs[section_id] = value
    return outputs
