from __future__ import annotations

import argparse
import hashlib
import html
import json
from collections import defaultdict
from pathlib import Path

import pdfplumber


SHEET_FIELDS = {
    "chips": ["chip_id", "series", "core", "flash_kb", "ram_kb", "max_freq_mhz", "package_types", "voltage_min", "voltage_max"],
    "pin_mux": ["chip_id", "pin_name", "pin_number", "function_name", "is_supported", "default_function"],
    "peripherals": ["chip_id", "peripheral_type", "instance_count", "max_resolution", "max_frequency_mhz"],
    "electrical": ["chip_id", "param_name", "min_value", "max_value", "unit"],
    "memory": ["chip_id", "memory_type", "size_kb"],
}

KIND_LABELS = {
    "model": "型号、存储与外设",
    "electrical": "电气参数",
    "pin": "管脚与复用功能",
}


def render_evidence(evidence: dict, output_path: Path, resolution: int = 150) -> None:
    with pdfplumber.open(evidence["source_file"]) as pdf:
        page = pdf.pages[int(evidence["page"]) - 1]
        x0, top, x1, bottom = [float(value) for value in evidence["bbox"]]
        padding = 16
        crop_box = (
            max(0.0, x0 - padding),
            max(0.0, top - padding),
            min(float(page.width), x1 + padding),
            min(float(page.height), bottom + padding),
        )
        cropped = page.crop(crop_box)
        image = cropped.to_image(resolution=resolution, antialias=True)
        image.save(output_path, format="PNG")


