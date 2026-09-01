from __future__ import annotations

from copy import deepcopy
from typing import Any


class Collector:
    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema
        self.data: dict[str, list[dict[str, Any]]] = {name: [] for name in schema["sheets"]}
        self.index: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {name: {} for name in schema["sheets"]}
        self.conflicts: list[dict[str, Any]] = []
        self.current_source_rank = 0

    def checkpoint(self) -> dict[str, Any]:
        """Capture mutable collector state before processing one source document."""
        return {
            "data": deepcopy(self.data),
            "index": deepcopy(self.index),
            "conflicts": deepcopy(self.conflicts),
            "current_source_rank": self.current_source_rank,
        }

    def restore(self, checkpoint: dict[str, Any]) -> None:
        """Discard every collector mutation made after a document checkpoint."""
        self.data = checkpoint["data"]
        self.index = checkpoint["index"]
        self.conflicts = checkpoint["conflicts"]
        self.current_source_rank = checkpoint["current_source_rank"]

    def add(
        self,
        sheet: str,
        row: dict[str, Any],
        evidence_id: str,
        confidence: str = "A",
        field_evidence: dict[str, str | list[str]] | None = None,
    ) -> None:
        fields = self.schema["sheets"][sheet]["fields"]
        normalized = {field: row.get(field) for field in fields}
        normalized["_evidence_ids"] = [evidence_id]
        if field_evidence is None:
            normalized["_field_evidence"] = {
                field: [evidence_id]
                for field in fields
                if normalized.get(field) not in (None, "", [])
            }
        else:
            normalized["_field_evidence"] = {
                field: list(dict.fromkeys(value if isinstance(value, list) else [value]))
                for field, value in field_evidence.items()
                if field in fields and value
            }
        normalized["_confidence"] = confidence
        normalized["_source_rank"] = self.current_source_rank
        key_fields = self.schema["sheets"][sheet]["key"]
        key = tuple(normalized.get(field) for field in key_fields)
        if any(value in (None, "") for value in key):
            return
        existing = self.index[sheet].get(key)
        if existing is None:
            self.index[sheet][key] = normalized
            self.data[sheet].append(normalized)
            return
        differences = {}
        for field in fields:
            old = existing.get(field)
            new = normalized.get(field)
            if old in (None, "") and new not in (None, ""):
                existing[field] = new
                if field in normalized["_field_evidence"]:
                    existing["_field_evidence"][field] = list(normalized["_field_evidence"][field])
            elif new not in (None, "") and old not in (None, "") and old != new:
                differences[field] = {"existing": old, "incoming": new}
            elif new not in (None, "", []) and old == new:
                current = existing["_field_evidence"].setdefault(field, [])
                for incoming_id in normalized["_field_evidence"].get(field, []):
                    if incoming_id not in current:
                        current.append(incoming_id)
        if evidence_id not in existing["_evidence_ids"]:
            existing["_evidence_ids"].append(evidence_id)
        if differences:
            existing_rank = int(existing.get("_source_rank") or 0)
            incoming_rank = int(normalized.get("_source_rank") or 0)
            if incoming_rank > existing_rank:
                for field, change in differences.items():
                    existing[field] = change["incoming"]
                    if field in normalized["_field_evidence"]:
                        existing["_field_evidence"][field] = list(normalized["_field_evidence"][field])
                existing["_source_rank"] = incoming_rank
                existing.pop("_ambiguous", None)
                resolution = "replaced_with_newer_revision"
            elif incoming_rank < existing_rank:
                resolution = "kept_newer_revision"
            else:
                existing["_ambiguous"] = True
                resolution = "ambiguous_same_revision"
            self.conflicts.append({"sheet": sheet, "key": key, "differences": differences, "evidence_id": evidence_id, "resolution": resolution})
