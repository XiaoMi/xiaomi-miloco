"""pre_llm_call 上下文注入：profile 分级与文本块装配。"""

from __future__ import annotations

import pytest
from miloco_plugin_pkg import context_injection as ci


@pytest.fixture
def tmp_miloco_home(tmp_path, monkeypatch):
    """临时 MILOCO_HOME，隔离真实配置。"""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    return tmp_path


# ---------- resolve_profile ----------

def test_profile_cron(tmp_miloco_home):
    assert ci.resolve_profile("anything", platform="cron") == "minimal"
    assert ci.resolve_profile("miloco:cron:perception-digest") == "minimal"
    assert ci.resolve_profile("cron:foo") == "minimal"
    assert ci.resolve_profile("s", user_message="[cron:habit-suggest]") == "minimal"


def test_profile_rule_and_suggestion(tmp_miloco_home):
    assert ci.resolve_profile("miloco-rule-abc") == "rule"
    assert ci.resolve_profile("miloco-suggest-xyz") == "suggestion"


def test_profile_full(tmp_miloco_home):
    assert ci.resolve_profile("agent:main:miloco") == "full"
    assert ci.resolve_profile("anything-else") == "full"


# ---------- inject_context ----------

def test_full_includes_catalog_and_capabilities(tmp_miloco_home, monkeypatch):
    monkeypatch.setattr(ci, "get_catalog", lambda: "# devices catalog\n灯|客厅|light|online")
    out = ci.inject_context(session_id="agent:main:miloco", user_message="把客厅灯打开")
    assert out is not None
    ctx = out["context"]
    assert "## 能力概览" in ctx
    # 数据块
    assert "# devices catalog" in ctx
    assert "## 家庭档案" in ctx


def test_minimal_includes_identity_notify_timezone(tmp_miloco_home, monkeypatch):
    """minimal profile 注入 identity + timezone + notify + language（对齐 OpenClaw）。"""
    monkeypatch.setattr(ci, "get_catalog", lambda: "# devices catalog\nx")
    out = ci.inject_context(session_id="miloco:cron:digest", platform="cron")
    assert out is not None
    ctx = out["context"]
    assert "Miloco" in ctx  # B_IDENTITY
    assert "时区" in ctx  # B_TIMEZONE
    assert "通知用户" in ctx  # B_NOTIFY
    assert "输出语言" in ctx  # B_LANGUAGE


def test_identity_block_does_not_override_host_persona(tmp_miloco_home, monkeypatch):
    """插件是能力层不是人格层：注入不得写死 agent 身份（对齐 OpenClaw）。

    本块逐轮进宿主 agent 上下文（hermes 侧还会进 <system> 消息），写死
    "你是……Miloco" 会顶掉用户给自己 agent 设的名字与人设。所有 profile 都要守住。
    """
    monkeypatch.setattr(ci, "get_catalog", lambda: "")
    for sid, platform in (
        ("agent:main:miloco", None),
        ("miloco-rule-1", None),
        ("miloco-suggest-1", None),
        ("miloco:cron:digest", "cron"),
    ):
        out = ci.inject_context(session_id=sid, platform=platform)
        assert out is not None
        ctx = out["context"]
        assert "你是经验丰富的家庭智能管家 Miloco" not in ctx, sid
        assert "不是你的身份" in ctx, sid
        assert "按你自己的设定回答" in ctx, sid
        # 能力叙述本身保留，装了插件仍知道自己能干什么
        assert "家庭管家的能力" in ctx, sid


def test_empty_catalog_omitted(tmp_miloco_home, monkeypatch):
    """catalog 空但 full profile → prepend 仍有能力概览，context 不为 None。"""
    monkeypatch.setattr(ci, "get_catalog", lambda: "")
    out = ci.inject_context(session_id="agent:main:miloco", user_message="hi")
    assert out is not None
    assert "# devices catalog" not in out["context"]
    assert "## 能力概览" in out["context"]


def test_minimal_includes_identity_and_timezone(tmp_miloco_home, monkeypatch):
    """minimal profile 注入 identity + timezone（对齐 OpenClaw）。"""
    out = ci.inject_context(session_id="x", platform="cron")
    assert out is not None
    assert "Miloco" in out["context"]
    assert "时区" in out["context"]


def test_full_returns_dict_with_blocks(tmp_miloco_home, monkeypatch):
    """full profile + 有 catalog → prepend 有能力概览+时区，append 有 catalog + home_profile。"""
    monkeypatch.setattr(ci, "get_catalog", lambda: "# devices catalog\n灯|客厅")
    out = ci.inject_context(session_id="agent:main:miloco", user_message="hi")
    assert out is not None
    assert "context" in out
    assert "## 能力概览" in out["context"]
    assert "## 时间与时区" in out["context"]
    assert "# devices catalog" in out["context"]


def test_timezone_block_present_in_all_profiles(tmp_miloco_home):
    """时区块在所有 profile 中均注入（对齐 OpenClaw）。"""
    for sid in ("agent:main:miloco", "miloco:cron:digest", "miloco-rule-1", "miloco-suggest-1"):
        out = ci.inject_context(session_id=sid, platform="cron" if "cron" in sid else None)
        if out:
            assert "## 时间与时区" in out["context"], f"missing timezone in {sid}"


# ---------- build_home_profile_block ----------

def test_home_profile_demotes_headings(tmp_miloco_home):
    prof = tmp_miloco_home / "home-profile" / "profile.md"
    prof.parent.mkdir(parents=True)
    prof.write_text("# 家庭档案\n爸爸喜欢 25 度\n## 作息\n早起", encoding="utf-8")
    block = ci.build_home_profile_block()
    assert "## 家庭档案" in block
    # 原 H1 降为 H2（与已有的 "## 家庭档案" 合流），原 H2 降为 H3
    assert "### 作息" in block
    assert "\n# 家庭档案" not in block  # 不应残留独立 H1


def test_home_profile_missing_sentinel(tmp_miloco_home):
    # 无 profile.md → load 层返回哨兵串 (暂无内容)，build 层补上标题后返回
    block = ci.build_home_profile_block()
    assert block == "## 家庭档案\n\n(暂无内容)"


# ---------- 异常安全 ----------

def test_inject_never_raises(tmp_miloco_home, monkeypatch):
    def boom():
        raise RuntimeError("catalog blew up")
    monkeypatch.setattr(ci, "get_catalog", boom)
    out = ci.inject_context(session_id="agent:main")
    # 钩子绝不抛：catalog 异常时应降级返回（仍含指令块）或 None，不能上抛
    assert out is None or "context" in out
