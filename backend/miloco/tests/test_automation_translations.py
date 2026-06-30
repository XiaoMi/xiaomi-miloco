from __future__ import annotations

from miloco.automation.translations import translate_miot_value_label


def test_translate_miot_value_label_localizes_common_english_values():
    assert translate_miot_value_label("On") == "开"
    assert translate_miot_value_label("Off") == "关"
    assert translate_miot_value_label("Not Detected") == "未检测到"
    assert translate_miot_value_label("running") == "运行中"
    assert translate_miot_value_label("Level3") == "3 档"
    assert translate_miot_value_label("No Faults") == "无故障"
    assert translate_miot_value_label("Stop Upper Limit") == "上限停止"
    assert translate_miot_value_label("Rising") == "上升中"
    assert translate_miot_value_label("No One") == "无人"
    assert translate_miot_value_label("Wired And Wireless") == "有线和无线"


def test_translate_miot_value_label_keeps_unknown_values_unchanged():
    assert translate_miot_value_label("Vendor Custom") == "Vendor Custom"
    assert translate_miot_value_label(7) == "7"
