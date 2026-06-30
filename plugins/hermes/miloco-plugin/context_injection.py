"""pre_llm_call 钩子：按 session profile 注入 miloco 上下文（**被动信息**，不是身份宣告）。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/hooks/prompt.ts`` +
``home-profile/helpers.ts`` + ``home-profile/injection.ts``。

**Hermes pre_llm_call 的核心约束**（见 ``hermes_cli/plugins.py:1713-1721``）：
只能往 ``user_message`` 注入 ``{"context": text}``，**不能改 system prompt**。
所以本钩子输出只能放"事实陈述"——身份/能力宣告属于 user AGENTS.md 自配范畴，
不应在 plugin 的 user-context 里硬塞（语义降级："你是 X"会污染 user message）。

OpenClaw 端的 ``prependSystemContext`` 走 system prompt 通路，Hermes fork 没这层，
所以"指令性 block"（identity/capabilities/notify/language）全部删掉，**只留
neutral data**：tools 索引（被动清单）+ 感知数据格式（中性 schema）+ 数据源路径
（家庭档案 / 感知记忆位置）。数据块（home-profile 内容 / 设备目录）是真 fact，
user-context 带它们合理。

profile 判定（与 TS 端 ``resolveProfile`` 对齐）：
- ``platform == "cron"`` 或 session_id 含 ``":cron:"`` / ``"miloco:cron:"`` → minimal
- session_id 含 ``"miloco-rule"``     → rule
- session_id 含 ``"miloco-suggest"``  → suggestion
- 其余（含一切用户 IM）             → full
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .catalog import get_catalog
from .paths import miloco_home

logger = logging.getLogger(__name__)


Profile = str  # "full" | "suggestion" | "rule" | "minimal"


# ---------------------------------------------------------------------------
# profile 判定
# ---------------------------------------------------------------------------

def resolve_profile(
    session_id: Optional[str],
    platform: Optional[str] = None,
    user_message: Optional[str] = None,
) -> Profile:
    """与 TS 端 ``resolveProfile(sessionKey, {prompt, trigger})`` 等价。

    cron 标识三选一命中即 minimal：``platform == "cron"``、
    user_message 以 ``[cron:`` 开头、session_id 含 ``:cron:`` 或以 ``cron:`` 开头。
    """
    key = session_id or ""

    if (
        platform == "cron"
        or (user_message or "").startswith("[cron:")
        or ":cron:" in key
        or key.startswith("cron:")
    ):
        return "minimal"
    if "miloco-rule" in key:
        return "rule"
    if "miloco-suggest" in key:
        return "suggestion"
    return "full"


# ---------------------------------------------------------------------------
# 静态块——**只放事实陈述，不放指令**
# 删除的身份/能力宣告原本来自 openclaw prompt.ts,但 OpenClaw 走 system prompt
# 通路(``prependSystemContext``),Hermes fork 没该通路(plugin 只能 inject
# user context),硬塞进 user message 会让 LLM 当 user 引用读、语义降级。
# Identity 类的内容由用户在 Hermes AGENTS.md / agent system prompt 自配。
# ---------------------------------------------------------------------------

# B_IDENTITY = ""  # 占位说明:身份宣告已彻底删,见上头 docstring。
B_IDENTITY = ""

B_CAPABILITIES = """## 工具索引(被动清单)
本环境配套的 miloco-* skills（已同步到 ``~/.hermes/skills/miloco-*``,行为定义各自 SKILL.md）:

按用途分组:
- **设备控制**: miloco-devices / miloco-miot-scope / miloco-miot-admin
- **身份**: miloco-miot-identity / miloco-miot-identity-register
- **家庭**: miloco-home-observe / miloco-home-patrol / miloco-home-profile / miloco-home-promote / miloco-home-prune
- **感知 / 通知**: miloco-perception / miloco-perception-digest / miloco-notify
- **习惯 / 任务**: miloco-habit-suggest / miloco-create-task / miloco-terminate-task

调用约定: 该用什么 skill/CLI 完成什么动作,各 skill 的 SKILL.md 里有具体纪律。本字段不做行为指令、不替代 SKILL.md 描述。"""

PERCEPTION_FORMAT = {
    "voice": (
        "- 语音指令（header `[感知引擎]语音提醒：`）：每条按 key:value 多段竖排（与规则触发同形），"
        "多条用 `═══` 分隔。字段：时间、来源、画面描述（可选）、说话人、语音指令。"
    ),
    "suggestion": (
        "- 事件提醒（header `[感知引擎]事件提醒：`）：每条按 key:value 多段竖排，多条用 `═══` 分隔。"
        "字段：时间、来源、画面描述（可选）、检测到、事件优先级、建议。"
    ),
    "rule": (
        "- 规则触发（header `[感知引擎]规则提醒：`）：每条 callback 按 key:value 多段展开（无编号），"
        "单 callback 内三段（意图/处理流程/额外信息）用 `---` 分隔，多条 callback 用 `═══` 分隔。结构：\n"
        "  ```\n"
        "  [感知引擎]规则提醒：\n"
        "  时间：HH:MM:SS                              ← fire 时刻\n"
        "  来源：房间的设备(did=xxx)                    ← 触发设备身份\n"
        "  画面描述：场景                                ← 可选，有摄像头画面时\n"
        "  触发条件：rule 条件文本\n"
        "  触发原因：原因\n"
        "\n"
        "  **意图**：\n"
        "  <业务文案：本次 fire 要做什么，可能多行>\n"
        "\n"
        "  ---\n"
        "\n"
        "  **处理流程**：                               ← 仅 record-bound rule（task 绑了 record）出现，按时间序 1→2→3 执行：\n"
        "  1. 前置闸门——fire 前 get record，若 status=completed → 跳过 step 2 和所有通知；意图里的设备动作不受影响\n"
        "  2. record 写操作纪律——按 JSON 字段名选对应 CLI（actual_started_at/exited_at → session-start/end；意图首句 计数加一 → progress-inc / 事件追加 → event-append），先于通知 / 设备动作执行\n"
        "  3. 后置判定——按 mutate 响应：status 首次翻 completed → 本次通知达标；noop=true+task_paused → 静默\n"
        "  细节按段内具体指引执行，不要心算。\n"
        "\n"
        "  ---\n"
        "\n"
        "  **额外信息**：\n"
        '  {"task_id": "...", "actual_started_at": "ISO", ...}\n'
        "  ```\n"
        "**意图** = 业务文案；**额外信息** = 单行 JSON，task_id / 时间戳等 fire-time 参数从这里取，别扫文本。"
    ),
}


def _build_perception(profile: Profile) -> str:
    formats: List[str]
    if profile == "full":
        formats = [PERCEPTION_FORMAT["voice"], PERCEPTION_FORMAT["suggestion"], PERCEPTION_FORMAT["rule"]]
    elif profile == "suggestion":
        formats = [PERCEPTION_FORMAT["suggestion"]]
    else:  # rule
        formats = [PERCEPTION_FORMAT["rule"]]
    return (
        "## 感知\n"
        "家中的事件由感知引擎推送给你，按类型分节（语音提醒 / 事件提醒 / 规则提醒），"
        "每节以对应 header 开头。三类条目都按 key:value 多段竖排，多条同类用 `═══` 分隔；"
        "规则提醒在元信息段之后再有意图 / 处理流程 / 额外信息三段，段间用 `---` 分隔。"
        "画面描述字段在有摄像头画面时出现。格式：\n"
        + "\n".join(formats)
        + "\n\n"
        "字段：**来源** = 设备注册的真实房间（判断房间以它为准，别从文本里猜）；"
        "括号 `did` 是回控设备的唯一标识；**时间**（`HH:MM:SS`）= 画面捕获时刻。\n\n"
        "收到多条时，先合并再响应：\n"
        "- **去重**：短时间内可能有多条语义相近的推送，当作同一件事，取信息最全的只响应一次。\n"
        "- **跨相机融合理解**：可能同时推来多达 4 个摄像头的画面；不同摄像头或是同一房间的不同视角、"
        "或是同一家不同房间。要融合起来理解，既看清各房间在发生什么，也判断事件之间可能的关联。"
    )


B_MEMORY = """## 数据源(被动信息)
- **感知记忆** —— 家中近期感知事件归档到 ``$MILOCO_HOME/events/``（按日期组织的 md），需要时用 ``memory_search`` 工具查询。
- **家庭档案** —— 当前成员快照,见下方 ``## 家庭档案`` 块(空档案占位 ``(暂无内容)``)。

调用详情: 涉及过往成员行为/历史事件时用相应 skill 或 CLI,具体纪律在 skill 的 SKILL.md。本字段只标数据位置、不发指令。"""

# 留空占位:与 TS 端一致(B_RULE_EXEC / B_CONSTRAINTS)。
B_RULE_EXEC = ""
B_CONSTRAINTS = ""

# B_NOTIFY / B_LANGUAGE 已彻底删:
# - 通知规则的"动手前先读 miloco-notify"属于 skill 行为纪律,放在
#   ``plugins/skills/miloco-notify/SKILL.md`` 里（已存在),pre_llm_call 不重复。
# - 输出语言遵循用户上下文即可,LLM 会自然匹配,不需要 plugin 指令。
B_NOTIFY = ""
B_LANGUAGE = ""


# ---------------------------------------------------------------------------
# 动态数据块
# ---------------------------------------------------------------------------

DEVICE_CATALOG_INTRO = """## 设备目录(数据)
下方 ``# devices catalog`` 是预注入的高频设备子集（≤50 台,非全量）。字段规则见该块头部注释。

边界参考:
- 本目录**只用于快速拿到已点名单台设备**的 ``did`` / ``spec_name``。
- 凡涉及设备**集合 / 多台 / 不确定数量**（无论查询还是控制），或本目录查不到目标，先 ``device list`` 拉全量再处理。
- ``device control / props / action`` 或 ``scene`` 命令的具体纪律（选择/集合判定/安全确认/补 on/错误处理）以 ``miloco-devices`` skill 的 SKILL.md 为准——本字段只是路径索引,不重复纪律。"""


def _home_profile_path() -> Path:
    """家庭档案渲染产物：``$MILOCO_HOME/home-profile/profile.md``。"""
    return miloco_home() / "home-profile" / "profile.md"


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def load_home_profile() -> str:
    """读 profile.md；缺失返回哨兵串 ``(暂无内容)``。"""
    return _read_text_safe(_home_profile_path()) or "(暂无内容)"


def build_home_profile_block() -> str:
    """与 TS 端 ``buildHomeProfileBlock`` 对齐：把 profile.md 整体降一级后返回。

    空档案哨兵串无标题行，补上 ``## 家庭档案`` 以免 append 区出现孤立文本。
    """
    md = load_home_profile().strip()
    if not md:
        return ""
    demoted = re.sub(r"^(#{1,5}) ", r"#\1 ", md, flags=re.MULTILINE)
    if demoted.startswith("## 家庭档案"):
        return demoted
    return f"## 家庭档案\n\n{md}"


def build_pending_suggestion_block() -> str:
    """待回应习惯建议的注入块。

    移植自 ``home-profile/injection.ts`` 的 ``buildPendingSuggestionBlock``。
    仅在确有未作废 ``asked`` 条目时返回，否则空串（正常日子完全静默）。
    """
    # 延迟导入避免循环依赖（tools_habit 也会 import 本模块）。
    try:
        from .tools_habit import load_open_questions
        open_items = load_open_questions()
    except Exception as exc:  # noqa: BLE001
        logger.debug("load_open_questions failed: %s", exc)
        return ""
    if not open_items:
        return ""

    items = "\n".join(f"- [{e['key']}] {e['title']}：{e['suggestion']}" for e in open_items)
    return (
        "## 等用户回应的习惯建议\n\n"
        "你此前主动向用户推荐过把下面的习惯设成任务，正在等用户回应（**请勿重复推送同一条**）：\n\n"
        f"{items}\n\n"
        "**如何处理用户这条消息：**\n"
        "- 若是肯定/选择/否定语气（\"好/可以/行/就第一个/不用了/不要\"等）且**没有**其它明确意图 → 这就是对上面建议的答复：\n"
        '  - 同意 → **先用一句话复述命中的是哪条**，再加载 miloco-create-task skill 据该 suggestion 建任务；**建成、拿到 task_id 后** `miloco_habit_suggest(action="resolve", key, outcome="created", task_id="<新任务id>")`。若 create-task 当轮以反问/中断结束、未建成 → 先不 resolve，条目留待用户补答后再落地（勿凭空 resolve）。\n'
        '  - 拒绝 → `miloco_habit_suggest(action="resolve", key="<对应 key>", outcome="rejected")`，简短回应即可，**之后不再就这条打扰**。\n'
        '- 多条待回应时按用户指代（"第一个/那个喝水的"）定位对应 key。\n'
        "- 若用户这条消息**与这些建议无关**（在说别的事）→ **忽略本段，照常处理，不要调用 resolve**。"
    )


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def _build_prepend(profile: Profile) -> str:
    """被动信息块（按 prompt.ts §3 序,但已经全去掉指令性内容）。

    Hermes pre_llm_call 只能往 user message 注入,不能改 system prompt,
    所以这里不放 identity 宣告也不放"必须先...做..."类指令。只放:
    - 工具索引(B_CAPABILITIES):告诉 LLM 这个环境里有哪些 skill 可用
    - 感知格式(B_PERCEPTION / PERCEPTION_FORMAT 拼装):被动数据 schema
    - 数据源(B_MEMORY):被动信息路径

    profile 决定下哪些 block:
    - rule / minimal:不附完整工具索引和感知格式(只塞必要的)
    - full:全量
    """
    parts: List[str] = []
    if B_IDENTITY:                  # 保留兼容位（已设 "" 空串）
        parts.append(B_IDENTITY)
    if profile == "full":
        parts.append(B_CAPABILITIES)
    if profile != "minimal":
        parts.append(_build_perception(profile))
    if profile == "rule" and B_RULE_EXEC:
        parts.append(B_RULE_EXEC)
    if profile != "minimal":
        parts.append(B_MEMORY)
    if B_CONSTRAINTS:
        parts.append(B_CONSTRAINTS)
    # B_NOTIFY / B_LANGUAGE 都已置空,这里也跳过(if truthy 过滤):
    parts = [p for p in parts if p]
    return "\n\n".join(parts)


def _build_append(profile: Profile) -> str:
    """数据块（档案 → 待回应 → 目录），minimal 不带。"""
    if profile == "minimal":
        return ""
    parts: List[str] = []

    profile_block = build_home_profile_block()
    if profile_block:
        parts.append(profile_block)

    if profile == "full":
        pending = build_pending_suggestion_block()
        if pending:
            parts.append(pending)

    catalog = get_catalog()
    if catalog:
        # 套 ```text 围栏：catalog 是类 TSV 数据块，行首 `#` 是注释前缀而非
        # markdown 标题，裸贴会让 `# devices catalog` 在 `## 设备目录`(H2) 下
        # 被解析成 H1 倒挂。
        parts.append(f"{DEVICE_CATALOG_INTRO}\n\n```text\n{catalog}\n```")

    return "\n\n".join(parts)


def inject_context(
    session_id: str = "",
    user_message: str = "",
    conversation_history: Optional[list] = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """``pre_llm_call`` 回调：返回 ``{"context": text}`` 注入到本回合 user message。

    签名与 Hermes ``pre_llm_call`` 契约一致
    （见 website/docs/user-guide/features/hooks.md）。任何装配异常都降级为
    返回 None——绝不让插件崩掉主对话。
    """
    try:
        profile = resolve_profile(session_id, platform, user_message)
        prepend = _build_prepend(profile)
        append = _build_append(profile)

        sections = [prepend] if prepend else []
        if append:
            sections.append(append)
        if not sections:
            return None

        # 用分隔线把指令块和数据块分开，便于 agent 区分。
        context = "\n\n---\n\n".join(sections)
        return {"context": context}
    except Exception as exc:  # noqa: BLE001 - 钩子绝不抛
        logger.exception("miloco context_inject 失败: %s", exc)
        return None
