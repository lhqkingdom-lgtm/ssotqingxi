from __future__ import annotations

import re
from typing import Any

from core.field_rules import observations_to_candidates
from core.ir import FieldCandidateIR, TableIR
from core.mapped_cell import MappedCell
from core.table_engine import TableGroup
from core.table_ir_builder import group_to_table_ir
from core.text import compact
from core.units import convert_values
from topologies.matrix_observer import (
    MatrixObservation,
    first_entity_column,
    observe_matrix,
    select_matrix,
    structural_column_headers,
    table_grids,
    text_direction_variants,
)
from topologies.model_column_matrix import numbers, wildcard_match


def is_stm32_header(value: str) -> bool:
    return "STM32" in compact(value).upper()


def ordering_suffix(model: str) -> tuple[str, str]:
    match = re.match(r"STM32[A-Z]{1,2}\d{2,3}([A-Z])([A-Z0-9])", model, re.I)
    if match:
        return match.group(1).upper(), match.group(2).upper()
    # Newer STM32 family stems are not always letters followed by a single
    # digit run (for example L4R5 and G0B1).  Their concrete device code still
    # ends with the package and density positions, so fall back to those two
    # positions without naming any particular series.
    fallback = re.fullmatch(r"STM32[A-Z0-9]{3,10}?([A-Z])([A-Z0-9])", model, re.I)
    if fallback:
        return fallback.group(1).upper(), fallback.group(2).upper()
    raise ValueError(f"unsupported STM32 ordering-code shape: {model}")


def header_model_patterns(header: str) -> list[str]:
    """Split a physical column header that lists multiple STM32 variants."""
    result = []
    for variant in text_direction_variants(header):
        for item in (item for item in re.split(r"(?=STM32)", variant) if item.startswith("STM32")):
            normalized = compact(item).upper()
            result.append(normalized)
            # ST feature matrices often compact variants that differ only in
            # the final ordering code, for example STM32H562AI/G. Expand that
            # structural shorthand to STM32H562AI and STM32H562AG.
            alternative = re.fullmatch(
                r"(STM32[A-Z]{1,2}\d{2,3}[A-Z0-9])([A-Z0-9])((?:/[A-Z0-9])+)",
                normalized,
            )
            if alternative:
                prefix = alternative.group(1)
                result.append(prefix + alternative.group(2))
                result.extend(prefix + suffix for suffix in alternative.group(3).split("/") if suffix)
            family_alternative = re.fullmatch(
                r"(STM32[A-Z]{1,2})(\d{2,3})((?:/\d{2,3})+)([A-Z0-9]+)",
                normalized,
            )
            if family_alternative:
                family = family_alternative.group(1)
                suffix = family_alternative.group(4)
                numbers = [family_alternative.group(2), *family_alternative.group(3).split("/")[1:]]
                result.extend(f"{family}{number}{suffix}" for number in numbers)
    return list(dict.fromkeys(result))


