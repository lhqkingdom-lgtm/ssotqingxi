from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pdfplumber


SHEET_LABELS = {
    "chips": "芯片参数",
    "pin_mux": "管脚复用",
    "peripherals": "外设",
    "electrical": "电气参数",
    "memory": "存储器",
}

KIND_LABELS = {
    "model": "型号、存储与外设",
    "electrical": "电气参数",
    "pin": "管脚与复用功能",
}


def version_rank(version: str) -> int:
    parts = [int(part) for part in str(version or "0").split(".") if part.isdigit()]
    return sum(part * (1000 ** (3 - index)) for index, part in enumerate(parts[:4]))


def row_anchor(sheet: str, row: dict[str, Any], schema: dict[str, Any]) -> str:
    values = [sheet, *(row.get(field) for field in schema["sheets"][sheet]["key"])]
    canonical = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return "row-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def natural_key(value: Any) -> list[Any]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(value or ""))]


def render_evidence(item: dict[str, Any], output_path: Path, resolution: int) -> None:
    if output_path.exists() and output_path.stat().st_size > 0:
        return
    with pdfplumber.open(item["source_file"]) as pdf:
        page = pdf.pages[int(item["page"]) - 1]
        x0, top, x1, bottom = [float(value) for value in item["bbox"]]
        padding = 16
        crop_box = (
            max(0.0, x0 - padding),
            max(0.0, top - padding),
            min(float(page.width), x1 + padding),
            min(float(page.height), bottom + padding),
        )
        page.crop(crop_box).to_image(resolution=resolution, antialias=True).save(output_path, format="PNG")


def consecutive_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda value: (int(value["page"]), value["evidence_id"])):
        if not groups or int(item["page"]) > int(groups[-1][-1]["page"]) + 1:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def public_row(
    row: dict[str, Any],
    sheet: str,
    schema: dict[str, Any],
    evidence_id: str,
    evidence_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = schema["sheets"][sheet]["fields"]
    result = {field: row.get(field) for field in fields}
    result["_anchor"] = row_anchor(sheet, row, schema)
    result["_evidence_id"] = evidence_id
    result["_field_evidence"] = row.get("_field_evidence", {})
    result["_field_pages"] = {
        field: [
            {"evidence_id": item_id, "page": evidence_by_id[item_id]["page"], "source_file": Path(evidence_by_id[item_id]["source_file"]).name}
            for item_id in item_ids
            if item_id in evidence_by_id
        ]
        for field, item_ids in row.get("_field_evidence", {}).items()
    }
    result["_confidence"] = row.get("_confidence", "")
    return result


def group_pin_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row.get("chip_id"), row.get("package_type"), row.get("pin_name"), row.get("pin_number"), row.get("default_function"))].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        functions = sorted({str(item.get("function_name") or "") for item in items if item.get("function_name")}, key=natural_key)
        row = {
            "chip_id": key[0],
            "package_type": key[1],
            "pin_name": key[2],
            "pin_number": key[3],
            "function_name": functions,
            "is_supported": all(bool(item.get("is_supported")) for item in items),
            "af_code": None,
            "default_function": key[4],
            "constraints": None,
            "_anchor": items[0]["_anchor"],
            "_evidence_id": items[0]["_evidence_id"],
            "_field_evidence": {
                field: list(dict.fromkeys(
                    evidence_id
                    for item in items
                    for evidence_id in item.get("_field_evidence", {}).get(field, [])
                ))
                for field in ("chip_id", "pin_name", "pin_number", "function_name", "is_supported", "default_function")
            },
            "_field_pages": {
                field: [page for item in items for page in item.get("_field_pages", {}).get(field, [])]
                for field in ("chip_id", "pin_name", "pin_number", "function_name", "is_supported", "default_function")
            },
            "_confidence": min((str(item.get("_confidence") or "Z") for item in items), default=""),
            "_record_count": len(items),
        }
        output.append(row)
    output.sort(key=lambda row: (natural_key(row.get("chip_id")), float(row.get("pin_number") or 0), natural_key(row.get("pin_name"))))
    return output


def focus_term(sheet: str, row: dict[str, Any]) -> str:
    return str(row.get("pin_name") if sheet == "pin_mux" else row.get("chip_id") or "")


