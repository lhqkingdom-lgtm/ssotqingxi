from __future__ import annotations

import argparse
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


CHECKED_FIELDS = ("flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max")


def classify(field: str, row: dict[str, Any], memory_types: set[str]) -> tuple[str, str]:
    if field == "flash_kb" and "MTP" in memory_types:
        return "routed_to_other_field", "原文为 MTP，已写入 memory.MTP，不能当作 Flash。"
    if field == "package_types" and row.get(field) == []:
        return "needs_source_review", "当前证据未定位到与该型号绑定的封装。"
    return "not_found", "当前规则未从原文证据中取得该字段；需查看对应证据页确认是否应补全。"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a compact missing-field audit without modifying Excel.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-html", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.extracted.read_text(encoding="utf-8"))
    memory_by_chip: dict[str, set[str]] = defaultdict(set)
    for row in payload["data"].get("memory", []):
        memory_by_chip[str(row.get("chip_id"))].add(str(row.get("memory_type")))
    items = []
    counts: Counter[str] = Counter()
    for row in payload["data"].get("chips", []):
        for field in CHECKED_FIELDS:
            value = row.get(field)
            if value not in (None, "", []):
                continue
            category, explanation = classify(field, row, memory_by_chip.get(str(row.get("chip_id")), set()))
            counts[category] += 1
            items.append({
                "chip_id": row.get("chip_id"),
                "field": field,
                "category": category,
                "explanation": explanation,
                "pdf_source_file": row.get("pdf_source_file"),
                "evidence_pages": row.get("evidence_pages"),
            })
    report = {"summary": dict(counts), "items": items}
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(item.get(key) or ''))}</td>" for key in ("chip_id", "field", "category", "pdf_source_file", "evidence_pages", "explanation")) + "</tr>"
        for item in items
    )
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>空值审计</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:24px;color:#172033}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8e0ea;padding:8px;text-align:left;vertical-align:top}}th{{background:#1f4e78;color:white;position:sticky;top:0}}tr:nth-child(even){{background:#f4f7fb}}.summary{{margin:12px 0;padding:12px;background:#eef5fb}}</style>
<h1>芯片字段空值审计</h1><div class='summary'>分类统计：{html.escape(json.dumps(dict(counts), ensure_ascii=False))}</div>
<table><thead><tr><th>chip_id</th><th>字段</th><th>分类</th><th>PDF</th><th>证据页</th><th>说明</th></tr></thead><tbody>{rows}</tbody></table></html>"""
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.write_text(page, encoding="utf-8")
    print(json.dumps({"items": len(items), "summary": dict(counts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