def discover_model_variants(
    rows: list[list[str]],
    seed_model: str,
    recipe: dict[str, Any],
    ordering_config: dict[str, Any],
) -> list[str]:
    """Enumerate concrete two-code STM32 models represented by a matrix.

    The filename-derived model is only a seed for resolving genuine wildcards;
    explicit matrix suffixes and model columns remain authoritative.
    """
    headers = structural_column_headers(rows, is_stm32_header)
    combined = compact(" ".join(headers.values())).upper()
    discovered: list[str] = []

    # Some matrices put the common family prefix only in the table title and
    # use compact column suffixes such as R5VI/R5VG.  Infer the missing prefix
    # from the seed only when one complete suffix is actually present, then
    # apply that suffix's character shape to sibling headers.
    target = compact(seed_model).upper()
    short_match = _best_short_header_suffix(headers, target)
    if short_match is not None:
        _, suffix = short_match
        prefix = target[: -len(suffix)]
        shape = "".join(r"\d" if char.isdigit() else "[A-Z]" for char in suffix)
        for _, header in sorted(headers.items()):
            if "STM32" in header:
                continue
            discovered.extend(prefix + token for token in re.findall(shape, header))

    suffix_base = re.search(r"(STM32[A-Z0-9]{3,10}?)_+", combined)
    suffixes = re.findall(r"_([A-Z0-9])([A-Z0-9])", combined)
    if suffix_base and suffixes:
        discovered.extend(suffix_base.group(1) + "".join(suffix) for suffix in suffixes)

    header_entries = sorted(headers.items())
    for _, header in header_entries:
        for pattern in header_model_patterns(header):
            if re.fullmatch(r"STM32[A-Z0-9]{3,10}?[A-WYZ0-9]{2}", pattern):
                discovered.append(pattern)

    aliases = [
        compact(item).lower()
        for item in recipe.get("fields", {}).get("flash_kb", {}).get("rows", ["flash"])
    ]
    density_row = next(
        (row for row in rows if any(alias in compact(" ".join(row[:2])).lower() for alias in aliases)),
        None,
    )
    flash_codes: dict[float, list[str]] = {}
    for code, value in ordering_config.get("flash_code_kb", {}).items():
        flash_codes.setdefault(float(value), []).append(str(code).upper())
    if density_row:
        unit_config = recipe.get("units", {}).get("memory_kb", {})
        density_label = " ".join(density_row[:2])
        # A wildcard family header commonly spans several adjacent density
        # columns.  The normalized grid keeps the continuation cells as empty
        # header entries; those cells must not terminate the owning header's
        # column range.
        model_header_entries = [
            (column, header)
            for column, header in header_entries
            if header_model_patterns(header)
        ]
        for position, (start, header) in enumerate(model_header_entries):
            end = (
                model_header_entries[position + 1][0]
                if position + 1 < len(model_header_entries)
                else len(density_row)
            )
            for pattern in header_model_patterns(header):
                if not re.fullmatch(r"STM32[A-Z0-9]{3,10}?[A-Z0-9X]{2}", pattern) or not pattern.endswith("X"):
                    continue
                resolved_prefix = "".join(
                    seed_model[index] if char == "X" and index < len(seed_model) else char
                    for index, char in enumerate(pattern[:-1])
                )
                for column in range(start, min(end, len(density_row))):
                    for value in convert_values(density_row[column], unit_config, density_label):
                        discovered.extend(resolved_prefix + code for code in flash_codes.get(float(value), []))

    concrete = [
        model for model in dict.fromkeys(discovered)
        if re.fullmatch(r"STM32[A-Z0-9]{5,12}", model)
    ]
    return concrete or [compact(seed_model).upper()]


def discover_device_summary_models(
    page_texts: list[str],
    ordering_config: dict[str, Any],
) -> list[str]:
    """Return concrete part numbers explicitly listed in device summaries."""
    package_codes = {
        str(code).upper()
        for code in ordering_config.get("pin_count_codes", {})
    }
    flash_codes = {
        str(code).upper()
        for code in ordering_config.get("flash_code_kb", {})
    }
    if not package_codes or not flash_codes:
        return []

    result: list[str] = []
    for text in page_texts:
        normalized = compact(text).lower()
        if "devicesummary" not in normalized and "器件汇总" not in normalized:
            continue
        for model in re.findall(r"STM32[A-Z]{1,2}\d{2,3}[A-Z0-9]{2}", text, re.I):
            candidate = compact(model).upper()
            package_code, flash_code = ordering_suffix(candidate)
            if package_code in package_codes and flash_code in flash_codes:
                result.append(candidate)
    return list(dict.fromkeys(result))


