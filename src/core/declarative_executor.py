from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Any, Callable

from core.field_dictionary import (
    load_field_dictionary,
    match_common_field,
    peripheral_count_field,
)
from core.ir import DocumentIR, TableIR
from core.mapped_cell import MappedCell
from core.pin_ir import pin_identity_issues, resolve_pin_mux
from core.recipe_executor import peripheral_count, reduce_values
from core.table_ir_legacy_view import IRDocumentTableView
from core.table_index import DocumentTableIndex, build_table_index
from core.text import clean, compact, parse_ranges, scalar_or_none
from core.units import convert_values
from topologies.matrix_observer import table_grids, table_matches, text_direction_variants
from topologies.operating_conditions import (
    extract_operating_conditions,
    is_absolute_maximum_context,
    parse_compound_operating_conditions,
)
from topologies.package_matrix import parse_package_names
from topologies.pin_af_matrix import observe_pin_af_matrices, unique_pin_af_lookup
from topologies.pin_definition import extract_multi_package_pin_definitions, extract_pin_definitions, split_functions
from topologies.model_column_matrix import intersect_model_patterns, wildcard_match
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


class DeclarativeNotFound(ValueError):
    """A valid component configuration did not match this document's structure."""


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


def _model_patterns(text: str, profile: Any) -> list[str]:
    result: list[str] = []
    normalized = clean(text).upper()
    compact_normalized = compact(text).upper()
    reversed_text = compact_normalized[::-1]
    variants: list[tuple[str, bool]] = [(normalized, True)]
    if compact_normalized != normalized:
        variants.append((compact_normalized, True))
    if reversed_text not in {normalized, compact_normalized}:
        variants.append((reversed_text, True))
    for variant, allow_suffix_alternatives in variants:
        # Expand shared numeric infix notation before applying the model regex.
        # In forms such as ``QZ767/769Bx`` neither abbreviated left token is a
        # complete model until the common suffix is restored.
        for shared in re.finditer(
            r"([A-Z][A-Z0-9]*[A-Z])(\d{1,4})\s*/\s*(\d{1,4})([A-Z][A-Z0-9]*)",
            variant,
            re.I,
        ):
            prefix, left_code, right_code, common_suffix = shared.groups()
            if len(left_code) != len(right_code):
                continue
            for candidate in (
                f"{prefix}{left_code}{common_suffix}",
                f"{prefix}{right_code}{common_suffix}",
            ):
                if profile.model_re.fullmatch(candidate):
                    result.append(compact(candidate).upper())
        prepared = profile.boundary_re.sub(" ", variant)
        for match in profile.model_re.finditer(prepared):
            base = compact(match.group(0)).upper()
            result.append(base)
            tail = prepared[match.end() :]
            shared_infix = (
                re.match(r"\s*/\s*([A-Z0-9]{1,3})([A-Z0-9]+)", tail, re.I)
                if allow_suffix_alternatives
                else None
            )
            if shared_infix:
                replacement, common_suffix = shared_infix.groups()
                primary = f"{base}{common_suffix.upper()}"
                alternate = (
                    f"{base[:-len(replacement)]}{replacement.upper()}"
                    f"{common_suffix.upper()}"
                )
                for candidate in (primary, alternate):
                    if profile.model_re.fullmatch(candidate):
                        result.append(candidate)
            alternatives = (
                re.match(r"(?:\s*/\s*[A-Z0-9]{1,3})+", tail, re.I)
                if allow_suffix_alternatives
                else None
            )
            if alternatives:
                for suffix in re.findall(r"/\s*([A-Z0-9]{1,3})", alternatives.group(0), re.I):
                    candidate = f"{base[:-len(suffix)]}{suffix.upper()}"
                    if profile.model_re.fullmatch(candidate):
                        result.append(candidate)
    return list(dict.fromkeys(result))


def _catalog_model_matches_pattern(
    pattern: str,
    model: str,
    wildcard_markers: tuple[str, ...],
) -> bool:
    if wildcard_match(pattern, model, wildcard_markers):
        return True
    normalized_pattern = compact(pattern).upper()
    normalized_model = compact(model).upper()
    return len(normalized_pattern) > len(normalized_model) and wildcard_match(
        normalized_pattern[: len(normalized_model)],
        normalized_model,
        wildcard_markers,
    )


def _uses_only_declared_literal_wildcards(
    pattern: str,
    wildcard_markers: tuple[str, ...],
    literal_codes_by_offset: dict[int, set[str]],
) -> bool:
    """True when every wildcard-shaped character is an ordering-code value.

    A character such as ``X`` is not inherently a wildcard. Some ordering-code
    legends assign it a concrete meaning (for example, a package or pin-count
    code). The legend, rather than a vendor/model exception, resolves that
    ambiguity.
    """
    normalized = compact(pattern).upper()
    markers = {item.upper() for item in wildcard_markers}
    positions = [index for index, char in enumerate(normalized) if char in markers]
    if not positions:
        return False
    return all(
        normalized[index] in literal_codes_by_offset.get(index - len(normalized), set())
        for index in positions
    )


def _aligned_row_labels(
    rows: list[list[str]],
    cells: dict[tuple[int, int], Any],
    row_index: int,
    value_column: int,
    first_model_column: int,
) -> list[str]:
    """Align multiline labels and values split across a merged row span."""
    by_id = {cell.cell_id: cell for cell in cells.values()}
    labels: list[str] = []
    seen_sources: set[str] = set()
    for label_column in range(first_model_column):
        cell = cells.get((row_index, label_column))
        if cell is None:
            continue
        source = by_id.get(cell.inherited_from, cell) if cell.inherited_from else cell
        if source.cell_id in seen_sources:
            continue
        seen_sources.add(source.cell_id)
        raw = source.raw_text
        parts = [clean(part) for part in str(raw).splitlines() if compact(part)]
        if source.row_span > 1 and len(parts) > source.row_span:
            counts = []
            for physical_row in range(source.row, source.row + source.row_span):
                raw_value = (
                    rows[physical_row][value_column]
                    if physical_row < len(rows) and value_column < len(rows[physical_row])
                    else ""
                )
                counts.append(len([part for part in str(raw_value).splitlines() if compact(part)]))
            if sum(counts) == len(parts) and row_index >= source.row:
                relative = row_index - source.row
                start = sum(counts[:relative])
                selected = parts[start : start + counts[relative]]
                # When one merged label contains more logical labels than
                # physical rows, its first line is the semantic parent for
                # the subordinate labels placed on later rows.
                parts = parts[:1] + selected if relative > 0 else selected
        label = clean(" ".join(parts))
        if label:
            labels.append(label)
    return labels


def _models_in_columns(
    table: TableIR,
    profile: Any,
    catalog: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    accumulated: dict[str, dict[str, Any]] = {}
    catalog_items = catalog.get("models", []) if isinstance(catalog, dict) else (catalog or [])
    catalog_by_model = {
        str(item["model_id"]).upper(): item
        for item in catalog_items
    }
    literal_codes_by_offset = {
        int(offset): {str(code).upper() for code in codes}
        for offset, codes in (
            catalog.get("literal_codes_by_offset", {}) if isinstance(catalog, dict) else {}
        ).items()
    }
    wildcard_markers = tuple(getattr(profile, "wildcard_markers", ("X",)))
    dictionary = load_field_dictionary(profile.field_dictionary_file, profile.field_aliases)
    upper_bound_markers = [
        compact(item).casefold()
        for item in dictionary.get("semantics", {}).get("upper_bound_any", [])
    ]
    active_model_columns: list[tuple[int, str]] = []
    active_first_model_column: int | None = None
    for _, page, rows, cells in table_grids(table, inherit_merged_values=True):
        candidates: list[tuple[int, list[tuple[int, str]]]] = []
        for row_index, row in enumerate(rows):
            found: list[tuple[int, str]] = []
            for column, value in enumerate(row):
                source_cell = cells.get((row_index, column))
                if source_cell is not None and source_cell.inherited_from:
                    continue
                exact = profile.model_tokens(compact(value), exact_only=True)
                if not catalog_by_model:
                    column_patterns = _model_patterns(value, profile)
                    scope_patterns = _model_patterns(table.title, profile)
                    inferred = list(dict.fromkeys(
                        concrete
                        for scope_pattern in scope_patterns
                        for column_pattern in column_patterns
                        if (
                            concrete := intersect_model_patterns(
                                column_pattern, scope_pattern, wildcard_markers
                            )
                        )
                        if profile.model_tokens(concrete, exact_only=True)
                    ))
                    if inferred and source_cell is not None:
                        covered_columns = [
                            candidate_column
                            for candidate_column in range(len(row))
                            if (
                                (candidate := cells.get((row_index, candidate_column)))
                                is not None
                                and (
                                    candidate.cell_id == source_cell.cell_id
                                    or candidate.inherited_from == source_cell.cell_id
                                )
                            )
                        ]
                        if len(inferred) == len(covered_columns):
                            found.extend(zip(covered_columns, inferred))
                            continue
                        if len(inferred) > 1 or len(covered_columns) > 1:
                            raise DeclarativeNotFound(
                                "model pattern intersection does not map one-to-one to "
                                "physical value columns"
                            )
                        exact.extend(inferred)
                if catalog_by_model:
                    patterns = _model_patterns(value, profile)
                    literal_patterns = [
                        pattern
                        for pattern in patterns
                        if profile.model_re.fullmatch(pattern)
                        and _uses_only_declared_literal_wildcards(
                            pattern, wildcard_markers, literal_codes_by_offset
                        )
                    ]
                    exact = [
                        model
                        for model in [*exact, *literal_patterns]
                        if model.upper() in catalog_by_model
                        or _uses_only_declared_literal_wildcards(
                            model, wildcard_markers, literal_codes_by_offset
                        )
                    ]
                    wildcard_patterns = [
                        pattern
                        for pattern in patterns
                        if not _uses_only_declared_literal_wildcards(
                            pattern, wildcard_markers, literal_codes_by_offset
                        )
                    ]
                    matched = [
                        model
                        for model in catalog_by_model
                        if any(
                            _catalog_model_matches_pattern(pattern, model, wildcard_markers)
                            for pattern in wildcard_patterns
                        )
                    ]
                    if len(matched) > 1:
                        for data_row_index, data_row in enumerate(
                            rows[row_index + 1 :], start=row_index + 1
                        ):
                            if column >= len(data_row):
                                continue
                            labels = [
                                clean(label)
                                for label in data_row[:column]
                                if compact(label)
                            ]
                            field_name = match_common_field(" / ".join(labels), dictionary)
                            if field_name not in {"flash_kb", "ram_kb", "max_freq_mhz"}:
                                continue
                            unit_name = dictionary["fields"][field_name].get("unit")
                            values = convert_values(
                                data_row[column],
                                dictionary["units"][unit_name],
                                " ".join(labels),
                            )
                            if len({float(value) for value in values}) > 1:
                                raise DeclarativeNotFound(
                                    "shared model column contains multiple relation-dependent "
                                    "quantities"
                                )
                    relation_dependent_upper_bound = False
                    if len(matched) > 1:
                        for data_row in rows[row_index + 1 :]:
                            if column >= len(data_row):
                                continue
                            labels = [
                                clean(label)
                                for label in data_row[:column]
                                if compact(label)
                            ]
                            field_name = match_common_field(" / ".join(labels), dictionary)
                            if field_name not in {"flash_kb", "ram_kb", "max_freq_mhz"}:
                                continue
                            normalized_value = compact(data_row[column]).casefold()
                            if any(marker in normalized_value for marker in upper_bound_markers):
                                relation_dependent_upper_bound = True
                                break
                    if relation_dependent_upper_bound:
                        raise DeclarativeNotFound(
                            "shared model column contains an upper-bound quantity; "
                            "a declared ordering relation is required for concrete values"
                        )
                    if matched and source_cell is not None:
                        covered_columns = [
                            candidate_column
                            for candidate_column in range(len(row))
                            if (
                                (candidate := cells.get((row_index, candidate_column)))
                                is not None
                                and (
                                    candidate.cell_id == source_cell.cell_id
                                    or candidate.inherited_from == source_cell.cell_id
                                )
                            )
                        ]
                        for data_row in rows[row_index + 1 :]:
                            distinct = {
                                compact(data_row[candidate_column])
                                for candidate_column in covered_columns
                                if candidate_column < len(data_row)
                                and compact(data_row[candidate_column])
                            }
                            if len(distinct) > 1:
                                raise DeclarativeNotFound(
                                    "model column pattern covers unresolved subcolumns with "
                                    "different values"
                                )
                    exact.extend(matched)
                found.extend((column, model) for model in dict.fromkeys(exact))
            if found:
                candidates.append((row_index, found))
        if candidates:
            header_row, model_columns = max(candidates, key=lambda item: len(item[1]))
            first_model_column = min(column for column, _ in model_columns)
        elif active_model_columns and active_first_model_column is not None:
            widest_row = max((len(row) for row in rows), default=0)
            if widest_row <= max(column for column, _ in active_model_columns):
                continue
            header_row = -1
            model_columns = active_model_columns
            first_model_column = active_first_model_column
        else:
            continue
        # A model token in the first column identifies a row-oriented matrix,
        # not a model value column. Reject that shape instead of accepting the
        # first data row as a one-model header.
        if first_model_column == 0:
            continue
        if candidates:
            active_model_columns = list(model_columns)
            active_first_model_column = first_model_column
        columns_by_model: dict[str, set[int]] = {}
        for column, model in model_columns:
            columns_by_model.setdefault(model, set()).add(column)
        ambiguous = {model: columns for model, columns in columns_by_model.items() if len(columns) > 1}
        if ambiguous:
            raise ValueError(
                "model column pattern intersection is ambiguous within one table fragment: "
                f"{sorted((model, sorted(columns)) for model, columns in ambiguous.items())}"
            )
        for column, model in model_columns:
            values = []
            for row_index, row in enumerate(rows):
                if row_index == header_row or column >= len(row) or not compact(row[column]):
                    continue
                labels = _aligned_row_labels(
                    rows, cells, row_index, column, first_model_column
                )
                if not labels:
                    continue
                values.append({"label": " / ".join(labels), **_cell(cells, row_index, column)})
            item = accumulated.setdefault(
                model,
                {
                    "model_id": model,
                    "page": page,
                    "source_table_id": table.table_id,
                    "source_table_title": table.title,
                    "model_source": catalog_by_model.get(model, {}).get(
                        "model_source",
                        _cell(cells, header_row, column) if header_row >= 0 else {},
                    ),
                    "values": [],
                },
            )
            item["values"].extend(values)
    return list(accumulated.values())


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
        data_start = next(
            (
                row_index
                for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1)
                if model_column < len(row)
                and profile.model_tokens(compact(row[model_column]), exact_only=True)
            ),
            len(rows),
        )

        # PDF table extractors often split a vertically written header into one
        # glyph per physical row (for example 触/摸/按/键/数). The model header
        # can sit in the middle of that band, so reading only its physical row
        # silently loses the field identity. Reconstruct every logical column
        # from the complete header band before the first concrete model row.
        width = max((len(row) for row in rows[:data_start]), default=0)
        headers: dict[int, str] = {}
        for column in range(width):
            parts = list(dict.fromkeys(
                clean(row[column])
                for row in rows[:data_start]
                if column < len(row) and compact(row[column])
            ))
            if not parts:
                continue
            containing = [
                part
                for part in parts
                if all(
                    part == other or compact(other) in compact(part)
                    for other in parts
                )
            ]
            if len(containing) == 1:
                headers[column] = containing[0]
                continue
            groups: list[str] = []
            glyphs = ""
            for part in parts:
                if len(compact(part)) <= 2:
                    glyphs += clean(part)
                else:
                    if glyphs:
                        groups.append(glyphs)
                        glyphs = ""
                    groups.append(clean(part))
            if glyphs:
                groups.append(glyphs)
            headers[column] = " / ".join(groups)

        for row_index, row in enumerate(rows[data_start:], start=data_start):
            if model_column >= len(row):
                continue
            models = profile.model_tokens(compact(row[model_column]), exact_only=True)
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


