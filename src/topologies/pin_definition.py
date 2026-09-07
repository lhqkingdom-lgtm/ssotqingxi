from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from core.header_paths import disambiguate_semantic_column, first_complete_header_prefix, select_column_by_header_path
from core.pin_ir import PinDefinitionIR
from core.qualifiers import preferred_qualifier_indices
from core.text import clean, compact
from core.validation import valid_pin_designator
from topologies.matrix_observer import text_direction_variants


def _equal_with_one_adjacent_transposition(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) != len(right):
        return False
    differences = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
    return (
        len(differences) == 2
        and differences[1] == differences[0] + 1
        and left[differences[0]] == right[differences[1]]
        and left[differences[1]] == right[differences[0]]
    )


def _equal_with_one_character_move(left: str, right: str) -> bool:
    """Allow the local ordering defect produced by vertical PDF glyph runs."""
    left_match = re.fullmatch(r"([A-Z]+)(\d+)", left)
    right_match = re.fullmatch(r"([A-Z]+)(\d+)", right)
    if not left_match or not right_match or left_match.group(2) != right_match.group(2):
        return False
    source, target = left_match.group(1), right_match.group(1)
    if len(source) != len(target) or sorted(source) != sorted(target):
        return False
    for source_index, character in enumerate(source):
        remainder = source[:source_index] + source[source_index + 1 :]
        for target_index in range(len(source)):
            if remainder[:target_index] + character + remainder[target_index:] == target:
                return True
    return False


def _contains_package_header(value: str, expected_package: str) -> bool:
    normalized = compact(value).upper()
    if expected_package in normalized:
        return True
    # Datasheet pin tables sometimes shorten a concrete ball-grid package
    # (for example UFBGA132) to BGA132, while other columns retain vendor
    # qualifiers.  Bind by the BGA class and exact ball count, then let the
    # existing qualifier policy distinguish unqualified/SMPS variants.  Do
    # not apply this relaxation to leaded packages, whose prefixes are not
    # interchangeable.
    expected_bga = re.fullmatch(r"[A-Z]*BGA(\d+)", expected_package)
    if expected_bga and re.search(rf"(?:^|_)[A-Z]*BGA{re.escape(expected_bga.group(1))}(?:_|$)", normalized):
        return True
    width = len(expected_package)
    return any(
        _equal_with_one_adjacent_transposition(normalized[index : index + width], expected_package)
        or _equal_with_one_character_move(normalized[index : index + width], expected_package)
        for index in range(max(0, len(normalized) - width + 1))
    )


def _package_header_columns(
    headers: list[Any],
    pin_counts: tuple[int, ...],
    expected_packages: list[str],
) -> list[int]:
    matches: list[int] = []
    package_kinds = ("LQFP", "UFQFPN", "TSSOP", "VFQFPN", "BGA", "WLCSP")
    for index, cell in enumerate(headers):
        for value in text_direction_variants(clean(cell)):
            if (
                any(_contains_package_header(value, package) for package in expected_packages)
                or (
                    not expected_packages
                    and any(str(item) in value for item in pin_counts)
                    and any(package in value for package in package_kinds)
                )
            ):
                matches.append(index)
                break
    return list(dict.fromkeys(matches))


def package_column(
    headers: list[Any],
    pin_count: int | Iterable[int] | None = None,
    package_types: Iterable[str] = (),
    qualifier_preferences: Iterable[str] = (),
) -> int | None:
    pin_counts = (
        tuple(int(item) for item in pin_count)
        if isinstance(pin_count, (list, tuple, set))
        else (() if pin_count is None else (int(pin_count),))
    )
    expected_packages = list(dict.fromkeys(compact(item).upper() for item in package_types if compact(item)))
    for expected_package in expected_packages:
        matches = _package_header_columns(headers, pin_counts, [expected_package])
        if len(matches) > 1:
            preferences = [compact(item).lower() for item in qualifier_preferences if compact(item)]
            selected_indices = preferred_qualifier_indices(
                [clean(headers[index]) for index in matches],
                preferences,
            )
            matches = [matches[index] for index in selected_indices]
            return matches[0] if len(matches) == 1 else None
        if matches:
            return matches[0]
    pin_count_matches = _package_header_columns(headers, pin_counts, [])
    return pin_count_matches[0] if len(set(pin_count_matches)) == 1 else None


