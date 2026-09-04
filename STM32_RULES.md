# STM32 小批量规则包

当前规则包：`config/profiles/stm32_v1.json`。

## 字段来源

- `chip_id`：PDF 文件名中的完整基础型号，例如 `stm32f103c8.pdf` -> `STM32F103C8`。
- `series`：型号的系列段，例如 `STM32F103C8` -> `STM32F103`。
- `core`：STM32 当前统一写入模板允许的 `ARM`；具体 Cortex-M 等级保留在原始 PDF 证据中。
- `flash_kb`：系列特性矩阵中目标封装代码和容量代码交叉对应的 Flash 容量。
- `ram_kb`：同一目标列的 SRAM 容量；合并单元格只允许在同一特性矩阵内向右继承。
- `max_freq_mhz`：特性矩阵中的 `CPU frequency` 或 `Max. CPU frequency`。
- `package_types`：目标引脚代码对应的 `Package/Packages` 单元格。
- `voltage_min/max`：优先 `General operating conditions` 的 `Standard operating voltage` 行。
- `memory`：由已定位的 Flash/SRAM 行同步生成，不从标题中的 “up to” 容量推断；片内 SRAM 按模板标准名写为 `SRAM_Internal`。
- `peripherals`：只生成模板已有的 SPI、I2C、UART、ADC 类型。SPI/I2C/UART 的 `instance_count` 表示接口实例数；ADC 表示外部输入通道数，例如 `19 + 2` 写入 `19`，内部通道不相加。
- `electrical`：供电工作范围按模板标准名写为 `VDD`；只有文档给出唯一且不依赖订货后缀的工作温度时才写入 `T_operating`。
- `pin_mux`：只读取目标引脚数对应的封装列；无该封装脚号的行不生成。功能来自同一行的默认、Alternate functions 和 Additional/Remap 列。

## 当前限制

- 文件名必须含基础型号；若只有系列名或 `xx` 占位符则拒绝处理。
- 基础型号不含完整订货号的温度后缀，因此温度范围只有在文档给出唯一、不依赖后缀的范围时才写入。
- 第一轮先验证 C0 与 F1 两种表格；H7 的大规模 pin/ball 矩阵在前两种通过后再纳入。
