from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EXPECTED_SOURCE_TABLES = ("chips", "pin_mux", "peripherals", "electrical", "memory")
SUPPORTED_TRANSFORMS = {None, "jsonb", "external_ram_kb", "nullable_integer", "af_integer"}


class DatabaseMappingError(ValueError):
    """Raised when legacy output cannot be mapped without losing meaning."""


def load_database_mapping(path: Path) -> dict[str, Any]:
    mapping = json.loads(path.read_text(encoding="utf-8"))
    validate_database_mapping(mapping)
    return mapping


def validate_database_mapping(mapping: dict[str, Any]) -> None:
    tables = mapping.get("tables")
    if not isinstance(tables, dict) or tuple(tables) != EXPECTED_SOURCE_TABLES:
        raise DatabaseMappingError(f"database mapping tables must be {EXPECTED_SOURCE_TABLES}")
    audit = mapping.get("audit")
    if not isinstance(audit, dict):
        raise DatabaseMappingError("database mapping requires an audit policy")
    for field in ("source_reviewed_field", "reviewer_field", "status_field", "unreviewed_status", "reviewed_status"):
        if not isinstance(audit.get(field), str) or not audit[field]:
            raise DatabaseMappingError(f"invalid audit policy field {field}")
    target_tables: set[str] = set()
    for source_table, config in tables.items():
        if not isinstance(config, dict) or not isinstance(config.get("target_table"), str):
            raise DatabaseMappingError(f"invalid target table for {source_table}")
        if config["target_table"] in target_tables:
            raise DatabaseMappingError(f"duplicate target table {config['target_table']}")
        target_tables.add(config["target_table"])
        columns = config.get("columns")
        if not isinstance(columns, list) or not columns:
            raise DatabaseMappingError(f"missing column mappings for {source_table}")
        sources: set[str] = set()
        targets: set[str] = set()
        for column in columns:
            if not isinstance(column, dict) or not isinstance(column.get("source"), str) or not isinstance(column.get("target"), str):
                raise DatabaseMappingError(f"invalid column mapping in {source_table}")
            if column["source"] in sources or column["target"] in targets:
                raise DatabaseMappingError(f"duplicate column mapping in {source_table}: {column}")
            if column.get("transform") not in SUPPORTED_TRANSFORMS:
                raise DatabaseMappingError(f"unsupported transform in {source_table}: {column.get('transform')}")
            merge_fields = column.get("merge_fields", [])
            if not isinstance(merge_fields, list):
                raise DatabaseMappingError(f"merge_fields must be an array in {source_table}")
            merge_targets: set[str] = set()
            for merged in merge_fields:
                if not isinstance(merged, dict) or not isinstance(merged.get("source"), str) or not isinstance(merged.get("target"), str):
                    raise DatabaseMappingError(f"invalid merge_fields entry in {source_table}")
                if merged["target"] in merge_targets:
                    raise DatabaseMappingError(f"duplicate merge_fields target in {source_table}: {merged['target']}")
                merge_targets.add(merged["target"])
            sources.add(column["source"])
            targets.add(column["target"])
        for audit_target in (audit["reviewer_field"], audit["status_field"]):
            if audit_target in targets:
                raise DatabaseMappingError(f"audit target {audit_target} is duplicated in {source_table}")
        column_order = config.get("column_order")
        if not isinstance(column_order, list) or not column_order or len(column_order) != len(set(column_order)):
            raise DatabaseMappingError(f"invalid column_order for {source_table}")
        expected_targets = targets | set(config.get("database_managed_columns", [])) | {
            audit["reviewer_field"], audit["status_field"]
        }
        if set(column_order) != expected_targets:
            raise DatabaseMappingError(
                f"column_order for {source_table} must contain every mapped, audit, and database-managed column exactly once"
            )


def _jsonb_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _transform(value: Any, transform: str | None) -> Any:
    if transform == "jsonb":
        return _jsonb_value(value)
    if transform == "nullable_integer":
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and float(value).is_integer():
            return int(value)
        text = str(value).strip()
        return int(text) if text.isdigit() else None
    if transform == "af_integer":
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and float(value).is_integer():
            return int(value)
        text = str(value).strip().upper()
        return int(text[2:]) if text.startswith("AF") and text[2:].isdigit() else None
    return value


def _mapped_value(
    row: dict[str, Any],
    column: dict[str, Any],
    external_ram_by_chip: dict[str, Any],
    detailed_ram_by_chip: dict[str, set[Any]],
) -> Any:
    transform = column.get("transform")
    if transform == "external_ram_kb":
        value = external_ram_by_chip.get(str(row.get("chip_id")), row.get(column["source"]))
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DatabaseMappingError(
                f"chips {_row_identity(row)}: external RAM {value!r} cannot be represented by database integer ram_kb"
            )
        if not float(value).is_integer():
            chip_id = str(row.get("chip_id"))
            if value not in detailed_ram_by_chip.get(chip_id, set()):
                raise DatabaseMappingError(
                    f"chips {_row_identity(row)}: non-integer RAM {value!r} is not preserved in the memory table"
                )
            # The database summary column is integer-only.  Keep it empty
            # rather than round a real value; the exact numeric value remains
            # available in ssot_mcu_memory.
            return None
        value = int(value)
    else:
        value = _transform(row.get(column["source"]), transform)
    merge_fields = column.get("merge_fields", [])
    if not merge_fields:
        return value
    if value in (None, "", []):
        result: dict[str, Any] = {}
    elif isinstance(value, dict):
        result = dict(value)
    else:
        result = {"values": value}
    for merged in merge_fields:
        merged_value = row.get(merged["source"])
        if merged_value in (None, "", []):
            continue
        target = merged["target"]
        if target in result and result[target] != merged_value:
            raise DatabaseMappingError(f"jsonb merge target {target} would overwrite a different value")
        result[target] = merged_value
    return result or None


