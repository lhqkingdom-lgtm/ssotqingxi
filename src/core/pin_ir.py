from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core.ir import BBox


@dataclass(frozen=True)
class PinDefinitionIR:
    chip_id: str
    package_type: str | None
    pin_name: str
    pin_number: int | str
    function_name: str
    default_function: str
    source_table_id: str
    source_cell_id: str
    page: int
    bbox: BBox | None


@dataclass(frozen=True)
class ResolvedPinMuxIR:
    definition: PinDefinitionIR
    af_code: str | None
    af_source_table_id: str | None = None
    af_source_cell_id: str | None = None
    af_page: int | None = None

    def output_row(self) -> dict[str, Any]:
        item = self.definition
        return {
            "chip_id": item.chip_id,
            "pin_name": item.pin_name,
            "pin_number": item.pin_number,
            "function_name": item.function_name,
            "is_supported": True,
            "af_code": self.af_code,
            "default_function": item.default_function,
            "constraints": None,
        }


def reconcile_wrapped_definitions(
    definitions: list[PinDefinitionIR],
    af_lookup: Mapping[tuple[str, str], Any],
) -> list[PinDefinitionIR]:
    """Join adjacent fragments only when an AF observation proves the result."""
    result: list[PinDefinitionIR] = []
    index = 0
    while index < len(definitions):
        item = definitions[index]
        key = (item.pin_name, item.function_name)
        if key in af_lookup:
            result.append(item)
            index += 1
            continue
        merged: PinDefinitionIR | None = None
        merged_end = index
        for end in range(index + 1, min(index + 3, len(definitions) - 1) + 1):
            candidates = definitions[index : end + 1]
            if any(
                candidate.pin_name != item.pin_name
                or candidate.pin_number != item.pin_number
                or candidate.package_type != item.package_type
                for candidate in candidates
            ):
                break
            function_name = "".join(candidate.function_name for candidate in candidates)
            if (item.pin_name, function_name) in af_lookup:
                merged = PinDefinitionIR(**{**item.__dict__, "function_name": function_name})
                merged_end = end
        result.append(merged or item)
        index = merged_end + 1 if merged is not None else index + 1
    return result


def resolve_pin_mux(
    definitions: Iterable[PinDefinitionIR],
    af_lookup: Mapping[tuple[str, str], Any],
) -> list[ResolvedPinMuxIR]:
    reconciled = reconcile_wrapped_definitions(list(definitions), af_lookup)
    result: list[ResolvedPinMuxIR] = []
    for definition in reconciled:
        observation = af_lookup.get((definition.pin_name, definition.function_name))
        result.append(
            ResolvedPinMuxIR(
                definition=definition,
                af_code=getattr(observation, "af_code", None),
                af_source_table_id=getattr(observation, "source_table_id", None),
                af_source_cell_id=getattr(observation, "source_cell_id", None),
                af_page=getattr(observation, "page", None),
            )
        )
    return result


def pin_identity_issues(definitions: Iterable[PinDefinitionIR]) -> list[dict[str, Any]]:
    """Report contradictory GPIO identities without guessing a correction."""
    by_name: dict[str, set[int | str]] = {}
    by_number: dict[int | str, set[str]] = {}
    for item in definitions:
        by_name.setdefault(item.pin_name, set()).add(item.pin_number)
        by_number.setdefault(item.pin_number, set()).add(item.pin_name)
    issues = []
    for pin_name, numbers in sorted(by_name.items()):
        if len(numbers) > 1:
            issues.append({"kind": "pin_name_multiple_numbers", "pin_name": pin_name, "values": sorted(map(str, numbers))})
    for pin_number, names in sorted(by_number.items(), key=lambda pair: str(pair[0])):
        if len(names) > 1:
            issues.append({"kind": "pin_number_multiple_names", "pin_number": pin_number, "values": sorted(names)})
    return issues
