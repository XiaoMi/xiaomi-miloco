"""tests for miloco_cli.catalog (PR3 - device catalog)."""

from __future__ import annotations

from miloco_cli.catalog import (
    SpecLine,
    _build_spec_line,
    _device_filtered_keys_in_order,
    _format_extra,
    _resolve_keys_for_device,
    _skeleton_signature,
    build_catalog,
)

# ─── SpecLine.render / _format_extra ──────────────────────────────────────────


def test_specline_prop_render_no_extra():
    # name|access|format
    sl = SpecLine(key="on", fmt="bool", wr="wr", extra="", unit="", is_action=False)
    assert sl.render() == "on|wr|bool"


def test_specline_prop_render_with_extra_and_unit():
    # name|access|format|constraint|unit
    sl = SpecLine(
        key="brightness",
        fmt="uint8",
        wr="wr",
        extra="[1,100;1]",
        unit="%",
        is_action=False,
    )
    assert sl.render() == "brightness|wr|uint8|[1,100;1]|%"


def test_specline_action_render_no_params():
    sl = SpecLine(key="turn-on", fmt="", wr="x", extra="", unit="", is_action=True)
    assert sl.render() == "turn-on|x"


def test_specline_action_render_with_params():
    sl = SpecLine(
        key="play-text", fmt="", wr="x", extra="text:string", unit="", is_action=True
    )
    assert sl.render() == "play-text|x|text:string"


def test_format_extra_range():
    assert _format_extra({"value_range": [0, 100, 1]}) == "[0,100;1]"


def test_format_extra_range_no_step():
    assert _format_extra({"value_range": [0, 100]}) == "[0,100]"


def test_format_extra_enum_preserves_order():
    extra = _format_extra(
        {
            "value_list": [
                {"name": "Cool", "value": 2},
                {"name": "Heat", "value": 5},
                {"name": "Auto", "value": 1},
            ]
        }
    )
    # spec 原序：Cool/Heat/Auto，不能字典序重排
    assert extra == "Cool=2,Heat=5,Auto=1"


def test_format_extra_action_params():
    extra = _format_extra(
        {"in_params": [{"name": "text", "format": "string"}]}
    )
    assert extra == "text:string"


# ─── 同设备同 type_name 消歧 ─────────────────────────────────────────────────


def test_resolve_keys_no_conflict():
    spec = {
        "prop.2.1": {"type_name": "on"},
        "prop.2.2": {"type_name": "brightness"},
    }
    assert _resolve_keys_for_device(spec) == {
        "prop.2.1": "on",
        "prop.2.2": "brightness",
    }


