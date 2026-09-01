from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def choose_pdfs(input_dir: Path) -> list[dict[str, Any]]:
    groups: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(input_dir.rglob("*.pdf"), key=lambda item: str(item).casefold()):
        groups[hash_file(path)].append(path)
    docs = []
    for digest, paths in sorted(groups.items()):
        preferred = sorted(paths, key=lambda item: ("副本" in item.name, "mm" in item.stem.lower(), "水印" in item.name, len(item.name)))[0]
        version_match = re.search(r"(?:REV|V)\s*([0-9]+(?:\.[0-9]+){0,3})", preferred.name, re.I)
        version = version_match.group(1) if version_match else "0"
        parts = [int(part) for part in version.split(".")]
        version_rank = sum(part * (1000 ** (3 - index)) for index, part in enumerate(parts[:4]))
        docs.append({
            "doc_id": digest[:16],
            "sha256": digest,
            "path": preferred,
            "duplicates": [str(item) for item in paths],
            "version": version,
            "version_rank": version_rank,
        })
    return docs


def extract_page_texts(path: Path, progress: Callable[[int, int], None] | None = None) -> list[str]:
    reader = PdfReader(str(path))
    total = len(reader.pages)
    result = []
    for index, page in enumerate(reader.pages, start=1):
        result.append((page.extract_text() or "").replace("\x00", ""))
        if progress is not None:
            progress(index, total)
    return result
