from __future__ import annotations

from pathlib import Path
from typing import Any

from core.collector import Collector
from core.declarative_executor import execute_declarative_plan
from core.evidence import Evidence
from core.profile import DocumentProfile
from core.table_ir_builder import build_document_ir
from core.text import scalar_or_none


def _evidence(
    doc: dict[str, Any],
    source: dict[str, Any],
    *,
    kind: str,
    sequence: int,
) -> Evidence:
    page = int(source.get("page") or 1)
    bbox = source.get("bbox") or (0.0, 0.0, 0.0, 0.0)
    return Evidence(
        evidence_id=f"{doc['doc_id']}-decl-{sequence}",
        doc_id=doc["doc_id"],
        source_file=str(doc["path"]),
        page=page,
        bbox=[float(value) for value in bbox],
        kind=kind,
        confidence="A",
        raw_text=str(source.get("evidence_raw_value", source.get("raw_value")) or ""),
    )


def _component_output(
    profile: DocumentProfile,
    outputs: dict[str, Any],
    component: str,
) -> Any:
    matches = [
        outputs[section["id"]]
        for section in profile.payload["declarative"]["sections"]
        if section["component"] == component and outputs.get(section["id"]) is not None
    ]
    if len(matches) > 1:
        raise ValueError(f"declarative adapter currently requires one {component} section")
    return matches[0] if matches else None


def _section_completion(
    plan: dict[str, Any],
    outputs: dict[str, Any],
) -> tuple[dict[str, str], str]:
    """Report every section, but gate document completion on required sections only."""
    sections = plan["sections"]
    statuses = {
        section["id"]: (
            "success" if outputs.get(section["id"]) is not None else "not_found"
        )
        for section in sections
    }
    required_complete = all(
        statuses[section["id"]] == "success"
        for section in sections
        if section.get("required", True)
    )
    return statuses, "complete" if required_complete else "partial"


