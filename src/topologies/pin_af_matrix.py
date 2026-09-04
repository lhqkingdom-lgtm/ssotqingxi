from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import replace
from typing import Callable, Iterable

from core.ir import BBox, TableIR
from core.text import compact
from topologies.matrix_observer import table_grids, table_matches


FunctionSplitter = Callable[[object, str], list[str]]


@dataclass(frozen=True)
class PinAFObservation:
    pin_name: str
    function_name: str
    af_code: str
    source_table_id: str
    source_cell_id: str
    page: int
    bbox: BBox | None


def observe_pin_af_matrices(
    tables: Iterable[TableIR],
    *,
    locator: dict[str, object],
    pin_pattern: re.Pattern[str],
    split_functions: FunctionSplitter,
    identity_terms: Iterable[str] = (),
) -> list[PinAFObservation]:
    """Observe pin/function/AF triples from any AF-column matrix."""
    header_pattern = re.compile(str(locator.get("header_pattern", r"^AF\d+$")), re.I)
    identity_pattern_text = compact(str(locator.get("identity_pattern", "")))
    identity_pattern = re.compile(identity_pattern_text, re.I) if identity_pattern_text else None
    normalized_identities = tuple(compact(item).upper() for item in identity_terms if compact(item))
    minimum_columns = int(locator.get("minimum_af_columns", 2))
    pin_scan_columns = int(locator.get("pin_scan_columns", 3))
    observations: list[PinAFObservation] = []
    for table in tables:
        if not table_matches(table, locator):
            continue
        # A document may contain reference matrices for sibling devices.  If a
        # table names one or more entity families, only consume it when one of
        # those identities matches the requested entity.  Unlabelled matrices
        # remain eligible because their identity is normally established by the
        # surrounding document.
        if identity_pattern is not None and normalized_identities:
            named_identities = tuple(
                compact(match.group(0)).upper()
                for match in identity_pattern.finditer(table.title)
            )
            if named_identities and not any(
                target in named or named in target
                for target in normalized_identities
                for named in named_identities
            ):
                continue
        for fragment, page, rows, cells in table_grids(table):
            del fragment
            header_row = None
            af_columns: dict[int, str] = {}
            for row_index, row in enumerate(rows[:6]):
                candidate = {
                    column: compact(value).upper()
                    for column, value in enumerate(row)
                    if header_pattern.fullmatch(compact(value).upper())
                }
                if len(candidate) >= minimum_columns:
                    header_row = row_index
                    af_columns = candidate
                    break
            if header_row is None:
                continue
            first_af_column = min(af_columns)
            for row_index, row in enumerate(rows[header_row + 1 :], start=header_row + 1):
                pin_name = None
                for value in row[: min(first_af_column, pin_scan_columns)]:
                    match = pin_pattern.search(compact(value))
                    if match:
                        pin_name = match.group(0).upper()
                        break
                if pin_name is None:
                    continue
                for column, af_code in af_columns.items():
                    if column >= len(row) or not compact(row[column]) or compact(row[column]) in {"-", "--"}:
                        continue
                    source = cells.get((row_index, column))
                    for function_name in split_functions(row[column], pin_name):
                        observations.append(
                            PinAFObservation(
                                pin_name=pin_name,
                                function_name=function_name,
                                af_code=af_code,
                                source_table_id=table.table_id,
                                source_cell_id=source.cell_id if source else "",
                                page=page,
                                bbox=source.bbox if source else None,
                            )
                        )
    return observations


def unique_pin_af_lookup(observations: Iterable[PinAFObservation]) -> dict[tuple[str, str], PinAFObservation]:
    result: dict[tuple[str, str], PinAFObservation] = {}
    for observation in observations:
        key = (observation.pin_name, observation.function_name)
        existing = result.get(key)
        if existing is not None and existing.af_code != observation.af_code:
            codes = sorted(
                set(existing.af_code.split(",")) | set(observation.af_code.split(",")),
                key=lambda value: int(value[2:]) if re.fullmatch(r"AF\d+", value) else value,
            )
            result[key] = replace(existing, af_code=",".join(codes))
            continue
        result.setdefault(key, observation)
    return result
