# 声明式引擎阶段状态

本文件只记录已经取得证据的状态。“运行成功”不等于语义正确，未执行独立盲测的阶段不得标记为完成。

## R0：旧结构冻结 - 进行中

- 已保留 `pre-declarative-adapter-v1` 标签与旧 Adapter。
- 已建立 A 集回归基线和一项外设数量维度人工审计。
- 尚缺：18 份文档的完整高风险字段认证及连续两次规范化哈希证明。

## R1：执行契约和路由 - 完成

- 支持 `legacy`、`shadow`、`declarative`，默认仍为 `legacy`。
- shadow 比较业务值、文档归属、证据和冲突；无声明式计划时禁止回退。
- 验收数据：阶段检查点 `5e04c45`；251 项测试和 14 个子测试通过；旧端到端回归通过。
- 盲测：未用于证明解析准确率。R1只验证路由与比较契约。

## R2：通用声明式执行器 - 进行中

已实现：

- 有限组件注册表；配置不能执行任意代码。
- section 依赖排序、未知依赖、循环依赖和未知组件检查。
- 型号按列、型号按行、前缀与后缀组合列三种矩阵结构。
- 公共字段字典驱动的 Flash、RAM、CPU频率、工作电压、复合工作环境和封装映射。
- 每个输出值保留源单元格、页码与位置。
- 标准 min/typ/max 工作条件组件拒绝普通特性矩阵，避免混用拓扑。
- 配置字段显式声明 parser、reducer、qualifier 和 required；未知解析器、未知限定器和必填字段缺失均直接失败。
- 通用订货码图例、具体型号目录和通配特性矩阵可以按依赖关系跨表连接；型号必须来自源文档目录，容量子列必须由源文档订货码含义选择。

当前真实回归证据：

- CA32F0156：3 个型号；Flash、RAM、CPU频率、工作电压、温度、封装与人工查看的第6页一致。
- STM32C031：8 个型号；Flash、RAM、CPU频率、工作电压与人工查看的第9页一致。
- MiAi-603：11 个型号的 Flash、RAM、封装已由行式矩阵配置提取并与第9页人工核对。
- MiAi-6099：6 个型号的 RAM、工作电压、封装已由行式矩阵配置提取并与第10页人工核对。
- STM32F103：从第1页型号目录取得8个具体型号，按源文档订货码连接第10页容量子列；8个型号的 Flash、RAM、频率、电压、封装共48个字段与人工真值一致。
- STM32G474：从第2页型号目录取得15个具体型号，按第234页订货码连接第15至16页矩阵；15个型号的 Flash、RAM、频率、电压、封装共90个字段与人工真值一致。

冻结后配置保留测试（候选提交 `5902103`）：

- CA51F152S3：未修改引擎或配置即提取 2 个具体型号；Flash、RAM、工作电压、封装共 10 个字段值与第8页人工核对一致。
- STM32G474 的冻结保留首跑失败结果仍保留；它随后转为 R2.2 开发回归样本，用于实现通用跨表关系，不能再次计作盲测通过。

盲测状态：

- 开发回归数据不是独立盲测。
- 已在代码冻结后运行两份未参与本轮组件开发的配置保留文档，一正一负；它们来自已知仓库，不能冒充正式 B 集盲测。

R2剩余门槛：

- 冻结 R2.2 候选，并用未参与本轮实现的新文档执行 R2.3 配置保留测试。
- 至少两个真实文档族的正例、反例均通过。
- 完成正式 B 集盲测；人工核对值、单位、型号归属和证据。
- 全量单元测试及 legacy A 集回归零退化。

### R2.3 通用性留出迭代（2026-09-09）

本轮坚持“代码冻结 -> 新文档首跑 -> 人工核对原页 -> 失败样本转开发集 -> 修改后必须更换新留出集”。所有失败首跑均保留，修改后不回算盲测成功；正式 B/C 集未被消费。

已定位并修复的通用根因：

