from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Evidence:
    evidence_id: str
    doc_id: str
    source_file: str
    page: int
    bbox: list[float]
    kind: str
    confidence: str
    raw_text: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()

