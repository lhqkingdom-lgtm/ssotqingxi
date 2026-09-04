from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from core.collector import Collector


T = TypeVar("T")


@dataclass(frozen=True)
class SectionResult:
    name: str
    status: str
    value: Any = None
    review_item: dict[str, Any] | None = None


def run_optional_section(
    collector: Collector,
    evidences: list[Any],
    *,
    name: str,
    source_file: str,
    operation: Callable[[], T],
    pages: str | None = None,
) -> SectionResult:
    """Run one recoverable extraction section as an isolated transaction."""
    checkpoint = collector.checkpoint()
    evidence_length = len(evidences)
    try:
        value = operation()
    except Exception as exc:
        collector.restore(checkpoint)
        del evidences[evidence_length:]
        review_item = {
            "section": name,
            "status": "failed",
            "source_file": source_file,
            "pages": pages,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
        return SectionResult(name=name, status="failed", review_item=review_item)
    collector.commit(checkpoint)
    return SectionResult(name=name, status="success", value=value)
