from __future__ import annotations

_VALUE_LABELS_ZH: dict[str, str] = {
    "absent": "无人",
    "active": "活跃",
    "alarm": "报警",
    "aloof": "远离",
    "approach": "接近",
    "auto": "自动",
    "away": "离家",
    "bright": "亮",
    "brightest": "最亮",
    "charging": "充电中",
    "close": "关闭",
    "closed": "关闭",
    "closing": "关闭中",
    "cool": "制冷",
    "detected": "已检测到",
    "dim": "暗",
    "discharging": "放电中",
    "default": "默认",
    "down": "下降",
    "dry": "除湿",
    "empty": "空",
    "encounter obstacles": "遇阻",
    "fan": "送风",
    "fault": "故障",
    "favorite": "收藏",
    "five": "5 个",
    "four": "4 个",
    "full": "满",
    "heat": "制热",
    "high": "高",
    "home": "在家",
    "idle": "空闲",
    "inactive": "未活跃",
    "lan": "有线网络",
    "left": "左",
    "locked": "已上锁",
    "low": "低",
    "manual": "手动",
    "medium": "中",
    "middle": "中",
    "motor fault": "电机故障",
    "motion": "有人移动",
    "no faults": "无故障",
    "no motion": "无人移动",
    "no one": "无人",
    "none": "无",
    "normal": "正常",
    "not detected": "未检测到",
    "off": "关",
    "off light exit": "离开关灯",
    "off light leave": "离开关灯",
    "off light sleep": "睡眠关灯",
    "offlightexit": "离开关灯",
    "offlightleave": "离开关灯",
    "offlightsleep": "睡眠关灯",
    "on": "开",
    "one": "1 个",
    "open": "打开",
    "opened": "打开",
    "opening": "打开中",
    "overweight": "超重",
    "pause": "暂停",
    "paused": "已暂停",
    "present": "有人",
    "right": "右",
    "run": "运行",
    "running": "运行中",
    "rising": "上升中",
    "reset": "重置",
    "sleep": "睡眠",
    "standby": "待机",
    "stop": "停止",
    "stop lower limit": "下限停止",
    "stop upper limit": "上限停止",
    "stopped": "已停止",
    "three": "3 个",
    "two": "2 个",
    "unknown": "未知",
    "unlock": "解锁",
    "unlocked": "未上锁",
    "up": "上",
    "wet": "潮湿",
    "wired and wireless": "有线和无线",
    "wireless": "无线",
    "wireless 2 g": "2.4G 无线",
    "wireless 5 g": "5G 无线",
}


def translate_miot_value_label(label: str | int | float | bool | None) -> str:
    """Return a Chinese display label for common MIoT enum values.

    The raw enum value is never changed; this helper only localizes the
    human-readable value-list description returned by ``/automation/device-spec``.
    """
    if label is None:
        return ""
    if isinstance(label, bool):
        return "开" if label else "关"
    text = str(label).strip()
    if not text:
        return text
    normalized = text.replace("_", " ").replace("-", " ").strip().lower()
    if normalized in _VALUE_LABELS_ZH:
        return _VALUE_LABELS_ZH[normalized]
    if normalized.startswith("level") and normalized[5:].isdigit():
        return f"{normalized[5:]} 档"
    return text
