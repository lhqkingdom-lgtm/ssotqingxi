# SSOT 数据手册解析与审核服务

本目录是一份可独立部署的运行目录。程序递归读取 `datasheet/` 中的文字版 PDF，自动识别 STM32 或 CA/MiAi 文档族，经过统一 Table IR 和固定字段规则生成五张业务表、证据数据、审核批次及 Excel。

## 目录

```text
./
├─ config/                 固定五表、公共字段词典和文档族配置
├─ datasheet/
│  ├─ stm32/              STM32 原始 PDF
│  └─ ca_miai/            CA/MiAi 原始 PDF
├─ output/                 所有解析批次和 Excel
├─ src/                    解析、Excel 和审核服务代码
├─ template/
│  └─ ssot_template.xlsx  Excel 模板
├─ run.ps1                 命令行端到端入口
└─ serve_review.ps1        文档管理与审核服务入口
```

输入、模板、配置和输出均从脚本所在目录解析，不依赖部署前的绝对路径。运行结果中的来源路径用于证据追溯，会记录本次运行时实际读取到的 PDF 路径。

## 命令行生成 Excel

在本目录打开 PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1
```

默认行为：

- 输入：`.\datasheet\`
- 模板：`.\template\ssot_template.xlsx`
- 输出：`.\output\ssot_run_时间戳\`
- 文档族：由 `.\config\document_families.json` 自动识别

也可以传相对路径：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 `
  -InputDir .\datasheet\stm32 `
  -OutputDir .\output\stm32_demo
```

只生成解析结果和 Excel、跳过审核证据图：

```powershell
powershell -ExecutionPolicy Bypass -File .\run.ps1 `
  -InputDir .\datasheet\stm32 `
  -OutputDir .\output\stm32_excel_only `
  -SkipReview
```

## 启动文档管理与审核服务

```powershell
powershell -ExecutionPolicy Bypass -File .\serve_review.ps1
```

打开 `http://127.0.0.1:8765/`。页面从 `.\datasheet\` 发现 PDF，从 `.\output\` 读取解析批次；前端发起的新解析也写入 `.\output\`。审核增删改和状态保存在对应批次的 `review_result.json`，导出的仍是固定五表 Excel。

## 运行环境

- Python 3，依赖见 `requirements.txt`
- Node.js
- `@oai/artifact-tool` 所在的 `node_modules`

脚本会优先使用当前用户的 Codex 随附运行时；没有随附运行时时会查找系统 `python` 和 `node`。也可显式传入 `-PythonExe`、`-NodeExe` 和 `-NodeModulesPath`。

## 固定契约

输出始终使用五张表：

- `chips`
- `pin_mux`
- `peripherals`
- `electrical`
- `memory`

公共字段、单位和表格拓扑位于 `config/`。文档族 profile 只保存识别条件、少量族别名、型号语义和拓扑选择。旧规则文件仅作只读对照，运行时不加载。

每个输出批次主要包含：

- `ssot_generated.xlsx`
- `extracted.json`
- `evidence.jsonl`
- `review/review_dataset.json`
- `review/review_result.json`（发生审核操作后产生）
- `review/evidence/`
