from __future__ import annotations

from typing import Any, Iterable

from core.ir import FieldCandidateIR
from core.text import compact
from topologies.matrix_observer import MatrixObservation


def match_field(label: str, rules: dict[str, Any], *, scope: str | None = None) -> tuple[str, str] | None:
    normalized = compact(label).lower()
    matches: list[tuple[int, str, str]] = []
    for group_name in ("fields", "peripherals"):
        for field_name, config in rules.get(group_name, {}).items():
            if scope is not None and config.get("match_scope", "row") != scope:
                continue
            aliases = [compact(item).lower() for item in config.get("rows", [])]
            conjunctions = [
                [compact(item).lower() for item in group if compact(item)]
                for group in config.get("rows_all", [])
            ]
            excludes = [compact(item).lower() for item in config.get("exclude_rows", [])]
            matched_aliases = [item for item in aliases if item in normalized]
            matched_conjunctions = [group for group in conjunctions if group and all(item in normalized for item in group)]
            if (matched_aliases or matched_conjunctions) and not any(item in normalized for item in excludes):
                scores = [len(item) for item in matched_aliases]
                scores.extend(sum(len(item) for item in group) for group in matched_conjunctions)
                matches.append((max(scores), group_name, field_name))
    if not matches:
        return None
    _, group_name, field_name = max(matches)
    return group_name, field_name


def semantic_key(label: str, rules: dict[str, Any]) -> str | None:
    matched = match_field(label, rules)
    if matched is None:
        return None
    group_name, field_name = matched
    prefix = "field" if group_name == "fields" else "peripheral"
    return f"{prefix}:{field_name}"


def observations_to_candidates(
    observations: Iterable[MatrixObservation],
    rules: dict[str, Any],
    *,
    entity_type: str,
) -> list[FieldCandidateIR]:
    result = []
    for observation in observations:
        label = compact(" ".join(observation.label_path))
        matched = match_field(observation.row_label, rules, scope="row")
        if matched is None:
            matched = match_field(label, rules, scope="path")
        if matched is None:
            continue
        field_group, field_name = matched
        result.append(
            FieldCandidateIR(
                candidate_id=f"FC-{observation.observation_id}-{field_group}-{field_name}",
                entity_type=entity_type,
                entity_key=observation.entity_key,
                field_group=field_group,
                field_name=field_name,
                label_path=observation.label_path,
                row_label=observation.row_label,
                raw_value=observation.raw_value,
                parsed_value=observation.raw_value,
                unit=None,
                condition=None,
                source_table_id=observation.source_table_id,
                source_cell_ids=(observation.source_cell_id,),
                evidence_pages=(observation.page,),
                bboxes=(observation.bbox,) if observation.bbox is not None else (),
            )
        )
    return result
