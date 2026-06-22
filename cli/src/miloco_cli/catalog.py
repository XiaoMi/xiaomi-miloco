"""设备目录 TSV 压缩（spec-injection-plan §2-§5）。

输入：home_info dict（每次从后端拉取，含 lite spec：type_name / service_description /
service_type_name / in_params / value_range / value_list / unit / format /
writeable / readable / description）。

输出：plain text TSV 格式的目录字符串，由 plugin 直接拼到 system context。

只读，无副作用。LRU 由 backend 在 control / status 成功路径自动写入，本文件
只读 snapshot。当前文件的职责是「给一个 home_info + LRU 状态，构造完整目录文本」。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# package 内部资源——uv tool install / pip install 后随包同行，不能依赖
# 仓库相对路径（那样在已安装环境下找不到，过滤静默退化为 no-op）。
WHITELIST_PATH = Path(__file__).parent / "whitelist.json"


# ─── LRU snapshot 客户端（只读） ──────────────────────────────────────────────
# 后端在 control / status 成功后直接写 SQLite ``device_lru``；这里只负责拉
# snapshot 并按 (LRU + 冷骨架) 合并出 capacity 槽位的展示 key 列表。

DEFAULT_CAPACITY = 7


def _empty_lru_state() -> dict:
    return {"version": 1, "updated_at": None, "histories": {}}


def load_lru_state() -> dict:
    """从后端拉 LRU snapshot。后端不可达时返回空状态——catalog 走 cold_start 兜底。"""
    from miloco_cli.client import api_get

    try:
        resp = api_get("/api/miot/device_history")
    except SystemExit:
        # api_get 在连接失败时调用 _connect_error → sys.exit(2)。
        # LRU 失败不应阻塞 catalog；空 state 让 cold_start 接管。
        return _empty_lru_state()
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("histories"), dict):
        return _empty_lru_state()
    return data


def cold_start_keys(
    spec_keys_in_order: Iterable[str],
    capacity: int = DEFAULT_CAPACITY,
) -> list[str]:
    """冷启动填充：按 spec 原顺序最多取 capacity 个 key。"""
    out: list[str] = []
    for k in spec_keys_in_order:
        if k in out:
            continue
        out.append(k)
        if len(out) >= capacity:
            break
    return out


def merged_keys(
    did: str,
    cold_start: list[str],
    capacity: int = DEFAULT_CAPACITY,
    *,
    state: dict | None = None,
    iid_to_key: dict[str, str] | None = None,
) -> list[str]:
    """目录构建用：返回翻译后的 type_name 列表（LRU 优先，cold_start 顶上）。

    LRU 里存 iid 形态；用 ``iid_to_key`` 翻译成 type_name 再合并 cold_start。
    翻不到的 iid（spec 改过、白名单缩过）静默丢弃。``available_keys`` 的二次
    复核仍由调用方负责。
    """
    if state is None:
        state = load_lru_state()
    if iid_to_key is None:
        iid_to_key = {}
    lru_iids = list(state["histories"].get(did, []))
    lru_keys = [iid_to_key[i] for i in lru_iids if i in iid_to_key]
    out: list[str] = []
    for k in lru_keys + cold_start:
        if k in out:
            continue
        out.append(k)
        if len(out) >= capacity:
            break
    return out


# ─── 数据结构 ─────────────────────────────────────────────────────────────────


@dataclass
class SpecLine:
    """一行 spec（prop 或 action）渲染所需的最小元数据。"""

    key: str  # 含可能的 @ 后缀
    fmt: str  # prop: bool/uint8/.. 等值类型；action: 空串（无值类型）
    wr: str  # access 字段，prop ∈ {wr, w, r}；action 恒为 ``x``（execute）
    extra: str  # prop: constraint（范围/枚举）；action: in_params 入参列表；无则空
    unit: str  # 仅 prop 行；无单位为空
    is_action: bool
    annotation: str = ""  # 可选展示注释，渲染为行尾 ``  # annotation``，与 key 物理隔离

    def render(self) -> str:
        """渲染为单行字符串。

        prop 行：   name|access|format|constraint|unit  [# 注释]
        action 行： name|access|in_params               [# 注释]   （access 恒为 'x'）
        """
        if self.is_action:
            if self.extra:
                line = f"{self.key}|{self.wr}|{self.extra}"
            else:
                line = f"{self.key}|{self.wr}"
        else:
            # prop: access 在前，format 在后，与 action 的 access 列对齐
            parts = [self.key, self.wr, self.fmt]
            if self.extra or self.unit:
                parts.append(self.extra)  # 无约束时为空串，产出 ||unit
            if self.unit:
                parts.append(self.unit)
            line = "|".join(parts)
        if self.annotation:
            line = f"{line}  # {self.annotation}"
        return line


@dataclass
class DeviceCatalogEntry:
    did: str
    name: str
    room: str
    category: str
    online: bool
    model: str
    spec_lines: list[SpecLine] = field(default_factory=list)
    # 冷骨架：whitelist 过滤后按 spec 原序取 top-capacity，不走 LRU。
    # 用于设备分组——保证 LRU 状态变化不会拆开本来该合并的同型设备。
    cold_spec_lines: list[SpecLine] = field(default_factory=list)


# ─── 工具函数 ─────────────────────────────────────────────────────────────────


# 仅替换 TSV / catalog 解析层会冲突的字符；中文 / 其它 Unicode 字符直接保留，
# 这样米家中文 description（"左键" / "中键" / "指示灯"）能进 ``@desc`` 后缀。
# 替换为 ``_`` 而不是 strip，保留单词边界（"Switch 1" → "Switch_1"，与原行为兼容）。
_DESC_FORBID_RE = re.compile(r"[\s|,:=@]+")


def normalize_desc(desc: str | None) -> str:
    if not desc:
        return ""
    s = _DESC_FORBID_RE.sub("_", desc.strip())
    return s.strip("_")


def _escape(value: str | None) -> str:
    """转义设备行字段中的 ``|``（避免破坏 TSV）。"""
    if value is None:
        return ""
    return str(value).replace("|", r"\|")


def _format_extra(entry: dict) -> str:
    """value_range / value_list / in_params → extra 字符串。

    - 数值范围：``[min,max;step]``，无 step 时退化为 ``[min,max]``
    - 枚举：``Name1=Val1,Name2=Val2,..``（保留 spec 原序）
    - action 入参：``name1:fmt,name2:fmt,..``
    其它情况返回空字符串。
    """
    value_range = entry.get("value_range")
    value_list = entry.get("value_list")
    in_params = entry.get("in_params")

    if value_range and isinstance(value_range, list) and len(value_range) >= 2:
        lo, hi = value_range[0], value_range[1]
        step = value_range[2] if len(value_range) >= 3 else None
        if step is None:
            return f"[{_num_str(lo)},{_num_str(hi)}]"
        return f"[{_num_str(lo)},{_num_str(hi)};{_num_str(step)}]"

    if value_list and isinstance(value_list, list):
        items = []
        for v in value_list:
            if not isinstance(v, dict):
                continue
            name = str(v.get("name", "")).replace("|", "").replace(",", "").replace(":", "")
            val = v.get("value")
            items.append(f"{name}={_num_str(val) if isinstance(val, (int, float)) else val}")
        if items:
            return ",".join(items)

    if in_params and isinstance(in_params, list):
        items = []
        for p in in_params:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name", "")).replace("|", "").replace(",", "").replace(":", "").replace("=", "")
            fmt = str(p.get("format", "")).replace("|", "").replace(",", "")
            items.append(f"{name}:{fmt}")
        if items:
            return ",".join(items)

    return ""


def _num_str(n) -> str:
    """整数不带小数点；浮点保留原值；其它原样转字符串。"""
    if isinstance(n, bool):
        return "true" if n else "false"
    if isinstance(n, int):
        return str(n)
    if isinstance(n, float):
        if n.is_integer():
            return str(int(n))
        return repr(n).rstrip("0").rstrip(".") or "0"
    return str(n)


# ─── 白名单 ───────────────────────────────────────────────────────────────────


def load_whitelist(path: Path | str = WHITELIST_PATH) -> set[tuple[str, str, str]]:
    """读包内 ``whitelist.json`` 返回三元组 set。文件不存在则返回空 set
    （等价于不过滤）。"""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {
        (e["service_type_name"], e["kind"], e["type_name"])
        for e in data.get("entries", [])
        if isinstance(e, dict)
        and e.get("service_type_name")
        and e.get("kind")
        and e.get("type_name")
    }


def _is_whitelisted(
    entry: dict, kind: str, whitelist: set[tuple[str, str, str]]
) -> bool:
    """空 whitelist → True（不过滤）。"""
    if not whitelist:
        return True
    service_type = entry.get("service_type_name")
    type_name = entry.get("type_name")
    if not service_type or not type_name:
        return False
    return (service_type, kind, type_name) in whitelist


# ─── 设备 spec 准备 ───────────────────────────────────────────────────────────


def _resolve_keys_for_device(spec: dict) -> dict[str, str]:
    """同设备内同 type_name 冲突 → 按 §2.1 优先级生成消歧后的 key。

    返回 ``{iid: key}``。
    """
    if not isinstance(spec, dict):
        return {}

    # 第一遍：统计 type_name 出现次数
    counts: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name")
        if type_name:
            counts[type_name] = counts.get(type_name, 0) + 1

    # 第二遍：分配 key，同时按完整 ``type_name@desc`` 计数
    result: dict[str, str] = {}
    desc_used: dict[str, int] = {}
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        type_name = entry.get("type_name")
        if not type_name:
            continue
        if counts.get(type_name, 0) <= 1:
            result[iid] = type_name
            continue
        normalized_desc = normalize_desc(entry.get("service_description"))
        if normalized_desc:
            key = f"{type_name}@{normalized_desc}"
            desc_used[key] = desc_used.get(key, 0) + 1
            result[iid] = key
        else:
            result[iid] = iid

    # 第三遍：``type_name@desc`` 在设备内仍冲突（多 entry 归一化后同 desc）→ 退化为 raw iid
    for iid, key in list(result.items()):
        if "@" in key and desc_used.get(key, 0) > 1:
            result[iid] = iid

    return result


def _is_iid_action(iid: str) -> bool:
    return iid.startswith("action.")


def _build_spec_line(iid: str, entry: dict, key: str) -> SpecLine:
    is_action = _is_iid_action(iid)
    annotation = _build_annotation(iid, entry, key)
    if is_action:
        extra = _format_extra(entry)
        return SpecLine(key=key, fmt="", wr="x", extra=extra, unit="", is_action=True, annotation=annotation)

    fmt = str(entry.get("format", "") or "")
    writeable = bool(entry.get("writeable"))
    readable = bool(entry.get("readable"))
    if writeable and readable:
        wr = "wr"
    elif writeable:
        wr = "w"
    else:
        wr = "r"
    extra = _format_extra(entry)
    unit = ""
    if entry.get("unit"):
        unit = str(entry["unit"]).replace("|", "").replace(",", "")
    return SpecLine(key=key, fmt=fmt, wr=wr, extra=extra, unit=unit, is_action=False, annotation=annotation)


def _build_annotation(iid: str, entry: dict, key: str) -> str:
    """为 catalog / device spec 输出生成 ``(注释)`` 后缀。

    - 裸 type_name（无冲突）：不加注释，type_name 自解释
    - type_name@service_desc：加 ``(type_desc)``，即属性自身描述（去掉
      service_desc 前缀，如"开关"）——agent 截断到 ``@`` 之前的 ``(`` 即可
    - raw iid（desc 冲突退化）：加 ``(service_desc type_desc)`` 完整描述，
      因为 iid 本身不带语义
    """
    desc = str(entry.get("description") or "")
    svc_desc = str(entry.get("service_description") or "")

    if key == iid:
        # raw iid 退化：完整描述（iid 本身不透明，必须注释）
        return _clean_annotation(desc) if desc else ""

    if "@" in key:
        type_name = key.split("@")[0]
    else:
        type_name = key

    # 用英文 prop_description 跟 type_name 比较：
    # 一致（如 mode == "Mode"）→ 不加注释；不一致 → 加中文 desc 辅助理解。
    prop_desc_en = str(entry.get("prop_description") or "")
    normalized_en = prop_desc_en.strip().lower().replace(" ", "-").replace("_", "-")
    if normalized_en == type_name:
        return ""

    # 英文不一致 → 尝试抠中文注释
    if svc_desc and desc.startswith(svc_desc):
        type_desc = desc[len(svc_desc):].strip()
        if type_desc:
            return _clean_annotation(type_desc)

    # 中文 type_desc 为空（desc == svc_desc 或子设备自定义名覆盖）→ 跳过
    return ""


_MAX_ANNOTATION_LEN = 20


def _clean_annotation(s: str) -> str:
    s = s.replace("|", "").replace("(", "").replace(")", "")
    if len(s) > _MAX_ANNOTATION_LEN:
        s = s[:_MAX_ANNOTATION_LEN] + "…"
    return s


def _device_filtered_keys_in_order(
    spec: dict,
    whitelist: set[tuple[str, str, str]],
    iid_to_key: dict[str, str],
) -> list[str]:
    """白名单过滤后的 key 列表，按 spec 原序。同时跳过双 false（writeable & readable
    都为 false 的 prop，通常是只读 + notify-only 之类的幽灵字段，§2.1 规定塞尾段
    或丢弃）。"""
    out: list[str] = []
    seen: set[str] = set()
    for iid, entry in spec.items():
        if not isinstance(entry, dict):
            continue
        kind = "action" if _is_iid_action(iid) else "prop"
        if kind == "prop":
            if not entry.get("writeable") and not entry.get("readable"):
                continue
        if not _is_whitelisted(entry, kind, whitelist):
            continue
        key = iid_to_key.get(iid)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _build_lines_for_device(
    spec: dict,
    keys: list[str],
    iid_to_key: dict[str, str],
) -> list[SpecLine]:
    key_to_iid = {v: k for k, v in iid_to_key.items()}
    out: list[SpecLine] = []
    for k in sorted(keys):  # 字典序——LRU 只影响集合，不影响排列顺序
        iid = key_to_iid.get(k)
        if not iid:
            continue
        entry = spec.get(iid)
        if not isinstance(entry, dict):
            continue
        out.append(_build_spec_line(iid, entry, k))
    return out


# ─── 设备选 50 ────────────────────────────────────────────────────────────────


def _device_priority_key(
    device: dict, whitelist_categories: set[str]
) -> tuple:
    """(在线 > 离线，名字非空 > 名字空，category 在白名单 > 不在)。"""
    category = device.get("category") or ""
    return (
        not bool(device.get("online")),
        not bool(device.get("name")),
        category not in whitelist_categories,
    )


def select_devices(
    devices: list[dict], whitelist: set[tuple[str, str, str]], cap: int = 50
) -> tuple[list[dict], list[dict]]:
    """选 ≤cap 个设备进 catalog，剩余的走 fallback（CLI 自查）。

    返回 (selected, overflow)。
    """
    whitelist_categories = {service_type for service_type, _, _ in whitelist}
    sorted_devs = sorted(devices, key=lambda d: _device_priority_key(d, whitelist_categories))
    return sorted_devs[:cap], sorted_devs[cap:]


# ─── 骨架签名 + 旁挂 ──────────────────────────────────────────────────────────


def _skeleton_signature(lines: list[SpecLine]) -> str:
    """(key, fmt, wr) 三元组按 key 字典序拼接。"""
    triples = sorted({(line.key, line.fmt, line.wr) for line in lines})
    return "\n".join(f"{key}|{fmt}|{wr}" for key, fmt, wr in triples)


def _pretty_render_group(
    group_devices: list[DeviceCatalogEntry],
    *,
    sharing_threshold: float = 0.8,
    _allow_degrade: bool = True,
) -> list[str]:
    """渲染一组同骨架的设备到 catalog 文本行（组间空行分隔由调用方插入）。

    - 行级 extra 全员一致 → 共享 spec 块
    - 任一不一致 → 该 key 在共享块剔除，旁挂到各设备行下
    - 共享块 < 80% 平均行数 → 退化为按完整 spec 文本严格分组（递归一次）

    ``_allow_degrade`` 显式约束递归深度：顶层默认 True，退化子调用置 False，
    保证最多递归一层（每个严格子组内 100% 共享，不会再次触发退化分支）。
    """
    if not group_devices:
        return []
    all_keys = sorted({line.key for device in group_devices for line in device.spec_lines})

    # 为每台设备建 key → rendered 的快速查找表
    device_renders: list[dict[str, str]] = [
        {line.key: line.render() for line in device.spec_lines}
        for device in group_devices
    ]

    # 行级 extra 一致性判断
    shared_keys: list[str] = []
    sidehang_keys: set[str] = set()
    for key in all_keys:
        renders = {dr.get(key) for dr in device_renders}
        if len(renders) == 1 and None not in renders:
            shared_keys.append(key)
        else:
            sidehang_keys.add(key)

    avg_rows = sum(len(d.spec_lines) for d in group_devices) / len(group_devices)
    if (
        _allow_degrade
        and avg_rows > 0
        and len(shared_keys) / avg_rows < sharing_threshold
    ):
        # 退化：按完整 spec 文本严格分组，逐个子组重新渲染
        subgroups: dict[str, list[DeviceCatalogEntry]] = {}
        for d in group_devices:
            text = "\n".join(sorted(line.render() for line in d.spec_lines))
            subgroups.setdefault(text, []).append(d)
        out: list[str] = []
        for sub in sorted(subgroups.values(), key=lambda g: -len(g)):
            if out:
                out.extend(["", ""])
            out.extend(_pretty_render_group(sub, _allow_degrade=False))
        return out

    out_lines: list[str] = []
    for device in sorted(group_devices, key=lambda d: (not d.online, d.did)):
        out_lines.append(
            "|".join([
                _escape(device.did),
                _escape(device.name),
                _escape(device.room),
                _escape(device.category),
                "online" if device.online else "offline",
            ])
        )
        for line in sorted(device.spec_lines, key=lambda sl: sl.key):
            if line.key in sidehang_keys:
                out_lines.append(f"  + {line.render()}")

    if shared_keys:
        out_lines.append("---")
        first_device_lines = {line.key: line for line in group_devices[0].spec_lines}
        for key in sorted(shared_keys):
            out_lines.append(first_device_lines[key].render())

    return out_lines


# ─── 顶层入口 ─────────────────────────────────────────────────────────────────


@dataclass
class CatalogResult:
    text: str
    selected_count: int
    overflow_count: int
    empty_count: int
    capacity: int
    estimated_tokens: int


_TOKEN_BUDGET = 5000  # spec-injection-plan §1.2 / §5.5

# Catalog 自描述格式说明，注入到目录头部，让模型不必去 SKILL.md 翻文档。
_FORMAT_LEGEND = [
    "# 数据格式：",
    "#   did|device_name|room|category|status     // 设备信息行",
    "#     + prop/action                          // 当前设备独有的 prop/action，以 '  + ' 前缀表示",
    "#   ...",
    "#   ---                                      // 分隔线，下方是整组共享属性",
    "#   spec_name|access|format|constraint|unit  // prop 行",
    "#   spec_name|access|in_params               // action 行",
    "#   ...",
    "#   (空两行)                                 // 组分隔：组与组之间空两行；拥有共享属性的设备归为一组，空行下方是新组，重复以上结构",
    "#",
    "# 字段解释：",
    "#   category：设备类别，如 light / air-conditioner",
    "#   status：设备状态，只有 online / offline 两种",
    "#   spec_name：prop / action 的名字，作为 miloco-cli device (control / props / action) 第二个参数；",
    "#     形如 on / brightness / play-text；同名冲突时自动带 @<子设备描述> 后缀消歧，如 on@左键",
    "#   行尾 ``  # 注释``（如有）：人类可读的中文说明，传入 cli 时忽略",
    "#   access：权限，必选；只能取 wr=读写 / w=只写 / r=只读（不能 control）/ x=可执行（仅 action）四值",
    "#   format：值的数据类型，可选；取值如 bool / uint8 / int8 / float 等",
    "#   constraint：数值约束；格式 1：范围 [min,max;step]；格式 2：枚举 Cool=2,Heat=5",
    "#   in_params：动作入参类型说明（name:format,..），CLI 调用时只按顺序传值，不传参数名",
    "#     例：play-text|x|text-content:string → miloco-cli device action <did> play-text \"文本\"",
    "#         start-cook|x|cook-mode:uint8   → miloco-cli device action <did> start-cook 1",
    "#   unit：物理单位，可选；如 celsius / percentage / kelvin 等",
]


_TOKEN_PUNCT = frozenset("|()=,:;[]{}.@#\"'<>!?+-*/\\")


def _estimate_tokens(text: str) -> int:
    """三段加权 token 估算：punct / 1 + (alpha+空白) / 3.2 + cjk / 0.9。

    分母来自对真实编码做 per-token 归类后的 chars/token 实测（o200k_base 视角，
    cl100k_base 数值接近）：
    - punct (``|()=,:`` 等 TSV 分隔符)：~1.08，按 1 估
    - alpha + 空白 (字母数字 / 空格 / 换行)：~2.89~6.16，TSV 上下文按 3.2 估
    - cjk (中文等非 ASCII)：~1.07，按 0.9 估（贴近 cl100k 的 0.77，留安全余量）

    本仓库 catalog（6235 chars，24% punct / 66% alpha+ws / 10% cjk）估算 3252 vs
    实际 cl100k 3241 (+0%) / o200k 3030 (+7%)，整体略偏高（安全方向：提前退化，
    不会溢出 token_budget）。
    """
    punct_chars = sum(1 for c in text if c in _TOKEN_PUNCT)
    cjk_chars = sum(1 for c in text if not c.isascii())
    rest_chars = len(text) - punct_chars - cjk_chars
    return max(1, int(punct_chars + rest_chars / 3.2 + cjk_chars / 0.9))


def _build_with_capacity(
    devices: list[dict],
    *,
    whitelist: set[tuple[str, str, str]],
    capacity: int,
    lru_state: dict,
) -> tuple[list[DeviceCatalogEntry], list[DeviceCatalogEntry]]:
    """构建 (有 spec 设备列表, 空 spec 设备列表)。"""
    populated: list[DeviceCatalogEntry] = []
    empty: list[DeviceCatalogEntry] = []
    for device in devices:
        spec = device.get("spec") or {}
        iid_to_key = _resolve_keys_for_device(spec)
        cold_keys = _device_filtered_keys_in_order(spec, whitelist, iid_to_key)[:capacity]
        cold_spec_lines = _build_lines_for_device(spec, cold_keys, iid_to_key)
        merged = merged_keys(
            device.get("did", ""),
            cold_keys,
            capacity=capacity,
            state=lru_state,
            iid_to_key=iid_to_key,
        )
        available_keys = set(iid_to_key.values())
        valid_keys = [key for key in merged if key in available_keys]
        spec_lines = _build_lines_for_device(spec, valid_keys, iid_to_key)
        catalog_entry = DeviceCatalogEntry(
            did=device.get("did", ""),
            name=device.get("name", "") or "",
            room=device.get("room") or "",
            category=device.get("category") or "",
            online=bool(device.get("online")),
            model=device.get("model") or "",
            spec_lines=spec_lines,
            cold_spec_lines=cold_spec_lines,
        )
        if spec_lines:
            populated.append(catalog_entry)
        else:
            empty.append(catalog_entry)
    return populated, empty


def _render_catalog(
    populated: list[DeviceCatalogEntry],
    *,
    sharing_threshold: float,
    updated_at: str | None,
) -> str:
    # 按冷骨架签名分组（不掺 LRU——避免同型设备因最近使用差异被拆到不同组）
    groups: dict[str, list[DeviceCatalogEntry]] = {}
    for e in populated:
        sig = _skeleton_signature(e.cold_spec_lines or e.spec_lines)
        groups.setdefault(sig, []).append(e)
    group_list = sorted(groups.values(), key=lambda g: -len(g))

    parts: list[str] = ["# devices catalog"]
    if updated_at:
        parts.append(f"# updated_at={updated_at}")
    # 不写"收录 M/N 台"计数：M=N 会被读成"已全量、无需再查"，与下面的硬规则冲突
    # （目录是生成时刻快照，M=N 也证明不了执行时刻仍全量）。改为无条件的定性警告。
    parts.append(
        "# 本目录是高频子集 + 生成时刻快照，随时可能过时、且未必收全——"
        "目标数量不定（复数语义、可能多台）的查询或控制，必须先 device list 拉最新全量再逐台处理；"
        "看得见的同类设备也未必齐全，看见 ≠ 全部，绝不可当全量"
    )
    parts.extend(_FORMAT_LEGEND)
    for i, g in enumerate(group_list):
        if i > 0:
            parts.extend(["", ""])
        parts.extend(_pretty_render_group(g, sharing_threshold=sharing_threshold))
    # spec 为空的设备（无可控属性）不渲染——agent 只能 control 有 spec 的设备，
    # 列出来反而消耗 token。``empty_count`` 仍在 CatalogResult 里供调试观察。
    return "\n".join(parts) + "\n"


def build_catalog(
    info: dict,
    *,
    whitelist: set[tuple[str, str, str]] | None = None,
    cap: int = 50,
    capacity: int = DEFAULT_CAPACITY,
    sharing_threshold: float = 0.8,
    token_budget: int = _TOKEN_BUDGET,
    lru_state: dict | None = None,
) -> CatalogResult:
    """从 home_info 构造目录 TSV。

    超过 token_budget 时按 §5.5 顺序降级：
        1. capacity 7 → 5
        2. 设备数 50 → 30
    返回 ``CatalogResult``，``text`` 是渲染好的目录字符串。
    """
    if whitelist is None:
        whitelist = load_whitelist()
    if lru_state is None:
        lru_state = load_lru_state()

    devices = info.get("devices", []) or []
    selected, overflow = select_devices(devices, whitelist, cap=cap)

    cur_capacity = capacity
    cur_cap = cap

    populated: list[DeviceCatalogEntry] = []
    empty: list[DeviceCatalogEntry] = []
    text = ""

    # 渐进降级
    while True:
        populated, empty = _build_with_capacity(
            selected,
            whitelist=whitelist,
            capacity=cur_capacity,
            lru_state=lru_state,
        )
        text = _render_catalog(
            populated,
            sharing_threshold=sharing_threshold,
            updated_at=info.get("updated_at"),
        )
        tokens = _estimate_tokens(text)
        if tokens <= token_budget:
            break
        # 1) capacity 7 → 5
        if cur_capacity > 5:
            cur_capacity = 5
            continue
        # 2) cap 50 → 30
        if cur_cap > 30:
            cur_cap = 30
            # 直接用新 pass 结果替换；不要把旧 overflow + extra_overflow 拼起来——
            # 旧 overflow 是 devices[50:]，新 pass 的 extra_overflow 是 devices[30:]，
            # 两者交集是 devices[50:]，叠加会让 overflow_count 被重复计入。
            selected, overflow = select_devices(devices, whitelist, cap=cur_cap)
            continue
        break

    return CatalogResult(
        text=text,
        selected_count=len(selected),
        overflow_count=len(overflow),
        empty_count=len(empty),
        capacity=cur_capacity,
        estimated_tokens=_estimate_tokens(text),
    )
