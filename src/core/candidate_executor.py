from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from core.ir import AggregatedFieldIR, FieldCandidateIR, ValidationIssueIR
from core.mapped_cell import MappedCell
from core.recipe_executor import memory_kb, parse_packages, peripheral_count, reduce_values
from core.text import compact


STANDARD_UNITS = {
    "flash_kb": "KB",
    "ram_kb": "KB",
    "max_freq_mhz": "MHz",
    "voltage_min": "V",
    "voltage_max": "V",
}


def candidate_to_mapped(candidate: FieldCandidateIR) -> MappedCell:
    prefix = "field" if candidate.field_group == "fields" else "peripheral"
    return MappedCell(
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


def _unique_candidates(items: Iterable[FieldCandidateIR]) -> list[FieldCandidateIR]:
    return list({item.candidate_id: item for item in items}.values())


def _field_contributors(
    name: str,
    candidates: list[FieldCandidateIR],
    package_pattern: str,
    rules: dict[str, Any],
) -> list[FieldCandidateIR]:
    selected = list(candidates)
    if name == "ram_kb":
        row_names = [compact(item.row_label).lower() for item in selected]
        has_total = any(item in {"ram", "sram"} for item in row_names)
        has_numbered = any(item.startswith("sram") and item[4:].isdigit() for item in row_names)
        if has_total and has_numbered:
            selected = [item for item in selected if not (compact(item.row_label).lower().startswith("sram") and compact(item.row_label).lower()[4:].isdigit())]
    result = []
    unit_config = None
    if name in {"flash_kb", "ram_kb"}:
        field_rule = rules.get("fields", {}).get(name, {})
        unit_config = rules.get("units", {}).get(field_rule.get("unit"))
        if unit_config is None:
            raise ValueError(f"unit dictionary missing for common field: {name}")
    for candidate in selected:
        mapped = candidate_to_mapped(candidate)
        if name in {"flash_kb", "ram_kb"} and memory_kb(mapped, unit_config) is None:
            continue
        if name == "package_types" and not parse_packages(candidate.raw_value, package_pattern):
            continue
        result.append(candidate)
    return result


def _peripheral_contributors(
    name: str,
    candidates: list[FieldCandidateIR],
    config: dict[str, Any],
) -> list[FieldCandidateIR]:
    return [
        candidate
        for candidate in candidates
        if peripheral_count(candidate_to_mapped(candidate), config.get("value_parser", "first_integer")) is not None
    ]


def _aggregate_record(
    *,
    entity_type: str,
    entity_key: str,
    field_group: str,
    field_name: str,
    value: Any,
    contributors: list[FieldCandidateIR],
    unit: str | None,
) -> AggregatedFieldIR:
    contributors = _unique_candidates(contributors)
    return AggregatedFieldIR(
        aggregate_id=f"AGG-{entity_type}-{entity_key}-{field_group}-{field_name}",
        entity_type=entity_type,
        entity_key=entity_key,
        field_group=field_group,
        field_name=field_name,
        value=value,
        unit=unit,
        candidate_ids=tuple(item.candidate_id for item in contributors),
        source_cell_ids=tuple(dict.fromkeys(cell for item in contributors for cell in item.source_cell_ids)),
        evidence_pages=tuple(sorted({page for item in contributors for page in item.evidence_pages})),
        bboxes=tuple(bbox for item in contributors for bbox in item.bboxes),
        raw_values=tuple(dict.fromkeys(item.raw_value for item in contributors)),
    )


def reduce_candidates(
    candidates: list[FieldCandidateIR],
    rules: dict[str, Any],
    *,
    package_pattern: str,
) -> tuple[dict[str, Any], list[AggregatedFieldIR]]:
    mapped = [candidate_to_mapped(item) for item in candidates]
    reduced = reduce_values(mapped, rules, package_pattern=package_pattern)
    grouped: dict[tuple[str, str], list[FieldCandidateIR]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.field_group, candidate.field_name)].append(candidate)
    entity_type = candidates[0].entity_type if candidates else ""
    entity_key = candidates[0].entity_key if candidates else ""
    aggregates: list[AggregatedFieldIR] = []
    for output_name, value in reduced["fields"].items():
        rule_name = "operating_voltage" if output_name in {"voltage_min", "voltage_max"} else output_name
        contributors = _field_contributors(rule_name, grouped.get(("fields", rule_name), []), package_pattern, rules)
        aggregates.append(
            _aggregate_record(
                entity_type=entity_type,
                entity_key=entity_key,
                field_group="fields",
                field_name=output_name,
                value=value,
                contributors=contributors,
                unit=STANDARD_UNITS.get(output_name),
            )
        )
    for name, value in reduced["peripherals"].items():
        config = rules.get("peripherals", {}).get(name, {})
        contributors = _peripheral_contributors(name, grouped.get(("peripherals", name), []), config)
        aggregates.append(
            _aggregate_record(
                entity_type=entity_type,
                entity_key=entity_key,
                field_group="peripherals",
                field_name=name,
                value=value,
                contributors=contributors,
                unit=None,
            )
        )
    return reduced, aggregates


