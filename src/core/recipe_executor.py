from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.mapped_cell import MappedCell
from topologies.model_column_matrix import numbers
from core.text import compact, scalar_or_none
from core.units import convert_number, convert_scalar, convert_values


def memory_kb(cell: MappedCell, unit_config: dict[str, Any]) -> int | float | None:
    conditioned = []
    for match in re.finditer(
        r"(\d+(?:\.\d+)?)\s*(KBYTES?|KBYTE|KB|MBYTES?|MBYTE|MB)\s+(?:FOR\s+)?(?:X{2,}|_+)([A-Z0-9])\b",
        str(cell.value or ""),
        re.I,
    ):
        value = convert_number(f"{match.group(1)}{match.group(2)}", unit_config)
        if value is not None:
            conditioned.append((match.group(3).upper(), value))
    if conditioned and cell.entity_key:
        target_code = compact(cell.entity_key).upper()[-1:]
        selected = [value for code, value in conditioned if code == target_code]
        return scalar_or_none(max(selected)) if selected else None
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
    value_text = re.sub(
        r"(?<=\d)\s*(?:\(\d+\))+\s*$", "", str(cell.value or "")
    )
    values = [int(value) for value in numbers(value_text)]
    if not values:
        return None
    if parser == "sum_explicit_addends":
        main = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", value_text)
        components = [int(value) for value in numbers(main)]
        if not components:
            return None
        explicitly_additive = "+" in main or bool(
            re.search(r"\b(?:extra|additional|plus)\b", main, re.I)
        )
        return sum(components) if explicitly_additive else max(components)
    if parser == "sum_components":
        package_qualified = [
            int(value)
            for value in re.findall(
                r"(\d+)\s+in\s+[A-Za-z][A-Za-z0-9/-]*\d+\b",
                value_text,
                re.I,
            )
        ]
        if package_qualified:
            # These are alternatives for package variants, not additive
            # components. Package pin counts must not become peripheral counts.
            return max(package_qualified)
        label_lines = [
            compact(part).lower()
            for part in str(cell.label or "").splitlines()
            if compact(part)
        ]
        value_lines = [part.strip() for part in value_text.splitlines() if part.strip()]
        if len(label_lines) == 1 and len(value_lines) > 1:
            # Table-path construction deliberately flattens whitespace in row
            # labels. Recover acronym-like logical subrows from the leaf path;
            # their order is retained by PDF table extraction.
            leaf_label = str(cell.label or "").split(" / ")[-1]
            flattened_parts = re.findall(
                r"\b[A-Z][A-Z0-9]*(?:\(\d+\))?(?:/[A-Z][A-Z0-9]*)?\b",
                leaf_label,
            )
            if len(flattened_parts) == len(value_lines):
                label_lines = [compact(part).lower() for part in flattened_parts]
        if len(label_lines) > 1 and len(label_lines) == len(value_lines):
            # Some PDF tables merge several logical peripheral rows into one
            # physical cell.  Keep the row alignment instead of discarding all
            # lines after the first footnote marker.  When a slash-qualified
            # label repeats a component on another line (for example a
            # USART/UART capability row followed by a UART-only row), the
            # slash values describe overlapping capabilities and must not be
            # added to each other.  Independent component rows remain additive.
            repeated_tokens = {
                token
                for line in label_lines
                for token in re.findall(r"[a-z]+", line)
                if sum(token in re.findall(r"[a-z]+", other) for other in label_lines) > 1
            }
            total = 0
            parsed_lines = 0
            for label_line, raw_line in zip(label_lines, value_lines):
                main_line = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", raw_line)
                components = [int(value) for value in numbers(main_line)]
                if not components:
                    continue
                label_tokens = set(re.findall(r"[a-z]+", label_line))
                overlaps_later_row = "/" in label_line and bool(label_tokens & repeated_tokens)
                total += max(components) if overlaps_later_row else sum(components)
                parsed_lines += 1
            if parsed_lines:
                return total
        main = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", value_text)
        components = [int(value) for value in numbers(main)]
        return sum(components) if components else None
    if parser == "adc_instance_count":
        label = compact(cell.label).lower()
        leaf_label = compact(cell.row_label).lower()
        if not cell.row_label:
            return None
        explicit_instances = re.search(
            r"(\d+)\s*[X×]\s*\d+\s*(?:BIT|位)", str(cell.value), re.I
        )
        if explicit_instances:
            return int(explicit_instances.group(1))
        leading_instance_with_channels = re.match(
            r"^\s*(\d+)\s*\(\s*\d+[^)]*(?:ext\.?|int\.?|channels?|通道|路)",
            str(cell.value),
            re.I,
        )
        if leading_instance_with_channels:
            return int(leading_instance_with_channels.group(1))
        if re.match(r"^\s*\d+\s*channels?\b", cell.value, re.I):
            return None
        channel_summary = any(
            marker in leaf_label
            for marker in ("numberofchannels", "nbrofchannels", "通道数量", "通道数", "路")
        )
        if channel_summary and re.match(r"^\s*(?:yes|available|supported)\b", cell.value, re.I):
            return 1
        if channel_summary:
            qualified_channels = re.search(
                r"\d+\s*(?:channels?|通道|路)", value_text, re.I
            )
            leading_values = (
                [int(value) for value in numbers(value_text[: qualified_channels.start()])]
                if qualified_channels
                else []
            )
            if leading_values:
                # A collapsed cell such as ``2 / 10 channels`` states the
                # converter count first and explicitly qualifies the latter
                # number as channels. A lone channel value remains insufficient
                # evidence for an instance count.
                qualified_value = int(numbers(qualified_channels.group(0))[0])
                return leading_values[0] if leading_values[0] < qualified_value else None
            # A collapsed two-row cell such as "3 / 16" carries the ADC
            # instance count followed by the channel count.  A package-
            # qualified list such as "20 in LQFP48, 21 in ..." does not.
            package_qualified = bool(
                re.search(r"\d+\s+in\s+[A-Za-z][A-Za-z0-9/-]*\d+\b", cell.value, re.I)
            )
            collapsed_numeric_pair = bool(
                re.fullmatch(r"\s*\d+\s*(?:[/,;]|\s+)\s*\d+\s*", value_text)
            )
            return (
                values[0]
                if len(values) == 2
                and collapsed_numeric_pair
                and re.search(r"adcs?", leaf_label)
                and not package_qualified
                else None
            )
        if re.search(r"\bchannels?\b", str(cell.value), re.I):
            return values[0] if len(values) >= 2 else None
        if "channel" in leaf_label or "channels" in label or "通道" in label or "路" in label:
            return None
        combined_text = f"{cell.label} {cell.value}"
        if (
            not re.fullmatch(r"\s*\d+\s*", str(cell.value))
            and re.search(r"\d+\s*(?:[- ]?bit|位)\s*(?:sar\s*)?adc", combined_text, re.I)
        ):
            # A resolution-qualified ADC mention does not state a converter
            # instance count. Profiles whose matrix defines such a column as
            # instances can select the ordinary first_integer parser instead.
            return None
        return values[0]
    if parser == "adc_channel_count":
        label = compact(cell.label).lower()
        slash_qualified_channels = re.search(
            r"((?:\d+\s*/\s*)+\d+)\s*(?:CHANNELS?|通道|路)",
            value_text,
            re.I,
        )
        if slash_qualified_channels:
            # One base sales type may cover several package variants. The
            # database projection has no package dimension, so retain the
            # maximum externally available channel count while preserving the
            # source cell as evidence for the package-specific alternatives.
            return max(int(value) for value in numbers(slash_qualified_channels.group(1)))
        labelled_channels = [
            int(value)
            for value in re.findall(r"(\d+)\s*(?:CHANNELS?|通道|路)", value_text, re.I)
        ]
        if labelled_channels:
            return labelled_channels[-1]
        has_channel_label = (
            "channel" in label
            or "numberofchannels" in label
            or "nbrofchannels" in label
            or "通道" in label
            or "路" in label
        )
        has_channel_value = bool(re.search(r"\bchannels?\b|通道|路", value_text, re.I))
        if not has_channel_label and not has_channel_value:
            # A plain "ADC"/"ADCs" row describes converter instances, not
            # input channels, so it is not a value source for this field.
            return None
        # Device tables commonly write external + internal channels (for
        # example "19 + 2").  The configured SSOT meaning is the external
        # input count, therefore keep the first term rather than summing it.
        if re.search(r"\d\s*\+\s*\d", value_text):
            return values[0]
        package_counts = [
            int(value)
            for value in re.findall(
                r"(\d+)\s+in\s+[A-Za-z][A-Za-z0-9/-]*\d+\b", value_text, re.I
            )
        ]
        if package_counts:
            return max(package_counts)
        # Some feature matrices use one vertically merged ADC label for
        # several named channel classes, for example Direct/Fast/Slow, while
        # the selected device cell contains one value per class.  These are
        # disjoint channel categories, so retaining only the largest value
        # loses part of the source meaning.  Require both multiple explicit
        # ``Number of ... channels`` labels and the matching number of values;
        # this keeps ordinary combined instance/channel rows unchanged.
        channel_classes = re.findall(r"numberof([a-z0-9]+?)channels", label)
        if len(set(channel_classes)) >= 2 and len(values) == len(channel_classes):
            return sum(values)
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
        channel_parser = config.get("channel_value_parser")
        channel_items = [
            (cell, value)
            for cell in unique
            if channel_parser and (value := peripheral_count(cell, channel_parser)) is not None
        ]
        if config.get("bare_numeric_role") == "channel_count":
            bare_channel_cells = [
                cell
                for cell in unique
                if re.search(r"\badcs?\b|adc", cell.label, re.I)
                and re.fullmatch(r"\s*\d+\s*", str(cell.value))
            ]
            known_channel_cells = {id(cell) for cell, _ in channel_items}
            channel_items.extend(
                (cell, int(str(cell.value).strip()))
                for cell in bare_channel_cells
                if id(cell) not in known_channel_cells
            )
            bare_channel_ids = {id(cell) for cell in bare_channel_cells}
            counts = [
                value
                for cell in unique
                if id(cell) not in bare_channel_ids
                if (
                    value := peripheral_count(
                        cell, config.get("value_parser", "first_integer")
                    )
                ) is not None
            ]
        channel_counts = [value for _, value in channel_items]
        if not counts and channel_parser == "adc_channel_count":
            # Some matrices vertically merge one "ADCs / Number of channels"
            # label across two value rows: a bare converter count followed by
            # an explicitly qualified channel maximum.  Use that structural
            # pairing only when both distinct values are present; a lone
            # channel number must never be reinterpreted as an instance count.
            by_label: dict[str, list[tuple[MappedCell, int]]] = defaultdict(list)
            for cell in unique:
                value = peripheral_count(cell, channel_parser)
                if value is not None:
                    by_label[compact(cell.label).lower()].append((cell, value))
            paired_cells: list[MappedCell] = []
            paired_instances: list[int] = []
            for label, items in by_label.items():
                bare = [
                    (cell, value)
                    for cell, value in items
                    if re.fullmatch(r"\s*\d+\s*", str(cell.value))
                ]
                qualified = [
                    (cell, value)
                    for cell, value in items
                    if re.search(
                        r"\b(?:up\s*to|channels?)\b|\d+\s+in\s+[A-Za-z][A-Za-z0-9/-]*\d+\b",
                        str(cell.value),
                        re.I,
                    )
                ]
                collapsed_bare_pair = (
                    len(items) == 2
                    and len(bare) == 2
                    and "adc" in label
                )
                channel_classes = re.findall(r"numberof([a-z0-9]+?)channels", label)
                structured_channel_pair = (
                    len(items) == 2
                    and len(bare) == 1
                    and len(set(channel_classes)) >= 2
                    and any(len(numbers(cell.value)) == len(channel_classes) for cell, _ in items)
                )
                if bare and (qualified or collapsed_bare_pair or structured_channel_pair) and len({value for _, value in items}) >= 2:
                    instance_cell, instance_value = min(bare, key=lambda item: item[1])
                    paired_cells.append(instance_cell)
                    paired_instances.append(instance_value)
            if paired_instances:
                counts = paired_instances
                paired_ids = {id(cell) for cell in paired_cells}
                channel_counts = [value for cell, value in channel_items if id(cell) not in paired_ids]
        if not counts and not channel_counts:
            continue
        count = None
        reducer = config.get("reducer", "max")
        if counts and reducer == "sum":
            count = sum(counts)
        elif counts and reducer == "sum_distinct_labels":
            by_label: dict[str, int] = {}
            for cell in unique:
                value = peripheral_count(cell, config.get("value_parser", "first_integer"))
                if value is not None:
                    label = compact(cell.label).lower()
                    by_label[label] = max(value, by_label.get(label, 0))
            # Ambiguous parent/child rows are paired above using their numeric
            # relationship. In that case the direct parser intentionally
            # returns no values, so retain the recovered instance count rather
            # than turning an empty label map into the false count zero.
            count = sum(by_label.values()) if by_label else max(counts)
        elif counts:
            count = max(counts)
        item: dict[str, Any] = {"count": count if counts else None}
        if channel_counts:
            channel_reducer = config.get("channel_reducer", "max")
            item["channel_count"] = sum(channel_counts) if channel_reducer == "sum" else max(channel_counts)
        if config.get("resolution_from_label"):
            resolutions = [
                int(match.group(1))
                for cell in unique
                if (
                    match := re.search(
                        r"(\d+)\s*(?:[- ]?bit|位)", compact(cell.label), re.I
                    )
                )
            ]
            if resolutions:
                item["resolution"] = max(resolutions)
        peripherals[name] = item
        sources[f"peripheral:{name}"] = sorted({cell.page for cell in unique})

    return {"fields": fields, "peripherals": peripherals, "sources": sources}
