from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.mapped_cell import MappedCell
from topologies.model_column_matrix import numbers
from core.text import compact, scalar_or_none
from core.units import convert_number, convert_scalar, convert_values


def memory_kb(cell: MappedCell, unit_config: dict[str, Any]) -> int | float | None:
    values = convert_values(cell.value, unit_config, cell.label)
    if not values:
        return None
    return scalar_or_none(max(values))


def memory_total_from_label(cell: MappedCell, unit_config: dict[str, Any]) -> int | float | None:
    """Return an explicit aggregate capacity embedded in a hierarchy label."""
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(KBYTES?|KBYTE|KB|MBYTES?|MBYTE|MB)\b", cell.label, re.I)
    if not matches:
        return None
    values = [
        value
        for number, unit in matches
        if (value := convert_number(f"{number}{unit}", unit_config)) is not None
    ]
    if not values:
        return None
    return scalar_or_none(max(values))


def first_number(cell: MappedCell) -> int | float | None:
    values = numbers(cell.value)
    return scalar_or_none(values[0]) if values else None


def frequency_mhz(cell: MappedCell, unit_config: dict[str, Any]) -> int | float | None:
    """Read a frequency, selecting a model-labelled condition when present."""
    text = str(cell.value or "")
    target = compact(cell.entity_key).upper()
    labelled = []
    for match in re.finditer(
        r"([A-Z][A-Z0-9]{5,})\s*:\s*(?:\d+(?:\.\d+)?\s*[×xX]\s*)?(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
        text,
        re.I,
    ):
        label = compact(match.group(1)).upper()
        if target and target.startswith(label):
            value = convert_number(f"{match.group(2)}{match.group(3)}", unit_config)
            if value is not None:
                labelled.append((len(label), float(value)))
    if labelled:
        return scalar_or_none(max(labelled)[1])
    values = [
        value
        for match in re.finditer(
            r"(?:\d+(?:\.\d+)?\s*[×xX]\s*)?(\d+(?:\.\d+)?)\s*(MHz|kHz|Hz)",
            text,
            re.I,
        )
        if (value := convert_number(f"{match.group(1)}{match.group(2)}", unit_config)) is not None
    ]
    if values:
        return scalar_or_none(max(values))
    raw_values = numbers(cell.value)
    return convert_scalar(max(raw_values), f"{cell.label} {cell.value}", unit_config) if raw_values else None


def required_unit_config(recipe: dict[str, Any], field_config: dict[str, Any]) -> dict[str, Any]:
    unit_name = field_config.get("unit")
    if not unit_name:
        raise ValueError("common numeric field has no unit dictionary reference")
    unit_config = recipe.get("units", {}).get(unit_name)
    if not unit_config:
        raise ValueError(f"unit dictionary missing required unit: {unit_name}")
    return unit_config


def parse_packages(value: str, package_pattern: str) -> list[str]:
    result = []
    for candidate in (str(value), str(value)[::-1]):
        for match in re.finditer(package_pattern, candidate, re.I):
            package = compact(match.group(0)).upper()
            if package not in result:
                result.append(package)
    return result


def peripheral_count(cell: MappedCell, parser: str) -> int | None:
    values = [int(value) for value in numbers(cell.value)]
    if not values:
        return None
    if parser == "sum_components":
        main = re.split(r"\(|\[", cell.value, maxsplit=1)[0]
        components = [int(value) for value in numbers(main)]
        return sum(components) if components else None
    if parser == "adc_instance_count":
        label = compact(cell.label).lower()
        if not cell.row_label:
            return None
        if re.match(r"^\s*\d+\s*channels?\b", cell.value, re.I):
            return 1
        channel_summary = "numberofchannels" in label or "nbrofchannels" in label
        if channel_summary and re.match(r"^\s*(?:yes|available|supported)\b", cell.value, re.I):
            return 1
        if channel_summary and not re.search(r"\d+[- ]?bit", cell.label, re.I):
            return None
        if "adcchannels" in label and "numberofchannels" not in label and not re.search(r"adcs\b", cell.label, re.I):
            return 1
        return values[0]
    if parser == "adc_channel_count":
        label = compact(cell.label).lower()
        value_text = str(cell.value or "")
        has_channel_label = "channel" in label or "numberofchannels" in label or "nbrofchannels" in label
        has_channel_value = bool(re.search(r"\bchannels?\b", value_text, re.I))
        if not has_channel_label and not has_channel_value:
            # A plain "ADC"/"ADCs" row describes converter instances.  The
            # SSOT template uses instance_count for ADC input-channel count,
            # so that row is not a value source for this field.
            return None
        # Device tables commonly write external + internal channels (for
        # example "19 + 2").  The configured SSOT meaning is the external
        # input count, therefore keep the first term rather than summing it.
        if re.search(r"\d\s*\+\s*\d", value_text):
            return values[0]
        # In combined rows such as "2 10", the smaller first number is the
        # ADC instance count and the larger number is the channel count.
        return max(values)
    return values[0]


