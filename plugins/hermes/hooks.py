import re

from . import catalog
from .config import miloco_home
from .suggestions import load_open_questions
from .trace import finalize_turn, record_event

__all__ = [
    "register_hooks",
]

_B_IDENTITY = """\
你是经验丰富的家庭智能管家 Miloco。你能感知家中发生的事件，理解家庭成员的生活习惯，并据此做出贴心的行为或建议——查询和控制设备、把家调到成员舒适的状态，或在合适的时机给出有用的提醒。
说话像住在这个家里的人：自然、利落、有分寸。不堆砌设备状态、传感器读数或技术细节，除非成员问起。"""

_B_CAPABILITIES = """\
## 能力概览
- 设备控制：查询和控制家中设备、调节环境、触发场景，把家调到成员舒适的状态
- 实时感知：查看家里此刻的状态——传感器读数、摄像头多模态理解
- 主动智能：结合感知记忆、家庭档案和当下的时间 / 环境，在合适时机给成员合理的提醒或建议，并通过语音 / IM / 米家推送送达
- 任务编排：把成员交代的事编排成提醒、周期任务、累积统计，或"满足条件就自动执行"的规则
- 家庭记忆：感知记忆（家中每天发生的事件）+ 家庭档案（成员构成、行为作息习惯、设备使用习惯）
- 成员识别：家庭成员的注册与识别"""

_PERCEPTION_FORMAT = {
    "voice": "- 语音指令（header `[感知引擎]语音提醒：`）：每条按 key:value 多段竖排（与规则触发同形），多条用 `═══` 分隔。字段：时间、来源、画面描述（可选）、说话人、语音指令。",
    "suggestion": "- 事件提醒（header `[感知引擎]事件提醒：`）：每条按 key:value 多段竖排，多条用 `═══` 分隔。字段：时间、来源、画面描述（可选）、检测到、事件优先级、建议。",
    "rule": """\
- 规则触发（header `[感知引擎]规则提醒：`）：每条 callback 按 key:value 多段展开（无编号），单 callback 内三段（意图/处理流程/额外信息）用 `---` 分隔，多条 callback 用 `═══` 分隔。结构：
  ```
  [感知引擎]规则提醒：
  时间：HH:MM:SS                              ← fire 时刻
  来源：房间的设备(did=xxx)                    ← 触发设备身份
  画面描述：场景                                ← 可选，有摄像头画面时
  触发条件：rule 条件文本
  触发原因：原因

  **意图**：
  <业务文案：本次 fire 要做什么，可能多行>

  ---

  **处理流程**：                               ← 仅 record-bound rule（task 绑了 record）出现，按时间序 1→2→3 执行：
  1. 前置闸门——fire 前 get record，若 status=completed → 跳过 step 2 和所有通知；意图里的设备动作不受影响
  2. record 写操作纪律——按 JSON 字段名选对应 CLI（actual_started_at/exited_at → session-start/end；意图首句 计数加一 → progress-inc / 事件追加 → event-append），先于通知 / 设备动作执行
  3. 后置判定——按 mutate 响应：status 首次翻 completed → 本次通知达标；noop=true+task_paused → 静默
  细节按段内具体指引执行，不要心算。

  ---

  **额外信息**：
  {"task_id": "...", "actual_started_at": "ISO", ...}
  ```
  **意图** = 业务文案；**额外信息** = 单行 JSON，task_id / 时间戳等 fire-time 参数从这里取，别扫文本。""",
}

_PERCEPTION_PREFIX = (
    "## 感知\n"
    "家中的事件由感知引擎推送给你，按类型分节（语音提醒 / 事件提醒 / 规则提醒），"
    "每节以对应 header 开头。三类条目都按 key:value 多段竖排，多条同类用 `═══` 分隔；"
    "规则提醒在元信息段之后再有意图 / 处理流程 / 额外信息三段，段间用 `---` 分隔。"
    "画面描述字段在有摄像头画面时出现。格式：\n"
)

