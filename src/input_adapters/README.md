# MinerU input adapter

This adapter is an alternative front end for the shared `DocumentIR` / `TableIR` model. It does not call the legacy PDF table detector and does not change family field rules.

## Input

- MinerU `layout.json` with a `pdf_info` page list.
- The sibling `images` directory referenced by table `image_path` values.
- The MinerU output directory name should retain the original `<source-name>.pdf-<run-id>` convention when the internal PDF is UUID-named.

## Command

```powershell
python src/adapt_mineru.py --layout <mineru-dir>\layout.json --output-dir <output-dir>
```

## Output

- `mineru_table_ir.json`: the shared document/table/cell IR.
- `mineru_adapter_report.json`: hard structural errors and detected special table structures.
- `mineru_evidence_manifest.json`: stable table evidence IDs mapped to source file, page, bbox and original table image.

The special-structure report identifies rotated tables, merged multi-level headers, multi-package pin tables, AF matrices and conservative cross-page candidates. Cross-page candidates require equal normalized titles on adjacent pages; equal table numbers alone are not sufficient.

The adapter treats MinerU cell text as authoritative. It validates only structure and evidence availability; it does not correct business values or compare them with the legacy parser.