def rows_for_evidence(data: dict, evidence_ids: set[str], allowed_sheets: list[str]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for sheet in allowed_sheets:
        selected = []
        for index, row in enumerate(data.get(sheet, [])):
            if evidence_ids.intersection(row.get("_evidence_ids", [])):
                public = {field: row.get(field) for field in SHEET_FIELDS[sheet]}
                public["_row_id"] = f"{sheet}:{index}"
                selected.append(public)
        if selected:
            result[sheet] = selected
    return result


def build_cards(evidences: list[dict], data: dict, asset_dir: Path) -> list[dict]:
    model_items = [item for item in evidences if item["kind"] in {"basic", "model"}]
    electrical_items = [item for item in evidences if item["kind"] == "electrical"]
    pin_items = sorted((item for item in evidences if item["kind"] == "pin"), key=lambda item: (int(item["page"]), item["evidence_id"]))
    groups = []
    if model_items:
        groups.append(("model", model_items, ["chips", "memory", "peripherals"]))
    if electrical_items:
        groups.append(("electrical", electrical_items, ["electrical"]))
    if pin_items:
        groups.append(("pin", pin_items, ["pin_mux"]))

    cards = []
    for sequence, (kind, items, sheets) in enumerate(groups, start=1):
        evidence_ids = {item["evidence_id"] for item in items}
        images = []
        for item in items:
            filename = f"{item['evidence_id']}.png"
            render_evidence(item, asset_dir / filename)
            images.append({
                "src": f"assets/{filename}",
                "page": item["page"],
                "evidence_id": item["evidence_id"],
                "kind": item["kind"],
            })
        pages = sorted({int(item["page"]) for item in items})
        confidences = sorted({item.get("confidence", "") for item in items})
        rows = rows_for_evidence(data, evidence_ids, sheets)
        row_count = sum(len(values) for values in rows.values())
        warnings = []
        if not row_count:
            warnings.append("该证据区块没有关联到结构化结果")
        if "B" in confidences:
            warnings.append("包含上下文推导或通用回退值，建议重点核对")
        cards.append({
            "id": f"card-{sequence}-{kind}",
            "kind": kind,
            "title": KIND_LABELS[kind],
            "pages": pages,
            "confidence": "/".join(confidences),
            "images": images,
            "tables": rows,
            "row_count": row_count,
            "warnings": warnings,
        })
    return cards


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCU 参数复核原型</title>
<style>
:root{--nav:#17324d;--blue:#1f5f99;--line:#d7e0e8;--bg:#f4f7fa;--text:#17212b;--muted:#637282;--ok:#18864b;--bad:#c73d32;--skip:#6b7280;--warn:#b46900}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);font-family:"Microsoft YaHei","Segoe UI",sans-serif;font-size:14px}
header{position:sticky;top:0;z-index:20;background:var(--nav);color:#fff;padding:14px 22px;box-shadow:0 2px 8px #0003}
.topline{display:flex;align-items:center;gap:18px}.topline h1{font-size:19px;margin:0}.source{opacity:.8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.stats{display:flex;gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap}.stat{background:#ffffff18;border:1px solid #ffffff30;padding:6px 10px;border-radius:7px}.toolbar{margin-left:auto;display:flex;gap:8px}
button{border:0;border-radius:6px;padding:7px 12px;cursor:pointer;font-weight:600}.toolbar button{background:#fff;color:var(--nav)}
.filters{max-width:1560px;margin:16px auto 0;padding:0 18px;display:flex;gap:8px}.filters button{background:#e5ebf1;color:#34495e}.filters button.active{background:var(--blue);color:#fff}
main{max-width:1560px;margin:12px auto 50px;padding:0 18px}.card{background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:18px;box-shadow:0 3px 12px #2233440d;overflow:hidden}
.card-head{display:flex;gap:16px;align-items:center;padding:13px 16px;border-bottom:1px solid var(--line)}.card-title{font-size:17px;font-weight:700}.meta{color:var(--muted)}.badge{padding:3px 8px;border-radius:10px;background:#e7f0f8;color:#23577f;font-size:12px}.status-badge{margin-left:auto}
.review-grid{display:grid;grid-template-columns:minmax(420px,46%) minmax(500px,54%);min-height:520px}.evidence-panel{background:#eef2f5;border-right:1px solid var(--line);padding:12px;overflow:auto;max-height:760px}.evidence{background:#fff;border:1px solid #cfd9e2;border-radius:7px;margin-bottom:12px;overflow:hidden}.evidence img{width:100%;display:block;cursor:zoom-in}.caption{padding:7px 9px;color:var(--muted);font-size:12px}
.result-panel{padding:12px;overflow:auto;max-height:760px}.table-block{margin-bottom:16px}.table-title{font-weight:700;margin:0 0 7px;display:flex;justify-content:space-between}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:7px;max-height:420px}table{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}th{position:sticky;top:0;background:#245b86;color:#fff;text-align:left;z-index:2}th,td{padding:6px 8px;border-right:1px solid #dce4eb;border-bottom:1px solid #dce4eb;white-space:nowrap}tbody tr:nth-child(even){background:#f5f8fb}td[contenteditable=true]{outline:none;background:#fffdf4}td.edited{background:#fff0b8;color:#6d4700;font-weight:600}
.warning{margin:10px 16px 0;padding:8px 10px;background:#fff4de;color:#7b4c00;border-left:4px solid #e1a33b;border-radius:4px}.actions{display:flex;align-items:center;gap:9px;padding:12px 16px;border-top:1px solid var(--line);background:#fafcfd}.actions .spacer{flex:1}.pass{background:#dff3e8;color:#116a3b}.issue{background:#fde2df;color:#9d2c25}.ignore{background:#e8ebee;color:#4d5863}.reset{background:#eaf1f7;color:#315c7d}.active-pass{background:var(--ok)!important;color:#fff}.active-issue{background:var(--bad)!important;color:#fff}.active-ignore{background:var(--skip)!important;color:#fff}
.note{color:var(--muted);font-size:12px}.empty{text-align:center;color:var(--muted);padding:40px}.modal{display:none;position:fixed;inset:0;background:#000c;z-index:50;padding:24px;overflow:auto}.modal.open{display:flex;align-items:flex-start;justify-content:center}.modal img{max-width:95%;background:#fff;box-shadow:0 8px 30px #000}.prototype{max-width:1560px;margin:12px auto;padding:0 18px;color:#526474}
@media(max-width:980px){.review-grid{grid-template-columns:1fr}.evidence-panel{border-right:0;border-bottom:1px solid var(--line)}.toolbar{margin-left:0}.topline{flex-wrap:wrap}}
</style>
</head>
<body>
<header><div class="topline"><h1>MCU 参数复核</h1><div class="source" id="sourceName"></div><div class="toolbar"><button id="exportBtn">导出审核结果</button><button id="clearBtn">清空本地状态</button></div></div><div class="stats" id="stats"></div></header>
<div class="prototype">原型说明：单元格可直接编辑；黄色表示已修改。审核状态和修改内容保存在当前浏览器，导出为 JSON。正式版会在全部问题解决且硬校验通过后生成 Excel。</div>
<div class="filters" id="filters"><button class="active" data-filter="all">全部</button><button data-filter="pending">待审核</button><button data-filter="pass">已通过</button><button data-filter="issue">有问题</button><button data-filter="ignore">已忽略</button></div>
<main id="app"></main><div class="modal" id="modal"><img id="modalImage" alt="证据大图"></div>
<script id="review-data" type="application/json">__DATA__</script>
<script>
const payload=JSON.parse(document.getElementById('review-data').textContent);const storeKey='ssot-review-'+payload.run_id;let state=JSON.parse(localStorage.getItem(storeKey)||'{"cards":{},"edits":{}}');let currentFilter='all';
const esc=v=>v===null||v===undefined?'':String(Array.isArray(v)?JSON.stringify(v):v);const save=()=>localStorage.setItem(storeKey,JSON.stringify(state));
function stats(){const counts={pending:0,pass:0,issue:0,ignore:0};payload.cards.forEach(c=>counts[state.cards[c.id]?.status||'pending']++);document.getElementById('stats').innerHTML=`<span class="stat">卡片 ${payload.cards.length}</span><span class="stat">待审核 ${counts.pending}</span><span class="stat">通过 ${counts.pass}</span><span class="stat">有问题 ${counts.issue}</span><span class="stat">忽略 ${counts.ignore}</span><span class="stat">结构化记录 ${payload.total_rows}</span>`}
function setStatus(id,status){let reason='';if(status==='ignore'){reason=prompt('请填写忽略原因：',state.cards[id]?.reason||'')||'';if(!reason)return}state.cards[id]={status,reason,updated_at:new Date().toISOString()};save();render()}
function render(){stats();const app=document.getElementById('app');app.textContent='';let shown=0;payload.cards.forEach(card=>{const status=state.cards[card.id]?.status||'pending';if(currentFilter!=='all'&&status!==currentFilter)return;shown++;const el=document.createElement('section');el.className='card';el.dataset.status=status;const head=document.createElement('div');head.className='card-head';head.innerHTML=`<div class="card-title"></div><span class="badge"></span><span class="meta"></span><span class="badge status-badge"></span>`;head.querySelector('.card-title').textContent=card.title;head.querySelectorAll('.badge')[0].textContent='置信度 '+card.confidence;head.querySelector('.meta').textContent='PDF 第 '+card.pages.join('、')+' 页 · '+card.row_count+' 条结果';head.querySelector('.status-badge').textContent={pending:'待审核',pass:'已通过',issue:'有问题',ignore:'已忽略'}[status];el.appendChild(head);
card.warnings.forEach(w=>{const d=document.createElement('div');d.className='warning';d.textContent='提示：'+w;el.appendChild(d)});const grid=document.createElement('div');grid.className='review-grid';const left=document.createElement('div');left.className='evidence-panel';card.images.forEach(img=>{const box=document.createElement('div');box.className='evidence';const im=document.createElement('img');im.src=img.src;im.alt='PDF证据';im.onclick=()=>{document.getElementById('modalImage').src=img.src;document.getElementById('modal').classList.add('open')};const cap=document.createElement('div');cap.className='caption';cap.textContent=`第 ${img.page} 页 · ${img.kind} · ${img.evidence_id}`;box.append(im,cap);left.appendChild(box)});grid.appendChild(left);
const right=document.createElement('div');right.className='result-panel';Object.entries(card.tables).forEach(([sheet,rows])=>{const block=document.createElement('div');block.className='table-block';const title=document.createElement('div');title.className='table-title';title.innerHTML=`<span></span><span class="note"></span>`;title.children[0].textContent=sheet;title.children[1].textContent=rows.length+' 行';block.appendChild(title);const wrap=document.createElement('div');wrap.className='table-wrap';const table=document.createElement('table');const fields=Object.keys(rows[0]||{}).filter(k=>!k.startsWith('_'));const thead=document.createElement('thead');const hr=document.createElement('tr');fields.forEach(f=>{const th=document.createElement('th');th.textContent=f;hr.appendChild(th)});thead.appendChild(hr);table.appendChild(thead);const tbody=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');fields.forEach(field=>{const td=document.createElement('td');const key=card.id+'|'+sheet+'|'+row._row_id+'|'+field;const original=esc(row[field]);td.contentEditable='true';td.dataset.key=key;td.dataset.original=original;td.textContent=state.edits[key]?.value??original;if(state.edits[key]&&state.edits[key].value!==original)td.classList.add('edited');td.addEventListener('input',()=>{const value=td.textContent.trim();if(value===original)delete state.edits[key];else state.edits[key]={value,original,card_id:card.id,sheet,row_id:row._row_id,field,updated_at:new Date().toISOString()};td.classList.toggle('edited',value!==original);save()});tr.appendChild(td)});tbody.appendChild(tr)});table.appendChild(tbody);wrap.appendChild(table);block.appendChild(wrap);right.appendChild(block)});grid.appendChild(right);el.appendChild(grid);
const actions=document.createElement('div');actions.className='actions';actions.innerHTML=`<button class="pass">通过</button><button class="issue">有问题</button><button class="ignore">忽略</button><button class="reset">撤销本卡修改</button><span class="spacer"></span><span class="note"></span>`;actions.querySelector('.pass').onclick=()=>setStatus(card.id,'pass');actions.querySelector('.issue').onclick=()=>setStatus(card.id,'issue');actions.querySelector('.ignore').onclick=()=>setStatus(card.id,'ignore');actions.querySelector('.reset').onclick=()=>{Object.keys(state.edits).filter(k=>k.startsWith(card.id+'|')).forEach(k=>delete state.edits[k]);save();render()};actions.querySelector('.note').textContent=state.cards[card.id]?.reason?('忽略原因：'+state.cards[card.id].reason):'';if(status!=='pending')actions.querySelector('.'+status)?.classList.add('active-'+status);el.appendChild(actions);app.appendChild(el)});if(!shown){const d=document.createElement('div');d.className='empty';d.textContent='当前筛选条件下没有卡片';app.appendChild(d)}}
document.getElementById('sourceName').textContent=payload.source_file;document.querySelectorAll('#filters button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#filters button').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentFilter=b.dataset.filter;render()});document.getElementById('modal').onclick=e=>{if(e.target.id==='modal'||e.target.id==='modalImage')e.currentTarget.classList.remove('open')};
document.getElementById('exportBtn').onclick=()=>{const out={run_id:payload.run_id,source_file:payload.source_file,exported_at:new Date().toISOString(),review:state};const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='review_state.json';a.click();URL.revokeObjectURL(a.href)};document.getElementById('clearBtn').onclick=()=>{if(confirm('确定清空本页面的审核状态和修改吗？')){state={cards:{},edits:{}};save();render()}};render();
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an editable local HTML review prototype from extraction evidence.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--document-contains", default="MiAi-6099A1")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = args.output_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    all_evidence = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [item for item in all_evidence if args.document_contains.casefold() in Path(item["source_file"]).name.casefold()]
    if not matching:
        raise SystemExit(f"No evidence document matched: {args.document_contains}")
    source_file = matching[0]["source_file"]
    matching = [item for item in matching if item["source_file"] == source_file]
    cards = build_cards(matching, extracted["data"], asset_dir)
    run_id = hashlib.sha256((str(args.extracted.resolve()) + source_file).encode("utf-8")).hexdigest()[:12]
    payload = {
        "run_id": run_id,
        "source_file": source_file,
        "cards": cards,
        "total_rows": sum(card["row_count"] for card in cards),
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    output_html = HTML_TEMPLATE.replace("__DATA__", data_json)
    (args.output_dir / "index.html").write_text(output_html, encoding="utf-8")
    (args.output_dir / "prototype_manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output_dir / "index.html"), "cards": len(cards), "images": sum(len(card["images"]) for card in cards), "rows": payload["total_rows"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