def validate_hard_rules(
    candidates: list[FieldCandidateIR],
    aggregates: list[AggregatedFieldIR],
    rules: dict[str, Any],
) -> list[ValidationIssueIR]:
    issues: list[ValidationIssueIR] = []
    for candidate in candidates:
        if not candidate.entity_key.strip():
            issues.append(ValidationIssueIR("primary_key_empty", "error", "", candidate.field_name, "主键字段为空", (candidate.candidate_id,)))
    aggregate_index = {(item.entity_key, item.field_group, item.field_name): item for item in aggregates}
    numeric_fields = {"flash_kb", "ram_kb", "max_freq_mhz", "operating_voltage"}
    for candidate in candidates:
        if candidate.field_group == "fields" and candidate.field_name in numeric_fields:
            output_names = ("voltage_min", "voltage_max") if candidate.field_name == "operating_voltage" else (candidate.field_name,)
            if not any((candidate.entity_key, "fields", name) in aggregate_index for name in output_names):
                issues.append(ValidationIssueIR("numeric_unparseable", "error", candidate.entity_key, candidate.field_name, f"数值字段完全无法解析: {candidate.raw_value}", (candidate.candidate_id,)))
        if candidate.field_name == "pin_number":
            try:
                if int(float(candidate.raw_value)) <= 0:
                    issues.append(ValidationIssueIR("pin_number_nonpositive", "error", candidate.entity_key, candidate.field_name, "脚号小于等于 0", (candidate.candidate_id,)))
            except ValueError:
                issues.append(ValidationIssueIR("numeric_unparseable", "error", candidate.entity_key, candidate.field_name, f"数值字段完全无法解析: {candidate.raw_value}", (candidate.candidate_id,)))
    by_entity = defaultdict(dict)
    for aggregate in aggregates:
        by_entity[aggregate.entity_key][aggregate.field_name] = aggregate
    for entity_key, values in by_entity.items():
        for prefix in ("voltage", "temperature"):
            minimum = values.get(f"{prefix}_min")
            maximum = values.get(f"{prefix}_max")
            if minimum and maximum and minimum.value > maximum.value:
                issues.append(ValidationIssueIR("minimum_gt_maximum", "error", entity_key, prefix, "最小值大于最大值", minimum.candidate_ids + maximum.candidate_ids))
    for group_name in ("fields", "peripherals"):
        for field_name, config in rules.get(group_name, {}).items():
            if config.get("reducer") not in {"single", "first_nonempty"}:
                continue
            grouped = defaultdict(list)
            for candidate in candidates:
                if candidate.field_group == group_name and candidate.field_name == field_name:
                    grouped[(candidate.entity_key, candidate.source_table_id)].append(candidate)
            for (entity_key, _), items in grouped.items():
                values = {compact(item.raw_value).lower() for item in items}
                if len(values) > 1:
                    issues.append(ValidationIssueIR("contradictory_same_key", "error", entity_key, field_name, "同一来源表格中完全相同主键出现互相矛盾的值", tuple(item.candidate_id for item in items)))
    return issues