def package_header_observation(
    table: TableIR,
    observations: list[MatrixObservation],
    target_model: str,
    package_pattern: str,
) -> MatrixObservation | None:
    if not observations:
        return None
    target_column = observations[0].column
    for fragment, page, rows, cells in table_grids(table):
        for row_index, row in enumerate(rows[:3]):
            if target_column >= len(row):
                continue
            for variant in text_direction_variants(row[target_column]):
                match = re.search(package_pattern, variant, re.I)
                if not match:
                    continue
                source = cells.get((row_index, target_column))
                if source is None:
                    continue
                return MatrixObservation(
                    observation_id=f"OBS-{table.table_id}-f{fragment}-r{row_index}-c{target_column}-package-header",
                    entity_key=target_model,
                    source_table_id=table.table_id,
                    page=page,
                    row=row_index,
                    column=target_column,
                    label_path=("Package",),
                    row_label="Package",
                    raw_value=match.group(0),
                    source_cell_id=source.cell_id,
                    bbox=source.bbox,
                )
    return None


def trailing_wildcard_match(pattern: str, target: str) -> bool:
    prefix = pattern.rstrip("X")
    return len(prefix) < len(pattern) and "X" not in prefix and target.startswith(prefix)


def wildcard_family_prefix_match(pattern: str, target: str) -> bool:
    """Match a family header against a longer ordering code.

    Datasheet matrices may identify a product family (for example a wildcard
    device name) while the input filename carries package and option suffixes.
    """
    if "X" not in pattern:
        return False
    expression = re.escape(pattern).replace("X", "[A-Z0-9]")
    return bool(re.match(f"^{expression}", target, re.I))


def _best_short_header_suffix(headers: dict[int, str], target: str) -> tuple[int, str] | None:
    matches: list[tuple[int, int, str]] = []
    for column, header in headers.items():
        normalized = compact(header).upper()
        if not normalized or "STM32" in normalized:
            continue
        for length in range(3, min(8, len(target)) + 1):
            suffix = target[-length:]
            if suffix in normalized:
                matches.append((length, -column, suffix))
    if not matches:
        return None
    length, negative_column, suffix = max(matches)
    return -negative_column, suffix


def select_model_column(
    rows: list[list[str]],
    target_model: str,
    recipe: dict[str, Any],
    ordering_config: dict[str, Any],
) -> int:
    target = compact(target_model).upper()
    headers = structural_column_headers(rows, is_stm32_header)
    package_code, flash_code = ordering_suffix(target)
    expected_flash = ordering_config.get("flash_code_kb", {}).get(flash_code)
    aliases = [
        compact(item).lower()
        for item in recipe.get("fields", {}).get("flash_kb", {}).get("rows", ["flash"])
    ]
    density_row = next(
        (row for row in rows if any(alias in compact(" ".join(row[:2])).lower() for alias in aliases)),
        None,
    )
    unit_config = recipe.get("units", {}).get("memory_kb", {})
    density_label = " ".join(density_row[:2]) if density_row else ""

    def density_values(value: str) -> list[int | float]:
        return convert_values(value, unit_config, density_label)
    exact_direct = []
    wildcard_direct = []
    for column, header in headers.items():
        patterns = header_model_patterns(header)
        if any(target == pattern or target in pattern for pattern in patterns):
            exact_direct.append(column)
        elif any(
            trailing_wildcard_match(pattern, target) or wildcard_family_prefix_match(pattern, target)
            for pattern in patterns
        ):
            wildcard_direct.append(column)
    if exact_direct:
        return exact_direct[0]
    for start in wildcard_direct:
        later_groups = [
            column
            for column, header in headers.items()
            if column > start and any(is_stm32_header(variant) for variant in text_direction_variants(header))
        ]
        end = min(later_groups) if later_groups else len(density_row or [])
        if density_row and expected_flash is not None:
            for column in range(start, min(end, len(density_row))):
                if expected_flash in density_values(density_row[column]):
                    return column
        return start
    short_match = _best_short_header_suffix(headers, target)
    if short_match is not None:
        return short_match[0]
    combined = compact(" ".join(headers.values())).upper()
    suffixes = re.findall(r"_([A-Z0-9])([A-Z0-9])", combined)
    candidates = [index + 2 for index, suffix in enumerate(suffixes) if suffix == (package_code, flash_code)]
    if candidates:
        return candidates[0]
    family = target[:-2]
    family_header = f"{family}{package_code}X"
    starts = [
        column
        for column, header in headers.items()
        if family_header in header or wildcard_match(header, target)
    ]
    for start in starts:
        later_groups = [
            column
            for column, header in headers.items()
            if column > start and any(is_stm32_header(variant) for variant in text_direction_variants(header))
        ]
        end = min(later_groups) if later_groups else max(headers, default=start) + 1
        for column in range(start, end):
            if density_row and column < len(density_row) and expected_flash in density_values(density_row[column]):
                return column
        if density_row is None:
            return start
    if density_row and expected_flash is not None:
        density_matches = [column for column, value in enumerate(density_row) if expected_flash in density_values(value)]
        if len(density_matches) == 1:
            return density_matches[0]
    raise ValueError(f"cannot select target column for {target_model}")