- 跨页矩阵列粒度变化：用归一化几何横坐标连接容量子列与续表合并列，不使用页码、型号或固定列号。
- 相邻封装名换行碰撞：对保留换行/去换行两种扫描结果做来源一致性消歧，修复伪 `LQFP64W`、伪 `BGA144`，保留原文真实并列封装。
- 完整型号表头被忽略：矩阵同时接受目录可验证的完整型号表头和通配表头。
- 标题词汇耦合：标题不匹配时，只允许“必需字段 + 可验证型号列 + 订货码关系”结构回退。
- 可选依赖控制流：目录/订货码结构不存在时尝试下一声明式布局，不再解引用空依赖。
- 已有按列布局未接入：通过配置把 `models_in_columns` 接入 STM32 profile；不同布局结果不一致时仍硬失败。
- RAM 合并语义：排除 backup/cache 后，按不同 RAM 区域标签去重求和；摘要布局仍取总量最大值。
- 多条件范围：同一单元格的所有工作范围均解析，输出覆盖范围的最小/最大值。
- 通配模式冒充具体型号：profile 显式声明通配标记，带通配标记的 token 不得进入具体型号集合。
- 跨系列对比表误选：存在设备目录时，按列矩阵只允许输出目录中的型号。
- 目录具体型号与通配表头无法连接：按 profile 声明的通配标记做模式兼容，只允许扩展到文档目录已有的具体型号；跨页同型号字段按连续表合并，同页多列命中仍硬失败。
- 合并表头隐藏容量子列：若共享表头覆盖不同物理值列，非关系型布局拒绝猜值，交给订货码容量关系选择。
- 压缩订货码后缀丢失：支持 `完整型号A/B` 同位置后缀备选，并允许目录基本型号匹配更长的订货码前缀；具体型号仍必须来自目录。
- “Up to”误作精确容量：上限词由公共字典配置；小于上限的具体容量必须来自订货码图例，并把字段证据指向图例原行。
- 特性矩阵缺少封装：可配置启用“同一原文行的型号模式—封装关系”回退；只有型号模式和封装同句且兼容时补全，不按脚数或文件名猜测。

人工语义核对证据（程序成功不计，以下均查看了型号目录、特性表和证据页）：

- STM32L475：6/6 型号、36/36 字段与证据正确（冻结首跑正例）。
- STM32F205：首跑失败后转开发集；修后 14/14 型号、84/84 字段与证据正确。
- STM32F415/F417：首跑漏掉全部 F415，判失败；修后 10/10 型号、60/60 字段与证据正确。
- STM32L486：冻结首跑 5/5 型号、30/30 字段与证据正确。
- STM32F215/F217：冻结首跑 12/12 型号、72/72 字段与证据正确。
- STM32L496：首跑标题过滤失败；修后 11/11 型号、66/66 字段与证据正确。
- STM32F358：冻结首跑 3/3 型号、18/18 字段与证据正确。
- STM32L471：首跑出现伪 BGA144，判失败；修后 8/8 型号、48/48 字段与证据正确。
- STM32F328：首跑控制流失败，修后又由人工发现 RAM 12 KB 错误；最终为 1/1 型号、6/6 字段，RAM 正确合并为 16 KB。
- STM32L162：3/3 型号；首次人工结论因漏看同单元格第二电压范围已撤回，范围解析修后为 1.65-3.6 V。
- STM32F102：首跑因通配伪型号产生布局歧义，判失败；修后 4/4 具体型号、24/24 字段正确。
- STM32L082：程序首跑成功但人工发现电压遗漏第二条件，判失败；修后 3/3 型号的范围均为 1.65-3.6 V。
- STM32F101 xF/xG：冻结首跑 6/6 型号、36/36 字段与原页一致。
- STM32L151xD/L152xD：第7轮首跑失败后转开发集；修后 8/8 型号、48/48 字段与第2、12–13页一致。
- STM32F405/F407：第8轮首跑因布局顺序比较失败，进一步核对发现其中一个布局把 G 容量误作 512 KB；转开发集后，关系型布局输出 11/11 型号并保留 E/G 容量差异。
- STM32H562/H563：第8轮程序曾显示 12 型号成功，但人工核对第2页目录发现应有22型号，判失败；修后 22/22，G=1024 KB、I=2048 KB，G 的证据指向第272页订货码行。
- STM32G070：第9轮因特性表无封装而首跑失败；第7、29–30页存在型号模式与封装显式关系，通用关系回退修后 3/3 型号、18/18字段正确。
- STM32L552：第9轮程序显示6型号成功，但人工核对第2页目录应为11型号且C容量被误作512 KB，判失败；压缩订货码关系修后11/11，C=256 KB、E=512 KB，Flash证据指向第334页原始图例行。
- STM32U585：第10轮冻结首跑 7/7 型号、42/42 字段与第2、18–20页一致；RAM 按既定业务口径为784 KB系统SRAM，不含单列的2 KB backup SRAM。
- STM32F303：第10轮程序显示2型号成功，但人工核对第2页目录应为6型号，判失败；目录后缀简写与矩阵共用通用展开后，K/C/R×6/8 共6/6型号、36/36字段与第2、12页一致。
- STM32F070：第11轮冻结首跑 4/4 型号、24/24字段与第1、10页一致。
- STM32L476：第11轮冻结首跑 14/14型号、84/84字段与第2、15–16、261页一致。
- 第11轮人工真值共18个型号、108个字段，`verify_semantic_truth.py` 返回 PASS；冻结代码快照哈希为 `5aeaf6ba59ded72813bf5d705bfd0d22ba36df122f1f2e18575880b0eb239cb1`。