def reduce_values(
    mapped: list[MappedCell],
    recipe: dict[str, Any],
    *,
    package_pattern: str,
) -> dict[str, Any]:
    grouped: dict[str, list[MappedCell]] = defaultdict(list)
    for cell in mapped:
        grouped[cell.key].append(cell)
    fields: dict[str, Any] = {}
    peripherals: dict[str, dict[str, Any]] = {}
    sources: dict[str, list[int]] = {}

    for name, config in recipe.get("fields", {}).items():
        cells = grouped.get(f"field:{name}", [])
        if not cells:
            continue
        unique = list({(cell.label, cell.value, cell.page): cell for cell in cells}.values())
        if name in {"flash_kb", "ram_kb"}:
            unit_config = required_unit_config(recipe, config)
            if name == "ram_kb":
                row_names = [compact(cell.row_label).lower() for cell in unique]
                has_total = any(item in {"ram", "sram"} for item in row_names)
                has_numbered_regions = any(re.fullmatch(r"sram\d+", item) for item in row_names)
                if has_total and has_numbered_regions:
                    unique = [
                        cell
                        for cell in unique
                        if not re.fullmatch(r"sram\d+", compact(cell.row_label).lower())
                    ]
            label_total_values = (
                {value for cell in unique if (value := memory_total_from_label(cell, unit_config)) is not None}
                if config.get("prefer_total_from_label")
                else set()
            )
            label_totals = [max(label_total_values)] if label_total_values else []
            values = label_totals or [value for cell in unique if (value := memory_kb(cell, unit_config)) is not None]
        elif name == "max_freq_mhz":
            unit_config = required_unit_config(recipe, config)
            parser = config.get("value_parser", "frequency_mhz")
            values = [
                value
                for cell in unique
                if (value := (frequency_mhz(cell, unit_config) if parser == "frequency_mhz" else first_number(cell))) is not None
            ]
        elif name == "operating_voltage":
            candidates = [numbers(cell.value)[:2] for cell in unique]
            ranges = [item for item in candidates if len(item) == 2]
            if ranges:
                fields["voltage_min"] = min(item[0] for item in ranges)
                fields["voltage_max"] = max(item[1] for item in ranges)
                sources["voltage_min"] = sorted({cell.page for cell in unique})
                sources["voltage_max"] = sorted({cell.page for cell in unique})
            continue
        elif name == "package_types":
            packages = []
            for cell in unique:
                for package in parse_packages(cell.value, package_pattern):
                    if package not in packages:
                        packages.append(package)
            fields[name] = packages
            sources[name] = sorted({cell.page for cell in unique})
            continue
        else:
            values = [cell.value for cell in unique]
        if not values:
            continue
        reducer = config.get("reducer", "first_nonempty")
        if reducer == "sum":
            fields[name] = scalar_or_none(sum(values))
        elif reducer == "max":
            fields[name] = scalar_or_none(max(values))
        else:
            fields[name] = values[0]
        sources[name] = sorted({cell.page for cell in unique})

    for name, config in recipe.get("peripherals", {}).items():
        cells = grouped.get(f"peripheral:{name}", [])
        if not cells:
            continue
        unique = list({(cell.label, cell.value, cell.page): cell for cell in cells}.values())
        counts = [
            value
            for cell in unique
            if (value := peripheral_count(cell, config.get("value_parser", "first_integer"))) is not None
        ]
        if not counts:
            continue
        reducer = config.get("reducer", "max")
        if reducer == "sum":
            count = sum(counts)
        elif reducer == "sum_distinct_labels":
            by_label: dict[str, int] = {}
            for cell in unique:
                value = peripheral_count(cell, config.get("value_parser", "first_integer"))
                if value is not None:
                    label = compact(cell.label).lower()
                    by_label[label] = max(value, by_label.get(label, 0))
            count = sum(by_label.values())
        else:
            count = max(counts)
        item: dict[str, Any] = {"count": count}
        if config.get("resolution_from_label"):
            resolutions = [
                int(match.group(1))
                for cell in unique
                if (match := re.search(r"(\d+)\s*[- ]?bit", cell.label, re.I))
            ]
            if resolutions:
                item["resolution"] = max(resolutions)
        peripherals[name] = item
        sources[f"peripheral:{name}"] = sorted({cell.page for cell in unique})

    return {"fields": fields, "peripherals": peripherals, "sources": sources}
