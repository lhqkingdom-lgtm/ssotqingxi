# 配置与转换器职责边界（profile v2）

运行时配置固定为四层：

1. `config/schema.json`：五张业务表、字段顺序、主键、类型、范围和末尾五个追溯字段。
2. `config/common_field_dictionary.json`：跨厂商公共字段词义、表头同义词和单位换算。
3. `config/profiles/<family>.json`：文档族识别、公共词典的少量别名增量、型号语义、语法格式和拓扑选择。
4. `config/table_presets/*.json`：表格拓扑的语义角色与候选表特征。

profile 中禁止出现页码、坐标、固定行列、预期值、封装码表、Flash 容量码表或旧规则文件引用。封装码、容量码只允许从当前 PDF 的订货编码图例得到；没有图例时保持空白并进入审核。

公共拓扑代码负责跨页、多级表头、转置、型号矩阵、多封装列、管脚定义、AF 矩阵和工作条件表。`src/core` 与 `src/topologies` 不得包含厂商名判断。文档族适配器只负责流程编排和少量型号语义，并调用公共拓扑。

新增普通叫法时优先在公共词典增加一个别名；只有确属某文档族的叫法才加入 profile 的 `dictionary.aliases`。出现新的表格拓扑时才新增独立公共转换器和 preset，不在主入口堆厂商分支。

旧 `parameter_rules.json` 与 `stm32_parameter_rules.json` 仅保留为只读历史对照，运行时不加载。