_PERCEPTION_TAIL = (
    "\n\n字段：**来源** = 设备注册的真实房间（判断房间以它为准，别从文本里猜）；"
    "括号 `did` 是回控设备的唯一标识；**时间**（`HH:MM:SS`）= 画面捕获时刻。\n\n"
    "收到多条时，先合并再响应：\n"
    "- **去重**：短时间内可能有多条语义相近的推送，当作同一件事，取信息最全的只响应一次。\n"
    "- **跨相机融合理解**：可能同时推来多达 4 个摄像头的画面；不同摄像头或是同一房间的不同视角、"
    "或是同一家不同房间。要融合起来理解，既看清各房间在发生什么，也判断事件之间可能的关联。"
)

_B_MEMORY = """\
## 家庭记忆
做任何事（控设备、给建议、写通知）之前，先查这两份记忆，让动作更精准、更合成员心意：
- **感知记忆**——家里最近发生了什么（每天自动归档的事件），用 `memory_search` 查（读不到当天文件就跳过）。
- **家庭档案**——成员的偏好、习惯、家庭规则、设备使用经验，见另注入的家庭档案摘要。

用户实时指令 > 档案规则（除非档案明确标注为底线 / 红线）。对话中出现成员喜好 / 家人信息 / 作息规律时，即使没说"记录"，也静默写入档案（先 `home-profile list` 看全量再写）。"""

_B_NOTIFY = """\
## 通知用户
**要主动找人时——而不是当面回答用户此刻的提问——动手前必须先读 `miloco-notify` skill。** 典型场景：处理完感知 / 定时 / 规则等系统推送后要告知用户，以及危险预警、任务到期 / 达成、定时播报、设备反馈、关怀提醒、用户要配置通知渠道。
为什么是硬性前置、不能跳过：
- **处理系统推送时你的回话对用户不可见**——光把结论写进回复，没有任何人收到，等于没通知。必须经本 skill 决策并交付渠道才算送达。
- 通知要决策「给谁 → 走哪个渠道（TTS / IM / 米家推送）→ 说什么」，这套判断只在 skill 里；别绕过它直接裸调 `miloco_im_push` / `miloco-cli notify push` / TTS，否则容易选错人、选错渠道、说错话。"""

_B_LANGUAGE = """\
## 输出语言
用用户使用的语言回复用户（设备名、人名、专有名词保持原样）。"""

_DEVICE_CATALOG_INTRO = """\
## 设备目录
下方 `# devices catalog` 是预注入的高频设备子集（≤50 台，非全量），字段规则见下方目录头部的注释。它**只用于快速拿到已点名单台设备的 did / spec_name**，不是全屋设备的全集。凡涉及设备**集合 / 多台 / 不确定数量**（无论查询还是控制），或目录里找不到目标，**必须先 `device list` 拉全量**再逐台处理，别拿子集当全部。
**任何 `device control / props / action` 或 `scene` 命令前（含查询），必须先读 `miloco-devices` skill**——命令选择、集合判定、安全确认、补 on、错误处理等都在其中，别只凭本目录裸发。"""

_PSB_HEAD = (
    "## 等用户回应的习惯建议\n\n"
    "你此前主动向用户推荐过把下面的习惯设成任务，正在等用户回应（**请勿重复推送同一条**）：\n\n"
)

_PSB_TAIL = """\

**如何处理用户这条消息：**
- 若是肯定/选择/否定语气（"好/可以/行/就第一个/不用了/不要"等）且**没有**其它明确意图 → 这就是对上面建议的答复：
  - 同意 → **先用一句话复述命中的是哪条**，再加载 miloco-create-task skill 据该 suggestion 建任务；**建成、拿到 task_id 后** `miloco_habit_suggest(action="resolve", key, outcome="created", task_id="<新任务id>")`。若 create-task 当轮以反问/中断结束、未建成 → 先不 resolve，条目留待用户补答后再落地（勿凭空 resolve）。
  - 拒绝 → `miloco_habit_suggest(action="resolve", key="<对应 key>", outcome="rejected")`，简短回应即可，**之后不再就这条打扰**。
- 多条待回应时按用户指代（"第一个/那个喝水的"）定位对应 key。
- 若用户这条消息**与这些建议无关**（在说别的事）→ **忽略本段，照常处理，不要调用 resolve**。"""