def attach_focus(cards: list[dict[str, Any]], evidence_by_id: dict[str, dict[str, Any]]) -> None:
    rows_by_evidence: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for card in cards:
        for sheet, rows in card["tables"].items():
            for row in rows:
                rows_by_evidence[row.get("_evidence_id", "")].append((sheet, row))
    for evidence_id, targets in rows_by_evidence.items():
        item = evidence_by_id.get(evidence_id)
        if not item:
            continue
        x0, top, x1, bottom = [float(value) for value in item["bbox"]]
        crop_top = max(0.0, top - 16)
        crop_bottom = bottom + 16
        try:
            with pdfplumber.open(item["source_file"]) as pdf:
                page = pdf.pages[int(item["page"]) - 1]
                crop_bottom = min(float(page.height), crop_bottom)
                cache: dict[str, list[dict[str, Any]]] = {}
                for sheet, row in targets:
                    term = focus_term(sheet, row)
                    if not term:
                        continue
                    if term not in cache:
                        matches = page.search(re.escape(term), regex=True, case=False) or []
                        cache[term] = [match for match in matches if match.get("top", -1) >= top - 4 and match.get("bottom", 1e9) <= bottom + 4]
                    if cache[term]:
                        match = cache[term][0]
                        height = max(1.0, crop_bottom - crop_top)
                        row["_focus"] = {
                            "evidence_id": evidence_id,
                            "y": max(0.0, min(1.0, ((float(match["top"]) + float(match["bottom"])) / 2 - crop_top) / height)),
                        }
        except Exception:
            continue


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCU 参数原文复核</title>
<style>
:root{--nav:#16324a;--blue:#1f5f8b;--cyan:#dff3fb;--bg:#edf2f6;--line:#cfd9e2;--muted:#637381;--ok:#177245;--okbg:#e8f7ef;--bad:#b42318;--badbg:#fff0ee}*{box-sizing:border-box}html,body{height:100%;margin:0}body{font:14px/1.45 "Microsoft YaHei","Segoe UI",sans-serif;color:#17212b;background:var(--bg);overflow:hidden}button,input,select{font:inherit}header{height:94px;background:var(--nav);color:#fff;display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;padding:9px 18px;box-shadow:0 2px 8px #0003}.brand{display:flex;align-items:baseline;gap:14px;width:100%}h1{font-size:19px;margin:0;white-space:nowrap}.headstats{font-size:12px;color:#d5e2ec;white-space:nowrap}.toolbar{width:100%;display:flex;align-items:center;gap:8px;min-width:0}.search{width:min(400px,32vw);padding:7px 10px;border:1px solid #ffffff44;border-radius:7px;background:#fff;color:#17212b}.filterselect{max-width:230px;padding:7px 9px;border:1px solid #ffffff55;border-radius:7px;background:#fff;color:#17212b}.action{border:1px solid #ffffff55;background:#ffffff17;color:#fff;border-radius:7px;padding:7px 10px;cursor:pointer;white-space:nowrap;margin-left:auto}.layout{height:calc(100% - 94px);display:grid;grid-template-columns:310px minmax(0,1fr)}aside{background:#f8fafc;border-right:1px solid var(--line);display:flex;flex-direction:column;min-width:0}.sidehead{padding:11px 12px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;color:var(--muted)}#cards{overflow:auto;padding:8px}.navitem{width:100%;text-align:left;border:1px solid transparent;background:transparent;border-radius:8px;padding:9px 10px;margin-bottom:5px;cursor:pointer}.navitem:hover{background:#edf4f8}.navitem.active{background:#e0f0fa;border-color:#9ac9e4}.navtitle{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.navmeta{font-size:12px;color:var(--muted);margin-top:3px;display:flex;gap:7px}.badge{display:inline-flex;align-items:center;border-radius:999px;padding:1px 7px;font-size:11px;font-weight:700}.badge.ok{color:var(--ok);background:var(--okbg)}.badge.pending{color:#52616d;background:#e9eef2}.badge.issue{color:var(--bad);background:var(--badbg)}.workspace{min-width:0;display:grid;grid-template-rows:auto minmax(0,1fr)}.cardbar{background:#fff;border-bottom:1px solid var(--line);padding:10px 14px;display:flex;align-items:center;gap:12px}.cardtitle{font-size:17px;font-weight:800}.doc{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.cardmeta{margin-left:auto;color:var(--muted);white-space:nowrap}.reviewbtn{border:1px solid var(--line);border-radius:7px;padding:7px 12px;background:#fff;cursor:pointer}.reviewbtn.pass.active,.reviewbtn.pass:hover{background:var(--okbg);border-color:#84c4a2;color:var(--ok)}.reviewbtn.issue.active,.reviewbtn.issue:hover{background:var(--badbg);border-color:#e7a39d;color:var(--bad)}.split{min-height:0;display:grid;grid-template-columns:minmax(420px,56%) minmax(440px,44%);gap:1px;background:var(--line)}.evidencepane,.resultpane{background:#fff;min-height:0;display:flex;flex-direction:column}.panehead{height:44px;padding:7px 11px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px;font-weight:700}.hint{font-size:12px;font-weight:400;color:var(--muted);margin-left:auto}.imagetabs,.sheettabs{display:flex;gap:5px;overflow:auto}.tab{border:1px solid var(--line);border-radius:6px;background:#f5f8fa;padding:5px 8px;white-space:nowrap;cursor:pointer}.tab.active{background:var(--blue);border-color:var(--blue);color:#fff}.imageviewport{position:relative;flex:1;overflow:auto;background:#dfe6eb;padding:14px}.imagestage{position:relative;margin:auto;width:max-content;max-width:100%}.imagestage img{display:block;max-width:none;width:100%;min-width:720px;background:#fff;box-shadow:0 3px 12px #2344}.focusline{display:none;position:absolute;left:0;right:0;height:24px;transform:translateY(-50%);background:#ffea0060;border:2px solid #e58b00;box-shadow:0 0 0 9999px #00000008;pointer-events:none}.resultbody{flex:1;min-height:0;display:grid;grid-template-rows:minmax(150px,42%) minmax(220px,58%)}.recordlist{overflow:auto;border-bottom:1px solid var(--line)}.record{width:100%;display:grid;grid-template-columns:minmax(150px,1.1fr) minmax(180px,1.8fr) auto;gap:10px;align-items:center;text-align:left;border:0;border-bottom:1px solid #e5ebf0;background:#fff;padding:8px 11px;cursor:pointer}.record:hover{background:#f3f8fb}.record.active{background:var(--cyan);box-shadow:inset 4px 0 var(--blue)}.recordkey{font-weight:700}.recordsummary{color:#52616d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.compare{overflow:auto;padding:10px 12px}.comparetitle{display:flex;align-items:center;gap:8px;margin-bottom:8px;font-weight:800}.comparegrid{display:grid;grid-template-columns:180px minmax(240px,1fr);border:1px solid var(--line);border-radius:7px;overflow:hidden}.cell{padding:7px 9px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);min-width:0;word-break:break-word}.cell.head{background:#edf3f7;font-weight:700;position:sticky;top:0}.fieldvalue{width:100%;border:0;border-right:1px solid var(--line);border-bottom:1px solid var(--line);background:transparent;text-align:left;padding:7px 9px;cursor:pointer}.fieldvalue:hover,.fieldvalue.active{background:#dff3fb}.fieldpage{float:right;color:#637381;font-size:11px}.empty{padding:30px;color:var(--muted);text-align:center}.shortcuts{font-size:11px;color:#cbd8e2}.modal{display:none;position:fixed;inset:0;background:#000d;z-index:50;padding:20px;overflow:auto}.modal.open{display:flex;justify-content:center;align-items:flex-start}.modal img{max-width:none;background:#fff;box-shadow:0 8px 30px #000}.zoom{border:1px solid var(--line);background:#fff;border-radius:5px;padding:4px 8px;cursor:pointer}
@media(max-width:1100px){.layout{grid-template-columns:250px minmax(0,1fr)}.split{grid-template-columns:1fr}.evidencepane{display:none}.search{width:230px}}
</style></head><body><header><div class="brand"><h1>MCU 参数原文复核</h1><div class="headstats" id="stats"></div><span class="shortcuts">J/K 切换 · 1 通过 · 2 有问题</span></div><div class="toolbar"><input id="search" class="search" placeholder="搜索型号、管脚、文件名"><select id="docFilter" class="filterselect" aria-label="按文档筛选"><option value="">全部文档</option></select><select id="kindFilter" class="filterselect" aria-label="按参数类型筛选"><option value="">全部参数类型</option></select><select id="statusFilter" class="filterselect" aria-label="按复核状态筛选"><option value="">全部复核状态</option><option value="pending">待复核</option><option value="pass">已通过</option><option value="issue">有问题</option></select><button id="export" class="action">导出复核结果</button></div></header><div class="layout"><aside><div class="sidehead"><span>证据区块</span><span id="visibleCount"></span></div><div id="cards"></div></aside><section class="workspace"><div class="cardbar" id="cardbar"></div><div class="split"><div class="evidencepane"><div class="panehead"><span>PDF 原始证据（唯一标准）</span><div class="imagetabs" id="imageTabs"></div><button class="zoom" id="zoomOut">−</button><button class="zoom" id="zoomIn">＋</button><span class="hint">选择右侧记录后自动定位并标黄</span></div><div class="imageviewport" id="imageViewport"><div class="imagestage" id="imageStage"><img id="evidenceImage" alt="PDF 原表截图"><div class="focusline" id="focusLine"></div></div></div></div><div class="resultpane"><div class="panehead"><span>本次提取结果</span><div class="sheettabs" id="sheetTabs"></div><span class="hint" id="sheetCount"></span></div><div class="resultbody"><div class="recordlist" id="recordList"></div><div class="compare" id="compare"></div></div></div></div></section></div><div class="modal" id="modal"><img id="modalImg" alt="证据大图"></div><script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent);const $=id=>document.getElementById(id);const fmt=v=>v===null||v===undefined||v===''?'':Array.isArray(v)?v.join(' · '):String(v);const labels=data.field_labels;let query='',docFilter='',kindFilter='',statusFilter='',activeId=data.cards[0]?.id,activeSheet='',activeRow=0,activeImage=0,activeField='',zoom=1;const storeKey='mcu-review-v4:'+data.run_id;let reviews=JSON.parse(localStorage.getItem(storeKey)||'{}');
function save(){localStorage.setItem(storeKey,JSON.stringify(reviews))}function hay(card){return(card.document+' '+card.title+' '+JSON.stringify(card.tables)).toLowerCase()}function visible(){return data.cards.filter(card=>(!query||hay(card).includes(query))&&(!docFilter||card.document===docFilter)&&(!kindFilter||card.kind===kindFilter)&&(!statusFilter||statusFilter==='pending'&&!reviews[card.id]||reviews[card.id]?.status===statusFilter))}function current(){return data.cards.find(card=>card.id===activeId)||visible()[0]}function statusBadge(card){const s=reviews[card.id]?.status;if(s==='pass')return'<span class="badge ok">已通过</span>';if(s==='issue')return'<span class="badge issue">有问题</span>';return'<span class="badge pending">待复核</span>'}
function renderSidebar(){const list=visible();$('visibleCount').textContent=list.length+'/'+data.cards.length;$('cards').innerHTML=list.map(card=>`<button class="navitem ${card.id===activeId?'active':''}" data-id="${card.id}"><div class="navtitle">${card.document}</div><div class="navmeta"><span>${card.title}</span>${statusBadge(card)}</div></button>`).join('')||'<div class="empty">没有匹配项</div>';$('cards').querySelectorAll('.navitem').forEach(btn=>btn.onclick=()=>{activeId=btn.dataset.id;activeSheet='';activeRow=0;activeImage=0;render()})}
function recordTitle(sheet,row){if(sheet==='chips')return fmt(row.chip_id);if(sheet==='pin_mux')return`${fmt(row.chip_id)} · ${fmt(row.package_type)} · ${fmt(row.pin_name)} · 脚${fmt(row.pin_number)}`;if(sheet==='peripherals')return`${fmt(row.chip_id)} · ${fmt(row.peripheral_type)}`;if(sheet==='electrical')return`${fmt(row.chip_id)} · ${fmt(row.param_name)}`;return`${fmt(row.chip_id)} · ${fmt(row.memory_type)}`}function recordSummary(sheet,row){if(sheet==='chips')return`Flash ${fmt(row.flash_kb)} KB · RAM ${fmt(row.ram_kb)} KB · ${fmt(row.voltage_min)}–${fmt(row.voltage_max)} V · ${fmt(row.package_types)}`;if(sheet==='pin_mux')return fmt(row.function_name);if(sheet==='peripherals')return`实例 ${fmt(row.instance_count)} · 通道 ${fmt(row.channel_count)} · 分辨率 ${fmt(row.max_resolution)}`;if(sheet==='electrical')return`${fmt(row.min_value)} ～ ${fmt(row.max_value)} ${fmt(row.unit)}`;return`${fmt(row.size_kb)} KB`}
function renderCard(){const card=current();if(!card){$('cardbar').innerHTML='<div class="empty">没有匹配项</div>';return}activeId=card.id;const st=reviews[card.id]?.status;$('cardbar').innerHTML=`<span class="cardtitle">${card.title}</span><span class="doc">${card.document}</span><span class="cardmeta">PDF 第 ${card.pages.join('、')} 页 · ${card.row_count} 条原始记录</span><button class="reviewbtn pass ${st==='pass'?'active':''}" id="pass">通过</button><button class="reviewbtn issue ${st==='issue'?'active':''}" id="issue">有问题</button>`;$('pass').onclick=()=>mark('pass');$('issue').onclick=()=>mark('issue');const sheets=Object.keys(card.tables);if(!sheets.includes(activeSheet))activeSheet=sheets[0]||'';$('sheetTabs').innerHTML=sheets.map(sheet=>`<button class="tab ${sheet===activeSheet?'active':''}" data-sheet="${sheet}">${data.sheet_labels[sheet]}</button>`).join('');$('sheetTabs').querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>{activeSheet=btn.dataset.sheet;activeRow=0;renderResults()});renderImages();renderResults()}
function renderImages(){const card=current();$('imageTabs').innerHTML=card.images.map((img,i)=>`<button class="tab ${i===activeImage?'active':''}" data-index="${i}">第${img.page}页 · ${img.role}</button>`).join('');$('imageTabs').querySelectorAll('.tab').forEach(btn=>btn.onclick=()=>{activeImage=Number(btn.dataset.index);showImage()});showImage()}
function showImage(){const card=current(),img=card.images[activeImage]||card.images[0];if(!img)return;$('evidenceImage').src=img.src;$('evidenceImage').style.width=(zoom*100)+'%';$('focusLine').style.display='none';$('evidenceImage').ondblclick=()=>{$('modalImg').src=img.src;$('modal').classList.add('open')};setTimeout(focusSelected,80)}
function renderResults(){const card=current(),rows=card.tables[activeSheet]||[];if(activeRow>=rows.length)activeRow=0;$('sheetCount').textContent=rows.length+' 个可视复核项';$('recordList').innerHTML=rows.map((row,i)=>`<button class="record ${i===activeRow?'active':''}" data-row="${i}"><span class="recordkey">${recordTitle(activeSheet,row)}</span><span class="recordsummary">${recordSummary(activeSheet,row)}</span>${row._confidence?`<span class="badge pending">置信度 ${row._confidence}</span>`:''}</button>`).join('')||'<div class="empty">此证据区块没有结构化结果</div>';$('recordList').querySelectorAll('.record').forEach(btn=>btn.onclick=()=>{activeRow=Number(btn.dataset.row);activeField='';renderResults()});renderCompare();focusSelected()}
function renderCompare(){const rows=current().tables[activeSheet]||[],row=rows[activeRow];if(!row){$('compare').innerHTML='';return}const fields=Object.keys(row).filter(k=>!k.startsWith('_')&&fmt(row[k]));let html=`<div class="comparetitle"><span>${recordTitle(activeSheet,row)}</span>${row._confidence?`<span class="badge pending">置信度 ${row._confidence}</span>`:''}<span class="badge pending">点击字段值跳到该字段原文</span></div><div class="comparegrid"><div class="cell head">字段</div><div class="cell head">本次提取值</div>`;for(const f of fields){const pages=(row._field_pages?.[f]||[]).map(x=>x.page).filter((x,i,a)=>a.indexOf(x)===i).join('、');html+=`<div class="cell">${labels[f]||f}</div><button class="fieldvalue ${f===activeField?'active':''}" data-field="${f}">${fmt(row[f])||'—'}${pages?`<span class="fieldpage">PDF p${pages}</span>`:''}</button>`}html+='</div>';$('compare').innerHTML=html;$('compare').querySelectorAll('.fieldvalue').forEach(btn=>btn.onclick=()=>showFieldEvidence(btn.dataset.field))}
function showFieldEvidence(field){activeField=field;const row=(current()?.tables[activeSheet]||[])[activeRow],ids=row?._field_evidence?.[field]||[],card=current(),idx=card.images.findIndex(img=>ids.includes(img.evidence_id));if(idx>=0)activeImage=idx;renderImages();renderCompare()}
function focusSelected(){const row=(current()?.tables[activeSheet]||[])[activeRow],focus=row?._focus,fieldIds=activeField?(row?._field_evidence?.[activeField]||[]):[];if(activeField&&fieldIds.length){const card=current(),idx=card.images.findIndex(img=>fieldIds.includes(img.evidence_id));if(idx>=0&&idx!==activeImage){activeImage=idx;renderImages();return}}if(!focus||activeField&&!fieldIds.includes(focus.evidence_id)){$('focusLine').style.display='none';return}const card=current(),idx=card.images.findIndex(img=>img.evidence_id===focus.evidence_id);if(idx>=0&&idx!==activeImage){activeImage=idx;renderImages();return}const image=$('evidenceImage'),line=$('focusLine');if(!image.complete)return;line.style.display='block';line.style.top=(focus.y*100)+'%';const vp=$('imageViewport');vp.scrollTop=Math.max(0,image.scrollHeight*focus.y-vp.clientHeight/2)}function mark(status){reviews[activeId]={status,updated_at:new Date().toISOString()};save();move(1)}function move(delta){const list=visible(),i=list.findIndex(c=>c.id===activeId),next=list[Math.max(0,Math.min(list.length-1,i+delta))];if(next){activeId=next.id;activeSheet='';activeRow=0;activeImage=0;activeField='';render()}}function render(){renderSidebar();renderCard();$('stats').textContent=`${data.documents}份文档 · ${data.cards.length}个证据区块 · ${data.images}张截图 · ${data.total_rows}条结果`}
function resetToFirst(){const first=visible()[0];activeId=first?.id;activeSheet='';activeRow=0;activeImage=0;render()}const documents=[...new Set(data.cards.map(c=>c.document))].sort((a,b)=>a.localeCompare(b,'zh-CN',{numeric:true}));$('docFilter').innerHTML+=[...documents].map(v=>`<option value="${v}">${v}</option>`).join('');const kinds=[...new Map(data.cards.map(c=>[c.kind,c.title])).entries()];$('kindFilter').innerHTML+=kinds.map(([v,t])=>`<option value="${v}">${t}</option>`).join('');$('search').oninput=e=>{query=e.target.value.trim().toLowerCase();resetToFirst()};$('docFilter').onchange=e=>{docFilter=e.target.value;resetToFirst()};$('kindFilter').onchange=e=>{kindFilter=e.target.value;resetToFirst()};$('statusFilter').onchange=e=>{statusFilter=e.target.value;resetToFirst()};$('zoomIn').onclick=()=>{zoom=Math.min(2.4,zoom+.2);showImage()};$('zoomOut').onclick=()=>{zoom=Math.max(.7,zoom-.2);showImage()};$('modal').onclick=()=>$('modal').classList.remove('open');$('export').onclick=()=>{const blob=new Blob([JSON.stringify({run_id:data.run_id,reviews},null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='review_result.json';a.click();URL.revokeObjectURL(a.href)};document.addEventListener('keydown',e=>{if(e.target.matches('input,select'))return;if(e.key.toLowerCase()==='j')move(1);if(e.key.toLowerCase()==='k')move(-1);if(e.key==='1')mark('pass');if(e.key==='2')mark('issue');if(e.key==='/'){e.preventDefault();$('search').focus()}});render();
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a standalone side-by-side review page.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resolution", type=int, default=170)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_dir = args.output_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    evidences = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence_by_id = {item["evidence_id"]: item for item in evidences}
    source_ranks = {document["doc_id"]: version_rank(document.get("version", "0")) for document in extracted.get("documents", [])}
    source_by_doc = {document["doc_id"]: document["source_file"] for document in extracted.get("documents", [])}
    for item in evidences:
        render_evidence(item, image_dir / f"{item['evidence_id']}.png", args.resolution)

    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidences:
        by_doc[item["doc_id"]].append(item)
    cards: list[dict[str, Any]] = []
    evidence_to_card: dict[str, dict[str, Any]] = {}
    for doc_id, items in sorted(by_doc.items(), key=lambda pair: Path(source_by_doc.get(pair[0], pair[0])).name.casefold()):
        basic = [item for item in items if item["kind"] == "basic"]
        model = [item for item in items if item["kind"] == "model"]
        electrical = [item for item in items if item["kind"] == "electrical"]
        packages = [item for item in items if item["kind"] == "package"]
        pins = [item for item in items if item["kind"] == "pin"]
        definitions: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
        if basic or model or packages:
            definitions.append(("model", basic + model + packages, basic + model + packages + electrical))
        if electrical:
            definitions.append(("electrical", electrical, model + electrical))
        definitions.extend(("pin", group, model + group) for group in consecutive_groups(pins))
        for sequence, (kind, owning, display) in enumerate(definitions, start=1):
            card = {
                "id": f"card-{doc_id}-{kind}-{sequence}", "kind": kind, "title": KIND_LABELS[kind],
                "document": Path(source_by_doc.get(doc_id, display[0]["source_file"])).name,
                "pages": sorted({int(item["page"]) for item in display}),
                "images": [{"src": f"images/{item['evidence_id']}.png", "page": item["page"], "role": "关联型号表" if item not in owning else "直接来源", "evidence_id": item["evidence_id"]} for item in display],
                "tables": {sheet: [] for sheet in schema["sheets"]}, "row_count": 0,
            }
            cards.append(card)
            for item in owning:
                evidence_to_card[item["evidence_id"]] = card

    for sheet, rows in extracted["data"].items():
        for row in rows:
            candidates = [evidence_by_id[eid] for eid in row.get("_evidence_ids", []) if eid in evidence_by_id]
            if not candidates:
                continue
            rank = int(row.get("_source_rank") or 0)
            preferred = [item for item in candidates if source_ranks.get(item["doc_id"], 0) == rank] or candidates
            evidence = next((item for item in preferred if evidence_to_card.get(item["evidence_id"])), None)
            if not evidence:
                continue
            card = evidence_to_card[evidence["evidence_id"]]
            card["tables"][sheet].append(public_row(row, sheet, schema, evidence["evidence_id"], evidence_by_id))
            card["row_count"] += 1

    for card in cards:
        if card["tables"]["pin_mux"]:
            card["tables"]["pin_mux"] = group_pin_rows(card["tables"]["pin_mux"])
        card["tables"] = {sheet: rows for sheet, rows in card["tables"].items() if rows}
    attach_focus(cards, evidence_by_id)
    field_labels = {
        "chip_id": "芯片型号", "series": "系列", "core": "内核", "flash_kb": "Flash (KB)", "ram_kb": "RAM (KB)",
        "max_freq_mhz": "最高频率 (MHz)", "package_types": "封装", "voltage_min": "最低电压", "voltage_max": "最高电压",
        "package_type": "封装", "pin_name": "管脚名", "pin_number": "脚号", "function_name": "复用功能", "is_supported": "支持", "af_code": "AF 编码",
        "default_function": "默认功能", "constraints": "限制", "peripheral_type": "外设类型", "instance_count": "实例数", "channel_count": "通道数",
        "max_resolution": "最高分辨率", "max_frequency_mhz": "最高频率", "features": "特性", "param_name": "参数名",
        "min_value": "最小值", "max_value": "最大值", "unit": "单位", "conditions": "条件", "memory_type": "存储类型",
        "size_kb": "容量 (KB)", "organization": "组织形式",
    }
    payload = {
        "run_id": hashlib.sha1(str(args.extracted.resolve()).encode("utf-8")).hexdigest()[:12],
        "documents": len(by_doc), "cards": cards, "images": len(evidences),
        "total_rows": sum(len(rows) for rows in extracted["data"].values()),
        "sheet_labels": SHEET_LABELS, "field_labels": field_labels,
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (args.output_dir / "index.html").write_text(HTML.replace("__DATA__", data_json), encoding="utf-8")
    manifest = {
        "documents": payload["documents"], "cards": len(cards), "images": len(evidences), "rows": payload["total_rows"],
        "visual_review_items": sum(sum(len(rows) for rows in card["tables"].values()) for card in cards),
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output_dir / "index.html"), **manifest}, ensure_ascii=True))


if __name__ == "__main__":
    main()
