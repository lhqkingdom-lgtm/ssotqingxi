from __future__ import annotations

from pathlib import Path
from typing import Any

from core.collector import Collector
from core.declarative_executor import execute_declarative_plan
from core.evidence import Evidence
from core.profile import DocumentProfile
from core.table_ir_builder import build_document_ir


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
        raw_text=str(source.get("raw_value") or ""),
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
        collector.add(
            "chips",
            row,
            model_evidence.evidence_id,
            "A",
            field_evidence,
        )
    section_status = {
        section["id"]: (
            "success" if outputs.get(section["id"]) is not None else "not_found"
        )
        for section in plan["sections"]
    }
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
            "status": "complete" if all(value == "success" for value in section_status.values()) else "partial",
            "sections": section_status,
            "review_items": [],
        },
        evidences,
    )
