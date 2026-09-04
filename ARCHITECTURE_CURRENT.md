# 阶段 1 历史快照：现状盘点与冻结边界

> 本文只记录重构前状态，不代表当前运行架构。当前配置边界见 `CONFIGURATION_BOUNDARY.md`。

## 当前生产调用链

`run.ps1` 依次调用：

1. `src/extract.py`：扫描 PDF、装载单个规则包、提取、合并、硬校验并写出 `extracted.json` 与 `evidence.jsonl`。
2. `src/build_review.py`：按证据区块渲染 PDF 截图并生成独立复核 HTML。
3. `src/write_workbook.mjs`：按 `config/schema.json` 重建 5 张业务表并写 Excel。
4. `src/validate_workbook.mjs`：验证工作表、字段顺序、空白格式行和公式。

## 现有代码分类

### 通用代码

- `src/core/documents.py`：PDF 枚举、哈希去重、版本排序、分页文本。
- `src/core/profile.py`：规则包装载、型号/系列/功能名的配置驱动归一化。
- `src/core/collector.py`：按主键合并记录、记录证据与冲突。
- `src/core/evidence.py`、`src/core/metadata.py`：证据对象及 Excel 追溯字段。
- `src/core/text.py`：文本、数值、容量与范围解析。
- `build_review.py`、`write_workbook.mjs`、`validate_workbook.mjs`：厂商无关的输出层。

### CA/MiAi 专属代码

- 目前仍内嵌在 `src/extract.py`：基础参数段、横向/转置型号表、CA/MiAi 管脚表、外设、电气与存储映射，以及 `process_document()`。
- `config/profiles/ca_miai_v1.json`、`config/parameter_rules.json`、`config/chip_aliases.json`。

### STM32 专属代码

- `src/family_extractors/stm32.py`：目标型号编码、型号特性矩阵、多封装管脚列、旧版换行单元格和工作条件表。
- `config/profiles/stm32_v1.json`、`config/stm32_parameter_rules.json`、`config/stm32_aliases.json`。

### 当前耦合问题

- `src/extract.py` 同时承担 CLI 编排、通用流程、硬校验和 CA/MiAi 转换器四种职责。
- 入口通过“存在 `extractor_module` 就动态调用，否则执行内嵌 CA/MiAi”来分流；CA/MiAi 不是一个平等、独立的文档族模块。
- `ACTIVE_PROFILE`、型号正则和参数规则使用进程级全局变量，因此不适合在同一次运行中处理多个文档族。
- `run.ps1` 当前一次只选择一个规则包；尚无逐 PDF 自动分类和混合运行。

## 冻结基准

- `tests/baselines/ca_miai_current_v2.json`：3 份 CA/MiAi 文档，20 个型号；冻结当前已经清除旧冲突后的结果。旧的 `ca_miai_v1.json` 保留不覆盖，仅作为历史快照。
- `tests/baselines/stm32_v1_sample2.json`：STM32C031C4 与 STM32F103C8 两份已确认小样本。
- `tests/fixtures.json`：冻结输入文件名、字节数和 SHA-256，防止换 PDF 后误把差异归因于代码。
- `tests/regression.py`：比较全部业务字段、主键组成的完整记录、行证据 ID、证据对象、文档归属、冲突、硬校验与提取错误。
- `tests/verify_extraction_contract.py`：额外检查主键非空且唯一、每行证据存在且可解析、证据归属到已知文档，并可要求 0 冲突/0 硬错误/0 提取错误。

任何后续规则修正都必须先说明差异并建立针对性测试；不得用覆盖冻结基准来掩盖回归失败。
