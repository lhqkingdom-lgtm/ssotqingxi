# MCU 参数一键提取（第一版）

本工具递归读取一个文件夹中的 PDF 数据手册，按统一规则抽取参数，并基于指定模板生成一个机器导入 Excel 和一个独立复核 HTML。它不会修改或覆盖原始模板。

## 当前架构（阶段 4）

程序现在分为两层，当前只接收文字版 PDF：

- `src/core/`：厂商无关的文本清洗、单位解析、PDF 去重与版本排序、证据对象、主键合并与冲突处理。
- `config/profiles/`：文档族规则包，保存型号/封装/管脚正则、完整型号判定、系列派生、别名文件和参数规则文件位置。

默认读取 `config/document_families.json`，逐份 PDF 自动选择 CA/MiAi 或 STM32 规则包；未知或并列匹配会报错并停止。新增文档族时注册规则包；若新文档只是名称、表头或型号编码不同，不需要修改核心模块。若表格拓扑不同（例如特殊矩阵、全新的跨页关系），则还需新增对应转换器，并用该文档族自己的冻结样本回归。

规则包格式由 `config/profiles/profile.schema.json` 说明。运行时可以通过 `extract.py --profile <规则包.json>` 显式选择，`run.ps1` 默认选择当前 CA/MiAi 规则包。

## 回归门槛

`tests/regression.py` 会比较全部业务字段、主键集合、冲突结果、文档归属和 evidence_id；不是只比较总行数。冻结基准位于 `tests/baselines/ca_miai_v1.json`。重构或新增规则后，先运行单元测试，再对对应冻结文档集运行完整比较，比较失败时不能更新基准来掩盖差异。

## 一键运行

在 `D:\rag_mo` 打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\ssot_automation\run.ps1
```

默认输入为 `D:\rag_mo\原始文档`，默认模板为 `D:\rag_mo\ssot_template(1).xlsx`，输出到带时间戳的 `D:\rag_mo\output\ssot_run_*` 文件夹。

指定输入和输出：

```powershell
powershell -ExecutionPolicy Bypass -File .\ssot_automation\run.ps1 `
  -InputDir "D:\新文档" `
  -Template "D:\rag_mo\ssot_template(1).xlsx" `
  -OutputDir "D:\rag_mo\output\本次结果"
```

只允许 STM32 文档族（可选；默认会自动分类混合目录）：

```powershell
powershell -ExecutionPolicy Bypass -File .\ssot_automation\run.ps1 `
  -InputDir "D:\待处理的STM32文字版PDF" `
  -Profile "D:\rag_mo\ssot_automation\config\profiles\stm32_v1.json" `
  -OutputDir "D:\rag_mo\output\stm32_run"
```

当前只生成 Excel、暂时跳过复核页面：

```powershell
powershell -ExecutionPolicy Bypass -File .\ssot_automation\run.ps1 `
  -InputDir "D:\待处理的STM32文字版PDF" `
  -Profile "D:\rag_mo\ssot_automation\config\profiles\stm32_v1.json" `
  -OutputDir "D:\rag_mo\output\stm32_excel_only" `
  -SkipReview
```

规则包会自行声明参数规则、型号别名和可选的特殊表格转换器；新增同类文档时只需选择相应规则包，不需要修改一键脚本。

最终交付物只有：

- `ssot_generated.xlsx`：机器导入版；只有模板原有的 5 张表，不含公式、链接、批注或额外工作表。
- `review\index.html`：独立复核页；逐个来源表格显示 PDF 原图与该图产生的结构化结果，并可导出复核状态。

同一输出目录还会保留 `extracted.json`、`evidence.jsonl`、`row_map.json`、`review\images` 和 `previews`，供程序追溯与自检；它们不是需要导入的工作簿。

## 规则与安全策略

- 人可读规则：`PARAMETER_RULES.md`
- 机器可读规则：`config\parameter_rules.json`
- 表结构和主键：`config\schema.json`
- 可人工确认的型号别名：`config\chip_aliases.json`

第一版只抽取模板里已经出现过有效值的字段。模板中整列未填的 `af_code`、`constraints`、`features`、`conditions`、`organization` 等字段默认关闭。

通用处理原则：按表头语义识别表格，不依赖固定页码；同时支持普通横向型号表和型号作为列的转置表；完全相同 PDF 只处理一次；不同修订版以较新版本为准；同版本互相矛盾且无法可靠消歧的数据不写入业务表，只保留冲突记录。

## 复核方式

直接打开 `review\index.html`。左侧选择来源表格，中间查看 PDF 原图，右侧查看该图产生的数据；选中一条数据时会切换到其证据页并定位高亮。人工 Excel 只提供表结构，不参与正确性判断；唯一核对标准是 PDF 原文。复核状态保存在浏览器本地，也可导出为 `review_result.json`。
