"""session_map: (sessionKey, lane) → Hermes session_id 确定性映射。"""

from __future__ import annotations

from adapter_pkg.session_map import map_session


def test_defaults_when_empty():
    assert map_session(None, None) == "miloco:main:default"
    assert map_session("", "") == "miloco:main:default"


def test_deterministic():
    assert map_session("s1", "L1") == "miloco:s1:L1"
    assert map_session("s1", "L1") == map_session("s1", "L1")


def test_format():
    assert map_session("k", "lane") == "miloco:k:lane"
    assert map_session("k", None) == "miloco:k:default"
    assert map_session(None, "lane") == "miloco:main:lane"


def test_sanitizes_control_chars():
    # 控制字符被剥离，不破坏 session_id
    out = map_session("a\nb\tc", "d")
    assert "\n" not in out and "\t" not in out
    assert out.startswith("miloco:")


def test_different_inputs_differ():
    assert map_session("a", "1") != map_session("a", "2")
    assert map_session("a", "1") != map_session("b", "1")


def test_truncates_overlong():
    key = "x" * 500
    out = map_session(key, "y")
    # 限长 200，加前缀 "miloco:" + 冒号 + lane
    body = out.split(":", 1)[1]
    key_part, lane_part = body.rsplit(":", 1)
    assert len(key_part) <= 200
    assert lane_part == "y"
