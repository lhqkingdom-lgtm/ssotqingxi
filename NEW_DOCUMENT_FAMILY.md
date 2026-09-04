# 新增文档族标准流程

以下流程适用于 NXP、TI 或其他厂商，不能从单份 PDF 直接推全系列规则。

## 1. 选择并冻结小样本

- 选择 2～3 份代表文字版 PDF，至少覆盖不同系列、年代或封装表结构。
- 在 `tmp/testsets/<family>_sample/` 保存测试副本，不改原始 PDF。
- 在 `tests/fixtures.json` 登记文件名、字节数和 SHA-256。
- 先人工定位型号表、存储/外设表、电气表和管脚/AF 表的真实页码。

## 2. 判断只需配置还是需要转换器

若现有公共转换器已经支持表格拓扑，仅新增：

- `config/profiles/<family>_v2.json`
- 必要的型号别名 JSON
- `config/document_families.json` 注册项
- 配置边界单元测试

profile 只填写识别条件、公共词典增量、型号规则、语法格式和拓扑选择；不得填写页码、坐标、固定行列、预期值或订货码对照表。

以下情况才新增独立公共转换器：现有跨页、多级表头、行列转置、型号矩阵、按封装选择管脚列、AF 矩阵能力无法表达的新拓扑。转换器放在 `src/topologies/`，且不包含厂商名。文档族适配器放在 `src/family_extractors/<family>.py`，只暴露：

```python
def configure(profile): ...  # 可选
def process_document(doc, collector, aliases, profile): ...
```

严禁在 `extract.py` 或 `core/pipeline.py` 中添加厂商名称判断。

## 3. 建立该文档族基准

1. 运行小样本并要求 0 提取错误、0 硬校验错误。
2. 人工以 PDF 原文确认代表字段和证据页。
3. 将抽查项写入 `tests/field_evidence_expectations.json`。
4. 使用 `tests/regression.py freeze` 新建该文档族的首份基准。
5. 把基准路径登记到 `tests/fixtures.json`。

基准只能在明确、已复核的规则修正后新增版本；不得覆盖原基准来消除未知差异。

## 4. 小样本验收门槛

- 输入 PDF 哈希不变。
- 主键、业务字段、证据对象、文档归属、冲突和硬校验与基准一致。
- 每个非空业务字段都有可解析的字段级 evidence_id。
- Flash、RAM、频率、电压、封装和管脚/AF 抽查页正确。
- 与至少一个已有文档族混合运行时，结果等于各自独立运行结果的并集。
- Excel 只有 5 张业务表，字段顺序与 schema 一致，末尾 5 个追溯字段存在，无空列和空白格式行。
- 复核 HTML 能按文档/类型/状态筛选，点击字段可切换到对应 PDF 证据图。

小样本全部通过后，才能考虑全量运行；全量异常不能通过修改小样本基准来掩盖。