当前未完成项：

- 第8、9轮发现新结构缺口时均保持R2.3未完成；相关失败样本已转开发集，修后结果未回算盲测成功。
- 第11轮在最后一次代码冻结后取得两份新文档的首跑与人工语义双通过，因此“芯片基础字段声明式解析”的R2.3开发门槛已满足；正式30份B集仍未执行，不能据此宣称整体系统达到基本可用。
- 正式 30 份 B 集与 10 份 C 集仍需在候选代码最终冻结后执行并人工评分。

无退化证据：305 项单元测试通过；正式 acceptance 中 CA/MiAi 基线（20/2177/106/40/69）、STM32 基线（16/2470/64/16/32）、两套输出契约、混合输入、审核产物与 Excel 结构均通过，业务基线零差异。
## R3.1 memory projection (complete)

- Added a generic `memory_projection` component. Profiles map semantic quantity
  fields to canonical memory types; the component cannot contain model IDs,
  expected values, page numbers, or coordinates and refuses records without
  positive quantities and source evidence.
- The shared declarative adapter now emits canonical `memory` rows with field
  evidence. The legacy adapter remains the default and was not changed.
- STM32 development corpus round 11 produced 36/36 expected memory rows for 18
  manually reviewed models. Every value and evidence page matches the independent
  chip truth. In particular STM32F070C6/F6 are correctly 6 KB SRAM on page 10;
  the legacy extractor emits 16 KB, so legacy equality is not a correctness gate.
- MiAi/CA development corpus produced all 69 expected memory rows across 20
  models: Flash 14, MTP 6, EEPROM 6, SRAM total 9, internal 17 and external 17.
  Values were checked against the source tables and narrative memory sections.
- Frozen blind round 1 was not counted as a pass: 2/3 documents were correct;
  MiAi-604 exposed a single-model narrative topology gap, localized compound-unit
  bug (`K 字节`), and non-contributing evidence selection bug. It became a
  development corpus after those root-cause fixes.
- Frozen blind round 2 snapshot `fb613df7b111d655a06ac6de77b157f341a96b2bc93b77336a9b1e6d7e1d7bdc`
  passed on the first run: 3/3 documents, 17/17 models and 51/51 memory records.
  Every model/type/value/evidence page was visually checked against CA51F3 pages
  6/9, CA51F7 pages 5/7 and MiAi-60A pages 6/9, then independently replayed by
  `tests/verify_memory_projection.py`.
- Generic extraction code contains no blind-set model names, page numbers or
  expected capacities. The legacy engine remains the default path.

## R3.2 electrical projection (complete)

- Added generic `document_ranges` and `electrical_projection` components.
  Profiles now declare how normalized operating-voltage and
  operating-temperature semantics map to canonical electrical parameters; the
  engine rejects malformed ranges, missing evidence, embedded expected values,
  page numbers and model whitelists.
- Range parsing now accepts an explicit plus sign on the upper bound (for
  example `-40℃~+85℃`). Document-scoped operating conditions can be inherited
  by every concrete model, while model-specific values take precedence.
- Table-derived temperature evidence is attached to the exact semantic row.
  This permits an explicit `工作环境温度` row inside a limits table without
  treating the table's `-0.3..6 V` stress voltage as an operating voltage.
- Development data produced 40/40 expected CA/MiAi electrical rows for 20
  models and 18/18 STM32 voltage rows for 18 models. The CA/MiAi evidence pages
  distinguish summary working voltage from the later temperature row.
- Frozen blind round 1 snapshot
  `78706e78db5a0d1bf3f4510e254e5fe82dc82527754e98eded5ab1421fa2ae54`
  passed on its first run: 3/3 documents, 26/26 models and 34/34 expected
  electrical records. Values, units and evidence pages were visually checked
  against CA51F2 pages 7/229, MiAi-603 pages 6/169 and STM32G474 page 16, then
  replayed by `tests/verify_electrical_projection.py`.
- STM32G474 lists two temperature grades in one matrix cell. Because the
  current base `chip_id` does not encode the ordering-code temperature grade,
  R3.2 deliberately does not collapse both alternatives into one invented
  `T_operating` value. This remains a data-model decision, not a parser failure.
