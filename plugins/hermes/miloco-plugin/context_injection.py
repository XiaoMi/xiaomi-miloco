"""pre_llm_call 钩子：按 session profile 注入 miloco 上下文。

移植自 openclaw TypeScript 插件 ``plugins/openclaw/src/hooks/prompt.ts`` +
``home-profile/helpers.ts`` + ``home-profile/injection.ts``。

Hermes 设计上 ``pre_llm_call`` 只能往 **user message** 注入 ``{"context": text}``
（保 prompt cache，不污染 system prompt）。openclaw 端原本分
``prependSystemContext`` / ``appendSystemContext`` 两段，这里合并成单个 context
块：先指令块（identity/capabilities/perception/memory/notify/language），再数据块
（home-profile / pending-suggestions / device-catalog），用分隔线隔开。
其中 notify 块只注入 miloco 后台会话（见 :func:`is_miloco_background_session`）。

profile 判定（与 TS 端 ``resolveProfile`` 对齐）：
- ``platform == "cron"`` 或 session_id 含 ``":cron:"`` / ``"miloco:cron:"`` → minimal
- session_id 含 ``"miloco-rule"``     → rule
- session_id 含 ``"miloco-suggest"``  → suggestion
- 其余（含一切用户 IM）             → full
"""

from __future__ import annotations

import json
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


# cron 消息头：openclaw 把 cron turn 的消息改写成 ``[cron:<jobId> <jobName>] …``，
# backend schedule runner 则写成 ``[cron:<name>] …``。取方括号内整段做归属判断。
_CRON_HEADER_RE = re.compile(r"^\[cron:([^\]]*)\]")

# 受管 job 都叫 ``miloco-<name>``，要求 ``miloco-`` 出现在词首（方括号起首、或 jobId 后的
# 空格 / 冒号之后）。不做裸 substring 匹配：否则用户自建的「巡检 miloco 日志」这类 job 名
# 会被认领成后台会话。严格程度与 session_id 段判定对齐。
_MILOCO_JOB_RE = re.compile(r"(?:^|[\s:])miloco-")


def is_miloco_background_session(
    session_id: Optional[str],
    user_message: Optional[str] = None,
) -> bool:
    """与 TS 端 ``isMilocoBackgroundSession(sessionKey, {prompt})`` 等价。

    判断本轮是不是「miloco 后台会话」——由感知引擎 / miloco 定时任务 / 规则与任务事件
    拉起、turn 跑在后台、**回复对用户不可见**，只能按 miloco-notify skill 主动推送才算
    送达，故需注入 :data:`B_NOTIFY`；其余会话（用户 IM、常规 cron、CLI 主会话）不注入。

    两条线索任一命中即算：

    - session_id 有 miloco 段：backend dispatcher ``_ROUTE`` 写死
      ``agent:main:miloco{,-rule,-suggest}``、schedule runner 写死
      ``miloco-schedule:<cron_id>``，hermes 侧形如 ``miloco:cron:…`` / ``miloco-rule-<id>``。
    - cron 头带 miloco：isolated cron 的 session_id 是 ``agent:<id>:cron:<jobId>:run:<runId>``，
      jobId 随机、看不出归属，只能从消息头里的 job 名认领（miloco 自管的 job 都叫 ``miloco-*``）。
    """
    key = session_id or ""
    if any(seg == "miloco" or seg.startswith("miloco-") for seg in key.split(":")):
        return True
    m = _CRON_HEADER_RE.match((user_message or "").lstrip())
    return bool(m) and bool(_MILOCO_JOB_RE.search(m.group(1)))


# ---------------------------------------------------------------------------
# 静态指令块（抄自 prompt.ts，文本保持 1:1）
# ---------------------------------------------------------------------------

def _deploy_timezone() -> str:
    """读取部署时区（对齐 OpenClaw deployTimezone）。"""
    try:
        cfg_path = miloco_home() / "config.json"
        if cfg_path.is_file():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            tz = (cfg.get("server") or {}).get("timezone") or (cfg.get("timezone"))
            if tz:
                return str(tz)
    except (OSError, json.JSONDecodeError):
        pass
    import time as _time
    tz_idx = 1 if _time.localtime().tm_isdst > 0 else 0
    return _time.tzname[tz_idx] if _time.tzname[tz_idx] else "UTC"


def _build_timezone_block() -> str:
    tz = _deploy_timezone()
    return (
        f"## 时间与时区\n"
        f"家庭所在时区为 {tz}。感知事件、日志与 CLI 返回中的所有时刻"
        f"（如 `HH:MM:SS`）均已按此时区表示；创建定时任务也一律以此时区为准。"
        f"对话或系统消息中明确标注为 UTC / 其他时区的时刻，先换算到家庭时区再理解与表达；"
        f"向用户表达时间一律用家庭时区。"
        f"若上方家庭时区显示为 UTC，大概率是服务器未配置时区（没有家庭真住在 UTC）；"
        f"应与用户确认真实时区，并通过 `miloco-cli config set timezone <IANA>` 写入配置。"
    )

