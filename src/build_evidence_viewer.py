from __future__ import annotations

import argparse
import hashlib
import json
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
    "electrical": "电气参数与型号归属",
    "pin": "型号封装与管脚复用",
}


def version_rank(version: str) -> int:
    parts = [int(part) for part in str(version or "0").split(".") if part.isdigit()]
    return sum(part * (1000 ** (3 - index)) for index, part in enumerate(parts[:4]))


def canonical_key(sheet: str, row: dict[str, Any], schema: dict[str, Any]) -> str:
    values = [sheet, *(row.get(field) for field in schema["sheets"][sheet]["key"])]
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def row_anchor(sheet: str, row: dict[str, Any], schema: dict[str, Any]) -> str:
    return "row-" + hashlib.sha1(canonical_key(sheet, row, schema).encode("utf-8")).hexdigest()[:16]


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
        image = page.crop(crop_box).to_image(resolution=resolution, antialias=True)
        image.save(output_path, format="PNG")


def consecutive_groups(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for item in sorted(items, key=lambda value: (int(value["page"]), value["evidence_id"])):
        if not groups or int(item["page"]) > int(groups[-1][-1]["page"]) + 1:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def public_row(row: dict[str, Any], fields: list[str], sheet: str, schema: dict[str, Any]) -> dict[str, Any]:
    result = {field: row.get(field) for field in fields}
    result["_anchor"] = row_anchor(sheet, row, schema)
    return result


HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCU 参数证据</title>
<style>
:root{--nav:#183650;--blue:#235f91;--line:#d8e1e8;--bg:#f3f6f9;--muted:#607080;--mark:#fff1a8}*{box-sizing:border-box}body{margin:0;background:var(--bg);font-family:"Microsoft YaHei","Segoe UI",sans-serif;color:#17212b;font-size:14px}header{position:sticky;top:0;z-index:20;background:var(--nav);color:#fff;padding:13px 20px;box-shadow:0 2px 7px #0003}.head{display:flex;align-items:center;gap:14px}.head h1{font-size:19px;margin:0}.search{margin-left:auto;width:min(520px,45vw);padding:8px 11px;border:0;border-radius:6px}.stats{margin-top:8px;opacity:.82}.filters{max-width:1600px;margin:14px auto 0;padding:0 18px;display:flex;gap:8px;flex-wrap:wrap}.filters button{border:0;border-radius:6px;padding:7px 11px;cursor:pointer;background:#e3eaf0}.filters button.active{background:var(--blue);color:#fff}main{max-width:1600px;margin:12px auto 50px;padding:0 18px}.card{background:#fff;border:1px solid var(--line);border-radius:10px;margin-bottom:18px;overflow:hidden;box-shadow:0 3px 12px #2341}.card-head{padding:12px 15px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center}.title{font-weight:700;font-size:16px}.doc{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meta{margin-left:auto;color:var(--muted);white-space:nowrap}.grid{display:grid;grid-template-columns:minmax(420px,45%) minmax(520px,55%);min-height:460px}.images{padding:11px;background:#edf2f5;border-right:1px solid var(--line);max-height:720px;overflow:auto}.evidence{background:#fff;border:1px solid #cbd6df;border-radius:7px;margin-bottom:11px;overflow:hidden}.evidence img{display:block;width:100%;cursor:zoom-in}.cap{padding:6px 8px;color:var(--muted);font-size:12px}.tables{padding:11px;max-height:720px;overflow:auto}.block{margin-bottom:16px}.block-title{display:flex;justify-content:space-between;font-weight:700;margin-bottom:6px}.wrap{border:1px solid var(--line);border-radius:6px;overflow:auto;max-height:420px}table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px}th{position:sticky;top:0;background:var(--blue);color:#fff;text-align:left;z-index:2}th,td{padding:6px 8px;border-right:1px solid #dce4ea;border-bottom:1px solid #dce4ea;white-space:nowrap}tbody tr:nth-child(even){background:#f5f8fa}tr.target{background:var(--mark)!important;outline:2px solid #d49b00;outline-offset:-2px}.empty{padding:40px;text-align:center;color:var(--muted)}.modal{display:none;position:fixed;inset:0;background:#000c;z-index:50;padding:24px;overflow:auto}.modal.open{display:flex;justify-content:center;align-items:flex-start}.modal img{max-width:96%;background:#fff;box-shadow:0 8px 30px #000}
@media(max-width:980px){.grid{grid-template-columns:1fr}.images{border-right:0;border-bottom:1px solid var(--line)}.search{width:45vw}}
</style></head><body><header><div class="head"><h1>MCU 参数证据</h1><input class="search" id="search" placeholder="搜索型号、管脚、功能、文件名"></div><div class="stats" id="stats"></div></header><div class="filters" id="filters"><button class="active" data-kind="all">全部</button><button data-kind="model">型号/存储/外设</button><button data-kind="electrical">电气</button><button data-kind="pin">管脚</button></div><main id="app"></main><div class="modal" id="modal"><img id="modalImg" alt="证据大图"></div><script id="payload" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('payload').textContent);let filter='all';let query='';const app=document.getElementById('app');const str=v=>v===null||v===undefined?'':String(Array.isArray(v)?JSON.stringify(v):v);
function render(){app.textContent='';let shown=0;for(const card of data.cards){const hay=(card.document+' '+card.title+' '+JSON.stringify(card.tables)).toLowerCase();if(filter!=='all'&&card.kind!==filter)continue;if(query&&!hay.includes(query))continue;shown++;const sec=document.createElement('section');sec.className='card';sec.id=card.id;const head=document.createElement('div');head.className='card-head';head.innerHTML='<span class="title"></span><span class="doc"></span><span class="meta"></span>';head.children[0].textContent=card.title;head.children[1].textContent=card.document;head.children[2].textContent='PDF '+card.pages.join('、')+' 页 · '+card.row_count+' 条';sec.appendChild(head);const grid=document.createElement('div');grid.className='grid';const left=document.createElement('div');left.className='images';for(const image of card.images){const box=document.createElement('div');box.className='evidence';const img=document.createElement('img');img.src=image.src;img.alt='PDF证据';img.onclick=()=>{document.getElementById('modalImg').src=image.src;document.getElementById('modal').classList.add('open')};const cap=document.createElement('div');cap.className='cap';cap.textContent='第 '+image.page+' 页 · '+image.role+' · '+image.evidence_id;box.append(img,cap);left.appendChild(box)}grid.appendChild(left);const right=document.createElement('div');right.className='tables';for(const [sheet,rows] of Object.entries(card.tables)){if(!rows.length)continue;const block=document.createElement('div');block.className='block';const bt=document.createElement('div');bt.className='block-title';bt.innerHTML='<span></span><span></span>';bt.children[0].textContent=data.sheet_labels[sheet]||sheet;bt.children[1].textContent=rows.length+' 行';block.appendChild(bt);const wrap=document.createElement('div');wrap.className='wrap';const table=document.createElement('table');const fields=Object.keys(rows[0]).filter(k=>!k.startsWith('_'));const thead=document.createElement('thead');const hr=document.createElement('tr');for(const f of fields){const th=document.createElement('th');th.textContent=f;hr.appendChild(th)}thead.appendChild(hr);table.appendChild(thead);const tbody=document.createElement('tbody');for(const row of rows){const tr=document.createElement('tr');tr.id=row._anchor;for(const f of fields){const td=document.createElement('td');td.textContent=str(row[f]);tr.appendChild(td)}tbody.appendChild(tr)}table.appendChild(tbody);wrap.appendChild(table);block.appendChild(wrap);right.appendChild(block)}grid.appendChild(right);sec.appendChild(grid);app.appendChild(sec)}if(!shown){const d=document.createElement('div');d.className='empty';d.textContent='没有匹配的证据区块';app.appendChild(d)}jumpToHash()}
function jumpToHash(){const id=decodeURIComponent(location.hash.slice(1));if(!id)return;setTimeout(()=>{const row=document.getElementById(id);if(row){row.classList.add('target');row.scrollIntoView({block:'center'});return}const card=document.getElementById(id);if(card)card.scrollIntoView({block:'start'})},60)}document.getElementById('stats').textContent=`${data.documents}份文档 · ${data.cards.length}个证据区块 · ${data.images}张来源截图 · ${data.total_rows}条结构化记录`;document.getElementById('search').addEventListener('input',e=>{query=e.target.value.trim().toLowerCase();render()});document.querySelectorAll('#filters button').forEach(button=>button.onclick=()=>{document.querySelectorAll('#filters button').forEach(x=>x.classList.remove('active'));button.classList.add('active');filter=button.dataset.kind;render()});document.getElementById('modal').onclick=e=>{if(e.target.id==='modal'||e.target.id==='modalImg')e.currentTarget.classList.remove('open')};window.addEventListener('hashchange',jumpToHash);render();
</script></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a read-only evidence viewer for all extracted workbook rows.")
    parser.add_argument("--extracted", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resolution", type=int, default=145)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = args.output_dir / "images"
    asset_dir.mkdir(parents=True, exist_ok=True)
    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    evidences = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence_by_id = {item["evidence_id"]: item for item in evidences}
    source_ranks = {document["doc_id"]: version_rank(document.get("version", "0")) for document in extracted.get("documents", [])}
    source_by_doc = {document["doc_id"]: document["source_file"] for document in extracted.get("documents", [])}

    for item in evidences:
        render_evidence(item, asset_dir / f"{item['evidence_id']}.png", args.resolution)

    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidences:
        by_doc[item["doc_id"]].append(item)
    cards: list[dict[str, Any]] = []
    evidence_to_card: dict[str, dict[str, Any]] = {}
    for doc_id, items in sorted(by_doc.items(), key=lambda pair: Path(source_by_doc.get(pair[0], pair[0])).name.casefold()):
        basic = [item for item in items if item["kind"] == "basic"]
        model = [item for item in items if item["kind"] == "model"]
        electrical = [item for item in items if item["kind"] == "electrical"]
        pins = [item for item in items if item["kind"] == "pin"]
        definitions: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
        if basic or model:
            definitions.append(("model", basic + model, basic + model))
        if electrical:
            definitions.append(("electrical", electrical, model + electrical))
        for pin_group in consecutive_groups(pins):
            definitions.append(("pin", pin_group, model + pin_group))
        for sequence, (kind, owning, display) in enumerate(definitions, start=1):
            card = {
                "id": f"card-{doc_id}-{kind}-{sequence}",
                "kind": kind,
                "title": KIND_LABELS[kind],
                "document": Path(source_by_doc.get(doc_id, display[0]["source_file"])).name,
                "pages": sorted({int(item["page"]) for item in display}),
                "images": [{
                    "src": f"images/{item['evidence_id']}.png",
                    "page": item["page"],
                    "role": "关联依据" if item not in owning else "直接来源",
                    "evidence_id": item["evidence_id"],
                } for item in display],
                "tables": {sheet: [] for sheet in schema["sheets"]},
                "row_count": 0,
            }
            cards.append(card)
            for item in owning:
                evidence_to_card[item["evidence_id"]] = card

    for sheet, rows in extracted["data"].items():
        fields = schema["sheets"][sheet]["fields"]
        for row in rows:
            candidates = [evidence_by_id[evidence_id] for evidence_id in row.get("_evidence_ids", []) if evidence_id in evidence_by_id]
            if not candidates:
                continue
            target_rank = int(row.get("_source_rank") or 0)
            preferred = [item for item in candidates if source_ranks.get(item["doc_id"], 0) == target_rank] or candidates
            card = next((evidence_to_card.get(item["evidence_id"]) for item in preferred if evidence_to_card.get(item["evidence_id"])), None)
            if card is None:
                continue
            card["tables"][sheet].append(public_row(row, fields, sheet, schema))
            card["row_count"] += 1

    for card in cards:
        card["tables"] = {sheet: rows for sheet, rows in card["tables"].items() if rows}
    payload = {
        "documents": len(by_doc),
        "cards": cards,
        "images": len(evidences),
        "total_rows": sum(len(rows) for rows in extracted["data"].values()),
        "sheet_labels": SHEET_LABELS,
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (args.output_dir / "index.html").write_text(HTML.replace("__DATA__", data_json), encoding="utf-8")
    manifest = {
        "documents": len(by_doc),
        "cards": len(cards),
        "images": len(evidences),
        "rows": payload["total_rows"],
        "source_files": sorted({item["source_file"] for item in evidences}),
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Keep console output safe on Windows hosts using a legacy GBK code page.
    print(json.dumps({"output": str(args.output_dir / "index.html"), **manifest}, ensure_ascii=True))


if __name__ == "__main__":
    main()
