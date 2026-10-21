# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Scene rule task (场景联动任务) data models.

场景联动 = 一条 state 模式 rule 的 on_enter / on_exit 各挂一组动作的「规则任务」：
感知引擎 realtime_perceive 拿到 omni 结果后命中 rule 直接执行，完全不经过 agent
（执行路径是 runner 的 static slot，见 RuleRunner._execute_scene_action /
_execute_tts_action）。每个方向可以同时配两类动作：

  - **触发米家自动化场景**（``enter_scene_id`` / ``exit_scene_id``，rule 里是一条
    ``iid="scene"`` 的 RuleAction）；
  - **用指定音响播报一段 TTS**（``enter_tts`` / ``exit_tts``，rule 里是一条
    ``iid="scene_tts"`` 的 RuleAction + condition 上的 ``tts_enabled`` /
    ``tts_prompt``）。文案可以是静态的，也可以由 LLM 在命中时按画面动态生成——
    「每天出门前根据穿搭夸一句」就是后者。

本模块是 web「场景联动」tab 的后端——用 task + rule 两张现有表组合实现，完整复用
rule 的命中 / 抗抖 / 冷却机制：

  - enter：条件 query 从假变真并持续 enter_debounce_seconds（进入确认时间）
           → 触发 enter_scene_id，同时（若配了）播报 enter_tts；0 = 立即触发。实现上
           复用 Rule 的 duration_seconds + duration_ratio=1.0（STATE 模式 ENTERED 前置
           确认门槛，见 rule/schema.py 的 duration_seconds 注释）
  - exit ：条件真变假并持续 exit_debounce_seconds → 触发 exit_scene_id 并播报
           exit_tts；或配置 max_dwell_seconds 后「到期自动退出」→ 同样触发

任务启停 / 删除复用 TaskService（disable 同步 rule.enabled，delete 走 FK CASCADE）。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

from miloco.rule.schema import SceneTtsSlot

# 进入 / 退出两个方向共用的 TTS 生成指令（tts_prompt）长度上限。
MAX_TTS_PROMPT_LEN = 800
# 静态兜底文案长度上限（与 miloco.miot.voice.MAX_TTS_LEN 留出余量：住户可以写稍长，
# 真正下发前会再按 MAX_TTS_LEN 截断）。
MAX_TTS_TEXT_LEN = 200


def compose_tts_prompt(enter: SceneTtsSlot | None, exit: SceneTtsSlot | None) -> str | None:
    """把「进入 / 退出」两个方向的动态文案要求合成一条 ``scene_notes`` 追加段。

    ``RuleCondition.scene_notes`` 是住户自己写的判定补充细则，这里把 TTS 的写作要求接在
    它后面（``## 动态播报`` 段），随规则一起注入 omni prompt。两个方向都没开动态 TTS
    时返回 None。

    为什么要求只有一条：同一个规则每帧只会命中「进入」或「退出」其中一个方向，而 omni
    prompt 是按规则（不是按帧）下发的；把两个方向的要求都写清楚、让方向由当前命中的
    target_scene 决定，模型就不会把「进门该说的欢迎」套到「离开该说的再见」上。
    """
    parts: list[str] = []
    if enter is not None and enter.tts_content:
        ask = (enter.tts_prompt or '').strip()
        parts.append(f'- 进入时：{ask or "用一句自然、亲切的话结合本轮画面描述或夸赞"}')
    if exit is not None and exit.tts_content:
        ask = (exit.tts_prompt or '').strip()
        parts.append(f'- 退出时：{ask or "用一句自然、亲切的话结合本轮画面描述或夸赞"}')
    if not parts:
        return None
    return '## 动态播报要求\n' + '\n'.join(parts)


def split_scene_notes(scene_notes: str | None) -> str | None:
    """从 ``scene_notes`` 里剥掉本模块合成的「## 动态播报要求」段，返回住户自己写的部分。

    TTS 段是 ``compose_tts_prompt`` 追加在末尾的；读回视图时剥掉，住户在 web 里看到的
    「场景补充说明」就始终是它自己写的原文，不会被合成的段落污染（保存时再重新合成）。
    """
    text = scene_notes or ''
    idx = text.find('\n## 动态播报要求')
    if idx < 0 and text.startswith('## 动态播报要求'):
        idx = 0
    if idx < 0:
        return text or None
    return text[:idx].rstrip() or None


def merge_scene_notes(user_notes: str | None, tts_section: str | None) -> str | None:
    """住户原文 + 合成的 TTS 段 → 最终存进 ``scene_notes`` 的文本（空归一到 None）。"""
    base = (user_notes or '').strip()
    if not tts_section:
        return base or None
    return f'{base}\n\n{tts_section}' if base else tts_section


def normalize_tts_slot(slot: SceneTtsSlot | None) -> SceneTtsSlot | None:
    """空白串归一到 None；没有设备 did 时该 slot 本身无意义（由 service 传 None）。"""
    if slot is None:
        return None
    return SceneTtsSlot(
        tts_content=slot.tts_content,
        tts_prompt=(slot.tts_prompt or '').strip() or None,
        tts_text=(slot.tts_text or '').strip() or None,
    )


