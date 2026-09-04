# MCU PDF 提取项目最终架构

```text
run.ps1 / extract.py
        ↓
core.pipeline：PDF去重 → 自动分类 → 调用文档族转换器 → 合并
        ↓
core.collector / metadata / validation：主键、冲突、逐字段证据、硬校验
        ↓
extracted.json + evidence.jsonl
        ├─ write_workbook.mjs → 5张业务表 Excel
        └─ build_review.py → 独立复核 HTML + PDF证据截图
```

## 稳定边界

- `src/extract.py`：只解析命令参数并调用通用管线，不含厂商判断。
- `src/core/`：只含厂商无关的文档、配置、单位、合并、证据、元数据和校验能力。
- `config/document_families.json`：启用的文档族注册表。
- `config/profiles/*.json`：识别、型号编码、定位词和转换器选择。
- `src/family_extractors/ca_miai.py` 与 `stm32.py`：彼此独立的特殊表格转换器，不互相调用。
- `config/schema.json`：唯一 Excel 表结构；输出层不允许自行增加列或工作表。

## 失败策略

- 未识别或并列匹配的 PDF：记录明确错误并以非零状态结束，不猜文档族。
- 同版本同主键冲突：不写入歧义记录，保留冲突双方证据。
- 任意提取错误或硬校验错误：一键流程停止，不继续生成正式产物。
- 回归差异：先定位业务值还是证据归属变化；不得直接覆盖基准。
