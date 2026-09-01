from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.text import compact


REQUIRED_KEYS = {"profile_version", "profile_id", "rules_file", "aliases_file", "extractor_module", "detection", "patterns", "model_rules", "series_rules"}
REQUIRED_PATTERNS = {"model", "package", "pin", "concatenation_boundary"}


@dataclass(frozen=True)
class DocumentProfile:
    path: Path
    payload: dict[str, Any]
    rules_file: Path
    aliases_file: Path
    model_re: re.Pattern[str]
    package_re: re.Pattern[str]
    pin_re: re.Pattern[str]
    boundary_re: re.Pattern[str]

    @property
    def profile_id(self) -> str:
        return str(self.payload["profile_id"])

    def model_tokens(self, text: str, exact_only: bool = False) -> list[str]:
        result: list[str] = []
        prepared = self.boundary_re.sub(" ", text)
        strip_prefixes = tuple(self.payload["model_rules"].get("strip_prefixes", []))
        skip_suffixes = tuple(item.upper() for item in self.payload["model_rules"].get("skip_suffixes", []))
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
        for rule in self.payload["model_rules"].get("exact", []):
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
        for rule in self.payload["series_rules"]:
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
        for old, new in self.payload.get("function_replacements", {}).items():
            value = value.replace(old.upper(), str(new).upper())
        value = re.sub(r"UART(\d*)_(TX|RX)", lambda match: f"UART{match.group(1)}_{match.group(2)}" if match.group(1) else f"UART_{match.group(2)}", value)
        return value.strip("/;,，")


def load_profile(path: Path) -> DocumentProfile:
    resolved = path.resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        raise ValueError(f"profile missing required keys: {sorted(missing)}")
    if not isinstance(payload["patterns"], dict):
        raise ValueError("profile patterns must be an object")
    missing_patterns = REQUIRED_PATTERNS - set(payload["patterns"])
    if missing_patterns:
        raise ValueError(f"profile missing required patterns: {sorted(missing_patterns)}")
    base = resolved.parent
    rules_file = (base / payload["rules_file"]).resolve()
    aliases_file = (base / payload["aliases_file"]).resolve()
    if not rules_file.is_file():
        raise ValueError(f"profile rules_file not found: {rules_file}")
    if not aliases_file.is_file():
        raise ValueError(f"profile aliases_file not found: {aliases_file}")
    flags = re.I if payload.get("case_insensitive", True) else 0
    try:
        patterns = payload["patterns"]
        return DocumentProfile(
            path=resolved,
            payload=payload,
            rules_file=rules_file,
            aliases_file=aliases_file,
            model_re=re.compile(patterns["model"], flags),
            package_re=re.compile(patterns["package"], flags),
            pin_re=re.compile(patterns["pin"], flags),
            boundary_re=re.compile(patterns["concatenation_boundary"], flags),
        )
    except re.error as exc:
        raise ValueError(f"invalid profile regex: {exc}") from exc