class SceneTaskCreateRequest(BaseModel):
    """创建场景联动任务。每个方向（进入 / 退出）至少配一个动作，两个方向至少配一个。

    动作两类：触发米家自动化场景（``enter_scene_id`` / ``exit_scene_id``）与用指定
    音响播报 TTS（``enter_tts`` / ``exit_tts``），同一方向可以同时设置。
    """

    description: str = Field(..., min_length=1, max_length=200, description="任务名（同时作为 rule.name）")
    perceive_device_ids: list[str] = Field(..., description="感知设备 did 列表（任一命中即触发）")
    query: str = Field(..., description="进入条件（自然语言，进行时状态描述）")
    scene_notes: str | None = Field(
        None,
        max_length=8000,
        # 兼容过渡期旧键 system_context（该字段短暂用过此名）——老前端缓存里的请求
        # 不会被 extra='forbid' 挡下、也不会丢配置；写出恒用 scene_notes。
        validation_alias=AliasChoices("scene_notes", "system_context"),
        description=(
            "该规则的场景补充说明 / 注意事项（场景细节、目标区域、"
            "什么算/不算），随规则下发给 omni；None/空 = 不注入"
        ),
    )
    enter_scene_id: str | None = Field(None, description="进入时触发的米家场景 id；不配 = 进入不触发自动化")
    exit_scene_id: str | None = Field(None, description="退出时触发的米家场景 id；不配 = 退出不触发自动化")
    # TTS 动作：设备 did 放在 slot 外的一对字段里（rule 层是 RuleAction.did），文案要求
    # 放在 slot 内的 tts_prompt / tts_text。
    enter_tts_device_id: str | None = Field(None, description="进入时播报 TTS 的音响 did")
    enter_tts: SceneTtsSlot | None = Field(None, description="进入时的 TTS 配置（动态/静态文案）")
    exit_tts_device_id: str | None = Field(None, description="退出时播报 TTS 的音响 did")
    exit_tts: SceneTtsSlot | None = Field(None, description="退出时的 TTS 配置（动态/静态文案）")
    cooldown_minutes: int = Field(5, ge=1, le=1440, description="场景/播报触发冷却（分钟），进入/退出共用")
    enter_debounce_seconds: int = Field(0, ge=0, le=86400, description="进入确认时间（秒）：条件持续满足这么久才触发进入动作；0=立即触发（现状）")
    exit_debounce_seconds: int = Field(60, ge=0, le=86400, description="退出确认时间（秒）：条件持续不满足这么久才触发退出动作")
    max_dwell_seconds: int | None = Field(None, ge=1, le=86400, description="最长驻留（秒）：进入后到时强制退出并触发退出动作（到期自动退出）")
    enabled: bool = Field(True, description="创建后是否立即启用")

    model_config = {'extra': 'forbid'}

    @model_validator(mode='after')
    def _validate_actions(self):
        for direction, scene_id, dev_id, slot in (
            ('enter', self.enter_scene_id, self.enter_tts_device_id, self.enter_tts),
            ('exit', self.exit_scene_id, self.exit_tts_device_id, self.exit_tts),
        ):
            if bool(dev_id) != bool(slot):
                raise ValueError(
                    f'{direction}_tts_device_id 与 {direction}_tts 必须同时配置'
                    '（选音响时带上 TTS 配置，或两者都不配）'
                )
            if slot is not None:
                _validate_tts_slot(direction, slot)
        if not self.enter_scene_id and not self.exit_scene_id and not self.enter_tts and not self.exit_tts:
            raise ValueError(
                '进入 / 退出至少配置一个动作（触发自动化场景或播报 TTS）'
            )
        return self


def _validate_tts_slot(direction: str, slot: SceneTtsSlot) -> None:
    """静态文案与动态要求至少给一个（否则「播报」这句话永远说不出口）。"""
    prompt = (slot.tts_prompt or '').strip()
    text = (slot.tts_text or '').strip()
    if len(prompt) > MAX_TTS_PROMPT_LEN:
        raise ValueError(
            f'{direction}_tts.tts_prompt 不能超过 {MAX_TTS_PROMPT_LEN} 字'
        )
    if len(text) > MAX_TTS_TEXT_LEN:
        raise ValueError(
            f'{direction}_tts.tts_text 不能超过 {MAX_TTS_TEXT_LEN} 字'
        )
    if not prompt and not text:
        raise ValueError(
            f'{direction}_tts 至少要填「播报要求」或「兜底文案」之一'
        )


