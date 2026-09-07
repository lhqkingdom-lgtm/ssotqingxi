from __future__ import annotations

from typing import Any


def _different(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    return not isinstance(left, (int, float)) or abs(float(left) - float(right)) > tolerance


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
        if "sram_internal" in roles and "sram_external" in roles:
            component_total = roles["sram_internal"] + roles["sram_external"]
            if _different(declared_total, component_total):
                issues.append({
                    "level": "error",
                    "sheet": "memory",
                    "key": [chip_id],
                    "message": f"chips.ram_kb={declared_total} contradicts internal+external RAM={component_total}",
                })
    return issues