def _models_in_text_summary(
    context: DeclarativeContext,
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    scan_page_count = config.get("scan_page_count", 2)
    if not isinstance(scan_page_count, int) or not 1 <= scan_page_count <= 10:
        raise ValueError("models_in_text_summary scan_page_count must be an integer from 1 to 10")
    require_single_model = config.get("require_single_model", True)
    if not isinstance(require_single_model, bool):
        raise ValueError("models_in_text_summary require_single_model must be boolean")
    pages = context.page_texts[:scan_page_count]
    found: dict[str, dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    for page, text in enumerate(pages, start=1):
        previous_line = ""
        for line in (clean(item) for item in text.splitlines() if compact(item)):
            for model in context.profile.model_tokens(line, exact_only=True):
                found.setdefault(
                    model,
                    {"page": page, "bbox": (0.0, 0.0, 0.0, 0.0), "raw_value": line},
                )
            fragments = [
                (line, line),
                *(
                    [(f"{previous_line} {line}", line)]
                    if previous_line
                    and not re.search(
                        r"\d(?:[\d.]*)\s*(?:[kmgt]?bytes?|[kmgt]?b|[mgk]?hz|v\b|°\s*c\b)",
                        previous_line,
                        re.I,
                    )
                    else []
                ),
                *((fragment, fragment) for item in re.split(r"[,;•]", line) if compact(item) for fragment in [clean(item)]),
            ]
            for label, raw_value in dict.fromkeys(fragments):
                values.append(
                    {
                        "label": label,
                        "page": page,
                        "bbox": (0.0, 0.0, 0.0, 0.0),
                        "raw_value": raw_value,
                        "source_cell_id": None,
                    }
                )
            previous_line = line
    if not found or (require_single_model and len(found) != 1):
        return []
    return [
        {
            "model_id": model,
            "page": source["page"],
            "source_table_id": None,
            "source_table_title": "document summary",
            "model_source": source,
            "values": list(values),
        }
        for model, source in found.items()
    ]


def _models_in_grouped_columns(
    table: TableIR,
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> list[dict[str, Any]]:
    profile = context.profile
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
    alignment_relations = config.get("alignment_relations", [])
    if not isinstance(alignment_relations, list):
        raise ValueError("grouped model alignment_relations must be a list")
    ordering_section = config.get("ordering_section")
    legend = None
    if alignment_relations:
        if not isinstance(ordering_section, str) or ordering_section not in outputs:
            raise ValueError(
                "grouped model alignment_relations require an executed ordering_section"
            )
        legend = outputs[ordering_section]
        if not isinstance(legend, dict):
            raise DeclarativeNotFound("grouped model ordering relations were not found")
        for relation in alignment_relations:
            if (
                not isinstance(relation, dict)
                or set(relation) - {"relation", "code_offset", "field", "row_any"}
                or not {"relation", "code_offset"} <= set(relation)
                or ("field" in relation) == ("row_any" in relation)
            ):
                raise ValueError("grouped model alignment relation has an invalid shape")
            if relation["relation"] not in {
                "pin_count_codes", "flash_code_kb", "package_type_codes"
            }:
                raise ValueError("grouped model alignment references an unknown ordering relation")
            if not isinstance(relation["code_offset"], int) or relation["code_offset"] >= 0:
                raise ValueError("grouped model alignment code_offset must be negative")
            if "row_any" in relation and (
                not isinstance(relation["row_any"], list)
                or not relation["row_any"]
                or any(not isinstance(item, str) for item in relation["row_any"])
            ):
                raise ValueError("grouped model alignment row_any must be a string list")
    dictionary = load_field_dictionary(profile.field_dictionary_file, profile.field_aliases)
    grids = list(table_grids(table, inherit_merged_values=True))

    def grid_first_model_column(rows: list[list[str]]) -> int | None:
        for row in rows:
            for column, value in enumerate(row):
                if prefix_re.search(value):
                    return column
        return None

    def relation_observations(
        relation: dict[str, Any],
        rows: list[list[str]],
        first_model_column: int,
        column: int,
    ) -> set[float] | None:
        relation_name = relation["relation"]
        row_matches = []
        for row in rows[1:]:
            label = " / ".join(
                clean(value) for value in row[:first_model_column] if compact(value)
            )
            if "field" in relation:
                matched = match_common_field(label, dictionary) == relation["field"]
            else:
                normalized = compact(label).casefold()
                matched = any(
                    compact(alias).casefold() in normalized
                    for alias in relation["row_any"]
                )
            if matched and column < len(row):
                row_matches.append((label, row[column]))
        if not row_matches:
            relative_column = column - first_model_column
            for _, _, related_rows, _ in grids:
                related_first = grid_first_model_column(related_rows)
                if related_first is None:
                    continue
                current_count = len(rows[0]) - first_model_column
                related_count = len(related_rows[0]) - related_first
                if related_count == current_count:
                    related_offset = relative_column
                elif related_count and current_count % related_count == 0:
                    # A continuation page may merge equal-sized groups of
                    # subcolumns under one physical package cell.
                    related_offset = relative_column // (current_count // related_count)
                else:
                    continue
                related_column = related_first + related_offset
                for related_row in related_rows[1:]:
                    label = " / ".join(
                        clean(value)
                        for value in related_row[:related_first]
                        if compact(value)
                    )
                    if "field" in relation:
                        matched = match_common_field(label, dictionary) == relation["field"]
                    else:
                        normalized = compact(label).casefold()
                        matched = any(
                            compact(alias).casefold() in normalized
                            for alias in relation["row_any"]
                        )
                    if matched and related_column < len(related_row):
                        row_matches.append((label, related_row[related_column]))
        if not row_matches:
            # Some continued fragments collapse a different number of package
            # groups.  That relation is unavailable for this fragment; other
            # declared relations may still establish an unambiguous mapping.
            return None
        observed: set[float] = set()
        for label, raw_value in row_matches:
            if "field" in relation:
                unit_name = dictionary["fields"][relation["field"]].get("unit")
                unit_config = dictionary.get("units", {}).get(unit_name)
                if not unit_config:
                    raise ValueError(
                        f"grouped alignment field has no unit: {relation['field']}"
                    )
                observed.update(
                    float(value) for value in convert_values(raw_value, unit_config, label)
                )
            else:
                observed.update(
                    float(value)
                    for value in re.findall(r"[-+]?\d+(?:\.\d+)?", str(raw_value))
                )
        return observed

    def align_suffixes(
        suffixes: list[str],
        rows: list[list[str]],
        first_model_column: int,
    ) -> list[list[str]]:
        normalized_suffixes = [separator_re.sub("", suffix).upper() for suffix in suffixes]
        if not alignment_relations:
            if len(normalized_suffixes) != len(rows[0]) - first_model_column:
                raise DeclarativeNotFound(
                    "grouped model header has no relation for multi-model physical columns"
                )
            return [[suffix] for suffix in normalized_suffixes]
        compatible_by_column: list[set[str]] = []
        for column in range(first_model_column, len(rows[0])):
            compatible = set(normalized_suffixes)
            used_relations = 0
            for relation in alignment_relations:
                relation_name = relation["relation"]
                observed = relation_observations(
                    relation, rows, first_model_column, column
                )
                if observed is None:
                    continue
                used_relations += 1
                offset = relation["code_offset"]
                relation_values = legend.get(relation_name, {})
                accepted = set()
                for suffix in compatible:
                    if abs(offset) > len(suffix):
                        continue
                    expected = relation_values.get(suffix[offset].upper())
                    expected_values = expected if isinstance(expected, list) else [expected]
                    if any(
                        value is not None and float(value) in observed
                        for value in expected_values
                    ):
                        accepted.add(suffix)
                compatible = accepted
            if not used_relations:
                raise DeclarativeNotFound(
                    "grouped model alignment rows were unavailable for a physical column"
                )
            compatible_by_column.append(compatible)

        column_count = len(compatible_by_column)
        if len(normalized_suffixes) == column_count:
            one_to_one = [
                sorted(set(normalized_suffixes) & compatible)
                for compatible in compatible_by_column
            ]
            if all(len(group) == 1 for group in one_to_one):
                return one_to_one

        run_groups: list[list[str]] = []
        suffix_index = 0
        column_index = 0
        while column_index < column_count:
            compatible = compatible_by_column[column_index]
            run_end = column_index + 1
            while (
                run_end < column_count
                and compatible_by_column[run_end] == compatible
            ):
                run_end += 1
            candidate_end = suffix_index
            while (
                candidate_end < len(normalized_suffixes)
                and normalized_suffixes[candidate_end] in compatible
            ):
                candidate_end += 1
            run_length = run_end - column_index
            candidate_count = candidate_end - suffix_index
            if not candidate_count or candidate_count % run_length:
                run_groups = []
                break
            group_size = candidate_count // run_length
            run_groups.extend(
                normalized_suffixes[start : start + group_size]
                for start in range(suffix_index, candidate_end, group_size)
            )
            suffix_index = candidate_end
            column_index = run_end
        if len(run_groups) == column_count and suffix_index == len(normalized_suffixes):
            return run_groups

        if len(normalized_suffixes) % column_count == 0:
            size = len(normalized_suffixes) // column_count
            uniform = [
                normalized_suffixes[index * size : (index + 1) * size]
                for index in range(column_count)
            ]
            if all(
                group and all(suffix in compatible_by_column[index] for suffix in group)
                for index, group in enumerate(uniform)
            ):
                return uniform

        solutions: list[list[list[str]]] = []

        def partition(column_index: int, suffix_index: int, groups: list[list[str]]) -> None:
            if len(solutions) > 1:
                return
            if column_index == column_count:
                if suffix_index == len(normalized_suffixes):
                    solutions.append([list(group) for group in groups])
                return
            remaining_columns = column_count - column_index - 1
            maximum = len(normalized_suffixes) - suffix_index - remaining_columns
            for size in range(1, maximum + 1):
                group = normalized_suffixes[suffix_index : suffix_index + size]
                if not all(item in compatible_by_column[column_index] for item in group):
                    break
                partition(column_index + 1, suffix_index + size, [*groups, group])

        partition(0, 0, [])
        if len(solutions) != 1:
            relation_groups = [
                sorted(set(normalized_suffixes) & compatible)
                for compatible in compatible_by_column
            ]
            if (
                all(relation_groups)
                and Counter(item for group in relation_groups for item in group)
                == Counter(normalized_suffixes)
            ):
                # Continued tables may collapse ordering suffix variants into
                # the same base sales type. Equal relation signatures then
                # intentionally map that base type to more than one physical
                # column (for example two package-option columns).
                return relation_groups
            raise DeclarativeNotFound(
                "grouped model relations did not resolve one unambiguous suffix group per physical column"
            )
        return solutions[0]

    result: list[dict[str, Any]] = []
    incompatible_fragments: list[tuple[int, int, int]] = []
    wildcard_markers = tuple(getattr(profile, "wildcard_markers", ()))
    scope_patterns = list(
        dict.fromkeys(
            pattern
            for source_text in (
                table.title,
                *(
                    context.page_texts[page - 1]
                    for page in table.pages
                    if 1 <= page <= len(context.page_texts)
                ),
            )
            for pattern in _model_patterns(source_text, profile)
        )
    )

    def suffix_candidates(text: str) -> list[str]:
        variants = [text]
        token_reversed = re.sub(r"\S+", lambda match: match.group(0)[::-1], text)
        if token_reversed != text:
            variants.append(token_reversed)
        parsed = [
            [item.group(1) if item.lastindex else item.group(0) for item in suffix_re.finditer(value)]
            for value in variants
        ]
        return max(parsed, key=len, default=[])

    def concrete_models(prefix: str, suffix: str) -> list[str]:
        pattern = separator_re.sub("", f"{prefix}{suffix}").upper()
        candidates: list[str] = []
        literal_codes_by_offset = {
            relation["code_offset"]: {
                str(code).upper() for code in legend.get(relation["relation"], {})
            }
            for relation in alignment_relations
        }

        def accepted(candidate: str) -> bool:
            contains_marker = any(marker in candidate for marker in wildcard_markers)
            return bool(profile.model_tokens(candidate, exact_only=True)) and (
                not contains_marker
                or _uses_only_declared_literal_wildcards(
                    candidate, wildcard_markers, literal_codes_by_offset
                )
            )

        if accepted(pattern):
            candidates.append(pattern)
        for scope in scope_patterns:
            concrete = intersect_model_patterns(pattern, scope, wildcard_markers)
            if concrete and accepted(concrete):
                candidates.append(concrete.upper())
        return list(dict.fromkeys(candidates))

    def normalized_model_value(label: str, raw_value: str, model: str) -> int | float | None:
        scoped: list[float] = []
        for match in re.finditer(r"([-+]?\d+(?:\.\d+)?)\s*\(([^()]*)\)", raw_value):
            value, scope_text = match.groups()
            if any(
                _catalog_model_matches_pattern(pattern, model, wildcard_markers)
                for pattern in _model_patterns(scope_text, profile)
            ):
                scoped.append(float(value))
        if len(set(scoped)) == 1:
            return scalar_or_none(scoped[0])
        field_name = match_common_field(label, dictionary)
        for relation in alignment_relations:
            if not relation.get("field") or relation.get("field") != field_name:
                continue
            code_offset = relation["code_offset"]
            if abs(code_offset) > len(model):
                continue
            expected = legend.get(relation["relation"], {}).get(model[code_offset].upper())
            expected_values = expected if isinstance(expected, list) else [expected]
            unit_name = dictionary["fields"][field_name].get("unit")
            unit_config = dictionary.get("units", {}).get(unit_name)
            observed = convert_values(raw_value, unit_config, label) if unit_config else []
            selected = [
                float(value)
                for value in expected_values
                if value is not None and float(value) in {float(item) for item in observed}
            ]
            if len(set(selected)) == 1 and len({float(item) for item in observed}) > 1:
                return scalar_or_none(selected[0])
        return None

    for _, page, rows, cells in grids:
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
            suffixes = suffix_candidates(header_text[prefix_match.end() :])
            first_model_column = next(
                (column for column, value in enumerate(row) if value == header_text),
                None,
            )
            if first_model_column is None or not suffixes:
                continue
            available_columns = len(row) - first_model_column
            try:
                suffix_groups = align_suffixes(suffixes, rows, first_model_column)
            except DeclarativeNotFound:
                incompatible_fragments.append((page, len(suffixes), available_columns))
                continue
            for offset, suffix_group in enumerate(suffix_groups):
                column = first_model_column + offset
                models = list(
                    dict.fromkeys(
                        model
                        for suffix in suffix_group
                        for model in concrete_models(prefix, suffix)
                    )
                )
                if not models:
                    raise ValueError(
                        f"grouped model header produced no concrete model for {prefix}{suffix_group}"
                    )
                for model in models:
                    values = []
                    for row_index, value_row in enumerate(rows):
                        if row_index == header_row or column >= len(value_row) or not compact(value_row[column]):
                            continue
                        labels = [clean(value) for value in value_row[:first_model_column] if compact(value)]
                        if labels:
                            label = " / ".join(labels)
                            source = {"label": label, **_cell(cells, row_index, column)}
                            normalized_value = normalized_model_value(
                                label, str(source.get("raw_value") or ""), model
                            )
                            if normalized_value is not None:
                                source["normalized_value"] = normalized_value
                            values.append(source)
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
    merged: dict[str, dict[str, Any]] = {}
    for item in result:
        existing = merged.setdefault(item["model_id"], item)
        if existing is not item:
            existing["values"].extend(item["values"])
    package_pin_relations = [
        relation
        for relation in alignment_relations
        if relation["relation"] == "pin_count_codes"
        and "row_any" in relation
        and any("package" in compact(alias).casefold() for alias in relation["row_any"])
    ]
    if package_pin_relations and legend is not None:
        relation = package_pin_relations[0]
        offset = relation["code_offset"]
        pin_counts = legend.get("pin_count_codes", {})
        for item in merged.values():
            model = item["model_id"]
            expected = pin_counts.get(model[offset].upper()) if abs(offset) <= len(model) else None
            if expected is None:
                continue
            package_sources: dict[str, dict[str, Any]] = {}
            for _, _, related_rows, related_cells in grids:
                related_first = grid_first_model_column(related_rows)
                if related_first is None:
                    continue
                for row_index, related_row in enumerate(related_rows[1:], start=1):
                    label = " / ".join(
                        clean(value)
                        for value in related_row[:related_first]
                        if compact(value)
                    )
                    normalized = compact(label).casefold()
                    if not any(
                        compact(alias).casefold() in normalized
                        for alias in relation["row_any"]
                    ):
                        continue
                    for column in range(related_first, len(related_row)):
                        raw_value = related_row[column]
                        observed = {
                            float(value)
                            for value in re.findall(r"\d+(?:\.\d+)?", str(raw_value))
                        }
                        expected_values = expected if isinstance(expected, list) else [expected]
                        if not any(
                            value is not None and float(value) in observed
                            for value in expected_values
                        ):
                            continue
                        source = {"label": label, **_cell(related_cells, row_index, column)}
                        package_sources[str(source.get("source_cell_id"))] = source
            item["values"].extend(package_sources.values())
    return list(merged.values())


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
        "relation_sources": legend.relation_sources,
        "evidence_pages": list(legend.evidence_pages),
    }
    missing = [name for name in required if not result[name]]
    if missing:
        raise DeclarativeNotFound(f"ordering code legend missing required relations: {missing}")
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
    if outputs[ordering_section] is None:
        raise DeclarativeNotFound("model_catalog ordering structure was not found")
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
                for model in _model_patterns(raw_text, context.profile):
                    if not context.profile.model_tokens(model, exact_only=True):
                        continue
                    upper = model.upper()
                    if not all(
                        _position_relation_accepts(upper, rule, legend)
                        for rule in code_positions
                    ):
                        continue
                    if upper not in {item["model_id"] for item in candidates}:
                        candidates.append({"model_id": upper, "model_source": source})
    if not candidates:
        raise DeclarativeNotFound("model_catalog found no concrete models satisfying ordering relations")
    literal_codes_by_offset = {
        str(rule["offset"]): sorted(str(code).upper() for code in legend[rule["relation"]])
        for rule in code_positions
    }
    return {
        "models": sorted(candidates, key=lambda item: item["model_id"]),
        "literal_codes_by_offset": literal_codes_by_offset,
    }


def _raw_model_patterns(text: str, context: DeclarativeContext) -> list[str]:
    return _model_patterns(text, context.profile)


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
    if outputs[ordering_section] is None:
        raise DeclarativeNotFound("models_from_catalog ordering structure was not found")
    if not isinstance(density, dict) or set(density) != {"relation", "offset", "field"}:
        raise ValueError("models_from_catalog requires density_relation relation, offset and field")
    if density["relation"] not in {"flash_code_kb"} or density["field"] != "flash_kb":
        raise ValueError("models_from_catalog density_relation must use registered flash semantics")
    offset = density["offset"]
    if not isinstance(offset, int) or offset >= 0:
        raise ValueError("models_from_catalog density offset must be negative")
    legend = outputs[ordering_section]
    density_map = legend[density["relation"]]
    catalog_output = outputs[catalog_section]
    catalog = catalog_output["models"] if catalog_output is not None else []
    wildcard_markers = tuple(getattr(context.profile, "wildcard_markers", ("X",)))
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file, context.profile.field_aliases
    )
    unit_config = dictionary["units"]["memory_kb"]
    upper_bound_markers = [
        compact(item).casefold()
        for item in dictionary.get("semantics", {}).get("upper_bound_any", [])
    ]
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
                    if "X" in pattern or context.profile.model_tokens(pattern, exact_only=True):
                        headers.append((row_index, column, pattern))
        if not headers:
            continue
        table_catalog = catalog
        if not table_catalog:
            scope_patterns = _raw_model_patterns(table.title, context)
            derived: list[dict[str, Any]] = []
            for header_row, header_column, header_pattern in headers:
                for scope_pattern in scope_patterns:
                    model = intersect_model_patterns(
                        header_pattern, scope_pattern, wildcard_markers
                    )
                    if not model or not context.profile.model_tokens(model, exact_only=True):
                        continue
                    upper = model.upper()
                    if upper[-1:] not in density_map:
                        continue
                    pin_codes = legend.get("pin_count_codes", {})
                    if pin_codes and upper[-2:-1] not in pin_codes:
                        continue
                    if upper not in {item["model_id"] for item in derived}:
                        derived.append({
                            "model_id": upper,
                            "model_source": _cell(cells, header_row, header_column),
                        })
            table_catalog = derived
        if not table_catalog:
            continue
        first_model_column = min(column for _, column, _ in headers)
        starts = sorted({column for _, column, _ in headers})
        for catalog_item in table_catalog:
            model = catalog_item["model_id"]
            matches = [
                (row, column, pattern)
                for row, column, pattern in headers
                if _catalog_model_matches_pattern(pattern, model, wildcard_markers)
            ]
            if not matches:
                continue
            # A broad wildcard header can overlap a neighboring concrete
            # model column. Prefer the most specific observed header so a
            # concrete model never inherits the wildcard column's values.
            header_row, start, _ = min(
                matches,
                key=lambda item: (
                    sum(item[2].upper().count(marker.upper()) for marker in wildcard_markers),
                    len(item[2]),
                    item[1],
                ),
            )
            end = next((column for column in starts if column > start), len(rows[header_row]))
            expected = density_map.get(model[offset].upper()) if abs(offset) <= len(model) else None
            if expected is None:
                raise ValueError(f"no ordering density relation for model {model}")
            exact_density_columns: list[int] = []
            bounded_density_columns: list[int] = []
            density_row_index: int | None = None
            has_density_row = False
            for row_index, row in enumerate(rows):
                labels = [clean(value) for value in row[:first_model_column] if compact(value)]
                if match_common_field(" / ".join(labels), dictionary) != density["field"]:
                    continue
                has_density_row = True
                density_row_index = row_index
                for column in range(start, min(end, len(row))):
                    values = convert_values(row[column], unit_config, " ".join(labels))
                    if any(float(value) == float(expected) for value in values):
                        exact_density_columns.append(column)
                    elif (
                        values
                        and any(marker in compact(row[column]).casefold() for marker in upper_bound_markers)
                        and float(expected) <= max(float(value) for value in values)
                    ):
                        bounded_density_columns.append(column)
            density_columns = list(
                dict.fromkeys(exact_density_columns or bounded_density_columns)
            )
            bounded_density = not exact_density_columns and bool(bounded_density_columns)
            relation_derived_density = False
            if has_density_row and len(density_columns) != 1:
                raise ValueError(
                    f"model {model} density relation selected {len(density_columns)} columns"
                )
            if has_density_row:
                column = density_columns[0]
                selected_density_values = convert_values(
                    rows[density_row_index or 0][column],
                    unit_config,
                    density["field"],
                )
                relation_derived_density = bounded_density or len(
                    {float(value) for value in selected_density_values}
                ) > 1
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
                density_source = _cell(cells, density_row_index or 0, column)
                density_bbox = density_source.get("bbox")
                page_width = context.document.pages[page - 1].width
                if density_bbox and page_width:
                    item["_column_anchor_ratio"] = (
                        (float(density_bbox[0]) + float(density_bbox[2])) / 2 / float(page_width)
                    )
            else:
                # A device may encode the density exclusively in its ordering
                # code (for example a flashless part whose feature matrix starts
                # with SRAM).  When one concrete model header maps to exactly
                # one physical value column, the declared ordering relation is
                # sufficient evidence for the missing matrix field.  Wider
                # shared headers remain unresolved rather than being guessed.
                item = accumulated.get(model)
                if item is None:
                    if end - start != 1:
                        continue
                    relation_source = legend.get("relation_sources", {}).get(
                        density["relation"], {}
                    ).get(model[offset].upper())
                    if not relation_source:
                        continue
                    item = accumulated.setdefault(
                        model,
                        {
                            "model_id": model,
                            "page": page,
                            "source_table_id": table.table_id,
                            "source_table_title": table.title,
                            "model_source": catalog_item["model_source"],
                            "values": [
                                {
                                    "label": density["field"],
                                    **relation_source,
                                    "normalized_value": expected,
                                }
                            ],
                        },
                    )
                    header_source = _cell(cells, header_row, start)
                    header_bbox = header_source.get("bbox")
                    page_width = context.document.pages[page - 1].width
                    if header_bbox and page_width:
                        item["_column_anchor_ratio"] = (
                            (float(header_bbox[0]) + float(header_bbox[2]))
                            / 2
                            / float(page_width)
                        )
                column = start if end - start == 1 else None
                anchor_ratio = item.get("_column_anchor_ratio")
                page_width = context.document.pages[page - 1].width
            for row_index, row in enumerate(rows):
                if row_index <= header_row:
                    continue
                labels = [clean(value) for value in row[:first_model_column] if compact(value)]
                label = " / ".join(labels)
                if not label:
                    continue
                field_name = match_common_field(label, dictionary)
                selected_column = column
                if not has_density_row and end - start > 1:
                    if anchor_ratio is None or not page_width:
                        continue
                    target_x = float(anchor_ratio) * float(page_width)
                    candidates: dict[str, int] = {}
                    for candidate in range(start, min(end, len(row))):
                        source = _cell(cells, row_index, candidate)
                        bbox = source.get("bbox")
                        if not bbox or not compact(source.get("raw_value")):
                            continue
                        if float(bbox[0]) - 0.5 <= target_x <= float(bbox[2]) + 0.5:
                            candidates[str(source.get("source_cell_id"))] = candidate
                    if len(candidates) > 1:
                        raise ValueError(
                            f"model {model} continued fragment has ambiguous geometric column lineage for {label}"
                        )
                    selected_column = next(iter(candidates.values()), None)
                if selected_column is None or selected_column >= len(row) or not compact(row[selected_column]):
                    continue
                source = {"label": label, **_cell(cells, row_index, selected_column)}
                if relation_derived_density and field_name == density["field"]:
                    code = model[offset].upper()
                    relation_source = legend.get("relation_sources", {}).get(
                        density["relation"], {}
                    ).get(code)
                    if relation_source:
                        source = {
                            "label": label,
                            **relation_source,
                            "normalized_value": expected,
                        }
                item["values"].append(source)
    for item in accumulated.values():
        item.pop("_column_anchor_ratio", None)
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
    if reducer == "unique":
        distinct = list(dict.fromkeys(values))
        if len(distinct) != 1:
            raise ValueError(f"numeric values are not semantically unique: {distinct}")
        return distinct[0]
    raise ValueError(f"unsupported declarative numeric reducer: {reducer}")


def _document_model_package_relations(
    context: DeclarativeContext,
    model_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {model: [] for model in model_ids}
    markers = tuple(getattr(context.profile, "wildcard_markers", ("X",)))
    marker_set = {compact(item).upper() for item in markers if compact(item)}
    for page, text in enumerate(context.page_texts, start=1):
        for raw_line in text.splitlines():
            packages = parse_package_names(raw_line, context.profile.package_re.pattern)
            if not packages:
                continue
            patterns = _model_patterns(raw_line, context.profile)
            if not patterns:
                continue
            for model in model_ids:
                compatible = False
                for pattern in patterns:
                    if wildcard_match(pattern, model, markers):
                        compatible = True
                        break
                    wildcard_positions = [
                        index for index, character in enumerate(pattern) if character in marker_set
                    ]
                    if not wildcard_positions or len(pattern) < len(model):
                        continue
                    shared = pattern[: len(model)]
                    if wildcard_match(shared, model, markers):
                        compatible = True
                        break
                if compatible:
                    result[model].append(
                        {
                            "label": "Package",
                            "page": page,
                            "bbox": (0.0, 0.0, 0.0, 0.0),
                            "raw_value": raw_line,
                            "source_cell_id": None,
                        }
                    )
    return result


FIELD_PARSERS = {
    "flash_kb": {"quantity"},
    "ram_kb": {"quantity"},
    "mtp_kb": {"quantity"},
    "eeprom_kb": {"quantity"},
    "ram_total_kb": {"quantity"},
    "ram_internal_kb": {"quantity"},
    "ram_external_kb": {"quantity"},
    "max_freq_mhz": {"quantity"},
    "operating_voltage": {"range"},
    "operating_temperature": {"range"},
    "operating_conditions": {"compound_ranges"},
    "package_types": {"package_names"},
}
DEFAULT_FIELD_PARSER = {name: next(iter(values)) for name, values in FIELD_PARSERS.items()}
QUALIFIERS = {"nonempty", "not_absolute_maximum"}


def _expand_composite_model_values(
    models: list[dict[str, Any]],
    composite: Any,
    context: DeclarativeContext,
) -> None:
    """Expand parallel composite headers and values by semantic header name.

    For example, ``FLASH / SRAM`` and ``32 / 16`` become two sourced values.
    Alignment is accepted only when every header part has a declared semantic
    identity and the header/value arity agrees.  No positional field meaning is
    stored in either code or configuration.
    """
    if not isinstance(composite, dict):
        raise ValueError("model_matrix composite_columns must be an object")
    unknown = set(composite) - {"separator_pattern", "part_aliases"}
    if unknown:
        raise ValueError(f"composite_columns has unknown keys: {sorted(unknown)}")
    separator_pattern = composite.get("separator_pattern")
    if not isinstance(separator_pattern, str) or not separator_pattern:
        raise ValueError("composite_columns requires separator_pattern")
    try:
        separator = re.compile(separator_pattern)
    except re.error as exc:
        raise ValueError(f"invalid composite_columns separator_pattern: {exc}") from exc
    raw_aliases = composite.get("part_aliases", {})
    if not isinstance(raw_aliases, dict) or any(
        not isinstance(alias, str) or not compact(alias)
        or not isinstance(rule, dict)
        or set(rule) - {"semantic", "normalized_label"}
        or not isinstance(rule.get("semantic"), str)
        or not compact(rule.get("semantic"))
        or (
            "normalized_label" in rule
            and (
                not isinstance(rule["normalized_label"], str)
                or not compact(rule["normalized_label"])
            )
        )
        for alias, rule in raw_aliases.items()
    ):
        raise ValueError(
            "composite_columns part_aliases must map names to semantic rules"
        )
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    unknown_semantics = {
        rule["semantic"] for rule in raw_aliases.values()
    } - set(dictionary["fields"])
    if unknown_semantics:
        raise ValueError(
            f"composite_columns aliases target unknown fields: {sorted(unknown_semantics)}"
        )
    aliases = {compact(key).casefold(): value for key, value in raw_aliases.items()}

    for model in models:
        expanded: list[dict[str, Any]] = []
        for item in model.get("values", []):
            label = str(item.get("label") or "")
            label_parts = [clean(part) for part in separator.split(label) if compact(part)]
            if len(label_parts) < 2:
                expanded.append(item)
                continue
            alias_rules = [aliases.get(compact(part).casefold()) for part in label_parts]
            semantics = [
                rule["semantic"] if rule else match_common_field(part, dictionary)
                for part, rule in zip(label_parts, alias_rules)
            ]
            # A slash can also be prose or a unit expression. It is a composite
            # field only when every named part has a semantic identity.
            if any(semantic is None for semantic in semantics):
                expanded.append(item)
                continue
            value_parts = [clean(part) for part in separator.split(str(item.get("raw_value") or ""))]
            if len(value_parts) != len(label_parts):
                raise ValueError(
                    f"composite header/value arity mismatch for {label!r}: "
                    f"{len(label_parts)} headers, {len(value_parts)} values"
                )
            for part_label, part_value, semantic, alias_rule in zip(
                label_parts, value_parts, semantics, alias_rules
            ):
                expanded.append({
                    **item,
                    "label": (
                        alias_rule.get("normalized_label", part_label)
                        if alias_rule else part_label
                    ),
                    "raw_value": part_value,
                    "evidence_raw_value": item.get("raw_value"),
                    "unit_source_label": label,
                    "semantic_field": semantic,
                })
        model["values"] = expanded


def _merge_repeated_model_rows(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge repeated entity rows without deciding any field's business value."""
    merged: dict[str, dict[str, Any]] = {}
    seen_values: dict[str, set[tuple[Any, ...]]] = {}
    for model in models:
        model_id = str(model["model_id"])
        target = merged.setdefault(model_id, {
            **model,
            "values": [],
            "model_sources": [],
        })
        target["model_sources"].append(model.get("model_source", {}))
        signatures = seen_values.setdefault(model_id, set())
        for item in model.get("values", []):
            signature = (
                item.get("semantic_field"), compact(item.get("label")),
                compact(item.get("raw_value")), item.get("page"),
                item.get("source_cell_id"), tuple(item.get("bbox") or ()),
            )
            if signature in signatures:
                continue
            signatures.add(signature)
            target["values"].append(item)
    return list(merged.values())


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
    allowed_fields = set(FIELD_PARSERS)
    unknown_fields = set(field_plan) - allowed_fields
    if unknown_fields:
        raise ValueError(f"model_matrix fields contain unsupported targets: {sorted(unknown_fields)}")
    package_fallback_requested = False
    for field_name, settings in field_plan.items():
        if not isinstance(settings, dict):
            raise ValueError(f"model_matrix field {field_name} must be an object")
        unknown_settings = set(settings) - {
            "parser", "reducer", "qualifiers", "required", "fallback",
        }
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
        fallback = settings.get("fallback")
        if fallback is not None:
            if field_name != "package_types" or fallback != "document_model_relation":
                raise ValueError(
                    f"unsupported fallback {fallback!r} for model_matrix field {field_name}"
                )
            package_fallback_requested = True
    package_relations = (
        _document_model_package_relations(
            context, [str(model["model_id"]).upper() for model in models]
        )
        if package_fallback_requested
        else {}
    )
    for model in models:
        grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in field_plan}
        for value in model["values"]:
            field_name = value.get("semantic_field") or match_common_field(value["label"], dictionary)
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
            if (
                not items
                and field_name == "package_types"
                and settings.get("fallback") == "document_model_relation"
            ):
                items = package_relations.get(str(model["model_id"]).upper(), [])
            if not items:
                if settings.get("required"):
                    raise ValueError(
                        f"required field {field_name} missing for model {model['model_id']}"
                    )
                continue
            if parser == "quantity":
                unit_name = dictionary["fields"][field_name].get("unit")
                unit_config = dictionary.get("units", {}).get(unit_name)
                if not unit_config:
                    raise ValueError(f"field {field_name} has no registered unit conversion")
                reducer = settings.get("reducer", "max")
                parsed_items = [
                    (
                        item,
                        [item["normalized_value"]]
                        if "normalized_value" in item
                        else convert_values(
                            item["raw_value"], unit_config,
                            item.get("unit_source_label", item["label"]),
                        ),
                    )
                    for item in items
                ]
                if reducer == "sum_distinct_regions":
                    by_region: dict[str, list[tuple[dict[str, Any], int | float]]] = {}
                    for item, parsed in parsed_items:
                        by_region.setdefault(compact(item["label"]).casefold(), []).extend(
                            (item, number) for number in parsed
                        )
                    value = scalar_or_none(
                        sum(max(number for _, number in region_values) for region_values in by_region.values() if region_values)
                    )
                    sources[field_name] = list({
                        id(item): item
                        for region_values in by_region.values()
                        if region_values
                        for item, number in region_values
                        if number == max(candidate for _, candidate in region_values)
                    }.values())
                else:
                    values = [
                        number
                        for _, parsed in parsed_items
                        for number in parsed
                    ]
                    value = _reduce_numeric(values, reducer)
                    if reducer == "max":
                        sources[field_name] = [
                            item for item, parsed in parsed_items if value in parsed
                        ]
                    elif reducer == "first":
                        sources[field_name] = [
                            item for item, parsed in parsed_items if parsed and parsed[0] == value
                        ][:1]
                    else:
                        sources[field_name] = [item for item, parsed in parsed_items if parsed]
                if value is not None:
                    fields[field_name] = value
            elif parser == "range":
                parsed_items = [(item, parse_ranges(item["raw_value"])) for item in items]
                ranges = [value for _, parsed in parsed_items for value in parsed]
                if ranges:
                    target_prefix = {
                        "operating_voltage": "voltage",
                        "operating_temperature": "temperature",
                    }[field_name]
                    fields[f"{target_prefix}_min"] = min(item[0] for item in ranges)
                    fields[f"{target_prefix}_max"] = max(item[1] for item in ranges)
                    sources[field_name] = [item for item, parsed in parsed_items if parsed]
            elif parser == "compound_ranges":
                parsed_items = [
                    (item, parse_compound_operating_conditions(item["raw_value"]))
                    for item in items
                ]
                compound = [parsed for _, parsed in parsed_items]
                for target in (
                    "voltage_min",
                    "voltage_max",
                    "temperature_min",
                    "temperature_max",
                ):
                    values = [item[target] for item in compound if target in item]
                    if values:
                        fields[target] = min(values) if target.endswith("_min") else max(values)
                sources[field_name] = [item for item, parsed in parsed_items if parsed]
            elif parser == "package_names":
                parsed_items = [
                    (
                        item,
                        parse_package_names(item["raw_value"], context.profile.package_re.pattern),
                    )
                    for item in items
                ]
                packages = list(dict.fromkeys(package for _, parsed in parsed_items for package in parsed))
                if packages:
                    fields[field_name] = packages
                    sources[field_name] = [item for item, parsed in parsed_items if parsed]
            if settings.get("required"):
                targets = {
                    "range": (
                        ("temperature_min", "temperature_max")
                        if field_name == "operating_temperature"
                        else ("voltage_min", "voltage_max")
                    ),
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
            (
                item["table_id"],
                tuple(
                    sorted(
                        (
                            model["model_id"],
                            json.dumps(model.get("fields", {}), ensure_ascii=False, sort_keys=True),
                        )
                        for model in item["models"]
                    )
                ),
            )
            for item in matches
        }
        if len(signatures) != 1 and len(matches) > 1:
            semantic_maps = [
                {
                    model["model_id"]: json.dumps(
                        model.get("fields", {}), ensure_ascii=False, sort_keys=True
                    )
                    for model in item["models"]
                }
                for item in matches
            ]
            ranked = sorted(
                range(len(matches)),
                key=lambda index: len(semantic_maps[index]),
                reverse=True,
            )
            winner = ranked[0]
            winner_map = semantic_maps[winner]
            dominates = all(
                matches[index]["table_id"] == matches[winner]["table_id"]
                and set(candidate).issubset(winner_map)
                and all(winner_map[model] == fields for model, fields in candidate.items())
                for index, candidate in enumerate(semantic_maps)
                if index != winner
            )
            if dominates:
                return matches[winner]
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
        "alignment_relations",
        "fields",
        "variants",
        "scan_page_count",
        "require_single_model",
        "title_optional_when_model_header_matches",
        "composite_columns",
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
        "models_in_text_summary",
    }:
        raise ValueError("model_matrix layout must explicitly describe its model header structure")
    if layout == "models_in_text_summary":
        models = _models_in_text_summary(context, config)
        if not models:
            raise DeclarativeNotFound("document summary produced no unambiguous concrete model")
        if "fields" in config:
            _map_model_fields(models, config["fields"], context)
        return {"layout": layout, "table_id": None, "models": models}
    locator = {"title_any": config.get("title_any", []), **config.get("locator", {})}
    structural_title_fallback = config.get("title_optional_when_model_header_matches", False)
    if not isinstance(structural_title_fallback, bool):
        raise ValueError("title_optional_when_model_header_matches must be boolean")
    titled_candidates = []
    structural_candidates = []
    for table in context.document.tables:
        if table_matches(table, locator):
            titled_candidates.append(table)
            continue
        if structural_title_fallback and table_matches(table, {**locator, "title_any": []}):
            structural_candidates.append(table)
    # A title-less structural match is a recovery path for damaged captions,
    # not a peer of an explicitly titled table. Otherwise revision histories
    # and prose-heavy matrices can win merely because they mention more model
    # tokens in their body.
    candidates = titled_candidates or structural_candidates
    parsed = []
    for table in candidates:
        if layout == "models_in_columns":
            catalog_section = config.get("catalog_section")
            catalog = None
            if catalog_section is not None:
                if not isinstance(catalog_section, str) or catalog_section not in outputs:
                    raise ValueError("models_in_columns catalog_section must reference an executed section")
                catalog_output = outputs[catalog_section]
                if catalog_output is not None:
                    catalog = catalog_output
            models = _models_in_columns(table, context.profile, catalog)
        elif layout == "models_in_rows":
            models = _models_in_rows(table, context.profile, config)
        elif layout == "models_in_grouped_columns":
            models = _models_in_grouped_columns(table, context, config, outputs)
        else:
            models = _models_from_catalog(table, context, config, outputs)
        if models:
            parsed.append((len(models), table, models))
    if not parsed:
        raise DeclarativeNotFound("no configured model matrix produced concrete models")
    target_models = context.profile.model_tokens(
        Path(context.document.source_file).stem, exact_only=True
    )
    series_for_model = getattr(context.profile, "series_for_model", None)
    target_series = {
        series_for_model(model)
        for model in target_models
        if callable(series_for_model) and series_for_model(model)
    }

    def candidate_rank(candidate: tuple[int, TableIR, list[dict[str, Any]]]) -> tuple[int, int, int, int]:
        count, candidate_table, candidate_models = candidate
        matching_series = sum(
            1
            for model in candidate_models
            if callable(series_for_model)
            and series_for_model(str(model["model_id"])) in target_series
        )
        return (int(matching_series > 0), matching_series, count, -candidate_table.pages[0])

    _, table, models = max(parsed, key=candidate_rank)
    if "composite_columns" in config:
        _expand_composite_model_values(models, config["composite_columns"], context)
    models = _merge_repeated_model_rows(models)
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


def memory_projection_component(
    _: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project semantic model fields into canonical memory records.

    This component deliberately performs no document recognition. The upstream
    component owns extraction; this component only describes how an already
    identified semantic field maps to the output contract.
    """
    allowed = {"model_section", "mappings"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"memory_projection config has unknown keys: {sorted(unknown)}")
    model_section = config.get("model_section")
    if not isinstance(model_section, str) or model_section not in outputs:
        raise ValueError("memory_projection model_section must reference an executed section")
    model_output = outputs[model_section]
    if not isinstance(model_output, dict) or not isinstance(model_output.get("models"), list):
        raise ValueError("memory_projection model_section produced no model collection")
    mappings = config.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("memory_projection mappings must be a non-empty array")
    normalized_mappings: list[tuple[str, str | None, str, str]] = []
    seen_types: set[str] = set()
    for index, mapping in enumerate(mappings):
        if not isinstance(mapping, dict) or not {"source_field", "memory_type"} <= set(mapping) or set(mapping) - {"source_field", "memory_type", "source_section", "fallback_section"}:
            raise ValueError(
                f"memory_projection mapping {index} has an invalid shape"
            )
        source_field = mapping["source_field"]
        memory_type = mapping["memory_type"]
        source_section = mapping.get("source_section", model_section)
        fallback_section = mapping.get("fallback_section")
        if source_field not in FIELD_PARSERS or FIELD_PARSERS[source_field] != {"quantity"}:
            raise ValueError(f"memory_projection source field is not a quantity: {source_field}")
        if not isinstance(memory_type, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", memory_type):
            raise ValueError(f"invalid canonical memory type: {memory_type!r}")
        if memory_type in seen_types:
            raise ValueError(f"duplicate memory_projection memory type: {memory_type}")
        seen_types.add(memory_type)
        if not isinstance(source_section, str) or source_section not in outputs:
            raise ValueError(
                f"memory_projection source_section must reference an executed section: {source_section!r}"
            )
        if fallback_section is not None and (
            not isinstance(fallback_section, str) or fallback_section not in outputs
        ):
            raise ValueError(
                f"memory_projection fallback_section must reference an executed section: {fallback_section!r}"
            )
        normalized_mappings.append((source_section, fallback_section, source_field, memory_type))
    records: list[dict[str, Any]] = []
    for model in model_output["models"]:
        fields = model.get("fields", {})
        sources = model.get("field_sources", {})
        for source_section, fallback_section, source_field, memory_type in normalized_mappings:
            candidate_sections = [source_section]
            if fallback_section is not None:
                candidate_sections.append(fallback_section)
            size_kb = None
            field_sources: list[dict[str, Any]] = []
            for candidate_section in candidate_sections:
                if candidate_section == model_section:
                    source_fields = fields
                    source_map = sources
                else:
                    shared = outputs[candidate_section]
                    source_fields = shared.get("fields", {}) if isinstance(shared, dict) else {}
                    source_map = shared.get("field_sources", {}) if isinstance(shared, dict) else {}
                size_kb = source_fields.get(source_field)
                field_sources = source_map.get(source_field, [])
                if size_kb is not None:
                    break
            if size_kb is None:
                continue
            if not isinstance(size_kb, (int, float)) or isinstance(size_kb, bool) or size_kb < 0:
                raise ValueError(
                    f"memory_projection requires non-negative numeric {source_field} for {model['model_id']}"
                )
            if not field_sources:
                raise ValueError(
                    f"memory_projection requires source evidence for {source_field} on {model['model_id']}"
                )
            records.append({
                "chip_id": model["model_id"],
                "memory_type": memory_type,
                "size_kb": size_kb,
                "organization": None,
                "features": None,
                "model_source": model["model_source"],
                "value_sources": field_sources,
            })
    if not records:
        raise DeclarativeNotFound("memory_projection produced no records")
    return {"records": records}


def electrical_projection_component(
    _: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project normalized operating ranges into canonical electrical records."""
    allowed = {"model_section", "mappings"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"electrical_projection config has unknown keys: {sorted(unknown)}")
    model_section = config.get("model_section")
    if not isinstance(model_section, str) or model_section not in outputs:
        raise ValueError("electrical_projection model_section must reference an executed section")
    model_output = outputs[model_section]
    if not isinstance(model_output, dict) or not isinstance(model_output.get("models"), list):
        raise ValueError("electrical_projection model_section produced no model collection")
    mappings = config.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("electrical_projection mappings must be a non-empty array")
    semantics = {
        "operating_voltage": ("voltage_min", "voltage_max"),
        "operating_temperature": ("temperature_min", "temperature_max"),
    }
    normalized: list[tuple[str, str, str, list[str], str, str | None]] = []
    seen_parameters: set[str] = set()
    for index, mapping in enumerate(mappings):
        if (
            not isinstance(mapping, dict)
            or not {"semantic", "param_name", "unit", "evidence_fields"} <= set(mapping)
            or set(mapping) - {
                "semantic", "param_name", "unit", "evidence_fields",
                "source_section", "fallback_section",
            }
        ):
            raise ValueError(f"electrical_projection mapping {index} has an invalid shape")
        semantic = mapping["semantic"]
        param_name = mapping["param_name"]
        unit = mapping["unit"]
        evidence_fields = mapping["evidence_fields"]
        source_section = mapping.get("source_section", model_section)
        fallback_section = mapping.get("fallback_section")
        if semantic not in semantics:
            raise ValueError(f"electrical_projection has unknown semantic: {semantic!r}")
        if not isinstance(param_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", param_name):
            raise ValueError(f"invalid canonical electrical parameter: {param_name!r}")
        if param_name in seen_parameters:
            raise ValueError(f"duplicate electrical_projection parameter: {param_name}")
        seen_parameters.add(param_name)
        if not isinstance(unit, str) or not compact(unit):
            raise ValueError("electrical_projection unit must be a non-empty string")
        if (
            not isinstance(evidence_fields, list)
            or not evidence_fields
            or any(field not in FIELD_PARSERS for field in evidence_fields)
        ):
            raise ValueError("electrical_projection evidence_fields must name semantic fields")
        if not isinstance(source_section, str) or source_section not in outputs:
            raise ValueError(
                "electrical_projection source_section must reference an executed section"
            )
        if fallback_section is not None and (
            not isinstance(fallback_section, str) or fallback_section not in outputs
        ):
            raise ValueError(
                "electrical_projection fallback_section must reference an executed section"
            )
        normalized.append(
            (semantic, param_name, unit, evidence_fields, source_section, fallback_section)
        )
    records: list[dict[str, Any]] = []
    for model in model_output["models"]:
        fields = model.get("fields", {})
        field_sources = model.get("field_sources", {})
        for semantic, param_name, unit, evidence_fields, source_section, fallback_section in normalized:
            min_field, max_field = semantics[semantic]
            minimum = maximum = None
            sources: list[dict[str, Any]] = []
            for candidate_section in [source_section, fallback_section]:
                if candidate_section is None:
                    continue
                if candidate_section == model_section:
                    candidate_fields = fields
                    candidate_sources = field_sources
                else:
                    shared = outputs[candidate_section]
                    candidate_fields = (
                        shared.get("fields", shared) if isinstance(shared, dict) else {}
                    )
                    candidate_sources = shared.get("field_sources", {}) if isinstance(shared, dict) else {}
                candidate_min = candidate_fields.get(min_field)
                candidate_max = candidate_fields.get(max_field)
                if candidate_min is None and candidate_max is None:
                    continue
                minimum, maximum = candidate_min, candidate_max
                sources = next(
                    (
                        candidate_sources[field]
                        for field in evidence_fields
                        if candidate_sources.get(field)
                    ),
                    [],
                )
                break
            if minimum is None and maximum is None:
                continue
            if (
                not isinstance(minimum, (int, float))
                or isinstance(minimum, bool)
                or not isinstance(maximum, (int, float))
                or isinstance(maximum, bool)
                or minimum > maximum
            ):
                raise ValueError(
                    f"electrical_projection requires an ordered numeric {semantic} range "
                    f"for {model['model_id']}"
                )
            if not sources:
                raise ValueError(
                    f"electrical_projection requires source evidence for {semantic} "
                    f"on {model['model_id']}"
                )
            records.append({
                "chip_id": model["model_id"],
                "param_name": param_name,
                "min_value": minimum,
                "max_value": maximum,
                "unit": unit,
                "conditions": None,
                "value_sources": sources,
            })
    if not records:
        raise DeclarativeNotFound("electrical_projection produced no records")
    return {"records": records}


def document_ranges_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    _: dict[str, Any],
) -> dict[str, Any]:
    """Read document-scoped operating ranges from labelled summary text."""
    allowed = {"fields", "scan_page_count"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"document_ranges config has unknown keys: {sorted(unknown)}")
    fields = config.get("fields")
    supported = {"operating_voltage", "operating_temperature"}
    if (
        not isinstance(fields, list)
        or not fields
        or any(field not in supported for field in fields)
    ):
        raise ValueError("document_ranges fields must name supported operating ranges")
    scan_page_count = config.get("scan_page_count", len(context.page_texts))
    if not isinstance(scan_page_count, int) or not 1 <= scan_page_count <= 20:
        raise ValueError("document_ranges scan_page_count must be an integer from 1 to 20")
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    candidates: dict[str, list[tuple[tuple[float, float], dict[str, Any]]]] = {
        field: [] for field in fields
    }
    for page, text in enumerate(context.page_texts[:scan_page_count], start=1):
        for raw_line in text.splitlines():
            normalized = compact(raw_line)
            if not normalized or is_absolute_maximum_context(raw_line):
                continue
            field = match_common_field(raw_line, dictionary)
            if field not in candidates:
                continue
            if field == "operating_voltage" and not re.search(r"\d\s*(?:m?v)\b", raw_line, re.I):
                continue
            if field == "operating_temperature" and not re.search(r"(?:℃|°\s*C|\d\s*C\b)", raw_line, re.I):
                continue
            ranges = parse_ranges(raw_line)
            if not ranges:
                continue
            value = (
                min(item[0] for item in ranges),
                max(item[1] for item in ranges),
            )
            candidates[field].append((value, {
                "label": field,
                "page": page,
                "bbox": (0.0, 0.0, 0.0, 0.0),
                "raw_value": raw_line,
                "source_cell_id": None,
            }))
    result_fields: dict[str, int | float] = {}
    result_sources: dict[str, list[dict[str, Any]]] = {}
    target_prefix = {
        "operating_voltage": "voltage",
        "operating_temperature": "temperature",
    }
    for field, values in candidates.items():
        if not values:
            continue
        first_page = min(source["page"] for _, source in values)
        first_page_values = [item for item in values if item[1]["page"] == first_page]
        low = min(item[0][0] for item in first_page_values)
        high = max(item[0][1] for item in first_page_values)
        prefix = target_prefix[field]
        result_fields[f"{prefix}_min"] = scalar_or_none(low)
        result_fields[f"{prefix}_max"] = scalar_or_none(high)
        result_sources[field] = [item[1] for item in first_page_values]
    if not result_fields:
        raise DeclarativeNotFound("document_ranges produced no values")
    return {"fields": result_fields, "field_sources": result_sources}


def peripheral_projection_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Reduce dictionary-labelled model values into canonical peripheral rows."""
    allowed = {"model_section", "fallback_section", "fields"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"peripheral_projection config has unknown keys: {sorted(unknown)}")
    model_section = config.get("model_section")
    if not isinstance(model_section, str) or model_section not in outputs:
        raise ValueError("peripheral_projection model_section must reference an executed section")
    model_output = outputs[model_section]
    if not isinstance(model_output, dict) or not isinstance(model_output.get("models"), list):
        raise ValueError("peripheral_projection model_section produced no model collection")
    fallback_section = config.get("fallback_section")
    if fallback_section is not None and (
        not isinstance(fallback_section, str) or fallback_section not in outputs
    ):
        raise ValueError(
            "peripheral_projection fallback_section must reference an executed section"
        )
    field_plan = config.get("fields")
    if not isinstance(field_plan, dict) or not field_plan:
        raise ValueError("peripheral_projection fields must be a non-empty object")
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    supported_parsers = {
        "first_integer",
        "sum_components",
        "sum_explicit_addends",
        "adc_instance_count",
    }
    supported_channel_parsers = {"adc_channel_count"}
    supported_reducers = {"max", "sum", "sum_distinct_labels"}
    rules: dict[str, dict[str, Any]] = {}
    semantic_to_canonical: dict[str, str] = {}
    canonical_supports_resolution: dict[str, bool] = {}
    for semantic, settings in field_plan.items():
        definition = dictionary.get("fields", {}).get(semantic)
        if (
            not semantic.startswith("peripheral_")
            or not isinstance(definition, dict)
            or not definition.get("canonical")
        ):
            raise ValueError(f"peripheral_projection field is not registered: {semantic}")
        if not isinstance(settings, dict):
            raise ValueError(f"peripheral_projection field {semantic} must be an object")
        unknown_settings = set(settings) - {
            "value_parser", "reducer", "channel_value_parser", "channel_reducer",
            "resolution_from_label", "bare_numeric_role",
        }
        if unknown_settings:
            raise ValueError(
                f"peripheral_projection field {semantic} has unknown settings: "
                f"{sorted(unknown_settings)}"
            )
        value_parser = settings.get("value_parser", "first_integer")
        reducer = settings.get("reducer", "max")
        channel_parser = settings.get("channel_value_parser")
        channel_reducer = settings.get("channel_reducer", "max")
        if value_parser not in supported_parsers:
            raise ValueError(f"unsupported peripheral value parser: {value_parser}")
        if reducer not in supported_reducers:
            raise ValueError(f"unsupported peripheral reducer: {reducer}")
        if channel_parser is not None and channel_parser not in supported_channel_parsers:
            raise ValueError(f"unsupported peripheral channel parser: {channel_parser}")
        if channel_reducer not in {"max", "sum"}:
            raise ValueError(f"unsupported peripheral channel reducer: {channel_reducer}")
        if "resolution_from_label" in settings and not isinstance(
            settings["resolution_from_label"], bool
        ):
            raise ValueError("peripheral_projection resolution_from_label must be boolean")
        if settings.get("bare_numeric_role") not in {None, "instance_count", "channel_count"}:
            raise ValueError(
                "peripheral_projection bare_numeric_role must be instance_count or channel_count"
            )
        canonical = str(definition["canonical"])
        if canonical in rules:
            raise ValueError(f"duplicate canonical peripheral mapping: {canonical}")
        rules[canonical] = dict(settings)
        semantic_to_canonical[semantic] = canonical
        canonical_supports_resolution[canonical] = bool(
            definition.get("supports_resolution", False)
        )

    def prepare_values(
        raw_values: list[dict[str, Any]], entity_key: str, default_page: int
    ) -> tuple[list[MappedCell], dict[str, list[dict[str, Any]]]]:
        mapped: list[MappedCell] = []
        sources: dict[str, list[dict[str, Any]]] = {}
        for raw_item in raw_values:
            semantic = raw_item.get("semantic_field") or match_common_field(
                str(raw_item.get("label") or ""), dictionary
            )
            canonical = semantic_to_canonical.get(str(semantic))
            if canonical is None:
                continue
            label = str(raw_item.get("label") or "")
            mapped.append(MappedCell(
                key=f"peripheral:{canonical}",
                label=label,
                value=str(raw_item.get("raw_value") or ""),
                page=int(raw_item.get("page") or default_page),
                column=0,
                row_label=label.rsplit("/", 1)[-1].strip(),
                source_cell_id=str(raw_item.get("source_cell_id") or ""),
                bbox=raw_item.get("bbox"),
                entity_key=entity_key,
            ))
            sources.setdefault(canonical, []).append(raw_item)
        return mapped, sources

    def contributors(
        canonical: str,
        raw_sources: list[dict[str, Any]],
        entity_key: str,
        default_page: int,
    ) -> list[dict[str, Any]]:
        parser_names = [rules[canonical].get("value_parser", "first_integer")]
        if rules[canonical].get("channel_value_parser"):
            parser_names.append(rules[canonical]["channel_value_parser"])
        result: list[dict[str, Any]] = []
        for source in raw_sources:
            label = str(source.get("label") or "")
            candidate = MappedCell(
                key=f"peripheral:{canonical}",
                label=label,
                value=str(source.get("raw_value") or ""),
                page=int(source.get("page") or default_page),
                column=0,
                row_label=label.rsplit("/", 1)[-1].strip(),
                source_cell_id=str(source.get("source_cell_id") or ""),
                bbox=source.get("bbox"),
                entity_key=entity_key,
            )
            if any(peripheral_count(candidate, parser) is not None for parser in parser_names):
                result.append(source)
        return result

    shared_values: list[dict[str, Any]] = []
    if fallback_section is not None and isinstance(outputs[fallback_section], dict):
        shared_values = list(outputs[fallback_section].get("values", []))
    records: list[dict[str, Any]] = []
    absence_markers = {
        compact(item).casefold()
        for item in dictionary.get("semantics", {}).get("absence_any", [])
        if compact(item)
    }
    for model in model_output["models"]:
        entity_key = str(model["model_id"])
        default_page = int(model.get("page") or 1)
        mapped, model_sources = prepare_values(
            list(model.get("values", [])), entity_key, default_page
        )
        shared_mapped, shared_sources = prepare_values(
            shared_values, entity_key, default_page
        )
        model_reduced = reduce_values(
            mapped,
            {"peripherals": rules},
            package_pattern=context.profile.package_re.pattern,
        )["peripherals"]
        shared_reduced = reduce_values(
            shared_mapped,
            {"peripherals": rules},
            package_pattern=context.profile.package_re.pattern,
        )["peripherals"]
        explicitly_absent = {
            canonical
            for canonical, sources in model_sources.items()
            if any(
                compact(source.get("raw_value") or "").casefold() in absence_markers
                for source in sources
            )
        }
        for canonical, sources in shared_sources.items():
            if not canonical_supports_resolution.get(canonical, False):
                continue
            resolutions = [
                int(match.group(1))
                for source in sources
                if (
                    match := re.search(
                        r"(\d+)\s*(?:[- ]?bit|位)",
                        compact(
                            f"{source.get('label', '')} {source.get('raw_value', '')}"
                        ),
                        re.I,
                    )
                )
            ]
            if resolutions:
                shared_reduced.setdefault(canonical, {})["resolution"] = max(resolutions)
        for canonical in sorted(set(model_reduced) | set(shared_reduced)):
            primary = model_reduced.get(canonical, {})
            if canonical in explicitly_absent and not primary:
                continue
            fallback = shared_reduced.get(canonical, {})
            item = {
                key: primary.get(key) if primary.get(key) is not None else fallback.get(key)
                for key in ("count", "channel_count", "resolution")
            }
            count_semantic = peripheral_count_field(dictionary, canonical)
            raw_count = item.get("count")
            instance_count = raw_count if count_semantic in {"instance_count", "mixed"} else None
            channel_count = (
                raw_count if count_semantic == "channel_count" else item.get("channel_count")
            )
            primary_contributors = contributors(
                canonical, model_sources.get(canonical, []), entity_key, default_page
            )
            fallback_contributors = contributors(
                canonical, shared_sources.get(canonical, []), entity_key, default_page
            )
            count_sources = (
                primary_contributors
                if primary.get("count") is not None or primary.get("channel_count") is not None
                else fallback_contributors
            )
            resolution_pool = (
                model_sources.get(canonical, [])
                if primary.get("resolution") is not None
                else shared_sources.get(canonical, [])
            )
            resolution_sources = [
                source
                for source in resolution_pool
                if canonical_supports_resolution.get(canonical, False)
                if re.search(
                    r"\d+\s*(?:[- ]?bit|位)",
                    compact(
                        f"{source.get('label', '')} {source.get('raw_value', '')}"
                    ),
                    re.I,
                )
            ]
            contributing_sources = list({
                id(source): source
                for source in [*count_sources, *resolution_sources]
            }.values())
            if not contributing_sources:
                raise ValueError(
                    f"peripheral_projection requires source evidence for {canonical} "
                    f"on {model['model_id']}"
                )
            records.append({
                "chip_id": model["model_id"],
                "peripheral_type": canonical,
                "instance_count": instance_count,
                "channel_count": channel_count,
                "max_resolution": item.get("resolution"),
                "max_frequency_mhz": None,
                "features": None,
                "value_sources": contributing_sources,
                "field_sources": {
                    "instance_count": count_sources if instance_count is not None else [],
                    "channel_count": count_sources if channel_count is not None else [],
                    "max_resolution": resolution_sources if item.get("resolution") is not None else [],
                },
            })
    if not records:
        raise DeclarativeNotFound("peripheral_projection produced no records")
    return {"records": records}


def document_values_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    _: dict[str, Any],
) -> dict[str, Any]:
    """Expose labelled numeric summary statements as document-scoped values."""
    allowed = {
        "fields", "scan_page_count", "statement_any", "association_window",
        "scope_exclude_any",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"document_values config has unknown keys: {sorted(unknown)}")
    fields = config.get("fields")
    statement_any = config.get("statement_any")
    scan_page_count = config.get("scan_page_count", len(context.page_texts))
    association_window = config.get("association_window", 32)
    scope_exclude_any = config.get("scope_exclude_any", [])
    if not isinstance(fields, list) or not fields:
        raise ValueError("document_values fields must be a non-empty array")
    if not isinstance(statement_any, list) or not statement_any:
        raise ValueError("document_values statement_any must be a non-empty array")
    if not isinstance(scan_page_count, int) or not 1 <= scan_page_count <= 20:
        raise ValueError("document_values scan_page_count must be an integer from 1 to 20")
    if not isinstance(association_window, int) or association_window < 8:
        raise ValueError("document_values association_window must be an integer of at least 8")
    if not isinstance(scope_exclude_any, list) or any(
        not isinstance(item, str) for item in scope_exclude_any
    ):
        raise ValueError("document_values scope_exclude_any must be an array of strings")
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    if any(
        field not in dictionary.get("fields", {}) or not field.startswith("peripheral_")
        for field in fields
    ):
        raise ValueError("document_values fields must name registered peripheral semantics")
    markers = [compact(item).casefold() for item in statement_any if compact(item)]
    scope_exclusions = [
        compact(item).casefold() for item in scope_exclude_any if compact(item)
    ]
    if not markers:
        raise ValueError("document_values statement_any contains no usable markers")
    values: list[dict[str, Any]] = []
    for page, text in enumerate(context.page_texts[:scan_page_count], start=1):
        for raw_line in text.splitlines():
            for raw_clause in re.split(r"[,，;；、]", raw_line):
                normalized = compact(raw_clause).casefold()
                if not re.search(r"\d", normalized) or not any(marker in normalized for marker in markers):
                    continue
                if any(marker in normalized for marker in scope_exclusions):
                    continue
                for semantic in fields:
                    definition = dictionary["fields"][semantic]
                    aliases = sorted(
                        {
                            compact(alias).casefold()
                            for alias in definition.get("aliases", [])
                            if compact(alias)
                        },
                        key=len,
                        reverse=True,
                    )
                    match = next(
                        (
                            alias_match
                            for alias in aliases
                            if (alias_match := re.search(re.escape(alias), normalized, re.I))
                        ),
                        None,
                    )
                    if match is None:
                        continue
                    start = max(0, match.start() - association_window)
                    end = min(len(normalized), match.end() + association_window)
                    fragment = normalized[start:end]
                    if not re.search(r"\d", fragment):
                        continue
                    if match_common_field(fragment, dictionary) != semantic:
                        continue
                    values.append({
                        "label": fragment,
                        "page": page,
                        "bbox": (0.0, 0.0, 0.0, 0.0),
                        "raw_value": fragment,
                        "source_cell_id": None,
                    })
    if not values:
        raise DeclarativeNotFound("document_values produced no values")
    return {"values": values}


def document_quantities_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    _: dict[str, Any],
) -> dict[str, Any]:
    allowed = {"fields", "scan_page_count", "association_window"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"document_quantities config has unknown keys: {sorted(unknown)}")
    fields = config.get("fields")
    if not isinstance(fields, list) or not fields or any(field not in FIELD_PARSERS for field in fields):
        raise ValueError("document_quantities fields must name registered semantic fields")
    if any(FIELD_PARSERS[field] != {"quantity"} for field in fields):
        raise ValueError("document_quantities supports quantity fields only")
    scan_page_count = config.get("scan_page_count", len(context.page_texts))
    association_window = config.get("association_window", 32)
    if not isinstance(scan_page_count, int) or scan_page_count < 1:
        raise ValueError("document_quantities scan_page_count must be a positive integer")
    if not isinstance(association_window, int) or association_window < 1:
        raise ValueError("document_quantities association_window must be a positive integer")
    dictionary = load_field_dictionary(
        context.profile.field_dictionary_file,
        context.profile.field_aliases,
    )
    unit_config = dictionary["units"]["memory_kb"]
    unit_aliases = sorted(
        (compact(alias) for alias in unit_config["aliases"] if compact(alias)),
        key=len,
        reverse=True,
    )
    unit_pattern = "|".join(re.escape(alias) for alias in unit_aliases)
    quantity_pattern = re.compile(rf"[-+]?\d+(?:\.\d+)?\s*(?:{unit_pattern})", re.I)
    values: dict[str, list[tuple[int | float, dict[str, Any], int]]] = {
        field: [] for field in fields
    }
    for page, text in enumerate(context.page_texts[:scan_page_count], start=1):
        for raw_line in text.splitlines():
            normalized = compact(raw_line)
            quantities = list(quantity_pattern.finditer(normalized))
            if not quantities:
                continue
            for field in fields:
                rule = dictionary["fields"].get(field, {})
                aliases = sorted(
                    {compact(alias) for alias in [*rule.get("exact_aliases", []), *rule.get("aliases", [])] if compact(alias)},
                    key=len,
                    reverse=True,
                )
                for alias in aliases:
                    for alias_match in re.finditer(re.escape(alias), normalized, re.I):
                        ranked = sorted(
                            (
                                max(alias_match.start() - item.end(), item.start() - alias_match.end(), 0),
                                item,
                            )
                            for item in quantities
                        )
                        if not ranked or ranked[0][0] > association_window:
                            continue
                        distance, quantity = ranked[0]
                        converted = convert_values(quantity.group(0), unit_config)
                        if len(converted) != 1:
                            continue
                        source = {
                            "label": alias,
                            "page": page,
                            "bbox": (0.0, 0.0, 0.0, 0.0),
                            "raw_value": raw_line,
                            "source_cell_id": None,
                            "normalized_value": converted[0],
                        }
                        values[field].append((converted[0], source, distance))
    result_fields: dict[str, int | float] = {}
    result_sources: dict[str, list[dict[str, Any]]] = {}
    for field, candidates in values.items():
        if not candidates:
            continue
        best_distance = min(item[2] for item in candidates)
        closest = [item for item in candidates if item[2] == best_distance]
        distinct = {item[0] for item in closest}
        if len(distinct) != 1:
            raise ValueError(f"document_quantities has ambiguous closest values for {field}")
        result_fields[field] = closest[0][0]
        result_sources[field] = [item[1] for item in closest if item[0] == closest[0][0]]
    if not result_fields:
        raise DeclarativeNotFound("document_quantities produced no values")
    return {"fields": result_fields, "field_sources": result_sources}


def pin_projection_component(
    context: DeclarativeContext,
    config: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Project package-aware pin definitions and optional AF matrices.

    The component is family-neutral: table recognition and column policy come
    from a named topology preset, while model/package identities come from an
    earlier model component and (when needed) an ordering-code relation.
    """
    allowed = {"model_section", "recipe_preset", "ordering_section", "pin_count_relation"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"pin_projection config has unknown keys: {sorted(unknown)}")
    model_section = config.get("model_section")
    recipe_preset = config.get("recipe_preset")
    if not isinstance(model_section, str) or model_section not in outputs:
        raise ValueError("pin_projection requires an executed model_section")
    if not isinstance(recipe_preset, str) or not recipe_preset:
        raise ValueError("pin_projection requires recipe_preset")
    matrix = outputs.get(model_section) or {}
    models = matrix.get("models", [])
    if not models:
        raise DeclarativeNotFound("pin_projection model section produced no models")
    preset_path: Path = context.profile.topology_preset(recipe_preset)
    preset = json.loads(preset_path.read_text(encoding="utf-8"))
    topologies = preset.get("topologies", {})
    definition_recipe = topologies.get("pin_definition")
    af_recipe = topologies.get("af_matrix")
    if not isinstance(definition_recipe, dict):
        raise ValueError("pin_projection preset must define pin_definition")

    layout = str(definition_recipe.get("layout", "model_package_column"))
    if layout not in {"model_package_column", "multi_package_columns"}:
        raise ValueError(f"pin_projection unknown layout: {layout}")
    if layout == "multi_package_columns":
        tables = extract_multi_package_pin_definitions(
            IRDocumentTableView(context.document),
            models=[
                {
                    "model_id": item["model_id"],
                    "package_types": item.get("fields", {}).get("package_types") or [],
                }
                for item in models
            ],
            pin_pattern=context.profile.pin_re,
            package_pattern=context.profile.package_re,
            normalize_function=context.profile.normalize_function,
            recipe=definition_recipe,
            table_index=context.table_index,
        )
        definitions = [definition for table in tables for definition in table["rows"]]
        records = []
        for definition in definitions:
            row = resolve_pin_mux([definition], {})[0].output_row()
            source = {
                "label": "pin_definition", "source_cell_id": definition.source_cell_id,
                "page": definition.page, "bbox": definition.bbox,
                "raw_value": f"{definition.pin_number} {definition.pin_name} {definition.function_name}",
            }
            records.append({
                **row, "value_sources": [source],
                "field_sources": {field: [source] for field, value in row.items() if value not in (None, "", [])},
            })
        if not records:
            raise DeclarativeNotFound("pin_projection found no pin-function records")
        return {"records": records, "identity_issues": pin_identity_issues(definitions), "af_observation_count": 0}

    af_observations = []
    if isinstance(af_recipe, dict):
        af_observations = observe_pin_af_matrices(
            context.document.tables,
            locator=af_recipe,
            pin_pattern=context.profile.pin_re,
            split_functions=lambda value, pin_name: split_functions(
                value, context.profile.normalize_function, pin_name
            ),
            identity_terms=[context.profile.series_for_model(item["model_id"]) for item in models],
        )
    af_lookup = unique_pin_af_lookup(af_observations)
    table_view = IRDocumentTableView(context.document)
    records: list[dict[str, Any]] = []
    definitions = []
    pin_count_rule = config.get("pin_count_relation")
    ordering_section = config.get("ordering_section")
    if pin_count_rule is not None:
        if not isinstance(pin_count_rule, dict) or set(pin_count_rule) != {"relation", "offset"}:
            raise ValueError("pin_projection pin_count_relation requires relation and offset")
        if not isinstance(ordering_section, str) or ordering_section not in outputs:
            raise ValueError("pin_projection pin_count_relation requires ordering_section")
    for item in models:
        model = item["model_id"]
        packages = list(item.get("fields", {}).get("package_types") or [])
        pin_count: int | list[int] | None = None
        if pin_count_rule is not None:
            offset = pin_count_rule["offset"]
            relation = str(pin_count_rule["relation"])
            if not isinstance(offset, int) or offset >= 0 or abs(offset) > len(model):
                raise ValueError("pin_projection pin count offset must be a valid negative integer")
            legend = outputs.get(str(ordering_section)) or {}
            raw_counts = legend.get(relation, {}).get(model[offset].upper())
            if isinstance(raw_counts, list):
                pin_count = [int(value) for value in raw_counts]
            elif raw_counts is not None:
                pin_count = int(raw_counts)
        tables = extract_pin_definitions(
            table_view,
            model=model,
            pin_pattern=context.profile.pin_re,
            package_pattern=context.profile.package_re,
            normalize_function=context.profile.normalize_function,
            series_identity=context.profile.series_for_model(model),
            recipe=definition_recipe,
            package_types=packages,
            pin_count=pin_count,
            table_index=context.table_index,
        )
        for table in tables:
            definitions.extend(table["rows"])
            for resolved in resolve_pin_mux(table["rows"], af_lookup):
                row = resolved.output_row()
                definition = resolved.definition
                definition_source = {
                    "label": "pin_definition",
                    "source_cell_id": definition.source_cell_id,
                    "page": definition.page,
                    "bbox": definition.bbox,
                    "raw_value": f"{definition.pin_number} {definition.pin_name} {definition.function_name}",
                }
                value_sources = [definition_source]
                field_sources = {
                    field: [definition_source]
                    for field, value in row.items()
                    if field != "af_code" and value not in (None, "", [])
                }
                if resolved.af_code is not None:
                    af_source = {
                        "label": "alternate_function",
                        "source_cell_id": resolved.af_source_cell_id,
                        "page": resolved.af_page,
                        "bbox": None,
                        "raw_value": f"{definition.pin_name} {definition.function_name} {resolved.af_code}",
                    }
                    value_sources.append(af_source)
                    field_sources["af_code"] = [af_source]
                records.append({**row, "value_sources": value_sources, "field_sources": field_sources})
    if not records:
        raise DeclarativeNotFound("pin_projection found no pin-function records")
    return {
        "records": records,
        "identity_issues": pin_identity_issues(definitions),
        "af_observation_count": len(af_observations),
    }


COMPONENTS: dict[str, Component] = {
    "ordering_code_legend": ordering_code_legend_component,
    "model_catalog": model_catalog_component,
    "model_matrix": model_matrix_component,
    "operating_conditions": operating_conditions_component,
    "memory_projection": memory_projection_component,
    "electrical_projection": electrical_projection_component,
    "document_quantities": document_quantities_component,
    "document_ranges": document_ranges_component,
    "peripheral_projection": peripheral_projection_component,
    "document_values": document_values_component,
    "pin_projection": pin_projection_component,
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
        try:
            value = component(context, _section_config(section, profile), outputs)
        except DeclarativeNotFound:
            if section.get("required", True):
                raise
            value = None
        if value is None and section.get("required", True):
            raise ValueError(f"required declarative section produced no result: {section_id}")
        outputs[section_id] = value
    return outputs
