from __future__ import annotations

import re
from typing import Any


def _different(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    return not isinstance(left, (int, float)) or abs(float(left) - float(right)) > tolerance


def _package_summary_identity(value: Any) -> str:
    """Normalize only the auxiliary-ball suffix used inconsistently by summaries.

    Physical pin rows retain the exact package label (for example
    ``UFBGA176+25``).  Cross-section validation compares its base package to a
    marketing summary that may omit the auxiliary power-ball count.
    """
    normalized = str(value or "").replace(" ", "").upper()
    return re.sub(r"\+\d+$", "", normalized)


def validate_data_semantics(data: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Report cross-sheet semantic contradictions without changing data."""
    issues: list[dict[str, Any]] = []
    chip_ram = {row.get("chip_id"): row.get("ram_kb") for row in data.get("chips", [])}
    ram_rows: dict[str, dict[str, float]] = {}
    for row in data.get("memory", []):
        memory_type = str(row.get("memory_type") or "").casefold()
        if memory_type not in {"sram_total", "sram_internal", "sram_external"}:
            continue
        value = row.get("size_kb")
        if not isinstance(value, (int, float)):
            continue
        ram_rows.setdefault(str(row.get("chip_id") or ""), {})[memory_type] = float(value)

    for chip_id, roles in ram_rows.items():
        declared_total = chip_ram.get(chip_id)
        if declared_total is None:
            continue
        if "sram_total" in roles and _different(declared_total, roles["sram_total"]):
            issues.append({
                "level": "error",
                "sheet": "memory",
                "key": [chip_id, "SRAM_Total"],
                "message": f"chips.ram_kb={declared_total} contradicts SRAM_Total={roles['sram_total']}",
            })
        # A source-declared total is authoritative. Internal and external RAM may
        # occupy distinct address spaces and are not universally additive (for
        # example, MCU documentation can report XRAM as the product RAM size
        # while separately describing a small IRAM region).
        if (
            "sram_total" not in roles
            and "sram_internal" in roles
            and "sram_external" in roles
        ):
            component_total = roles["sram_internal"] + roles["sram_external"]
            if _different(declared_total, component_total):
                issues.append({
                    "level": "error",
                    "sheet": "memory",
                    "key": [chip_id],
                    "message": f"chips.ram_kb={declared_total} contradicts internal+external RAM={component_total}",
                })

    declared_packages = {
        str(row.get("chip_id") or ""): {
            _package_summary_identity(package)
            for package in row.get("package_types", [])
            if str(package).strip()
        }
        for row in data.get("chips", [])
    }
    pin_packages: dict[str, set[str]] = {}
    for row in data.get("pin_mux", []):
        package = str(row.get("package_type") or "").replace(" ", "").upper()
        if package:
            pin_packages.setdefault(str(row.get("chip_id") or ""), set()).add(
                _package_summary_identity(package)
            )
    for chip_id, observed in pin_packages.items():
        declared = declared_packages.get(chip_id, set())
        if declared and declared != observed:
            issues.append({
                "level": "error",
                "sheet": "pin_mux",
                "key": [chip_id],
                "message": (
                    "package sources disagree: chips.package_types="
                    f"{sorted(declared)} but pin-definition headers={sorted(observed)}"
                ),
            })
    return issues
