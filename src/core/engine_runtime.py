from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Protocol

from core.collector import Collector
from core.evidence import Evidence
from core.profile import DocumentProfile


ENGINE_MODES = {"legacy", "shadow", "declarative"}


class DocumentExtractor(Protocol):
    def process_document(
        self,
        doc: dict[str, Any],
        collector: Collector,
        aliases: dict[str, str],
        profile: DocumentProfile,
    ) -> tuple[dict[str, Any], list[Evidence]]: ...


@dataclass(frozen=True)
class ExtractionRun:
    record: dict[str, Any]
    evidences: list[Evidence]
    data: dict[str, list[dict[str, Any]]]
    conflicts: list[dict[str, Any]]


def load_extractor(module_name: str, profile: DocumentProfile) -> DocumentExtractor:
    module = importlib.import_module(module_name)
    if hasattr(module, "configure"):
        module.configure(profile)
    if not hasattr(module, "process_document"):
        raise ValueError(f"extractor module {module_name} does not expose process_document")
    return module


def resolve_engine_mode(requested: str | None, profile: DocumentProfile) -> str:
    mode = requested or str(profile.payload.get("execution", {}).get("default", "legacy"))
    if mode not in ENGINE_MODES:
        raise ValueError(f"unsupported extraction engine mode: {mode}")
    return mode


def declarative_module(profile: DocumentProfile) -> str:
    module_name = profile.payload.get("execution", {}).get("declarative_adapter")
    if not module_name:
        raise ValueError(
            f"profile {profile.profile_id} has no declarative execution plan; "
            "declarative and shadow modes must not silently fall back to legacy"
        )
    return str(module_name)


def isolated_run(
    extractor: DocumentExtractor,
    doc: dict[str, Any],
    aliases: dict[str, str],
    profile: DocumentProfile,
    schema: dict[str, Any],
) -> ExtractionRun:
    collector = Collector(schema)
    record, evidences = extractor.process_document(doc, collector, aliases, profile)
    return ExtractionRun(record, evidences, collector.data, collector.conflicts)


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if key not in {"ingested_by", "ingested_at"} and not key.startswith("_")
    }


def _sorted_rows(rows: list[dict[str, Any]], key_fields: list[str]) -> list[dict[str, Any]]:
    return sorted(
        (_public_row(row) for row in rows),
        key=lambda row: tuple(str(row.get(field) or "") for field in key_fields),
    )


def compare_runs(
    legacy: ExtractionRun,
    declarative: ExtractionRun,
    schema: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compare business semantics; a successful call is not proof of parity."""
    differences: list[dict[str, Any]] = []
    record_fields = ("models", "model_count", "status", "sections")
    for field in record_fields:
        if legacy.record.get(field) != declarative.record.get(field):
            differences.append({
                "scope": "document",
                "field": field,
                "legacy": legacy.record.get(field),
                "declarative": declarative.record.get(field),
            })
    for sheet, config in schema["sheets"].items():
        legacy_rows = _sorted_rows(legacy.data[sheet], config["key"])
        declarative_rows = _sorted_rows(declarative.data[sheet], config["key"])
        if legacy_rows != declarative_rows:
            differences.append({
                "scope": "sheet",
                "sheet": sheet,
                "legacy_count": len(legacy_rows),
                "declarative_count": len(declarative_rows),
                "legacy": legacy_rows,
                "declarative": declarative_rows,
            })
    legacy_evidence = sorted(
        (item.source_file, item.page, tuple(item.bbox), item.kind, item.raw_text)
        for item in legacy.evidences
    )
    declarative_evidence = sorted(
        (item.source_file, item.page, tuple(item.bbox), item.kind, item.raw_text)
        for item in declarative.evidences
    )
    if legacy_evidence != declarative_evidence:
        differences.append({
            "scope": "evidence",
            "legacy_count": len(legacy_evidence),
            "declarative_count": len(declarative_evidence),
        })
    if legacy.conflicts != declarative.conflicts:
        differences.append({
            "scope": "conflicts",
            "legacy_count": len(legacy.conflicts),
            "declarative_count": len(declarative.conflicts),
        })
    return differences
