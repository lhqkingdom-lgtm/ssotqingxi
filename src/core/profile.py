from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.field_dictionary import load_field_dictionary
from core.text import compact


REQUIRED_KEYS = {"profile_version", "profile_id", "config_version", "adapter", "detection", "dictionary", "model", "syntax", "topologies"}
REQUIRED_MODEL_KEYS = {"pattern", "concatenation_boundary", "exact", "series", "aliases_file"}
REQUIRED_SYNTAX_KEYS = {"package_pattern", "pin_pattern"}
ALLOWED_TOP_LEVEL_KEYS = REQUIRED_KEYS | {"description", "normalization", "execution"}
FORBIDDEN_PROFILE_KEYS = {
    "rules_file",
    "field_dictionary_file",
    "extractor_module",
    "patterns",
    "model_rules",
    "series_rules",
    "page",
    "pages",
    "bbox",
    "coordinates",
    "row_index",
    "column_index",
    "fixed_row",
    "fixed_column",
    "expected",
    "expected_value",
    "pin_count_codes",
    "flash_code_kb",
    "package_type_codes",
}


def _forbidden_profile_paths(value: Any, path: str = "profile") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in FORBIDDEN_PROFILE_KEYS:
                result.append(child)
            result.extend(_forbidden_profile_paths(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(_forbidden_profile_paths(item, f"{path}[{index}]"))
    return result


@dataclass(frozen=True)
class DocumentProfile:
    path: Path
    payload: dict[str, Any]
    aliases_file: Path
    model_re: re.Pattern[str]
    package_re: re.Pattern[str]
    pin_re: re.Pattern[str]
    boundary_re: re.Pattern[str]

    @property
    def field_dictionary_file(self) -> Path:
        reference = self.payload.get("dictionary", {}).get("base")
        if not reference:
            raise ValueError(f"profile has no field_dictionary_file: {self.path}")
        path = (self.path.parent / str(reference)).resolve()
        if not path.is_file():
            raise ValueError(f"common field dictionary not found: {path}")
        return path

    @property
    def field_aliases(self) -> dict[str, list[str]]:
        return self.payload.get("dictionary", {}).get("aliases", {})

    @property
    def adapter(self) -> str:
        return str(self.payload["adapter"])

    @property
    def config_version(self) -> str:
        return str(self.payload["config_version"])

    def topology_preset(self, name: str) -> Path:
        reference = self.payload.get("topologies", {}).get("presets", {}).get(name)
        if not reference:
            raise ValueError(f"profile has no {name!r} topology preset: {self.path}")
        path = (self.path.parent / str(reference)).resolve()
        if not path.is_file():
            raise ValueError(f"topology preset not found: {path}")
        return path

    @property
    def profile_id(self) -> str:
        return str(self.payload["profile_id"])

    def model_tokens(self, text: str, exact_only: bool = False) -> list[str]:
        result: list[str] = []
        prepared = self.boundary_re.sub(" ", text)
        model_config = self.payload["model"]
        strip_prefixes = tuple(model_config.get("strip_prefixes", []))
        skip_suffixes = tuple(item.upper() for item in model_config.get("skip_suffixes", []))
        for match in self.model_re.finditer(prepared):
            token = compact(match.group(0))
            for prefix in strip_prefixes:
                token = re.sub(rf"^{re.escape(prefix)}", "", token, flags=re.I)
            if any(token.upper().endswith(suffix) for suffix in skip_suffixes):
                continue
            if exact_only and not self.is_exact_model(token):
                continue
            if token not in result:
                result.append(token)
        return result

    def is_exact_model(self, token: str) -> bool:
        upper = token.upper()
        for rule in self.payload["model"].get("exact", []):
            prefix = str(rule["prefix"]).upper()
            if not upper.startswith(prefix):
                continue
            suffix = upper[len(prefix) :]
            if "min_total_length" in rule and len(upper) < int(rule["min_total_length"]):
                return False
            if len(suffix) < int(rule.get("min_suffix_length", 0)):
                return False
            if rule.get("require_alpha") and not re.search(r"[A-Z]", suffix):
                return False
            if rule.get("require_digit") and not re.search(r"\d", suffix):
                return False
            return True
        return False

    def series_for_model(self, model: str) -> str:
        upper = model.upper()
        for rule in self.payload["model"]["series"]:
            if rule["kind"] == "regex":
                match = re.match(str(rule["pattern"]), model, re.I)
                if match:
                    return match.group(int(rule.get("group", 1)))
                continue
            prefix = str(rule["prefix"])
            if not upper.startswith(prefix.upper()):
                continue
            if rule["kind"] == "prefix":
                return model[: len(prefix)]
            if rule["kind"] == "code":
                code_length = int(rule["code_length"])
                code = model[len(prefix) : len(prefix) + code_length]
                return f"{rule.get('output_prefix', prefix)}{code}"
        return re.sub(r"\d+$", "", model)

    def normalize_function(self, token: str) -> str:
        value = compact(token).upper().replace("[", "").replace("]", "")
        for old, new in self.payload.get("normalization", {}).get("function_aliases", {}).items():
            value = value.replace(old.upper(), str(new).upper())
        value = re.sub(r"UART(\d*)_(TX|RX)", lambda match: f"UART{match.group(1)}_{match.group(2)}" if match.group(1) else f"UART_{match.group(2)}", value)
        return value.strip("/;,，")


def load_profile(path: Path) -> DocumentProfile:
    resolved = path.resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        raise ValueError(f"profile missing required keys: {sorted(missing)}")
    if payload["profile_version"] != 2:
        raise ValueError("only compact document-family profile_version 2 is supported")
    unknown = set(payload) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"profile has unknown top-level keys: {sorted(unknown)}")
    forbidden = _forbidden_profile_paths(payload)
    if forbidden:
        raise ValueError(f"profile contains implementation details: {forbidden}")
    execution = payload.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("profile execution must be an object")
    unknown_execution = set(execution) - {"default", "declarative_adapter"}
    if unknown_execution:
        raise ValueError(f"profile execution has unknown keys: {sorted(unknown_execution)}")
    if execution.get("default", "legacy") not in {"legacy", "shadow", "declarative"}:
        raise ValueError("profile execution.default must be legacy, shadow or declarative")
    if "declarative_adapter" in execution and execution["declarative_adapter"] != "family_extractors.declarative":
        raise ValueError("profile declarative_adapter must use the shared family_extractors.declarative module")
    if not isinstance(payload["model"], dict):
        raise ValueError("profile model must be an object")
    missing_model = REQUIRED_MODEL_KEYS - set(payload["model"])
    if missing_model:
        raise ValueError(f"profile model missing required keys: {sorted(missing_model)}")
    if not isinstance(payload["syntax"], dict):
        raise ValueError("profile syntax must be an object")
    missing_syntax = REQUIRED_SYNTAX_KEYS - set(payload["syntax"])
    if missing_syntax:
        raise ValueError(f"profile syntax missing required keys: {sorted(missing_syntax)}")
    base = resolved.parent
    aliases_file = (base / payload["model"]["aliases_file"]).resolve()
    if not aliases_file.is_file():
        raise ValueError(f"profile aliases_file not found: {aliases_file}")
    dictionary_file = (base / payload["dictionary"]["base"]).resolve()
    if not dictionary_file.is_file():
        raise ValueError(f"common field dictionary not found: {dictionary_file}")
    load_field_dictionary(dictionary_file, payload["dictionary"].get("aliases", {}))
    presets = payload.get("topologies", {}).get("presets", {})
    if not isinstance(payload.get("topologies", {}).get("pipeline"), list) or not isinstance(presets, dict):
        raise ValueError("profile topologies must declare pipeline and presets")
    for reference in presets.values():
        preset_path = (base / str(reference)).resolve()
        if not preset_path.is_file():
            raise ValueError(f"topology preset not found: {preset_path}")
    flags = re.I
    try:
        model = payload["model"]
        syntax = payload["syntax"]
        return DocumentProfile(
            path=resolved,
            payload=payload,
            aliases_file=aliases_file,
            model_re=re.compile(model["pattern"], flags),
            package_re=re.compile(syntax["package_pattern"], flags),
            pin_re=re.compile(syntax["pin_pattern"], flags),
            boundary_re=re.compile(model["concatenation_boundary"], flags),
        )
    except re.error as exc:
        raise ValueError(f"invalid profile regex: {exc}") from exc