class SceneTaskUpdateRequest(BaseModel):
    """部分更新——全部字段可选。

    - 场景类字段置 None = 清空该方向（但不能两个方向同时清空）；
    - ``enter_tts`` / ``enter_tts_device_id`` 置 None = 移除该方向的 TTS（同理 ``exit_*``）；
    - ``enter_debounce_seconds`` / ``max_dwell_seconds`` 置 None 或 0 = 清除该行为
      （进入立即触发 / 不自动退出）；其余字段 None = 不动。
    """

    description: str | None = Field(None, min_length=1, max_length=200)
    perceive_device_ids: list[str] | None = None
    query: str | None = None
    # 显式置 None / "" = 清空该规则的场景补充说明（合法新值，非"不动"）。
    # validation_alias 兼容旧键 system_context（同 create）。
    scene_notes: str | None = Field(
        None,
        max_length=8000,
        validation_alias=AliasChoices("scene_notes", "system_context"),
    )
    enter_scene_id: str | None = None
    exit_scene_id: str | None = None
    enter_tts_device_id: str | None = None
    enter_tts: SceneTtsSlot | None = None
    exit_tts_device_id: str | None = None
    exit_tts: SceneTtsSlot | None = None
    cooldown_minutes: int | None = Field(None, ge=1, le=1440)
    enter_debounce_seconds: int | None = Field(None, ge=0, le=86400)
    exit_debounce_seconds: int | None = Field(None, ge=0, le=86400)
    max_dwell_seconds: int | None = Field(None, ge=1, le=86400)
    enabled: bool | None = None

    model_config = {'extra': 'forbid'}

    @model_validator(mode='after')
    def _validate_tts_pair(self):
        fields = self.model_fields_set
        for direction in ('enter', 'exit'):
            dev_key = f'{direction}_tts_device_id'
            slot_key = f'{direction}_tts'
            if (dev_key in fields) != (slot_key in fields):
                raise ValueError(
                    f'{dev_key} 与 {slot_key} 必须同时提交'
                    '（置 null 表示移除该方向的 TTS）'
                )
            slot = getattr(self, slot_key)
            # 只改配置不换设备（dev 字段本次没带）时也要校验每次提交的 slot。
            if slot is not None:
                _validate_tts_slot(direction, slot)
        return self


class SceneTtsView(BaseModel):
    """一个方向的 TTS 播报视图（web 回显用）。"""

    device_id: str | None = None
    device_name: str | None = None
    # 真 = 模型按本轮画面生成文案（tts_prompt 是写作要求）；False = 固定文案
    tts_content: bool = False
    tts_prompt: str | None = None
    tts_text: str | None = None


class SceneTaskView(BaseModel):
    """场景联动任务视图（task + rule 合体）。"""

    task_id: str
    description: str
    status: Literal["active", "paused"]
    paused_at: str | None = None
    created_at: str
    updated_at: str | None = None
    rule_id: str
    enabled: bool
    query: str
    # 住户自己写的场景补充说明（已剥掉合成的「## 动态播报要求」段，见 split_scene_notes）
    scene_notes: str | None = None
    perceive_device_ids: list[str]
    enter_scene_id: str | None = None
    enter_scene_name: str | None = None
    exit_scene_id: str | None = None
    exit_scene_name: str | None = None
    enter_tts: SceneTtsView | None = None
    exit_tts: SceneTtsView | None = None
    cooldown_minutes: int | None = None
    enter_debounce_seconds: int = 0
    exit_debounce_seconds: int = 60
    max_dwell_seconds: int | None = None
    # 动态 TTS 开着但动态文案生成当前不可用（感知引擎没起 / 未开纯场景触发模式）——
    # 前端据此提示「动态文案可能拿不到，会退回兜底文案」。
    dynamic_tts_ready: bool = True


class SceneVoiceDevice(BaseModel):
    """可选音响（/scene-tasks/voice-devices）。"""

    did: str
    name: str
    room_name: str | None = None
    online: bool = False


_TTS_DIRECTION_RE = re.compile(r'^- (进入|退出)时：(.*)$', re.MULTILINE)


def parse_tts_prompts(tts_section: str | None) -> tuple[str | None, str | None]:
    """从合成的「## 动态播报要求」段里拆出 (进入要求, 退出要求)。

    ``tts_enabled`` 是规则级的一个开关（进入 / 退出共用一段 scene_notes），两个方向的
    「要求」分别放在两行里，读回视图时据此还原。
    """
    enter: str | None = None
    exit_: str | None = None
    if not tts_section:
        return None, None
    for m in _TTS_DIRECTION_RE.finditer(tts_section):
        text = m.group(2).strip() or None
        if m.group(1) == '进入':
            enter = text
        else:
            exit_ = text
    return enter, exit_


__all__ = [
    'MAX_TTS_PROMPT_LEN',
    'MAX_TTS_TEXT_LEN',
    'SceneTaskCreateRequest',
    'SceneTaskUpdateRequest',
    'SceneTaskView',
    'SceneTtsView',
    'SceneVoiceDevice',
    'compose_tts_prompt',
    'merge_scene_notes',
    'normalize_tts_slot',
    'parse_tts_prompts',
    'split_scene_notes',
]
