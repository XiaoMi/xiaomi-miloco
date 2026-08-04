# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""pet_identities 的审计日志（**纯 additive、零行为改动**）。

该字段不入 OmniOutput（红线：不进 IdentityEngine / person 表），只落审计日志便于事后核「叫了谁、
凭什么」。故这里验证两件事：① high/mid 名单进日志；② **无论 conf 取值、字段是否畸形，都不影响
matched_rules 等下游行为**——PET_NAMING_SPEC「规则点名须 conf=high」的代码侧强制留待后续，因为
靠「宠物名是规则名子串」判定会误吞无关规则（宠物叫「宝宝」时连「宝宝独处提醒」一起丢）。
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


def test_mid_conf_pet_does_not_affect_matched_rules():
    # 关键回归：conf=mid **不得**影响规则命中——曾用「宠物名是规则名子串」抑制，会误吞与宠物
    # 无关的规则（宠物叫「宝宝」时连「宝宝独处提醒」这类人身安全规则一起丢），已撤掉。
    out = parse_omni_response(_wrap(json.dumps(_payload("mid"))), _MAPPING)
    assert [r.rule_id for r in out.matched_rules] == ["rule-uuid-1"]


def test_high_conf_pet_does_not_affect_matched_rules():
    out = parse_omni_response(_wrap(json.dumps(_payload("high"))), _MAPPING)
    assert [r.rule_id for r in out.matched_rules] == ["rule-uuid-1"]


def test_unrelated_rule_not_dropped_by_pet_name_collision():
    # 宠物名与人身安全规则撞名（「宝宝」）：规则必须照常命中
    data = {
        "caption": "宝宝一个人在客厅",
        "matched_rules": [
            {"rule_name": "[t123] 宝宝独处超过 10 分钟提醒", "reason": "独处", "hit": True}
        ],
        "speeches": [],
        "suggestions": [],
        "pet_identities": [{"name": "宝宝", "conf": "mid", "reason": "花色像"}],
    }
    out = parse_omni_response(
        _wrap(json.dumps(data)), {"[t123] 宝宝独处超过 10 分钟提醒": "rule-uuid-9"}
    )
    assert [r.rule_id for r in out.matched_rules] == ["rule-uuid-9"]


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
