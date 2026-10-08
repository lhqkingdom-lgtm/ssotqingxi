from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from family_extractors.ca_miai import adc_channel_value, adc_instance_value, discovered_peripheral_types, max_cpu_frequency_by_page, parse_basic_defaults, resolve_ram_total_kb, semantic_columns
from topologies.operating_conditions import is_absolute_maximum_context, parse_compound_operating_conditions


class CaScalarSemanticsTests(unittest.TestCase):
    def test_cpu_clock_conditions_supply_the_maximum_supported_frequency(self) -> None:
        text = "CPU 时钟为 8MHz 时，工作电压 2.2~5.5V；CPU 时钟为 16MHz 时，工作电压 2.7~5.5V"
        self.assertEqual(parse_basic_defaults(text)["max_freq_mhz"], 16)

    def test_full_document_frequency_search_uses_explicit_cpu_semantics(self) -> None:
        located = max_cpu_frequency_by_page([
            "内部高速 RC 振荡器 24 MHz",
            "电气特性 CPU 工作频率 - 8 MHz",
        ])
        self.assertEqual(located, (8, 1))

    def test_electrical_cpu_limit_outranks_equal_usage_example(self) -> None:
        located = max_cpu_frequency_by_page([
            "当 CPU 时钟为 8 MHz 时，串口误差如下",
            "电气特性 CPU 工作频率 - 8 MHz",
        ])
        self.assertEqual(located, (8, 1))

    def test_full_document_frequency_search_does_not_treat_oscillator_as_cpu_limit(self) -> None:
        self.assertIsNone(max_cpu_frequency_by_page(["内部高速 RC 振荡器 24 MHz"]))

    def test_full_document_frequency_search_does_not_treat_pll_limit_as_cpu_limit(self) -> None:
        self.assertIsNone(max_cpu_frequency_by_page(["PLL 的最高频率可达 40 MHz，可设置为系统时钟或外设时钟"]))

    def test_absolute_maximum_section_is_not_an_operating_context(self) -> None:
        self.assertTrue(is_absolute_maximum_context("23.1 极限参数 直流供电电压 -0.3 6 V"))
        self.assertFalse(is_absolute_maximum_context("工作条件 标准工作电压 2.2 5.5 V"))

    def test_generic_ram_column_is_a_total_not_an_external_component(self) -> None:
        self.assertEqual(semantic_columns({3: "SRAM 容量 (KB)"}), {"ram_total": 3})

    def test_explicit_ram_qualifiers_define_component_roles(self) -> None:
        self.assertEqual(
            semantic_columns({1: "External RAM (KB)", 2: "内部 RAM 容量"}),
            {"external_ram": 1, "internal_ram": 2},
        )

    def test_explicit_components_are_summed_only_when_total_is_absent(self) -> None:
        self.assertEqual(resolve_ram_total_kb(None, 0.25, 0.5), 0.75)

    def test_explicit_total_is_not_added_to_components(self) -> None:
        self.assertEqual(resolve_ram_total_kb(0.75, 0.25, 0.5), 0.75)

    def test_compound_operating_conditions_are_split_by_units(self) -> None:
        self.assertEqual(
            parse_compound_operating_conditions("2.0~5.5V/-40~85℃"),
            {
                "voltage_min": 2.0,
                "voltage_max": 5.5,
                "temperature_min": -40.0,
                "temperature_max": 85.0,
            },
        )

    def test_compound_conditions_do_not_depend_on_range_order(self) -> None:
        self.assertEqual(
            parse_compound_operating_conditions("-40 to 105 °C; 1.8 V to 3.6 V"),
            {
                "temperature_min": -40.0,
                "temperature_max": 105.0,
                "voltage_min": 1.8,
                "voltage_max": 3.6,
            },
        )

    def test_working_environment_header_routes_to_compound_parser(self) -> None:
        self.assertEqual(semantic_columns({4: "工作环境"}), {"operating_conditions": 4})

    def test_peripheral_discovery_is_dictionary_driven(self) -> None:
        dictionary = {
            "fields": {
                "peripheral_can": {
                    "aliases": ["can数量"],
                    "canonical": "CAN",
                    "unit": None,
                }
            }
        }
        columns = semantic_columns({2: "CAN数量"}, dictionary)
        self.assertEqual(columns, {"peripheral:CAN": 2})
        self.assertEqual(discovered_peripheral_types(columns, {"peripherals": {}}), ["CAN"])

    def test_multiple_physical_columns_for_one_peripheral_are_preserved(self) -> None:
        self.assertEqual(
            semantic_columns({2: "USART", 3: "UART"}),
            {"peripheral:UART": [2, 3]},
        )

    def test_adc_instance_and_channel_counts_are_parsed_separately(self) -> None:
        value = "2x12bit12Channel"
        header = "12bit ADC Number of channels"
        self.assertEqual(adc_instance_value(value, header), 2)
        self.assertEqual(adc_channel_value(value, header), 12)

    def test_plain_channel_column_is_not_mislabeled_as_instance_count(self) -> None:
        self.assertIsNone(adc_instance_value("20", "ADC channels"))
        self.assertEqual(adc_channel_value("20", "ADC channels"), 20)

    def test_resolution_only_adc_column_is_still_a_channel_count(self) -> None:
        self.assertIsNone(adc_instance_value("10", "12位ADC"))
        self.assertEqual(adc_channel_value("10", "12位ADC"), 10)

    def test_text_fallback_peripherals_join_configured_table_types(self) -> None:
        self.assertEqual(
            discovered_peripheral_types(
                {"peripheral:CAN": 2},
                {"peripherals": {"UART": {"count": 2}}},
            ),
            ["CAN", "UART"],
        )

    def test_touch_quantity_is_a_channel_count_not_an_instance_count(self) -> None:
        values = parse_basic_defaults("最大支持 24 通道触摸")
        self.assertEqual(values["peripherals"]["Touch"], {"channel_count": 24})

    def test_controller_quantity_remains_an_instance_count(self) -> None:
        values = parse_basic_defaults("支持 2 个 UART")
        self.assertEqual(values["peripherals"]["UART"], {"count": 2})

    def test_chinese_adc_classifier_means_converter_instances(self) -> None:
        values = parse_basic_defaults("集成两个12位ADC")
        self.assertEqual(values["peripherals"]["ADC"], {"count": 2, "resolution": 12})

    def test_adc_channel_classifier_does_not_become_instance_count(self) -> None:
        values = parse_basic_defaults("支持26路12位SAR ADC")
        self.assertEqual(values["peripherals"]["ADC"], {"channel_count": 26, "resolution": 12})

    def test_arabic_adc_classifier_means_converter_instances(self) -> None:
        values = parse_basic_defaults("内置 2 个 12 位 ADC")
        self.assertEqual(values["peripherals"]["ADC"], {"count": 2, "resolution": 12})


if __name__ == "__main__":
    unittest.main()