B_IDENTITY = (
    "你是经验丰富的家庭智能管家 Miloco。你能感知家中发生的事件，理解家庭成员的生活习惯，"
    "并据此做出贴心的行为或建议——查询和控制设备、把家调到成员舒适的状态，"
    "或在合适的时机给出有用的提醒。\n"
    "说话像住在这个家里的人：自然、利落、有分寸。不堆砌设备状态、传感器读数或技术细节，除非成员问起。"
)

B_CAPABILITIES = """## 能力概览
- 设备控制：查询和控制家中设备、调节环境、触发场景，把家调到成员舒适的状态
- 实时感知：查看家里此刻的状态——传感器读数、摄像头多模态理解
- 主动智能：结合感知记忆、家庭档案和当下的时间 / 环境，在合适时机给成员合理的提醒或建议，并通过语音 / IM / 米家推送送达
- 任务编排：把成员交代的事编排成提醒、周期任务、累积统计，或"满足条件就自动执行"的规则
- 家庭记忆：感知记忆（家中每天发生的事件）+ 家庭档案（成员构成、行为作息习惯、设备使用习惯）
- 成员识别：家庭成员的注册与识别"""

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


B_MEMORY = """## 家庭记忆
做任何事（控设备、给建议、写通知）之前，先查这两份记忆，让动作更精准、更合成员心意：
- **感知记忆**——家里最近发生了什么（每天自动归档的事件），用 `memory_search` 查（读不到当天文件就跳过）。
- **家庭档案**——成员的偏好、习惯、家庭规则、设备使用经验，见另注入的家庭档案摘要。

用户实时指令 > 档案规则（除非档案明确标注为底线 / 红线）。对话中出现成员喜好 / 家人信息 / 作息规律时，即使没说"记录"，也静默写入档案（先 `home-profile list` 看全量再写）。"""

# 留空占位：与 TS 端一致。
B_RULE_EXEC = ""
B_CONSTRAINTS = ""

# 只注入 miloco 后台会话（见 is_miloco_background_session）。既然作用域已由注入侧收敛，
# 正文就不再写「当面回答用户提问除外」这类例外——那句在语音 lane 反而是错的：语音提问的
# 答复同样得经 TTS 推回去。作用域交给 gate，正文只讲这类会话里该怎么做。
# 同理不提「用户要配置通知渠道」：配渠道必然发生在用户自己说话的会话里，而那种会话已经
# 不注入本块，写在这儿只会让模型以为后台也会有人来配。该入口交给 miloco-notify 的 skill
# description 兜（那是普通对话里唯一的加载触发器）。
B_NOTIFY = """## 通知用户
本轮由 miloco 后台触发（感知引擎 / 定时任务 / 规则或任务事件），会话不在用户面前——**你写进回复里的话没有任何人看得到**。
- 本轮**只要有信息要传达给家庭成员**（回应语音提问、危险预警、任务到期 / 达成、定时播报、设备异常、关怀提醒），**动手前必须先读 `miloco-notify` skill**：通知要决策「给谁 → 走哪个渠道（TTS / IM / 米家推送）→ 说什么」，这套判断只在 skill 里；别绕过它直接裸调 `miloco_im_push` / `miloco-cli notify push` / TTS，否则容易选错人、选错渠道、说错话。
- 本轮**不需要告知任何人**（只是归档、巡检、写记忆、改设备状态）→ 不必读本 skill，做完即止，别为了"有个交代"硬发一条。"""

B_LANGUAGE = "## 输出语言\n用用户使用的语言回复用户（设备名、人名、专有名词保持原样）。"


# ---------------------------------------------------------------------------
# 动态数据块
# ---------------------------------------------------------------------------

DEVICE_CATALOG_INTRO = """## 设备目录
下方 `# devices catalog` 是预注入的高频设备子集（≤50 台，非全量），字段规则见下方目录头部的注释。它**只用于快速拿到已点名单台设备的 did / spec_name**，不是全屋设备的全集。凡涉及设备**集合 / 多台 / 不确定数量**（无论查询还是控制），或目录里找不到目标，**必须先 `device list` 拉全量**再逐台处理，别拿子集当全部。
**任何 `device control / props / action` 或 `scene` 命令前（含查询），必须先读 `miloco-devices` skill**——命令选择、集合判定、安全确认、补 on、错误处理等都在其中，别只凭本目录裸发。"""


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

def _build_prepend(
    profile: Profile,
    session_id: Optional[str] = None,
    user_message: Optional[str] = None,
) -> str:
    """指令块，按 prompt.ts §3 序。"""
    parts: List[str] = [B_IDENTITY, _build_timezone_block()]
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
    # B_NOTIFY 只给 miloco 后台会话：那里回复对用户不可见，不主动推就等于没通知。
    # 用户 IM / 常规 cron 也注入的话，会把"回答用户此刻的提问""汇报刚做完的设备操作"
    # 一并误判成通知场景——白绕一次 skill，常规 cron 里还曾把一次汇报拖到 120s 超时。
    if is_miloco_background_session(session_id, user_message):
        parts.append(B_NOTIFY)
    parts.append(B_LANGUAGE)
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
        prepend = _build_prepend(profile, session_id, user_message)
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
