# 独立质量集

此目录只保存冻结清单、来源元数据和人工认证结果，不提交 PDF 本体。

流程：

1. 先按官方产品页收集文件，不读取字段答案；填写 `metadata.example.json` 同结构的来源元数据。
2. 用 `quality_gate.py freeze-manifest` 生成带 SHA-256 的冻结清单，并通过 `--exclude-manifest` 或 `--exclude-dir` 排除 A/B/C 既有集合。
3. 冻结代码版本后只运行一次 B；原始输出封存后，再由人工按 `annotations.example.json` 记录源文档真值与实际值。
4. 用 `quality_gate.py score --gate blind` 评分 B；用 `--gate config` 评分 C。失败文档转入 A，下一轮必须换全新的 B。

`expected` 与 `actual` 使用 JSON 原生值，类型不同即判不一致。`attribution_correct` 用于型号、封装等归属核验；`evidence_correct` 只有在引用页实际支持字段时才能为 true。
