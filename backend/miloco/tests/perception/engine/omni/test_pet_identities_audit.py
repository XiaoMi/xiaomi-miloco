# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""pet_identities 的审计日志 + matched_rules 后置兜底。

PET_NAMING_SPEC 要求「规则点名某只宠物时，须该宠物本轮以 conf=high 列入 pet_identities 才可
hit=true」。该字段不入 OmniOutput（红线：不进 IdentityEngine / person 表），故这里验证两件事：
① 解析出的名单落审计日志，便于事后核「叫了谁、凭什么」；② 只判到 mid 的宠物被规则点名 → 丢弃
不触发（规则命中会发通知，宁漏不误触发）。
"""

import json
import logging

from miloco.perception.engine.omni.response_parser import parse_omni_response

_MAPPING = {"[pet] 小黑上沙发提醒": "rule-uuid-1"}


def _wrap(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _payload(pet_conf: str | None, *, with_rule: bool = True) -> dict:
    data: dict = {
        "caption": "小黑跳上了沙发",
        "matched_rules": (
            [{"rule_name": "[pet] 小黑上沙发提醒", "reason": "宠物上沙发", "hit": True}]
            if with_rule
            else []
        ),
        "speeches": [],
        "suggestions": [],
    }
    if pet_conf is not None:
        data["pet_identities"] = [{"name": "小黑", "conf": pet_conf, "reason": "尾尖白毛吻合"}]
    return data


def test_rule_naming_mid_conf_pet_is_suppressed(caplog):
    # conf=mid → 规则点名该宠物不得触发（护栏的代码侧兜底）
    with caplog.at_level(logging.WARNING):
        out = parse_omni_response(_wrap(json.dumps(_payload("mid"))), _MAPPING)
    assert out.matched_rules == []
    assert "event=pet_rule_suppressed" in caplog.text


def test_rule_naming_high_conf_pet_passes():
    # conf=high → 规则照常命中（护栏只拦 mid，不改变正常路径）
    out = parse_omni_response(_wrap(json.dumps(_payload("high"))), _MAPPING)
    assert [r.rule_id for r in out.matched_rules] == ["rule-uuid-1"]


def test_pet_identities_logged_for_audit(caplog):
    with caplog.at_level(logging.INFO):
        parse_omni_response(_wrap(json.dumps(_payload("high"))), _MAPPING)
    assert "event=pet_identities" in caplog.text
    assert "小黑" in caplog.text


def test_missing_or_malformed_pet_identities_is_noop():
    # 字段缺失 / 非 list → 与今日行为逐字一致（回归保险：不得影响 matched_rules）
    out = parse_omni_response(_wrap(json.dumps(_payload(None))), _MAPPING)
    assert [r.rule_id for r in out.matched_rules] == ["rule-uuid-1"]

    data = _payload(None)
    data["pet_identities"] = "not-a-list"
    out2 = parse_omni_response(_wrap(json.dumps(data)), _MAPPING)
    assert [r.rule_id for r in out2.matched_rules] == ["rule-uuid-1"]