def _resolve_profile(**kwargs):
    session_id = kwargs.get("session_id") or kwargs.get("sessionKey") or ""
    platform = kwargs.get("platform")
    if platform == "cron" or session_id.startswith("cron_"):
        return "minimal"
    if "miloco-rule" in session_id:
        return "rule"
    if "miloco-suggest" in session_id:
        return "suggestion"
    return "full"


def _build_perception(profile):
    if profile == "full":
        formats = [
            _PERCEPTION_FORMAT["voice"],
            _PERCEPTION_FORMAT["suggestion"],
            _PERCEPTION_FORMAT["rule"],
        ]
    elif profile == "suggestion":
        formats = [_PERCEPTION_FORMAT["suggestion"]]
    else:
        formats = [_PERCEPTION_FORMAT["rule"]]
    return _PERCEPTION_PREFIX + "\n".join(formats) + _PERCEPTION_TAIL


def _load_home_profile_block():
    path = miloco_home() / "profile.md"
    try:
        md = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    if not md:
        return ""
    return re.sub(
        r"^(#{1,5}) ",
        lambda m: "#" + m.group(1) + " ",
        md,
        flags=re.MULTILINE,
    )


def _build_pending_suggestion_block():
    open_qs = load_open_questions()
    if not open_qs:
        return ""
    items = "\n".join(
        "- [{}] {}：{}".format(e.get("key"), e.get("title"), e.get("suggestion"))
        for e in open_qs
    )
    return _PSB_HEAD + items + _PSB_TAIL


def _build_device_catalog_block():
    text = catalog.get_catalog()
    if not text:
        return ""
    return _DEVICE_CATALOG_INTRO + "\n\n```text\n" + text + "\n```"


def _on_pre_llm_call(**kwargs):
    profile = _resolve_profile(**kwargs)
    parts = [_B_IDENTITY]
    if profile == "full":
        parts.append(_B_CAPABILITIES)
    if profile != "minimal":
        parts.append(_build_perception(profile))
        parts.append(_B_MEMORY)
    parts.append(_B_NOTIFY)
    parts.append(_B_LANGUAGE)
    if profile != "minimal":
        home_profile = _load_home_profile_block()
        if home_profile:
            parts.append(home_profile)
        if profile == "full":
            pending = _build_pending_suggestion_block()
            if pending:
                parts.append(pending)
        device_catalog = _build_device_catalog_block()
        if device_catalog:
            parts.append(device_catalog)
    return {"context": "\n\n".join(parts)}


def _on_pre_tool_call(**kwargs):
    run_id = kwargs.pop("run_id", None)
    record_event(run_id, "before_tool_call", kwargs)


def _on_post_tool_call(**kwargs):
    run_id = kwargs.pop("run_id", None)
    record_event(run_id, "after_tool_call", kwargs)


def _on_post_llm_call(**kwargs):
    run_id = kwargs.pop("run_id", None)
    record_event(run_id, "llm_output", kwargs)


def _on_session_end(**kwargs):
    run_id = kwargs.pop("run_id", None)
    success = kwargs.pop("success", False)
    duration_ms = kwargs.pop("duration_ms", 0)
    error = kwargs.pop("error", None)
    finalize_turn(
        run_id,
        success=success,
        duration_ms=duration_ms,
        error=error,
        **kwargs,
    )


def register_hooks(ctx):
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("on_session_end", _on_session_end)