def split_functions(
    value: object,
    normalize_function: Callable[[str], str],
    pin_name: str,
    *,
    wrapped_cell: bool = False,
) -> list[str]:
    text = str(value or "").replace("\r", "")
    if wrapped_cell:
        text = re.sub(r"\s*\n\s*", "", text)
    text = re.sub(r"(?<=_)\n\s*", "", text)
    text = re.sub(r"(?<=[A-Za-z0-9])\s*\n\s*(?=_)", "", text)
    text = re.sub(r"-\s*\n\s*", "-", text)
    text = re.sub(r"(?<=[A-Za-z])\s*\n\s*(?=\d+_)", "", text)
    text = re.sub(r"\b([A-Za-z])\s*\n\s*(?=[A-Za-z]{2,}\d)", r"\1", text)
    text = re.sub(r"\s*\n\s*(?=[A-Za-z](?:\s*[,/]|\s*$))", "", text)
    text = re.sub(r"\(\d+\)", "", text)
    values = []
    for part in re.split(r"[,/\n]", text):
        function = normalize_function(part)
        if (
            function
            and function not in {"-", "--", pin_name, f"({pin_name})"}
            and not function.endswith("-")
            and not re.fullmatch(r"\(?\d+\)?", function)
            and function not in values
        ):
            values.append(function)
    return values


def infer_pin_header_depth(
    rows: list[list[Any]],
    *,
    header_terms: Iterable[str],
    header_term_groups: Iterable[Iterable[str]],
    package_targets: Iterable[str | None],
    pin_count: int | Iterable[int] | None,
    qualifier_preferences: Iterable[str] = (),
) -> int | None:
    normalized_terms = tuple(compact(item).lower() for item in header_terms)
    normalized_groups = tuple(tuple(compact(item).lower() for item in group) for group in header_term_groups)

    def complete(prefix: list[list[Any]]) -> bool:
        header_text = compact(
            " ".join(
                variant for row in prefix for cell in row for variant in text_direction_variants(clean(cell))
            )
        ).lower()
        if not all(term in header_text for term in normalized_terms):
            return False
        if not all(all(term in header_text for term in group) for group in normalized_groups):
            return False
        pin_counts = (
            tuple(int(item) for item in pin_count)
            if isinstance(pin_count, (list, tuple, set))
            else (() if pin_count is None else (int(pin_count),))
        )
        # Header completeness needs proof that a package column exists, not a
        # premature unique choice.  Repeated package names can be disambiguated
        # only after the full merged header path (including model family) is
        # available.
        return any(
            _package_header_columns(
                list(row),
                pin_counts,
                [compact(target).upper()] if target else [],
            )
            for target in package_targets
            for row in prefix
        )

    return first_complete_header_prefix(rows, complete)


def _header_matches(value: str, terms: Iterable[str], groups: Iterable[Iterable[str]]) -> bool:
    normalized = compact(value).lower()
    return any(term in normalized for term in terms) or any(all(term in normalized for term in group) for group in groups)


