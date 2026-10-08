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
from topologies.package_matrix import parse_package_names


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


def _equal_with_one_alpha_omission(left: str, right: str) -> bool:
    """Accept one dropped glyph in a vertical package prefix, never its count."""
    left_match = re.fullmatch(r"([A-Z]+)(\d+)", left)
    right_match = re.fullmatch(r"([A-Z]+)(\d+)", right)
    if not left_match or not right_match or left_match.group(2) != right_match.group(2):
        return False
    source, target = left_match.group(1), right_match.group(1)
    # ``left`` is observed PDF text and ``right`` is the declared package.
    # Only a glyph missing from the observation is recoverable. Treating the
    # comparison symmetrically would make LFBGA and UFBGA indistinguishable
    # after dropping their differing first character.
    if len(source) + 1 != len(target):
        return False
    return any(target[:index] + target[index + 1 :] == source for index in range(len(target)))


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
    if expected_bga and re.search(
        rf"(?<![A-Z])BGA{re.escape(expected_bga.group(1))}(?!\d)", normalized
    ):
        return True
    width = len(expected_package)
    same_width_match = any(
        _equal_with_one_adjacent_transposition(normalized[index : index + width], expected_package)
        or _equal_with_one_character_move(normalized[index : index + width], expected_package)
        for index in range(max(0, len(normalized) - width + 1))
    )
    if same_width_match:
        return True
    # PDF text extraction can lose one glyph from a vertical alphabetic
    # package prefix (for example UFBGA100 -> UFBG100).  The numerical count
    # remains an exact structural guard, so this cannot turn a 144-ball column
    # into a 100-ball match.
    shorter_width = max(1, width - 1)
    return any(
        _equal_with_one_alpha_omission(normalized[index : index + shorter_width], expected_package)
        for index in range(max(0, len(normalized) - shorter_width + 1))
        if index == 0 or not normalized[index - 1].isalpha()
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
    package_pattern: re.Pattern[str],
    normalize_function: Callable[[str], str],
    series_identity: str,
    recipe: dict[str, Any],
    package_types: Iterable[str] = (),
    pin_count: int | Iterable[int] | None = None,
    table_index: Any | None = None,
) -> list[dict[str, Any]]:
    packages = list(package_types)
    package_targets: list[str | None] = packages or [None]
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
            table_package_targets = list(package_targets)
            pin_counts = {
                int(item)
                for item in (
                    pin_count
                    if isinstance(pin_count, (list, tuple, set))
                    else (() if pin_count is None else (pin_count,))
                )
            }
            if pin_counts:
                # The physical pin table is authoritative for package-column
                # identity. A summary elsewhere in the document can be stale;
                # discover every package header whose source-declared lead or
                # ball count matches the model instead of silently choosing the
                # first summary package.
                for header_row in header_rows:
                    for cell in header_row:
                        for variant in text_direction_variants(clean(cell)):
                            for match in package_pattern.finditer(variant):
                                package = compact(match.group(0)).upper()
                                count = re.search(r"\d+", package)
                                if (
                                    count
                                    and int(count.group(0)) in pin_counts
                                    and package not in table_package_targets
                                ):
                                    table_package_targets.append(package)
            selections: list[tuple[int, int, str | None]] = []
            for target_package in table_package_targets:
                search_packages = [target_package] if target_package else []
                # Bind columns only after the complete semantic header has
                # been seen.  A merged parent can contain ``Pin (package)``
                # while its child row contains ``Number`` and the sibling
                # identity column contains ``Name``; stopping at the parent
                # row loses that identity and yields a false empty result.
                header_index = depth - 1
                selected = select_column_by_header_path(
                    table.column_header_paths(depth),
                    leaf_terms=tuple(search_packages),
                    identity_terms=(series_identity,),
                    qualifier_preferences=tuple(preferences),
                )
                fallback_candidates = [
                    candidate
                    for package_headers in header_rows
                    for candidate in [package_column(package_headers, pin_count, search_packages, preferences)]
                    if candidate is not None
                ]
                candidate = fallback_candidates[0] if len(set(fallback_candidates)) == 1 else None
                if candidate is None and selected is None:
                    continue
                selection = (header_index, selected if selected is not None else candidate, target_package)
                if selection not in selections:
                    selections.append(selection)
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