def process_document(
    doc: dict[str, Any],
    collector: Collector,
    aliases: dict[str, str],
    profile: DocumentProfile,
) -> tuple[dict[str, Any], list[Evidence]]:
    del aliases  # Alias normalization belongs to registered components, not this adapter.
    plan = profile.payload.get("declarative")
    if not plan:
        raise ValueError(f"profile {profile.profile_id} has no declarative plan")
    path = Path(doc["path"])
    page_texts = doc["page_texts"]
    document = build_document_ir(path, page_texts, progress=doc.get("table_progress"))
    outputs = execute_declarative_plan(
        plan,
        document=document,
        page_texts=page_texts,
        profile=profile,
    )
    matrix = _component_output(profile, outputs, "model_matrix")
    if not matrix or not matrix.get("models"):
        raise ValueError("declarative plan produced no concrete models")
    conditions = _component_output(profile, outputs, "operating_conditions") or {}
    memory = _component_output(profile, outputs, "memory_projection") or {}
    electrical = _component_output(profile, outputs, "electrical_projection") or {}
    peripherals = _component_output(profile, outputs, "peripheral_projection") or {}
    pins = _component_output(profile, outputs, "pin_projection") or {}
    memory_by_model: dict[str, list[dict[str, Any]]] = {}
    for record in memory.get("records", []):
        memory_by_model.setdefault(record["chip_id"], []).append(record)
    evidences: list[Evidence] = []
    models: list[str] = []
    sequence = 0
    for item in matrix["models"]:
        model = item["model_id"]
        if model in models:
            continue
        models.append(model)
        sequence += 1
        model_evidence = _evidence(doc, item["model_source"], kind="model", sequence=sequence)
        evidences.append(model_evidence)
        fields = dict(item.get("fields", {}))
        if fields.get("ram_kb") is None:
            model_memory = memory_by_model.get(model, [])
            total = next(
                (entry["size_kb"] for entry in model_memory if entry["memory_type"] == "SRAM_Total"),
                None,
            )
            if total is None:
                regions = [
                    entry["size_kb"]
                    for entry in model_memory
                    if entry["memory_type"] in {"SRAM_Internal", "SRAM_External"}
                ]
                total = scalar_or_none(sum(regions)) if regions else None
            if total is not None:
                fields["ram_kb"] = total
        if fields.get("voltage_min") is None and conditions.get("voltage_min") is not None:
            fields["voltage_min"] = conditions["voltage_min"]
            fields["voltage_max"] = conditions.get("voltage_max")
        row = {
            "chip_id": model,
            "series": profile.series_for_model(model),
            "core": None,
            "flash_kb": fields.get("flash_kb"),
            "ram_kb": fields.get("ram_kb"),
            "max_freq_mhz": fields.get("max_freq_mhz"),
            "package_types": fields.get("package_types") or [],
            "voltage_min": fields.get("voltage_min"),
            "voltage_max": fields.get("voltage_max"),
        }
        field_evidence: dict[str, str] = {
            "chip_id": model_evidence.evidence_id,
            "series": model_evidence.evidence_id,
        }
        for source_field, sources in item.get("field_sources", {}).items():
            if not sources:
                continue
            sequence += 1
            field_source = _evidence(
                doc,
                sources[0],
                kind=f"model_matrix:{source_field}",
                sequence=sequence,
            )
            evidences.append(field_source)
            targets = {
                "operating_voltage": ("voltage_min", "voltage_max"),
                "operating_conditions": ("voltage_min", "voltage_max"),
            }.get(source_field, (source_field,))
            for target in targets:
                if row.get(target) is not None:
                    field_evidence[target] = field_source.evidence_id
        if row.get("ram_kb") is not None and "ram_kb" not in field_evidence:
            derived_sources = [
                source
                for entry in memory_by_model.get(model, [])
                if entry["memory_type"] in {"SRAM_Total", "SRAM_Internal", "SRAM_External"}
                for source in entry.get("value_sources", [])
            ]
            if derived_sources:
                sequence += 1
                derived_evidence = _evidence(
                    doc, derived_sources[0], kind="memory:ram_total", sequence=sequence
                )
                evidences.append(derived_evidence)
                field_evidence["ram_kb"] = derived_evidence.evidence_id
        collector.add(
            "chips",
            row,
            model_evidence.evidence_id,
            "A",
            field_evidence,
        )
    for record in electrical.get("records", []):
        value_sources = record.get("value_sources") or []
        sequence += 1
        value_evidence = _evidence(
            doc,
            value_sources[0],
            kind=f"electrical:{record['param_name']}",
            sequence=sequence,
        )
        evidences.append(value_evidence)
        collector.add(
            "electrical",
            {field: record.get(field) for field in (
                "chip_id", "param_name", "min_value", "max_value", "unit", "conditions"
            )},
            value_evidence.evidence_id,
            "A",
            {
                field: value_evidence.evidence_id
                for field in ("chip_id", "param_name", "min_value", "max_value", "unit")
            },
        )
    for record in peripherals.get("records", []):
        value_sources = record.get("value_sources") or []
        evidence_by_source: dict[int, str] = {}
        for source in value_sources:
            sequence += 1
            value_evidence = _evidence(
                doc,
                source,
                kind=f"peripheral:{record['peripheral_type']}",
                sequence=sequence,
            )
            evidences.append(value_evidence)
            evidence_by_source[id(source)] = value_evidence.evidence_id
        default_evidence_id = evidence_by_source[id(value_sources[0])]
        peripheral_field_evidence = {
            field: [evidence_by_source[id(source)] for source in sources]
            for field, sources in record.get("field_sources", {}).items()
            if sources
        }
        peripheral_field_evidence.update({
            "chip_id": default_evidence_id,
            "peripheral_type": default_evidence_id,
        })
        collector.add(
            "peripherals",
            {field: record.get(field) for field in (
                "chip_id", "peripheral_type", "instance_count", "channel_count",
                "max_resolution", "max_frequency_mhz", "features",
            )},
            default_evidence_id,
            "A",
            peripheral_field_evidence,
        )
    pin_evidence_cache: dict[tuple[Any, ...], str] = {}
    for record in pins.get("records", []):
        value_sources = record.get("value_sources") or []
        evidence_by_source: dict[int, str] = {}
        for source in value_sources:
            signature = (
                source.get("label"), source.get("source_cell_id"), source.get("page"),
                tuple(source.get("bbox") or ()), source.get("raw_value"),
            )
            evidence_id = pin_evidence_cache.get(signature)
            if evidence_id is None:
                sequence += 1
                value_evidence = _evidence(doc, source, kind=str(source.get("label") or "pin"), sequence=sequence)
                evidences.append(value_evidence)
                evidence_id = value_evidence.evidence_id
                pin_evidence_cache[signature] = evidence_id
            evidence_by_source[id(source)] = evidence_id
        default_evidence_id = evidence_by_source[id(value_sources[0])]
        pin_field_evidence = {
            field: [evidence_by_source[id(source)] for source in sources]
            for field, sources in record.get("field_sources", {}).items()
            if sources
        }
        collector.add(
            "pin_mux",
            {field: record.get(field) for field in (
                "chip_id", "package_type", "pin_name", "pin_number", "function_name", "is_supported",
                "af_code", "default_function", "constraints",
            )},
            default_evidence_id,
            "A",
            pin_field_evidence,
        )
    for record in memory.get("records", []):
        value_sources = record.get("value_sources") or []
        sequence += 1
        value_evidence = _evidence(
            doc,
            value_sources[0],
            kind=f"memory:{record['memory_type']}",
            sequence=sequence,
        )
        evidences.append(value_evidence)
        collector.add(
            "memory",
            {field: record.get(field) for field in (
                "chip_id", "memory_type", "size_kb", "organization", "features"
            )},
            value_evidence.evidence_id,
            "A",
            {
                "chip_id": value_evidence.evidence_id,
                "memory_type": value_evidence.evidence_id,
                "size_kb": value_evidence.evidence_id,
            },
        )
    section_status, document_status = _section_completion(plan, outputs)
    return (
        {
            "doc_id": doc["doc_id"],
            "sha256": doc["sha256"],
            "version": doc.get("version"),
            "source_file": str(path),
            "duplicate_paths": doc.get("duplicates", []),
            "page_count": len(page_texts),
            "model_count": len(models),
            "models": models,
            "evidence_count": len(evidences),
            "status": document_status,
            "sections": section_status,
            "review_items": [],
        },
        evidences,
    )
