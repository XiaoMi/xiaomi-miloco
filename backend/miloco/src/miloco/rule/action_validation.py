# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""RuleAction 的跨字段校验。

rule 写入与 task 写入共用这一份判据：task 的边界动作是运行期唯一来源，但它的
schema 是 ``list[dict]``，不经过 rule 侧那轮校验；两条路径各自写一套的话，迟早
会出现「rule 能建、task 能存、runner 不认」的第三种状态。

字段级约束（prompt 长度、sources 数量、max_chars 范围、未知键）由 Pydantic 的
:class:`GenerateSpec` 承担；这里只写跨字段关系。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from miloco.middleware.exceptions import ValidationException
from miloco.rule.schema import SCENE_IID, RuleAction, parse_device_iid

RESULT_PLACEHOLDER = "{{result}}"


def _has_result_placeholder(params: list[Any] | None) -> bool:
    return any(
        isinstance(item, str) and RESULT_PLACEHOLDER in item
        for item in (params or [])
    )


def action_validation_error(action: RuleAction, where: str) -> str | None:
    """返回第一条违反项；全部通过返回 ``None``。

    ``where`` 是调用方给出的定位，如 ``on_enter_actions[0]``。
    """
    prefix = f"{where} (did={action.did}, iid={action.iid})"

    # 与 runner._execute_action 同口径：三种形态之外一律拒。少了这道，
    # `scene.1.2` / `prop.2` 这类写法能建成功，运行期每次 fire 只在 rule_log
    # 里留一条 invalid_iid，规则永远是哑的。
    if action.iid != SCENE_IID and parse_device_iid(action.iid) is None:
        return (
            f"{prefix}: iid must be '{SCENE_IID}', 'prop.<siid>.<piid>' or "
            f"'action.<siid>.<aiid>'"
        )

    # 幂等分支会跳过判定直接下发，等于没有去重（原因见 SCENE_IID）。
    if action.iid == SCENE_IID and action.idempotent:
        return f"{prefix}: iid={SCENE_IID} requires idempotent=false"

    # 冷却是场景唯一的去重手段，而 runner 把 0 当「无冷却」——填 0 会让每次
    # fire 都真触发一次场景，正是执行侧闸门要防的那种。
    if action.iid == SCENE_IID and (action.cooldown_minutes or 0) < 1:
        return f"{prefix}: iid={SCENE_IID} requires cooldown_minutes >= 1"

    generate = action.generate
    if generate is None:
        # 拼错 generate（如 generat）会被 Pydantic 当未知字段丢掉（RuleAction
        # 不禁止额外键），动作照常下发，音箱把字面量 {{result}} 念出来。占位符
        # 必须伴随生成块，这条是那种拼写错误唯一能被抓住的地方。
        if _has_result_placeholder(action.params):
            return (
                f"{prefix}: params contains {RESULT_PLACEHOLDER} but generate "
                f"is missing"
            )
    else:
        parsed = parse_device_iid(action.iid)
        if parsed is None or parsed[0]:
            # 本期生成结果只用于动作参数；prop 的 value 是数字/布尔，scene
            # 没有参数通道。
            return (
                f"{prefix}: generate requires 'action.<siid>.<aiid>', "
                f"got '{action.iid}'"
            )
        # 生成动作读不到「当前值」，幂等预检必然落空；只靠冷却去重。
        if action.idempotent:
            return f"{prefix}: generate requires idempotent=false"
        if (action.cooldown_minutes or 0) < 1:
            return f"{prefix}: generate requires cooldown_minutes >= 1"
        # params 省略/为空等价于 ["{{result}}"]（runner 侧补）；非空时必须
        # 至少有一处占位，否则生成结果无处可去，设备只收到一份无关参数。
        if action.params and not _has_result_placeholder(action.params):
            return (
                f"{prefix}: generate requires at least one params item "
                f"containing {RESULT_PLACEHOLDER}"
            )
        # Pydantic 的 min_length 只数字符，纯空白能过；CLI 已按 strip 判，
        # 两处口径要一致，否则 CLI 拦下的空 prompt 走 HTTP 能存进库。
        if not generate.prompt.strip():
            return f"{prefix}: generate.prompt must be a non-empty string"
        if generate.sources and any(not s.strip() for s in generate.sources):
            return f"{prefix}: generate.sources must not contain empty dids"

    # idempotent=False 的 action 不会做「读现值后判跳过」，必须靠
    # cooldown_minutes 限频；否则 runner 的冷却分支被 None 短路，每次 ENTERED
    # 都重发。
    if not action.idempotent and action.cooldown_minutes is None:
        return f"{prefix}: idempotent=false requires cooldown_minutes"

    return None


def validate_action(action: RuleAction, where: str) -> None:
    """校验一条已解析的动作，违规时抛 :class:`ValidationException`。"""
    error = action_validation_error(action, where)
    if error:
        raise ValidationException(error)


def validate_action_dict(raw: Any, where: str) -> None:
    """校验 task 侧的原始动作 dict（先按 RuleAction 解析再走共享判据）。"""
    try:
        action = RuleAction.model_validate(raw)
    except ValidationError as e:
        details = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise ValidationException(f"{where}: 动作格式非法: {details}") from e
    validate_action(action, where)