def _row_identity(row: dict[str, Any]) -> str:
    keys = ("chip_id", "pin_name", "function_name", "peripheral_type", "param_name", "memory_type")
    return "/".join(str(row[key]) for key in keys if row.get(key) not in (None, "")) or "unknown row"


def map_data_to_database(
    data: dict[str, list[dict[str, Any]]],
    mapping: dict[str, Any],
    *,
    reviewer: str | None = None,
) -> dict[str, Any]:
    """Map the stable legacy five-table contract to insert-ready cpdb-test rows.

    The mapper is deliberately loss-intolerant for business fields unsupported by
    the target database. In particular, populated channel_count values block the
    export until the database schema gains that column.
    """
    validate_database_mapping(mapping)
    if tuple(data) != EXPECTED_SOURCE_TABLES:
        raise DatabaseMappingError(f"source data tables must be {EXPECTED_SOURCE_TABLES}")
    audit = mapping["audit"]
    external_ram_by_chip: dict[str, Any] = {}
    detailed_ram_by_chip: dict[str, set[Any]] = {}
    for memory_row in data["memory"]:
        if str(memory_row.get("memory_type", "")).startswith("SRAM"):
            detailed_ram_by_chip.setdefault(str(memory_row.get("chip_id")), set()).add(memory_row.get("size_kb"))
        if memory_row.get("memory_type") != "SRAM_External":
            continue
        chip_id = str(memory_row.get("chip_id"))
        size_kb = memory_row.get("size_kb")
        if chip_id in external_ram_by_chip and external_ram_by_chip[chip_id] != size_kb:
            raise DatabaseMappingError(f"memory {chip_id}: conflicting SRAM_External sizes")
        external_ram_by_chip[chip_id] = size_kb
    reviewer_value = reviewer.strip() if isinstance(reviewer, str) else None
    if reviewer is not None and not reviewer_value:
        raise DatabaseMappingError("reviewer must not be blank")

    target_tables: dict[str, list[dict[str, Any]]] = {}
    ingestion_times: set[str] = set()
    for source_table, table_config in mapping["tables"].items():
        target_table = table_config["target_table"]
        target_rows: list[dict[str, Any]] = []
        for row in data[source_table]:
            if not isinstance(row, dict):
                raise DatabaseMappingError(f"invalid row in {source_table}")
            identity = _row_identity(row)
            for field in table_config.get("reject_if_populated", []):
                if row.get(field) not in (None, "", []):
                    raise DatabaseMappingError(
                        f"{source_table} {identity}: target database has no {field} column; refusing lossy mapping"
                    )
            reviewed = row.get(audit["source_reviewed_field"])
            if not isinstance(reviewed, bool):
                raise DatabaseMappingError(f"{source_table} {identity}: is_reviewed must be boolean")
            status = audit["reviewed_status"] if reviewed else audit["unreviewed_status"]
            if status in audit.get("reviewer_required_for", []) and not reviewer_value:
                raise DatabaseMappingError(
                    f"{source_table} {identity}: reviewer is required before audit_status={status}"
                )
            target_row = {
                column["target"]: _mapped_value(row, column, external_ram_by_chip, detailed_ram_by_chip)
                for column in table_config["columns"]
            }
            target_row[audit["reviewer_field"]] = reviewer_value if reviewed else None
            target_row[audit["status_field"]] = status
            for column in table_config.get("database_managed_columns", []):
                target_row[column] = None
            target_row = {column: target_row[column] for column in table_config["column_order"]}
            target_rows.append(target_row)
            if isinstance(row.get("ingested_at"), str) and row["ingested_at"]:
                ingestion_times.add(row["ingested_at"])
        target_tables[target_table] = target_rows

    return {
        "mapping_version": mapping["mapping_version"],
        "target": mapping["target"],
        "source_ingested_at": sorted(ingestion_times),
        "warnings": [
            "source ingested_at is retained at batch level because cpdb-test has no semantically equivalent column"
        ] if ingestion_times else [],
        "tables": target_tables,
        "summary": {table: len(rows) for table, rows in target_tables.items()},
    }


def map_payload_to_database(
    payload: dict[str, Any],
    mapping: dict[str, Any],
    *,
    reviewer: str | None = None,
) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DatabaseMappingError("extraction payload has no data object")
    return map_data_to_database(data, mapping, reviewer=reviewer)


def database_workbook_payload(
    mapped: dict[str, Any], mapping: dict[str, Any]
) -> dict[str, Any]:
    """Expose database-shaped rows under the stable five workbook tab names."""
    validate_database_mapping(mapping)
    tables = mapped.get("tables")
    if not isinstance(tables, dict):
        raise DatabaseMappingError("mapped payload has no target tables")
    data: dict[str, list[dict[str, Any]]] = {}
    for source_table, table_config in mapping["tables"].items():
        target_table = table_config["target_table"]
        rows = tables.get(target_table)
        if not isinstance(rows, list):
            raise DatabaseMappingError(f"mapped payload is missing target table {target_table}")
        data[source_table] = rows
    return {
        "mapping_version": mapped.get("mapping_version"),
        "target": mapped.get("target"),
        "source_ingested_at": mapped.get("source_ingested_at", []),
        "warnings": mapped.get("warnings", []),
        "data": data,
        "summary": {name: len(rows) for name, rows in data.items()},
    }