def first_pin_data_row(rows: list[list[Any]], identity_header_aliases: Iterable[str]) -> int | None:
    """Find data start from a semantic identity header and a numeric lead column."""
    def plausible_identity(value: Any) -> bool:
        token = compact(value).upper()
        if not token or re.fullmatch(r"[-–—]+|\d+|\([A-Z0-9]+\)", token):
            return False
        # Pin identities are normally alphanumeric labels (VDD, NRST, PA0,
        # P0.5).  Requiring a real label prevents vertical package-header
        # fragments such as ``8`` and ``(A)`` from ending the header early.
        return bool(re.search(r"[A-Z]{2,}|P\d", token))

    aliases = tuple(compact(item).upper() for item in identity_header_aliases if compact(item))
    for header_row, row in enumerate(rows):
        for declared_col, cell in enumerate(row):
            normalized = compact(cell).upper()
            if not any(alias in normalized for alias in aliases):
                continue
            candidate_cols = [declared_col]
            if declared_col:
                candidate_cols.append(declared_col - 1)
            if declared_col + 1 < len(row):
                candidate_cols.append(declared_col + 1)
            for row_index in range(header_row + 1, len(rows)):
                current = rows[row_index]
                has_identity = any(
                    column < len(current) and plausible_identity(current[column])
                    for column in candidate_cols
                )
                has_number = any(
                    re.fullmatch(r"\d+", compact(value))
                    for value in current[:declared_col]
                )
                if has_identity and has_number:
                    return row_index
    return None


def headers_spanning_value_columns(
    raw_rows: list[list[Any]],
    cell_bboxes: dict[tuple[int, int], tuple[float, float, float, float] | None],
    keep_cols: list[int],
    header_end: int,
    value_cols: list[int],
    token_pattern: re.Pattern[str],
    normalize_token: Callable[[str], str] = lambda value: compact(value).upper(),
) -> dict[int, list[str]]:
    """Map narrow header tokens to wider value cells by horizontal overlap."""
    header_cells: list[tuple[str, float, float]] = []
    for row_index in range(min(header_end, len(raw_rows))):
        for raw_col, value in enumerate(raw_rows[row_index]):
            bbox = cell_bboxes.get((row_index, raw_col))
            if not value or bbox is None:
                continue
            for match in token_pattern.finditer(compact(value)):
                token = normalize_token(match.group(0))
                if token:
                    header_cells.append((token, float(bbox[0]), float(bbox[2])))

    result: dict[int, list[str]] = {}
    for column in value_cols:
        raw_col = keep_cols[column]
        value_span: tuple[float, float] | None = None
        for row_index in range(header_end, len(raw_rows)):
            if raw_col >= len(raw_rows[row_index]) or not re.fullmatch(r"\d+", compact(raw_rows[row_index][raw_col])):
                continue
            bbox = cell_bboxes.get((row_index, raw_col))
            if bbox is not None:
                value_span = (float(bbox[0]), float(bbox[2]))
                break
        if value_span is None:
            continue
        x0, x1 = value_span
        tokens = [
            token
            for token, token_x0, token_x1 in header_cells
            if x0 <= (token_x0 + token_x1) / 2 <= x1
        ]
        if tokens:
            result[column] = list(dict.fromkeys(tokens))
    return result


