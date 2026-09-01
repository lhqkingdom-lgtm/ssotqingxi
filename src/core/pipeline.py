from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from core.collector import Collector
from core.documents import choose_pdfs, extract_page_texts
from core.evidence import Evidence
from core.metadata import attach_ingestion_metadata
from core.profile import DocumentProfile, load_profile
from core.text import compact
from core.validation import validate


@dataclass(frozen=True)
class FamilyRuntime:
    profile: DocumentProfile
    rules: dict[str, Any]
    aliases: dict[str, str]
    extractor: Any
    rules_path: Path


def load_family(profile_path: Path, rules_override: Path | None = None, aliases_override: Path | None = None) -> FamilyRuntime:
    profile = load_profile(profile_path)
    rules_path = rules_override.resolve() if rules_override else profile.rules_file
    aliases_path = aliases_override.resolve() if aliases_override else profile.aliases_file
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    alias_payload = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else {"aliases": {}}
    aliases = {compact(key): compact(value) for key, value in alias_payload.get("aliases", {}).items()}
    module_name = profile.payload.get("extractor_module")
    if not module_name:
        raise ValueError(f"profile {profile.profile_id} does not declare extractor_module")
    module = importlib.import_module(str(module_name))
    if hasattr(module, "configure"):
        module.configure(profile, rules)
    if not hasattr(module, "process_document"):
        raise ValueError(f"extractor module {module_name} does not expose process_document")
    return FamilyRuntime(profile, rules, aliases, module, rules_path)


def profiles_from_registry(registry_path: Path) -> list[Path]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    return [(registry_path.parent / item).resolve() for item in registry.get("profiles", [])]


def detection_score(path: Path, page_texts: list[str], profile: DocumentProfile) -> int:
    detection = profile.payload.get("detection", {})
    filename = path.name.casefold()
    scan_pages = int(detection.get("scan_pages", 3))
    text = "\n".join(page_texts[:scan_pages]).casefold()
    filename_hits = sum(1 for term in detection.get("filename_any", []) if str(term).casefold() in filename)
    text_hits = sum(1 for term in detection.get("text_any", []) if str(term).casefold() in text)
    return filename_hits * int(detection.get("filename_weight", 10)) + text_hits * int(detection.get("text_weight", 1))


def select_family(path: Path, page_texts: list[str], families: list[FamilyRuntime]) -> FamilyRuntime:
    scored = [(detection_score(path, page_texts, family.profile), family) for family in families]
    eligible = [
        (score, family)
        for score, family in scored
        if score >= int(family.profile.payload.get("detection", {}).get("minimum_score", 1))
    ]
    if not eligible:
        scores = {family.profile.profile_id: score for score, family in scored}
        raise ValueError(f"document family is unknown: {path.name}; scores={scores}")
    highest = max(score for score, _ in eligible)
    winners = [family for score, family in eligible if score == highest]
    if len(winners) != 1:
        raise ValueError(f"document family is ambiguous: {path.name}; candidates={[family.profile.profile_id for family in winners]}")
    return winners[0]


def run_extraction(
    *,
    input_dir: Path,
    output_dir: Path,
    schema_path: Path,
    profile_paths: list[Path],
    ingested_by: str,
    ingested_at: str | None = None,
    limit: int = 0,
    rules_override: Path | None = None,
    aliases_override: Path | None = None,
) -> dict[str, Any]:
    if not profile_paths:
        raise ValueError("at least one document-family profile is required")
    if len(profile_paths) > 1 and (rules_override or aliases_override):
        raise ValueError("rules/aliases overrides are only valid with one explicit profile")
    output_dir.mkdir(parents=True, exist_ok=True)
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    families = [load_family(path, rules_override, aliases_override) for path in profile_paths]
    docs = choose_pdfs(input_dir)
    if limit:
        docs = docs[:limit]
    collector = Collector(schema)
    documents = []
    evidences: list[Evidence] = []
    errors = []
    print(f"[inventory] unique_pdfs={len(docs)} input={input_dir}", flush=True)
    for index, doc in enumerate(docs, start=1):
        document_started = perf_counter()
        print(f"[{index}/{len(docs)}] {doc['path'].name}", flush=True)
        checkpoint = collector.checkpoint()
        try:
            text_started = perf_counter()

            def text_progress(current: int, total: int) -> None:
                if current == 1 or current == total or current % 25 == 0:
                    print(f"  [text] page={current}/{total} elapsed={perf_counter() - text_started:.1f}s", flush=True)

            page_texts = extract_page_texts(doc["path"], progress=text_progress)
            family = select_family(doc["path"], page_texts, families)
            print(f"  [classify] family={family.profile.profile_id} pages={len(page_texts)}", flush=True)
            doc["page_texts"] = page_texts
            table_started = perf_counter()
            table_total = 0

            def table_progress(current: int, total: int, page_tables: int) -> None:
                nonlocal table_total
                table_total += page_tables
                if current == 1 or current == total or current % 10 == 0:
                    print(
                        f"  [tables] page={current}/{total} page_tables={page_tables} total_tables={table_total} elapsed={perf_counter() - table_started:.1f}s",
                        flush=True,
                    )

            doc["table_progress"] = table_progress
            record, doc_evidence = family.extractor.process_document(doc, collector, family.aliases, family.profile)
            if len(families) > 1:
                record["family_id"] = family.profile.profile_id
            documents.append(record)
            evidences.extend(doc_evidence)
            print(
                f"  [done] family={family.profile.profile_id} models={record['model_count']} evidence={record['evidence_count']} elapsed={perf_counter() - document_started:.1f}s",
                flush=True,
            )
        except Exception as exc:
            collector.restore(checkpoint)
            errors.append({"source_file": str(doc["path"]), "error": f"{type(exc).__name__}: {exc}"})
            print(f"  [error] {type(exc).__name__}: {exc} elapsed={perf_counter() - document_started:.1f}s", flush=True)
    for sheet, rows in collector.data.items():
        collector.data[sheet] = [row for row in rows if not row.get("_ambiguous")]
        collector.data[sheet].sort(key=lambda row: tuple(str(row.get(key) or "") for key in ("chip_id", "pin_number", "pin_name", "function_name", "peripheral_type", "param_name", "memory_type")))
    timestamp = ingested_at or datetime.now().astimezone().isoformat(timespec="seconds")
    attach_ingestion_metadata(collector.data, evidences, ingested_by, timestamp)
    issues = validate(collector.data, schema)
    rule_versions = {family.profile.profile_id: family.rules.get("rule_version") for family in families}
    rules_files = {family.profile.profile_id: str(family.rules_path.resolve()) for family in families}
    payload = {
        "schema_version": schema["version"],
        "rule_version": next(iter(rule_versions.values())) if len(rule_versions) == 1 else rule_versions,
        "rules_file": next(iter(rules_files.values())) if len(rules_files) == 1 else rules_files,
        "documents": documents,
        "data": collector.data,
        "conflicts": collector.conflicts,
        "validation_issues": issues,
        "errors": errors,
        "summary": {sheet: len(rows) for sheet, rows in collector.data.items()},
    }
    (output_dir / "extracted.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "evidence.jsonl").open("w", encoding="utf-8") as stream:
        for evidence in evidences:
            stream.write(json.dumps(evidence.to_dict(), ensure_ascii=False) + "\n")
    print(json.dumps({"summary": payload["summary"], "documents": len(documents), "conflicts": len(collector.conflicts), "issues": len(issues), "errors": len(errors)}, ensure_ascii=False))
    return payload
