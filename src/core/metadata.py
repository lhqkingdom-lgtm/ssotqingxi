from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from core.evidence import Evidence


METADATA_FIELDS = ("pdf_source_file", "evidence_pages", "ingested_by", "ingested_at", "is_reviewed")


def source_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def compact_page_ranges(pages: Iterable[int]) -> str:
    ordered = sorted(set(int(page) for page in pages))
    groups: list[tuple[int, int]] = []
    for page in ordered:
        if not groups or page > groups[-1][1] + 1:
            groups.append((page, page))
        else:
            groups[-1] = (groups[-1][0], page)
    return ",".join(str(start) if start == end else f"{start}-{end}" for start, end in groups)


def attach_ingestion_metadata(
    data: dict[str, list[dict[str, Any]]],
    evidences: Iterable[Evidence],
    ingested_by: str,
    ingested_at: str,
) -> None:
    evidence_index = {evidence.evidence_id: evidence for evidence in evidences}
    for rows in data.values():
        for row in rows:
            pages_by_source: dict[str, set[int]] = defaultdict(set)
            field_ids = [
                evidence_id
                for values in row.get("_field_evidence", {}).values()
                for evidence_id in values
            ]
            evidence_ids = list(dict.fromkeys([*row.get("_evidence_ids", []), *field_ids]))
            for evidence_id in evidence_ids:
                evidence = evidence_index.get(evidence_id)
                if evidence is not None:
                    pages_by_source[source_name(evidence.source_file)].add(evidence.page)
            sources = sorted(pages_by_source, key=str.casefold)
            row["pdf_source_file"] = "; ".join(sources)
            if len(sources) == 1:
                row["evidence_pages"] = compact_page_ranges(pages_by_source[sources[0]])
            else:
                row["evidence_pages"] = "; ".join(
                    f"{source}:p{compact_page_ranges(pages_by_source[source])}" for source in sources
                )
            row["ingested_by"] = ingested_by
            row["ingested_at"] = ingested_at
            row["is_reviewed"] = False