def extract_pin_definitions(
    pdf: Any,
    *,
    model: str,
    pin_pattern: re.Pattern[str],
    normalize_function: Callable[[str], str],
    series_identity: str,
    recipe: dict[str, Any],
    package_types: Iterable[str] = (),
    pin_count: int | Iterable[int] | None = None,
    table_index: Any | None = None,
) -> list[dict[str, Any]]:
    packages = list(package_types)
    package_targets: list[str | None] = packages[:1] or [None]
    if not packages and not pin_count:
        return []
    preferences = recipe["column_qualifier_preferences"]
    header_terms = [compact(item).lower() for item in recipe["required_header_terms"]]
    header_groups = [[compact(item).lower() for item in group] for group in recipe["required_header_groups"]]
    indexed_pages: set[int] = set()
    if table_index is not None:
        for entry in table_index.entries:
            indexed_text = entry.normalized_text
            if "pin_like" in entry.features and all(term in indexed_text for term in header_terms) and all(
                all(term in indexed_text for term in group) for group in header_groups
            ):
                indexed_pages.update(entry.pages)
    pin_name_terms = [compact(item).lower() for item in recipe["pin_name_aliases"]]
    pin_name_groups = [[compact(item).lower() for item in group] for group in recipe["pin_name_alias_groups"]]
    alternate_terms = [compact(item).lower() for item in recipe["function_aliases"]]
    default_terms = [compact(item).lower() for item in recipe["default_function_aliases"]]
    result = []
    for page_index, page in enumerate(pdf.pages):
        if table_index is not None and page_index + 1 not in indexed_pages:
            continue
        for table in page.find_tables():
            rows = table.extract()
            if len(rows) < 3:
                continue
            depth = infer_pin_header_depth(
                rows,
                header_terms=header_terms,
                header_term_groups=header_groups,
                package_targets=package_targets,
                pin_count=pin_count,
                qualifier_preferences=preferences,
            )
            if depth is None:
                continue
            header_rows = rows[:depth]
            selections: list[tuple[int, int, str | None]] = []
            for target_package in package_targets:
                search_packages = [target_package] if target_package else []
                for header_index, package_headers in enumerate(header_rows):
                    selected = select_column_by_header_path(
                        table.column_header_paths(header_index + 1),
                        leaf_terms=tuple(search_packages),
                        identity_terms=(series_identity,),
                        qualifier_preferences=tuple(preferences),
                    )
                    candidate = package_column(package_headers, pin_count, search_packages, preferences)
                    if candidate is None and selected is None:
                        continue
                    selection = (header_index, selected if selected is not None else candidate, target_package)
                    if selection not in selections:
                        selections.append(selection)
                    break
            for header_index, column, target_package in selections:
                semantic_rows = rows[: header_index + 1]
                header_band = [
                    compact(
                        " ".join(
                            variant
                            for row in semantic_rows
                            if index < len(row)
                            for variant in text_direction_variants(clean(row[index]))
                        )
                    ).lower()
                    for index in range(max(len(row) for row in semantic_rows))
                ]
                candidates = tuple(
                    index for index, cell in enumerate(header_band) if _header_matches(cell, pin_name_terms, pin_name_groups)
                )
                pin_name_column = disambiguate_semantic_column(
                    candidates,
                    table.column_header_paths(header_index + 1),
                    exclude_terms=tuple(compact(item).lower() for item in recipe["pin_name_exclude"]),
                    preference=str(recipe["pin_name_preference"]),
                )
                if pin_name_column is None:
                    continue
                alternate_columns = [index for index, cell in enumerate(header_band) if any(term in cell for term in alternate_terms)]
                function_columns = list(range(alternate_columns[0], len(rows[0]))) if alternate_columns else []
                default_column = next((index for index, cell in enumerate(header_band) if any(term in cell for term in default_terms)), None)
                page_rows = []
                for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 1):
                    if column >= len(row) or pin_name_column >= len(row):
                        continue
                    raw_number = clean(row[column])
                    pin_match = pin_pattern.search(clean(row[pin_name_column]))
                    if not pin_match or not valid_pin_designator(compact(raw_number)):
                        continue
                    pin_number: int | str = int(raw_number) if re.fullmatch(r"\d+", compact(raw_number)) else compact(raw_number).upper()
                    pin_name = pin_match.group(0).upper()
                    functions = []
                    if default_column is not None and default_column < len(row):
                        functions.extend(split_functions(row[default_column], normalize_function, pin_name))
                    for function_column in function_columns:
                        if function_column < len(row):
                            functions.extend(split_functions(row[function_column], normalize_function, pin_name))
                    for function in dict.fromkeys(functions):
                        page_rows.append(
                            PinDefinitionIR(
                                chip_id=model,
                                package_type=target_package,
                                pin_name=pin_name,
                                pin_number=pin_number,
                                function_name=function,
                                default_function=pin_name,
                                source_table_id=getattr(table, "table_id", ""),
                                source_cell_id=table.cell_id(row_index, pin_name_column) if hasattr(table, "cell_id") else "",
                                page=page_index + 1,
                                bbox=tuple(float(value) for value in table.bbox),
                            )
                        )
                if page_rows:
                    result.append({"page": page_index + 1, "bbox": table.bbox, "rows": page_rows})
    return result