- All 317 unit tests pass. Final legacy acceptance remains exact for CA/MiAi
  (20/2177/106/40/69) and STM32 (16/2470/64/16/32); mixed-family union, review
  output, five-sheet workbook structure and the 40 MB streaming writer pass.
  The legacy engine remains the default path.

## R3.3 peripheral projection (complete)

- Generic peripheral projection now separates controller instances, channels,
  resolution and qualified mode counts. Explicit addends are summed, while
  slash-qualified modes such as `6/3 (simplex)` are not incorrectly added.
- Blind round 14 was manually certified for STM32C051/C071/C092: 41 models and
  164 peripheral records, with every key, value and evidence page replayed by
  `tests/verify_peripheral_projection.py`.
- CA/MiAi regression exposed Chinese classifier semantics. The generic prose
  grammar now distinguishes `两个 ADC` (two instances) from `26 路 ADC`
  (26 channels); the shared dictionary recognizes the complete
  `ADC Number of channels` phrase without restoring unsafe substring matching.
- Final acceptance passes 356 unit tests and exact legacy business baselines:
  CA/MiAi 20/2177/106/40/69 and STM32 16/2470/64/16/32, plus mixed routing,
  review output and five-sheet streaming workbook validation at 40 MB peak RSS.

## R3.4 pin-mux projection (complete)

- Added a family-neutral `pin_projection` component. Configuration selects a
  model section, a pin-topology preset and (optionally) an ordering-code pin
  count relation; code contains no STM32 model names, expected rows or pages.
- STM32C031 and STM32F103 now run end-to-end through the declarative adapter.
  Against the legacy frozen output, C031 is 1618/1618 and F103 is 852/852;
  both have zero missing keys, zero extra keys and zero differences in AF code,
  default function, support flag or constraints.
- The combined two-document declarative run passes the output contract with
  16 models, 2470 pin-mux rows, zero conflicts, zero validation issues and zero
  extraction errors. Manual high-risk checking confirmed PA1
  `USART2_RTS_DE_CK` on C031 page 32 and its AF1 assignment on page 35.
- Pin identity is now package-aware end to end. `package_type` remains explicit
  in canonical data and is included in the pin key; database export preserves
  the unchanged 13-column `ssot_mcu_pin_mux` contract by placing package and
  non-integer physical designators losslessly in `constraints`.
- CA/MiAi legacy extraction now carries the already observed model-to-package
  relation into all 2177 pin rows. The physical-table topology no longer picks
  only the first package column, and rotated PDF package labels are handled by
  direction variants rather than model/page/coordinate rules.
- Novel blind round 1 first run remains a recorded failure: H753/U083 lost the
  package dimension. After the generic identity/column fixes, 10/10 manually
  certified package-aware records and all forbidden cross-package assignments
  pass. A real U083 summary-vs-pin-table `UFBGA48`/`UFQFPN48` contradiction is
  deliberately reported as partial instead of silently corrected.
- Novel blind round 2 first run remains a recorded failure: H523 tables used a
  valid `Number` + `Name (function after reset)` header without repeating the
  word `Pin`, and WBA power qualifiers were consumed as package suffixes. The
  shared structural header gate and package grammar were corrected; 11/11
  manually certified H523/WBA package records then passed.
- Novel blind round 3 retained exact `UFBGA176+25` physical labels while the
  marketing summary used `UFBGA176`. Cross-section validation now compares the
  base package identity without mutating the exact physical label; unlike the
  U083 prefix mismatch, an auxiliary-ball suffix is not a contradiction.
- Final untouched blind round 4 froze 101 configuration/source files at
  `af9448fd055b2afd7d61c458b7d59805d255275a060bfe639628da869afe5e68`.
  STM32H503 and STM32U073 passed on their first run: 2/2 documents complete,
  7 models, 5388 pin rows, zero conflicts/issues/errors. Nine high-risk records
  across LQFP, UFQFPN, WLCSP and UFBGA plus four forbidden cross-package
  assignments were visually checked on H503 page 53 and U073 page 32 and replay
  successfully with `tests/verify_pin_projection.py`.
- Final regression passes 364 unit tests and legacy business facts for CA/MiAi
  (20/2177/106/40/69) and STM32 (16/4388/64/16/32). Mixed routing produces 3706
  pin rows; review output and the five-sheet streaming workbook pass with 48 MB
  writer peak RSS. The old adapter remains available while the declarative path
  carries the new package-aware semantics.