def extract_multi_package_pin_definitions(
    pdf: Any,
    *,
    models: list[dict[str, Any]],
    pin_pattern: re.Pattern[str],
    package_pattern: re.Pattern[str],
    normalize_function: Callable[[str], str],
    recipe: dict[str, Any],
    table_index: Any,
) -> list[dict[str, Any]]:
    """Extract pin functions from matrices with one physical-pin column per package.

    This topology is driven entirely by header aliases, package syntax and
    geometry. Continued pages inherit column identity by normalized horizontal
    position; no page, model, package size or expected pin is embedded here.
    """
    required = {
        "required_any", "minimum_pin_tokens", "identity_header_aliases",
        "default_function_header_aliases", "default_function_fallback",
    }
    missing = required - set(recipe)
    if missing:
        raise ValueError(f"multi-package pin recipe missing keys: {sorted(missing)}")
    by_package: dict[str, list[str]] = {}
    model_packages: dict[str, list[str]] = {}
    for item in models:
        model_id = str(item["model_id"])
        packages = list(dict.fromkeys(
            compact(package).upper()
            for package in item.get("package_types", [])
            if compact(package)
        ))
        model_packages[model_id] = packages
        for package in packages:
            by_package.setdefault(package, []).append(model_id)
    required_any = [compact(value).casefold() for value in recipe["required_any"]]
    candidate_pages = {
        page
        for entry in table_index.entries
        if entry.pin_like_cells >= int(recipe["minimum_pin_tokens"])
        and (not required_any or any(term in entry.normalized_text for term in required_any))
        for page in entry.pages
    }
    identity_aliases = tuple(recipe["identity_header_aliases"])
    default_aliases = [compact(value).upper() for value in recipe["default_function_header_aliases"]]
    function_aliases = [
        compact(value).upper() for value in recipe.get("function_header_aliases", [])
        if compact(value)
    ]
    function_code_pattern = recipe.get("function_code_pattern")
    try:
        function_code_re = re.compile(str(function_code_pattern), re.I) if function_code_pattern else None
    except re.error as exc:
        raise ValueError(f"invalid multi-package function_code_pattern: {exc}") from exc
    annotation_prefixes = [
        compact(value).upper()
        for value in recipe.get("function_annotation_prefixes", [])
        if compact(value)
    ]
    fallback_default = str(recipe["default_function_fallback"])
    tolerance = float(recipe.get("continuation_position_tolerance", 0.035))
    variant_pattern = re.compile(str(recipe.get("variant_pattern", r"\(\s*([A-Z0-9]+)\s*\)")), re.I)
    active_columns: dict[float, tuple[list[str], str]] = {}
    extracted: list[dict[str, Any]] = []

    for page_index, page in enumerate(pdf.pages, start=1):
        if page_index not in candidate_pages:
            continue
        for table in page.find_tables():
            raw_rows = table.extract()
            if not raw_rows or len(raw_rows) < 3:
                continue
            width = max(len(row) for row in raw_rows)
            keep = [column for column in range(width) if any(column < len(row) and clean(row[column]) for row in raw_rows)]
            if len(keep) < 4:
                continue
            rows = [[clean(row[column]) if column < len(row) else "" for column in keep] for row in raw_rows]
            data_start = first_pin_data_row(rows, identity_aliases)
            declared_header_rows = [
                row_index
                for row_index, row in enumerate(rows)
                if any(
                    alias in compact(cell).upper()
                    for cell in row
                    for alias in (*identity_aliases, *function_aliases)
                )
            ]
            if declared_header_rows:
                declared_data_start = max(declared_header_rows) + 1
                data_start = (
                    declared_data_start
                    if data_start is None
                    else min(data_start, declared_data_start)
                )
            if data_start is None:
                data_start = next((
                    index for index, row in enumerate(rows)
                    if any(pin_pattern.search(value) for value in row)
                    and any(re.fullmatch(r"\d+", compact(value)) for value in row)
                ), None)
            if data_start is None:
                continue
            headers = rows[:data_start]
            header_by_column = [compact(" ".join(row[column] for row in headers)).upper() for column in range(len(keep))]
            declared_pin_column = next((
                column for column, header in enumerate(header_by_column)
                if any(compact(alias).upper() in header for alias in identity_aliases)
            ), None)
            pin_scores = {
                column: sum(bool(pin_pattern.search(row[column])) for row in rows[data_start:] if column < len(row))
                for column in range(len(keep))
            }
            if declared_pin_column is not None and pin_scores.get(declared_pin_column):
                pin_column = declared_pin_column
            else:
                best = max(pin_scores.values(), default=0)
                candidates = [column for column, score in pin_scores.items() if score == best and score]
                pin_column = min(candidates, key=lambda value: (abs(value - (declared_pin_column or 0)), value)) if candidates else None
            if pin_column is None:
                continue
            declared_function = next((
                column for column, header in enumerate(header_by_column)
                if any(alias in header for alias in function_aliases)
            ), None)
            declared_default = next((
                column for column, header in enumerate(header_by_column)
                if any(alias in header for alias in default_aliases)
            ), None)
            if declared_default is None:
                default_column = len(keep) - 1
            else:
                populated = lambda column: sum(column < len(row) and bool(compact(row[column])) for row in rows[data_start:])
                adjacent = [column for column in (declared_default - 1, declared_default + 1) if column >= 0 and populated(column)]
                default_column = declared_default if populated(declared_default) else (adjacent[0] if adjacent else declared_default)
            semantic_columns = {
                column for column in (pin_column, declared_function, default_column)
                if column is not None
            }
            number_columns = [
                column for column in range(len(keep))
                if column not in semantic_columns
                and any(
                    column < len(row) and valid_pin_designator(compact(row[column]))
                    for row in rows[data_start:]
                )
            ]
            spanned = headers_spanning_value_columns(
                raw_rows, table.cell_bboxes(), keep, data_start, number_columns,
                package_pattern, lambda value: re.sub(r"\([^)]*\)", "", compact(value)).upper(),
            )
            packages_by_column: dict[int, list[str]] = {column: [] for column in number_columns}
            variants_by_column: dict[int, str] = {column: "" for column in number_columns}
            header_metadata: dict[int, tuple[list[str], str]] = {}
            for column, header in enumerate(header_by_column):
                packages = list(dict.fromkeys(
                    re.sub(r"\([^)]*\)", "", compact(package)).upper()
                    for header_row in headers
                    if column < len(header_row)
                    for package in parse_package_names(
                        header_row[column], package_pattern.pattern
                    )
                ))
                variant = variant_pattern.search(header)
                header_metadata[column] = (packages, variant.group(1).upper() if variant else "")
            for column in number_columns:
                packages_by_column[column], variants_by_column[column] = header_metadata.get(column, ([], ""))
                for package in spanned.get(column, []):
                    if package not in packages_by_column[column]:
                        packages_by_column[column].append(package)
            unmapped = [column for column in number_columns if not packages_by_column[column]]
            detached = [column for column, (packages, _) in header_metadata.items() if packages and column not in number_columns]
            if unmapped and len(unmapped) == len(detached):
                for target, source in zip(sorted(unmapped), sorted(detached)):
                    packages_by_column[target], variants_by_column[target] = header_metadata[source]
            x0, _, x1, _ = (float(value) for value in table.bbox)
            positions: dict[int, float] = {}
            if x1 > x0:
                for column in number_columns:
                    raw_column = keep[column]
                    for row_index in range(data_start, len(raw_rows)):
                        cell = table.cell_bboxes().get((row_index, raw_column))
                        if (
                            cell and raw_column < len(raw_rows[row_index])
                            and valid_pin_designator(compact(raw_rows[row_index][raw_column]))
                        ):
                            positions[column] = ((cell[0] + cell[2]) / 2 - x0) / (x1 - x0)
                            break
            if any(packages_by_column.values()) or any(variants_by_column.values()):
                active_columns.clear()
            for column in number_columns:
                if not packages_by_column[column] and column in positions and active_columns:
                    nearest = min(active_columns, key=lambda position: abs(position - positions[column]))
                    if abs(nearest - positions[column]) <= tolerance:
                        packages_by_column[column], variants_by_column[column] = active_columns[nearest]
                if column in positions and (packages_by_column[column] or variants_by_column[column]):
                    active_columns[positions[column]] = (list(packages_by_column[column]), variants_by_column[column])

            inferred: dict[tuple[int, str], list[str]] = {}
            for package in {value for values in packages_by_column.values() for value in values}:
                columns = [column for column in number_columns if package in packages_by_column[column]]
                variants = [(variants_by_column[column], column) for column in columns]
                package_models = sorted(by_package.get(package, []), key=str.upper)
                if len(columns) > 1 and len(package_models) == len(columns) and all(value for value, _ in variants) and len({value for value, _ in variants}) == len(columns):
                    for model_id, (_, column) in zip(package_models, sorted(variants)):
                        inferred[(column, package)] = [model_id]
            page_rows: list[PinDefinitionIR] = []
            for row_index, row in enumerate(rows[data_start:], start=data_start):
                pin_cell = row[pin_column]
                match = pin_pattern.search(pin_cell)
                if not match:
                    continue
                pin_name = match.group(0).upper()
                raw_function_cell = (
                    row[declared_function]
                    if declared_function is not None and declared_function < len(row)
                    else pin_cell
                )
                functions_with_codes: list[tuple[str, str | None]] = []
                for part in re.split(
                    str(recipe.get("function_separator_pattern", r"[/,，]+")),
                    compact(raw_function_cell),
                ):
                    # Boot-mode annotations describe conditions, not additional
                    # mux functions. The marker is structural and configured
                    # function identities remain untouched.
                    normalized_part = compact(part).lstrip("(").upper()
                    if any(normalized_part.startswith(prefix) for prefix in annotation_prefixes):
                        continue
                    code_match = function_code_re.search(part) if function_code_re else None
                    af_code = code_match.group(1) if code_match and code_match.lastindex else (
                        code_match.group(0) if code_match else None
                    )
                    if code_match:
                        part = function_code_re.sub("", part)
                    function = normalize_function(part)
                    if not function or function == pin_name or pin_pattern.fullmatch(function):
                        continue
                    candidate = (function, af_code)
                    if candidate not in functions_with_codes:
                        functions_with_codes.append(candidate)
                functions = functions_with_codes
                if not functions:
                    continue
                default_function = row[default_column] or fallback_default
                for column in number_columns:
                    number = compact(row[column])
                    if not valid_pin_designator(number):
                        continue
                    packages = packages_by_column[column]
                    variant = variants_by_column[column]
                    model_ids = [
                        model_id for model_id, declared_packages in model_packages.items()
                        if variant and model_id.upper().endswith(variant)
                        and (not packages or any(package in packages for package in declared_packages))
                    ]
                    if not model_ids:
                        model_ids = list(dict.fromkeys(
                            model_id for package in packages
                            for model_id in inferred.get((column, package), by_package.get(package, []))
                        ))
                    if not model_ids and len(models) == 1:
                        only = str(models[0]["model_id"])
                        declared_packages = model_packages[only]
                        if (
                            len(declared_packages) == 1
                            and (
                                not packages
                                or any(declared_packages[0].startswith(item) for item in packages)
                            )
                        ):
                            model_ids = [only]
                    if not model_ids and packages:
                        model_ids = [
                            model_id for model_id, declared_packages in model_packages.items()
                            if any(
                                declared.startswith(observed)
                                for declared in declared_packages for observed in packages
                            )
                        ]
                    for model_id in model_ids:
                        declared_packages = model_packages.get(model_id, [])
                        applicable_packages = [
                            declared for declared in declared_packages
                            if not packages or any(declared.startswith(observed) for observed in packages)
                        ]
                        # A column without package identity cannot safely be
                        # projected onto several packages of the same model.
                        if not applicable_packages and len(declared_packages) == 1 and not packages:
                            applicable_packages = declared_packages
                        for package_type in applicable_packages:
                            for function, af_code in functions:
                                page_rows.append(PinDefinitionIR(
                                    chip_id=model_id, package_type=package_type,
                                    pin_name=pin_name,
                                    pin_number=int(number) if re.fullmatch(r"\d+", number) else number.upper(),
                                    function_name=function,
                                    default_function=default_function, source_table_id=getattr(table, "table_id", ""),
                                    source_cell_id=table.cell_id(
                                        row_index,
                                        declared_function if declared_function is not None else pin_column,
                                    ),
                                    page=page_index,
                                    bbox=tuple(float(value) for value in table.bbox),
                                    af_code=af_code,
                                ))
            if page_rows:
                extracted.append({"page": page_index, "bbox": table.bbox, "rows": page_rows})
    return extracted