def test_resolve_keys_with_desc_disambiguation():
    """三联开关：3 个 on，service_description 各异。"""
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "Switch 1"},
        "prop.3.1": {"type_name": "on", "service_description": "Switch 2"},
        "prop.4.1": {"type_name": "on", "service_description": "Switch 3"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "on@Switch_1"
    assert keys["prop.3.1"] == "on@Switch_2"
    assert keys["prop.4.1"] == "on@Switch_3"


def test_resolve_keys_chinese_description():
    """米家三联开关常见用中文 service_description：``左键`` / ``中键`` / ``右键``。

    Regression：旧 _DESC_NORMALIZE_RE 用 ``[^A-Za-z0-9_-]`` 把中文滤光，desc
    变空字符串，落到 ``@s.p`` 兜底分支，TSV 输出丢失语义。修复后中文应原样进
    ``@desc`` 后缀。
    """
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "左键"},
        "prop.3.1": {"type_name": "on", "service_description": "中键"},
        "prop.4.1": {"type_name": "on", "service_description": "右键"},
        "prop.10.1": {"type_name": "on", "service_description": "指示灯"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "on@左键"
    assert keys["prop.3.1"] == "on@中键"
    assert keys["prop.4.1"] == "on@右键"
    assert keys["prop.10.1"] == "on@指示灯"


def test_resolve_keys_fallback_to_raw_iid():
    """同 type_name 两个 prop，service_description 同样冲突 → 退化为 raw iid。"""
    spec = {
        "prop.2.1": {"type_name": "on", "service_description": "Main"},
        "prop.3.1": {"type_name": "on", "service_description": "Main"},
    }
    keys = _resolve_keys_for_device(spec)
    assert keys["prop.2.1"] == "prop.2.1"
    assert keys["prop.3.1"] == "prop.3.1"


# ─── 骨架签名 ────────────────────────────────────────────────────────────────


def test_skeleton_signature_includes_at_suffix():
    """两台设备 key 后缀不同（@Switch_1 vs @Key_1）应得到不同签名。"""
    a = [
        SpecLine("on@Switch_1", "bool", "wr", "", "", False),
        SpecLine("on@Switch_2", "bool", "wr", "", "", False),
    ]
    b = [
        SpecLine("on@Key_1", "bool", "wr", "", "", False),
        SpecLine("on@Key_2", "bool", "wr", "", "", False),
    ]
    assert _skeleton_signature(a) != _skeleton_signature(b)


def test_skeleton_signature_same_when_extras_differ():
    """两台空调，mode 枚举不同但 (key, fmt, wr) 相同 → 同骨架。"""
    a = [
        SpecLine("on", "bool", "wr", "", "", False),
        SpecLine("mode", "uint8", "wr", "Cool=2,Heat=5", "", False),
    ]
    b = [
        SpecLine("on", "bool", "wr", "", "", False),
        SpecLine("mode", "uint8", "wr", "Cool=2,Heat=5,Auto=1", "", False),
    ]
    assert _skeleton_signature(a) == _skeleton_signature(b)


# ─── build_catalog 端到端 ────────────────────────────────────────────────────


def _empty_lru():
    return {"version": 1, "updated_at": None, "histories": {}}


def _light_device(did, name, model="x.light.y", online=True, room="客厅"):
    return {
        "did": did,
        "name": name,
        "room": room,
        "category": "light",
        "online": online,
        "model": model,
        "spec": {
            "prop.2.1": {
                "type_name": "on",
                "service_type_name": "light",
                "service_description": "Light",
                "format": "bool",
                "writeable": True,
                "readable": True,
            },
            "prop.2.2": {
                "type_name": "brightness",
                "service_type_name": "light",
                "service_description": "Light",
                "format": "uint8",
                "writeable": True,
                "readable": True,
                "value_range": [1, 100, 1],
                "unit": "%",
            },
        },
    }


def test_build_catalog_merges_same_skeleton_devices():
    info = {
        "updated_at": "2026-04-28T03:30:00Z",
        "devices": [
            _light_device("lamp_001", "客厅主灯", "yeelink.light.color4"),
            _light_device("lamp_002", "餐厅吊灯", "mijia.light.bulb3", room="餐厅"),
        ],
    }
    r = build_catalog(info, lru_state=_empty_lru())
    text = r.text
    # 两台设备进同一组（共享 spec 块）
    assert "lamp_001" in text and "lamp_002" in text
    # 只数作分隔符的整行 ``---``（legend 里也提到了，不算）
    sep_lines = [ln for ln in text.splitlines() if ln.strip() == "---"]
    assert len(sep_lines) == 1
    assert "on|wr|bool" in text
    assert "brightness|wr|uint8|[1,100;1]|%" in text
    # catalog 头部应自带格式说明，不再依赖 SKILL.md
    assert "# 数据格式：" in text
    assert "did|device_name|room|category|status" in text
    # # models 注释已移除（用户反馈不需要）
    assert "# models" not in text


def test_build_catalog_groups_separated_by_blank_lines():
    """不同骨架的设备分到不同组，组间用两个空行（连续三个换行）分隔。"""
    info = {
        "updated_at": "2026-04-28T03:30:00Z",
        "devices": [
            _light_device("lamp_001", "客厅主灯"),
            {
                "did": "fan_001",
                "name": "落地扇",
                "room": "卧室",
                "category": "fan",
                "online": True,
                "model": "x.fan.y",
                "spec": {
                    "prop.2.1": {
                        "type_name": "on",
                        "service_type_name": "fan",
                        "service_description": "Fan",
                        "format": "bool",
                        "writeable": True,
                        "readable": True,
                    },
                },
            },
        ],
    }
    text = build_catalog(info, whitelist=set(), lru_state=_empty_lru()).text
    assert "lamp_001" in text and "fan_001" in text
    # 两台不同骨架 → 两组；组间分隔为两个空行（连续三个换行），组内无空行
    assert "\n\n\n" in text


def test_build_catalog_empty_spec_devices_excluded_from_render():
    """spec 为空的设备不渲染到目录文本里（empty_count 仍记录用于调试）。"""
    info = {
        "updated_at": "2026-04-28T03:30:00Z",
        "devices": [
            _light_device("lamp_001", "客厅主灯"),
            {
                "did": "sensor_001",
                "name": "温湿度",
                "room": "卧室",
                "category": "sensor",
                "online": False,
                "spec": {},
            },
        ],
    }
    r = build_catalog(info, lru_state=_empty_lru())
    assert "# 以下设备无可控属性" not in r.text
    assert "sensor_001" not in r.text
    assert "lamp_001" in r.text
    assert r.empty_count == 1


def test_build_catalog_aircond_mode_diff_sidehang():
    """三台空调：on / temp / fan / heater 一致、mode 枚举不同 → mode 旁挂。

    需要保证共享率 ≥ 0.8 才会进入旁挂分支；否则会退化为按 spec 文本分组。
    """

    common = {
        "service_type_name": "air-conditioner",
        "service_description": "Air Conditioner",
    }

    def ac(did, mode_extra):
        return {
            "did": did,
            "name": did,
            "room": "卧室",
            "category": "air-conditioner",
            "online": True,
            "model": "x.aircond.y",
            "spec": {
                "prop.2.1": {**common, "type_name": "on", "format": "bool",
                             "writeable": True, "readable": True},
                "prop.2.2": {**common, "type_name": "mode", "format": "uint8",
                             "writeable": True, "readable": True,
                             "value_list": mode_extra},
                "prop.2.3": {**common, "type_name": "target-temperature",
                             "format": "float", "writeable": True, "readable": True,
                             "value_range": [16, 30, 1], "unit": "℃"},
                "prop.2.4": {**common, "type_name": "heater", "format": "bool",
                             "writeable": True, "readable": True},
                "prop.2.5": {**common, "type_name": "eco", "format": "bool",
                             "writeable": True, "readable": True},
                # 共享率：4/5 = 0.8 ≥ 阈值，刚好不退化
            },
        }

    info = {
        "updated_at": "2026-04-28T03:30:00Z",
        "devices": [
            ac("ac_a", [{"name": "Cool", "value": 2}, {"name": "Heat", "value": 5}]),
            ac("ac_b", [{"name": "Cool", "value": 2}]),
            ac("ac_c", [{"name": "Cool", "value": 2}, {"name": "Heat", "value": 5}, {"name": "Auto", "value": 1}]),
        ],
    }
    # 这些 type_name 不在白名单 → 关白名单
    r = build_catalog(info, whitelist=set(), lru_state=_empty_lru())
    text = r.text
    # 组间用空行分隔；三台同骨架进同组 → 不应出现任何空行
    sep_lines = [ln for ln in text.splitlines() if ln.strip() == ""]
    assert len(sep_lines) == 0  # 三台同骨架进同组
    assert "on|wr|bool" in text
    assert "target-temperature|wr|float|[16,30;1]|℃" in text
    assert "  + mode|wr|uint8|Cool=2,Heat=5" in text
    assert "  + mode|wr|uint8|Cool=2,Heat=5,Auto=1" in text


def test_build_catalog_token_budget_degrades_capacity():
    """token 预算极小 → 触发 capacity 7→5 降级。"""
    # 50 个完全独立 model 的设备，每台 12 个 prop → 单组无法合并，输出体量大
    devs = []
    for i in range(50):
        d = {
            "did": f"dev_{i:03d}",
            "name": f"设备{i}",
            "room": "客厅",
            "category": f"cat-{i}",  # 每台不同 category → 不同骨架
            "online": True,
            "model": f"unique.model.{i}",
            "spec": {
                f"prop.2.{j}": {
                    "type_name": f"p{i}-{j}",
                    "service_type_name": f"svc-{i}",
                    "service_description": "S",
                    "format": "uint8",
                    "writeable": True,
                    "readable": True,
                    "value_range": [0, 100, 1],
                }
                for j in range(12)
            },
        }
        devs.append(d)
    info = {"updated_at": "x", "devices": devs}
    # 极小预算 + 无白名单 → 走完两档降级
    r = build_catalog(info, whitelist=set(), token_budget=50, lru_state=_empty_lru())
    assert r.capacity == 5


def test_build_catalog_cap_downgrade_overflow_no_double_count():
    """cap 50 → 30 降级时 overflow_count 不应把同一台设备重复计入。

    Regression：之前的 ``overflow + extra_overflow`` 会让 devices[50:] 在两份
    列表里同时出现，对 100 台设备的样本 overflow_count 报 120 而非 70。
    """
    devs = []
    for i in range(100):
        devs.append({
            "did": f"d{i:03d}", "name": f"灯{i}", "room": "r", "category": f"cat-{i}",
            "online": True, "model": f"m{i}",
            "spec": {
                "prop.2.1": {
                    "type_name": "on", "service_type_name": f"svc-{i}",
                    "service_description": "L", "format": "bool",
                    "writeable": True, "readable": True,
                }
            },
        })
    info = {"updated_at": "x", "devices": devs}
    r = build_catalog(info, whitelist=set(), token_budget=10, lru_state=_empty_lru())
    # 走到 cap 30 终态
    assert r.selected_count == 30
    # overflow_count 必须等于 100 - selected = 70
    assert r.overflow_count == 70


# ─── access 位：notify ────────────────────────────────────────────────────────


def test_spec_line_marks_notify_only_property():
    """只有 notify 的属性标 ``n``，不与只读的 ``r`` 混成一个标记。"""
    line = _build_spec_line(
        "prop.5.1",
        {"description": "门状态", "format": "uint8", "notify": True},
        "door-status",
    )
    assert line.wr == "n"


def test_spec_line_marks_readable_and_notify():
    line = _build_spec_line(
        "prop.5.1",
        {"description": "门状态", "format": "uint8", "readable": True, "notify": True},
        "door-status",
    )
    assert line.wr == "rn"


def test_spec_line_read_only_property_has_no_notify_bit():
    line = _build_spec_line(
        "prop.4.1",
        {"description": "电量", "format": "uint8", "readable": True},
        "battery",
    )
    assert line.wr == "r"


def test_spec_line_empty_access_is_not_reported_as_readable():
    """access 三位全空的属性给 ``-``。标成 ``r`` 会让它与真能读的长得一样。"""
    line = _build_spec_line(
        "prop.5.2", {"description": "异常", "format": "uint8"}, "abnormal"
    )
    assert line.wr == "-"


def test_catalog_keeps_notify_only_property():
    """notify-only 属性是 iot 条件项唯一可用的那一类，catalog 不能把它跳掉。"""
    spec = {
        "prop.5.1": {
            "description": "门状态",
            "format": "uint8",
            "notify": True,
            "type_name": "door-status",
            "service_type_name": "door",
        }
    }
    keys = _device_filtered_keys_in_order(
        spec, {("door", "prop", "door-status")}, {"prop.5.1": "door-status"}
    )
    assert keys == ["door-status"]


def test_catalog_skips_property_with_empty_access():
    spec = {
        "prop.5.2": {
            "description": "异常",
            "format": "uint8",
            "type_name": "abnormal",
            "service_type_name": "door",
        }
    }
    keys = _device_filtered_keys_in_order(
        spec, {("door", "prop", "abnormal")}, {"prop.5.2": "abnormal"}
    )
    assert keys == []