def make_column_selector(ordering_config: dict[str, Any]):
    return lambda rows, target, recipe: select_model_column(rows, target, recipe, ordering_config)


def first_model_column(rows: list[list[str]], recipe: dict[str, Any]) -> int:
    del recipe
    return first_entity_column(rows, is_stm32_header)


def column_headers(rows: list[list[str]], recipe: dict[str, Any]) -> dict[int, str]:
    del recipe
    return structural_column_headers(rows, is_stm32_header)


def _candidates_to_mapped(candidates: list[FieldCandidateIR]) -> list[MappedCell]:
    result = []
    for candidate in candidates:
        prefix = "field" if candidate.field_group == "fields" else "peripheral"
        result.append(
            MappedCell(
                key=f"{prefix}:{candidate.field_name}",
                label=compact(" ".join(candidate.label_path)),
                value=candidate.raw_value,
                page=candidate.evidence_pages[0],
                column=0,
                row_label=candidate.row_label,
                source_cell_id=candidate.source_cell_ids[0],
                bbox=candidate.bboxes[0] if candidate.bboxes else None,
                entity_key=candidate.entity_key,
            )
        )
    return result


def map_model_matrix(
    group: TableGroup,
    target_model: str,
    recipe: dict[str, Any],
    ordering_config: dict[str, Any],
) -> list[MappedCell]:
    table = group_to_table_ir(group)
    observations = observe_matrix(table, target_model, recipe, make_column_selector(ordering_config), is_stm32_header)
    return _candidates_to_mapped(observations_to_candidates(observations, recipe, entity_type="chip"))


def select_model_group(
    groups: list[TableGroup],
    target_model: str,
    recipe: dict[str, Any],
    ordering_config: dict[str, Any],
) -> tuple[TableGroup, list[MappedCell]]:
    tables = [group_to_table_ir(group) for group in groups]
    selected, mapped = select_table_ir(tables, target_model, recipe, ordering_config)
    original = next(group for group in groups if group.key == selected.table_id)
    return original, mapped


def select_table_ir(
    tables: list[TableIR],
    target_model: str,
    recipe: dict[str, Any],
    ordering_config: dict[str, Any],
) -> tuple[TableIR, list[MappedCell]]:
    table, observations = select_observations(tables, target_model, recipe, ordering_config)
    candidates = observations_to_candidates(observations, recipe, entity_type="chip")
    return table, _candidates_to_mapped(candidates)


def select_observations(
    tables: list[TableIR],
    target_model: str,
    matrix_config: dict[str, Any],
    ordering_config: dict[str, Any],
) -> tuple[TableIR, list[MatrixObservation]]:
    return select_matrix(
        tables,
        target_model,
        matrix_config,
        make_column_selector(ordering_config),
        is_stm32_header,
        identity_terms=(target_model[:-2],),
    )
