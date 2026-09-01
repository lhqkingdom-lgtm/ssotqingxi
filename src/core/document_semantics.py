from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.text import compact


@dataclass(frozen=True)
class SemanticValue:
    value: str
    pages: tuple[int, ...]


def filename_models(path: Path, pattern: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).upper() for match in re.finditer(pattern, path.stem, re.I)))


def resolve_target_model(path: Path, page_texts: list[str], config: dict[str, Any]) -> SemanticValue:
    models = filename_models(path, str(config["filename_model_pattern"]))
    if not models:
        raise ValueError(f"cannot derive target model from filename: {path.name}")
    if len(models) != 1:
        raise ValueError(f"ambiguous target models in filename {path.name}: {', '.join(models)}")
    model = models[0]
    series_match = re.match(str(config["series_pattern"]), model, re.I)
    if not series_match:
        raise ValueError(f"target model does not match the configured ordering shape: {model}")
    series = series_match.group(1).upper()
    limit = min(len(page_texts), int(config.get("verification_scan_pages", 25)))
    exact_pages = [index + 1 for index, text in enumerate(page_texts[:limit]) if model in compact(text).upper()]
    if exact_pages:
        return SemanticValue(model, tuple(exact_pages))
    series_pages = [index + 1 for index, text in enumerate(page_texts[:limit]) if series in compact(text).upper()]
    if not series_pages:
        raise ValueError(f"filename model {model} is not supported by the PDF text")
    return SemanticValue(model, tuple(series_pages))


def detect_arm_cores(page_texts: list[str], config: dict[str, Any]) -> SemanticValue:
    pattern = re.compile(str(config["core_pattern"]), re.I)
    limit = min(len(page_texts), int(config.get("core_scan_pages", 30)))
    found: dict[str, list[int]] = {}
    for page, text in enumerate(page_texts[:limit], start=1):
        normalized = text.replace("®", "").replace("™", "")
        for match in pattern.finditer(normalized):
            core = f"Cortex-{match.group(1).upper()}"
            found.setdefault(core, []).append(page)
    if not found:
        raise ValueError("no configured Arm Cortex core found in PDF text")
    value = "ARM " + " + ".join(found)
    pages = tuple(sorted({page for items in found.values() for page in items}))
    return SemanticValue(value, pages)
