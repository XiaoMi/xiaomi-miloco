"""Unit tests for KV-backed scope: home enabled set + camera disabled set.

Covers:
- filter.py round-trip via in-memory KVRepo stub
- enabled set semantics (empty = no filter)
- disabled set semantics (empty = no exclusion)
- service.switch_home / toggle_camera 单项写
- service.list_homes / list_cameras_with_state in_use 标记正确
- _assert_did_in_allowed_home 同时识别相机 did
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.middleware.exceptions import (
    MiotServiceException,
    ResourceNotFoundException,
    ValidationException,
)
from miloco.miot import filter as miot_filter
from miloco.miot.service import MiotService


class _FakeKV:
    """Minimal KVRepo replacement backed by an in-memory dict."""

    def __init__(self, initial: dict[str, str] | None = None):
        self._store: dict[str, str] = dict(initial or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._store.get(key, default)

    def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True

    def delete(self, key: str) -> bool:
        return self._store.pop(key, None) is not None


def _home(home_id: str, name: str = "Home"):
    return SimpleNamespace(home_id=home_id, home_name=name)


def _camera(did: str, home_id: str = "H1", *, online: bool = True, lan_online: bool = True):
    return SimpleNamespace(
        did=did,
        home_id=home_id,
        name=f"cam-{did}",
        online=online,
        lan_online=lan_online,
    )


# ─── filter.py: load/save round trips ────────────────────────────────────────


def test_allowed_home_ids_empty_returns_empty_set():
    kv = _FakeKV()
    assert miot_filter.allowed_home_ids(kv) == set()


def test_allowed_home_ids_with_values():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1", "H2"])})
    assert miot_filter.allowed_home_ids(kv) == {"H1", "H2"}


def test_allowed_home_ids_invalid_json_treated_as_empty(caplog):
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: "{not json"})
    with caplog.at_level("WARNING"):
        assert miot_filter.allowed_home_ids(kv) == set()
    assert "non-list-JSON" in caplog.text


def test_denied_camera_dids_empty():
    kv = _FakeKV()
    assert miot_filter.denied_camera_dids(kv) == set()


def test_denied_camera_dids_with_values():
    kv = _FakeKV({ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1", "c2"])})
    assert miot_filter.denied_camera_dids(kv) == {"c1", "c2"}


def test_voice_allowed_camera_dids_empty():
    kv = _FakeKV()
    assert miot_filter.voice_allowed_camera_dids(kv) == set()


def test_voice_allowed_camera_dids_with_values():
    kv = _FakeKV(
        {ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY: json.dumps(["c1", "c2"])}
    )
    assert miot_filter.voice_allowed_camera_dids(kv) == {"c1", "c2"}


def test_voice_allow_is_orthogonal_to_feed_deny():
    """语音白名单与感知黑名单互不影响：改一个不动另一个。"""
    kv = _FakeKV({ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"])})
    miot_filter.set_camera_voice_in_use(kv, "c2", True)
    assert miot_filter.denied_camera_dids(kv) == {"c1"}
    assert miot_filter.voice_allowed_camera_dids(kv) == {"c2"}


def test_is_home_allowed_no_filter():
    kv = _FakeKV()
    # 空启用集 → 什么都不允许
    assert miot_filter.is_home_allowed(kv, "H1") is False
    assert miot_filter.is_home_allowed(kv, None) is False


def test_is_home_allowed_with_filter():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    assert miot_filter.is_home_allowed(kv, "H1") is True
    assert miot_filter.is_home_allowed(kv, "H2") is False
    assert miot_filter.is_home_allowed(kv, None) is False


def test_filter_by_home_blocks_when_empty():
    kv = _FakeKV()
    items = {"a": _home("H1"), "b": _home("H2")}
    # 空启用集 → 过滤掉所有
    assert miot_filter.filter_by_home(kv, items) == {}


def test_filter_by_home_drops_disallowed():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    items = {"a": _home("H1"), "b": _home("H2")}
    assert set(miot_filter.filter_by_home(kv, items).keys()) == {"a"}


# ─── filter.py: select_active_camera_dids（投喂/拉流共用口径）───────────────────


def test_select_active_filters_home_denied_offline():
    kv = _FakeKV(
        {
            ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
            ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c2"]),
        }
    )
    cameras = {
        "c1": _camera("c1", home_id="H1"),  # 通过
        "c2": _camera("c2", home_id="H1"),  # 被拉黑 → 排除
        "c3": _camera("c3", home_id="H2"),  # 家庭未启用 → 排除
        "c4": _camera("c4", home_id="H1", online=False, lan_online=False),  # 离线 → 排除
    }
    assert miot_filter.select_active_camera_dids(kv, cameras) == ["c1"]


def test_select_active_require_lan_false_keeps_lan_stale():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    # 云端 online=True 但 lan_online=False（卡死态）
    cameras = {"c1": _camera("c1", home_id="H1", online=True, lan_online=False)}
    # require_lan=True（默认连接口径）→ 排除
    assert miot_filter.select_active_camera_dids(kv, cameras) == []
    # require_lan=False（应连数口径）→ 放过
    assert miot_filter.select_active_camera_dids(
        kv, cameras, require_lan=False
    ) == ["c1"]


def test_select_active_caps_by_did(monkeypatch):
    monkeypatch.setattr("miloco.miot.filter.MAX_ENABLED_CAMERAS", 2)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {
        "c3": _camera("c3", home_id="H1"),
        "c1": _camera("c1", home_id="H1"),
        "c2": _camera("c2", home_id="H1"),
    }
    # 超额 → 按 did 升序保留前 2
    assert miot_filter.select_active_camera_dids(kv, cameras) == ["c1", "c2"]
    # cap=False → 全集（输入顺序，列全集语义）
    assert set(miot_filter.select_active_camera_dids(kv, cameras, cap=False)) == {
        "c1",
        "c2",
        "c3",
    }


# ─── filter.py: write helpers ────────────────────────────────────────────────


def test_set_home_in_use_adds_and_removes():
    kv = _FakeKV()
    assert miot_filter.set_home_in_use(kv, "H1", True) == (["H1"], True)
    assert miot_filter.set_home_in_use(kv, "H2", True) == (["H1", "H2"], True)
    # adding a duplicate is a no-op (changed=False)
    assert miot_filter.set_home_in_use(kv, "H1", True) == (["H1", "H2"], False)
    assert miot_filter.set_home_in_use(kv, "H1", False) == (["H2"], True)
    # removing a non-existent id is a no-op
    assert miot_filter.set_home_in_use(kv, "ghost", False) == (["H2"], False)


def test_set_camera_in_use_inverts_disabled():
    kv = _FakeKV()
    # in_use=False adds to deny list
    assert miot_filter.set_camera_in_use(kv, "c1", False) == (["c1"], True)
    assert miot_filter.set_camera_in_use(kv, "c2", False) == (["c1", "c2"], True)
    # in_use=True removes from deny list
    assert miot_filter.set_camera_in_use(kv, "c1", True) == (["c2"], True)
    # idempotent on re-toggling true for missing did → no change
    assert miot_filter.set_camera_in_use(kv, "ghost", True) == (["c2"], False)


def test_set_camera_voice_in_use_writes_allowlist():
    kv = _FakeKV()
    # voice_in_use=True adds to voice allow list
    assert miot_filter.set_camera_voice_in_use(kv, "c1", True) == (["c1"], True)
    assert miot_filter.set_cameras_voice_in_use(kv, ["c2", "c3"], True) == (
        ["c1", "c2", "c3"],
        True,
    )
    # voice_in_use=False removes from voice allow list
    assert miot_filter.set_camera_voice_in_use(kv, "c1", False) == (["c2", "c3"], True)
    # no-op re-toggle → changed=False
    assert miot_filter.set_camera_voice_in_use(kv, "ghost", False) == (
        ["c2", "c3"],
        False,
    )


def test_set_in_use_no_op_skips_kv_write():
    """No-op toggles 不应该再写 kv。"""
    kv = _FakeKV()
    miot_filter.set_home_in_use(kv, "H1", True)
    before = kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY)
    # Re-add the same id — should not rewrite
    original_set = kv.set
    calls = {"n": 0}
    def counting_set(key, value):
        calls["n"] += 1
        return original_set(key, value)
    kv.set = counting_set  # type: ignore[assignment]
    miot_filter.set_home_in_use(kv, "H1", True)
    assert calls["n"] == 0
    assert kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY) == before


# ─── MiotService.list_homes / switch_home ────────────────────────────────────


def _make_service(devices: dict | None = None, cameras: dict | None = None, kv: _FakeKV | None = None) -> MiotService:
    kv = kv or _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=SimpleNamespace(
                execute_update=lambda *a, **kw: 0,
                execute_query=lambda *a, **kw: [],
            ),
            get=kv.get,
            set=kv.set,
        ),
        get_devices=AsyncMock(return_value=devices or {}),
        get_cameras=AsyncMock(return_value=cameras or {}),
        refresh_devices=AsyncMock(return_value=None),
        refresh_cameras=AsyncMock(return_value=None),
        refresh_scenes=AsyncMock(return_value=None),
        # 默认：awake 缓存空（全部相机镜头态未知→None，不 gate）。需要构造镜头关闭的
        # 测试自行覆盖该 mock 返回 {did: False}。
        read_cameras_awake=AsyncMock(side_effect=lambda dids, **kw: {}),
    )
    svc = MiotService(miot_proxy=proxy)

    async def _noop():
        return None

    svc._sync_camera_adapter = _noop  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]
    return svc


@pytest.mark.asyncio
async def test_list_homes_marks_in_use():
    devices = {
        "d1": _home("H1", "Family A"),
        "d2": _home("H2", "Family B"),
        "d3": _home("H1", "Family A"),  # duplicate home_id, dedupe
    }
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(devices=devices, kv=kv)
    homes = await svc.list_homes()
    by_id = {h["home_id"]: h for h in homes}
    assert by_id["H1"]["in_use"] is True
    assert by_id["H2"]["in_use"] is False
    assert len(homes) == 2  # H1 dedupe'd


@pytest.mark.asyncio
async def test_list_homes_auto_selects_first_when_empty():
    devices = {"d1": _home("H1"), "d2": _home("H2")}
    kv = _FakeKV()
    svc = _make_service(devices=devices, kv=kv)
    homes = await svc.list_homes()
    # 空启用集 → 自动选第一个家庭
    by_id = {h["home_id"]: h for h in homes}
    assert by_id["H1"]["in_use"] is True
    assert by_id["H2"]["in_use"] is False


@pytest.mark.asyncio
async def test_switch_home_persists_through_kv():
    """切换家庭：switch 后只有目标家庭 in_use=True，其余 False。"""
    kv = _FakeKV()
    svc = _make_service(devices={"d1": _home("H1"), "d2": _home("H2")}, kv=kv)
    res = await svc.switch_home("H1")
    assert isinstance(res, list)
    by_id = {h["home_id"]: h for h in res}
    assert by_id["H1"]["in_use"] is True
    assert by_id["H2"]["in_use"] is False
    assert json.loads(kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY)) == ["H1"]

    # 切换到 H2 → H1 自动停用
    res = await svc.switch_home("H2")
    by_id = {h["home_id"]: h for h in res}
    assert by_id["H2"]["in_use"] is True
    assert by_id["H1"]["in_use"] is False
    assert json.loads(kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY)) == ["H2"]


async def _drain_reset(mock, timeout: float = 1.0) -> None:
    """switch_home 的 reset 走 fire-and-forget 后台任务，轮询等它跑完（或超时）。"""
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if mock.await_count:
            return
        await asyncio.sleep(0.01)


def _kv_with_home(home_id: str) -> "_FakeKV":
    """预置某家庭为启用，避免 list_homes 兜底自动选家干扰 reset 计数。"""
    return _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps([home_id])})


@pytest.mark.asyncio
async def test_switch_home_resets_agent_sessions():
    """启用家庭真的变化时，后台批量 reset miloco session，传入 MILOCO_SESSION_KEYS 全集。"""
    from unittest.mock import patch

    from miloco.dispatch import MILOCO_SESSION_KEYS

    # 预置 H1 启用 → list_homes 不会兜底自动切；switch 到 H2 才是唯一一次启用集变化。
    svc = _make_service(
        devices={"d1": _home("H1"), "d2": _home("H2")}, kv=_kv_with_home("H1")
    )
    reset = AsyncMock(return_value={"reset": MILOCO_SESSION_KEYS, "failed": []})
    with patch("miloco.utils.agent_client.reset_agent_sessions", new=reset):
        res = await svc.switch_home("H2")
        await _drain_reset(reset)

    assert {h["home_id"]: h["in_use"] for h in res}["H2"] is True
    reset.assert_awaited_once_with(MILOCO_SESSION_KEYS)


@pytest.mark.asyncio
async def test_switch_home_noop_when_already_active_skips_reset():
    """切到"已是当前唯一启用"的家庭 → 启用集没变 → 不 reset，保住热上下文。"""
    from unittest.mock import patch

    svc = _make_service(
        devices={"d1": _home("H1"), "d2": _home("H2")}, kv=_kv_with_home("H2")
    )
    reset = AsyncMock()
    with patch("miloco.utils.agent_client.reset_agent_sessions", new=reset):
        res = await svc.switch_home("H2")  # 目标已启用
        await _drain_reset(reset, timeout=0.2)

    assert {h["home_id"]: h["in_use"] for h in res}["H2"] is True
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_home_reset_failure_is_swallowed():
    """reset 失败（openclaw 不可达）只 WARN，绝不打断切换本身。"""
    from unittest.mock import patch

    svc = _make_service(
        devices={"d1": _home("H1"), "d2": _home("H2")}, kv=_kv_with_home("H1")
    )
    reset = AsyncMock(side_effect=RuntimeError("openclaw down"))
    with patch("miloco.utils.agent_client.reset_agent_sessions", new=reset):
        res = await svc.switch_home("H2")  # 不抛异常
        await _drain_reset(reset)

    assert {h["home_id"]: h["in_use"] for h in res}["H2"] is True
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_homes_fallback_auto_switch_resets_sessions():
    """list_homes 兜底自动切换（选中家失效）也触发 reset——与显式切换同一 bug class。"""
    from unittest.mock import patch

    # 启用集 = {H_gone}（已从账号消失），可见家庭只有 H1/H2 → 无交集 → 兜底选 H1。
    svc = _make_service(
        devices={"d1": _home("H1"), "d2": _home("H2")}, kv=_kv_with_home("H_gone")
    )
    reset = AsyncMock()
    with patch("miloco.utils.agent_client.reset_agent_sessions", new=reset):
        homes = await svc.list_homes()
        await _drain_reset(reset)

    assert {h["home_id"]: h["in_use"] for h in homes}["H1"] is True
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_home_rejects_unknown():
    svc = _make_service(devices={"d1": _home("H1")})
    with pytest.raises(ValidationException):
        await svc.switch_home("xiaomi")  # typo / name 误传 id


@pytest.mark.asyncio
async def test_switch_home_returns_all_homes():
    """switch 返回全量家庭列表（不只是受影响的）。"""
    kv = _FakeKV()
    svc = _make_service(
        devices={"d1": _home("H1"), "d2": _home("H2"), "d3": _home("H3")}, kv=kv
    )
    res = await svc.switch_home("H2")
    assert len(res) == 3
    by_id = {h["home_id"]: h for h in res}
    assert by_id["H2"]["in_use"] is True
    assert by_id["H1"]["in_use"] is False
    assert by_id["H3"]["in_use"] is False


# ─── MiotService.list_cameras_with_state / toggle_camera ─────────────────────


@pytest.mark.asyncio
async def test_list_cameras_with_state_flags():
    """in_use = 活跃集（未停用 + home + 三态 + 上限）；三态并列指标 + is_online 兼容。"""
    cameras = {
        "c1": _camera("c1", home_id="H1"),  # 三态好但在停用集 → 不活跃
        "c2": _camera("c2", home_id="H1", lan_online=False),  # 局域网不可达 → 不活跃
        "c4": _camera("c4", home_id="H1"),  # 三态好 + 未停用 → 活跃
        "c3": _camera("c3", home_id="H2"),  # 别的家庭 → 过滤掉
    }
    devices = {d: v for d, v in cameras.items()}
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"]),
    })
    svc = _make_service(devices=devices, cameras=cameras, kv=kv)
    svc._connected_camera_dids = lambda: {"c4"}  # type: ignore[assignment]

    out = await svc.list_cameras_with_state()
    by_did = {c["did"]: c for c in out}

    # 按家庭过滤：只返回 H1 的相机（c3 属于 H2）
    assert set(by_did.keys()) == {"c1", "c2", "c4"}

    # c1：三态好但被停用 → 不在活跃集 → in_use=false
    assert by_did["c1"]["cloud_online"] is True
    assert by_did["c1"]["lan_reachable"] is True
    assert by_did["c1"]["in_use"] is False
    assert by_did["c1"]["is_online"] is True  # 兼容字段 = cloud && lan
    assert by_did["c1"]["connected"] is False

    # c2：未停用但局域网不可达 → 被可用性 gate 掉 → in_use=false
    assert by_did["c2"]["cloud_online"] is True
    assert by_did["c2"]["lan_reachable"] is False
    assert by_did["c2"]["in_use"] is False
    assert by_did["c2"]["is_online"] is False

    # c4：三态好 + 未停用 → 活跃 → in_use=true；awake 缓存空=未知(None)；connected 正交
    assert by_did["c4"]["in_use"] is True
    assert by_did["c4"]["awake"] is None
    assert by_did["c4"]["connected"] is True


@pytest.mark.asyncio
async def test_toggle_camera_writes_disabled():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(
        devices={"c1": _camera("c1")}, cameras={"c1": _camera("c1")}, kv=kv
    )

    res = await svc.toggle_camera([{"did": "c1", "in_use": False}])
    assert isinstance(res, list)
    assert any(c["did"] == "c1" and c["in_use"] is False for c in res)
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY)) == ["c1"]

    res = await svc.toggle_camera([{"did": "c1", "in_use": True}])
    assert isinstance(res, list)
    assert any(c["did"] == "c1" and c["in_use"] is True for c in res)


@pytest.mark.asyncio
async def test_toggle_camera_batch_atomic():
    """全部 did 校验通过后才一起写入；任一未知则整批拒绝。"""
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(
        devices={"c1": _camera("c1"), "c2": _camera("c2")},
        cameras={"c1": _camera("c1"), "c2": _camera("c2")},
        kv=kv
    )
    # 两个都合法 → 都写入停用集
    res = await svc.toggle_camera([{"did": "c1", "in_use": False}, {"did": "c2", "in_use": False}])
    assert isinstance(res, list)
    dids = {c["did"] for c in res}
    assert dids == {"c1", "c2"}
    assert all(c["in_use"] is False for c in res)

    # c1 合法 + ghost 未知 → 整批拒绝，c1 不写入
    with pytest.raises(ValidationException):
        await svc.toggle_camera([{"did": "c1", "in_use": False}, {"did": "ghost", "in_use": False}])
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY)) == [
        "c1", "c2"
    ]  # 不变


@pytest.mark.asyncio
async def test_toggle_camera_rejects_unknown():
    svc = _make_service(cameras={"c1": _camera("c1")})
    with pytest.raises(ValidationException):
        await svc.toggle_camera([{"did": "ghost", "in_use": False}])


# ─── MiotService: 拾音开关（voice_in_use，mic-off 语义）───────────────────────


@pytest.mark.asyncio
async def test_list_cameras_with_state_voice_flags():
    """voice_in_use 是存储偏好：在语音白名单即 True（**默认 False**），与 in_use 正交。"""
    cameras = {"c1": _camera("c1", home_id="H1"), "c2": _camera("c2", home_id="H1")}
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY: json.dumps(["c1"]),
    })
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)
    out = await svc.list_cameras_with_state()
    by_did = {c["did"]: c for c in out}
    assert by_did["c1"]["voice_in_use"] is True   # 在语音白名单
    assert by_did["c1"]["in_use"] is True          # 感知仍启用（正交）
    assert by_did["c2"]["voice_in_use"] is False   # 默认关闭


@pytest.mark.asyncio
async def test_toggle_camera_voice_writes_allowlist():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(
        devices={"c1": _camera("c1")}, cameras={"c1": _camera("c1")}, kv=kv
    )
    res = await svc.toggle_camera_voice([{"did": "c1", "voice_in_use": True}])
    assert any(c["did"] == "c1" and c["voice_in_use"] is True for c in res)
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)) == ["c1"]

    res = await svc.toggle_camera_voice([{"did": "c1", "voice_in_use": False}])
    assert any(c["did"] == "c1" and c["voice_in_use"] is False for c in res)
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)) == []


@pytest.mark.asyncio
async def test_toggle_camera_voice_rejects_unknown():
    svc = _make_service(cameras={"c1": _camera("c1")})
    with pytest.raises(ValidationException):
        await svc.toggle_camera_voice([{"did": "ghost", "voice_in_use": False}])


@pytest.mark.asyncio
async def test_toggle_camera_voice_rejected_when_camera_disabled():
    """拾音从属于感知：感知已关闭(在黑名单)的相机不允许设置拾音。"""
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"]),  # c1 感知已关闭
    })
    svc = _make_service(
        devices={"c1": _camera("c1")}, cameras={"c1": _camera("c1")}, kv=kv
    )
    with pytest.raises(ValidationException, match="声音"):
        await svc.toggle_camera_voice([{"did": "c1", "voice_in_use": True}])
    # 拒绝后不落库
    assert kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY) is None


@pytest.mark.asyncio
async def test_toggle_camera_off_on_preserves_voice_preference():
    """关相机不改写语音白名单：off→on 循环后语音偏好原样保留（存储偏好不落库为「自动关」）。"""
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(
        devices={"c1": _camera("c1")}, cameras={"c1": _camera("c1")}, kv=kv
    )
    # 先把 c1 语音打开（此时感知仍开）
    await svc.toggle_camera_voice([{"did": "c1", "voice_in_use": True}])
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)) == ["c1"]
    # 关相机感知 → 语音白名单不应被改写
    await svc.toggle_camera([{"did": "c1", "in_use": False}])
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)) == ["c1"]
    # 重新开相机感知 → 旧语音偏好仍在（voice_in_use=True）
    await svc.toggle_camera([{"did": "c1", "in_use": True}])
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY)) == ["c1"]
    out = await svc.list_cameras_with_state()
    by_did = {c["did"]: c for c in out}
    assert by_did["c1"]["in_use"] is True
    assert by_did["c1"]["voice_in_use"] is True  # 偏好保留


@pytest.mark.asyncio
async def test_toggle_camera_voice_does_not_restart_or_refresh():
    """语音开关不 refresh_cameras / _sync_camera_adapter / _restart_perception_engine
    （dispatch 阶段实时读 KV，改开关即时生效）。"""
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(
        devices={"c1": _camera("c1")}, cameras={"c1": _camera("c1")}, kv=kv
    )
    svc._miot_proxy.refresh_cameras = AsyncMock()
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._restart_perception_engine = AsyncMock()  # type: ignore[assignment]
    await svc.toggle_camera_voice([{"did": "c1", "voice_in_use": False}])
    svc._miot_proxy.refresh_cameras.assert_not_awaited()
    svc._sync_camera_adapter.assert_not_awaited()
    svc._restart_perception_engine.assert_not_awaited()


# ─── _assert_did_in_allowed_home ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assert_home_allowed_finds_camera_dict():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cam = _camera("cam1", home_id="H1")
    svc = _make_service(cameras={"cam1": cam}, kv=kv)
    # 不抛 = 通过相机字典分支
    await svc._assert_did_in_allowed_home("cam1")


@pytest.mark.asyncio
async def test_assert_home_allowed_rejects_disallowed_camera():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cam = _camera("cam1", home_id="H2")
    svc = _make_service(cameras={"cam1": cam}, kv=kv)
    with pytest.raises(ValidationException):
        await svc._assert_did_in_allowed_home("cam1")


@pytest.mark.asyncio
async def test_assert_home_allowed_unknown_did_404():
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(kv=kv)
    with pytest.raises(ResourceNotFoundException):
        await svc._assert_did_in_allowed_home("ghost")


@pytest.mark.asyncio
async def test_assert_did_auto_selects_first_home():
    """无启用家庭时自动选第一个（兜底），设备控制不被阻断。"""
    kv = _FakeKV()
    svc = _make_service(devices={"d1": _home("H1")}, kv=kv)
    # 初始无启用家庭
    assert miot_filter.allowed_home_ids(kv) == set()
    # 调用后自动启用 H1
    await svc._assert_did_in_allowed_home("d1")
    assert miot_filter.allowed_home_ids(kv) == {"H1"}


# ─── unbind_miot: scope config 清理 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_unbind_miot_clears_scope_config():
    """unbind 后 HOME_WHITE_LIST_KEY / CAMERA_BLACK_LIST_KEY 应从 KV 中删除，
    同时 LRU 全量清空（换账号后旧 did 全失效）。"""
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"]),
        ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY: json.dumps(["c1"]),
    })
    db_connector = MagicMock()
    db_connector.execute_update = MagicMock(return_value=0)
    db_connector.execute_query = MagicMock(return_value=[])
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db_connector,
            get=kv.get,
            set=kv.set,
            delete=kv.delete,
        ),
        deinit=AsyncMock(),
        init=AsyncMock(),
        refresh_cameras=AsyncMock(),
        get_devices=AsyncMock(return_value={}),
        get_cameras=AsyncMock(return_value={}),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]

    await svc.unbind_miot()

    assert kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY) is None
    assert kv.get(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY) is None
    assert kv.get(ScopeConfigKeys.CAMERA_VOICE_ALLOW_LIST_KEY) is None
    # LRU: 必须有一次 DELETE FROM device_lru
    lru_calls = [
        c for c in db_connector.execute_update.call_args_list
        if "device_lru" in str(c)
    ]
    assert any("DELETE" in str(c).upper() for c in lru_calls), (
        f"unbind_miot must DELETE FROM device_lru, got: {lru_calls}"
    )
    proxy.deinit.assert_awaited_once()
    proxy.init.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbind_miot_clears_scope_config_when_keys_absent():
    """unbind 在 scope key 不存在时也不应抛异常。"""
    kv = _FakeKV()
    db_connector = MagicMock()
    db_connector.execute_update = MagicMock(return_value=0)
    db_connector.execute_query = MagicMock(return_value=[])
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db_connector,
            get=kv.get,
            set=kv.set,
            delete=kv.delete,
        ),
        deinit=AsyncMock(),
        init=AsyncMock(),
        refresh_cameras=AsyncMock(),
        get_devices=AsyncMock(return_value={}),
        get_cameras=AsyncMock(return_value={}),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]

    await svc.unbind_miot()  # 不抛即通过


@pytest.mark.asyncio
async def test_unbind_miot_scope_cleared_even_if_deinit_fails():
    """scope 清理必须在 deinit() 之前完成——即使 deinit 抛异常，KV key 已落盘删除。

    不变量：unbind_miot() 先删 scope keys / LRU，再调 deinit()。
    若未来有人把清理挪到 deinit() 后面，此测试会 catch 到。
    """
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"]),
    })
    db_connector = MagicMock()
    db_connector.execute_update = MagicMock(return_value=0)
    db_connector.execute_query = MagicMock(return_value=[])
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db_connector,
            get=kv.get,
            set=kv.set,
            delete=kv.delete,
        ),
        deinit=AsyncMock(side_effect=RuntimeError("deinit boom")),
        init=AsyncMock(),
        get_devices=AsyncMock(return_value={}),
        get_cameras=AsyncMock(return_value={}),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]

    with pytest.raises(MiotServiceException):
        await svc.unbind_miot()

    assert kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY) is None, (
        "HOME_WHITE_LIST_KEY 应在 deinit() 之前删除"
    )
    assert kv.get(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY) is None, (
        "CAMERA_BLACK_LIST_KEY 应在 deinit() 之前删除"
    )


# ─── authorize_with_code: 换账号时 scope 清理 ────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_with_code_clears_scope_before_token_exchange():
    """直接绑新账号（不经 unbind）时也必须清理旧 scope 和 LRU，
    否则新账号设备会被旧启用集过滤为空。"""
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(["c1"]),
    })
    db_connector = MagicMock()
    db_connector.execute_update = MagicMock(return_value=0)
    db_connector.execute_query = MagicMock(return_value=[])
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db_connector,
            get=kv.get,
            set=kv.set,
            delete=kv.delete,
        ),
        get_miot_auth_info=AsyncMock(),
        deinit=AsyncMock(),
        init=AsyncMock(),
        refresh_cameras=AsyncMock(),
        get_devices=AsyncMock(return_value={}),
        get_cameras=AsyncMock(return_value={}),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]
    svc._restart_perception_engine = AsyncMock()  # type: ignore[assignment]

    await svc.authorize_with_code(code="test_code", state="test_state")

    assert kv.get(ScopeConfigKeys.HOME_WHITE_LIST_KEY) is None, (
        "authorize_with_code 应清除旧 HOME_WHITE_LIST_KEY"
    )
    assert kv.get(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY) is None, (
        "authorize_with_code 应清除旧 CAMERA_BLACK_LIST_KEY"
    )
    # LRU 必须清空
    lru_calls = [
        c for c in db_connector.execute_update.call_args_list
        if "device_lru" in str(c)
    ]
    assert any("DELETE" in str(c).upper() for c in lru_calls), (
        f"authorize_with_code must DELETE FROM device_lru, got: {lru_calls}"
    )
    proxy.get_miot_auth_info.assert_awaited_once()
    # 无可用家庭（devices/cameras 为空）→ 兜底逻辑无目标，启用集仍为空
    assert miot_filter.allowed_home_ids(kv) == set()


# ─── MiotProxy: scope entry-filter (build gate + prune branch) ───────────────
#
# These tests cover the入口过滤 framing: `_create_camera_img_manager` is the
# single write point into `_camera_img_managers`, so a scope check there means
# scope-denied dids never start pulling. `refresh_cameras`'s existing destroy
# loop is extended to also fire on scope-deny, which tears down历史 managers
# carried over from the pre-scope era. Pair `destroy()` and `unregister_lan`
# must stay coupled to keep LAN callback registrations consistent.

from miloco.config import (  # noqa: E402  (kept near MiotProxy tests for locality)
    reset_settings,
)
from miloco.miot import mips_listeners as bl_module  # noqa: E402
from miloco.miot import welcome_service as ws_module  # noqa: E402
from miloco.miot.client import MiotProxy  # noqa: E402


@pytest.fixture
def _scope_proxy_env(tmp_path, monkeypatch):
    """A MiotProxy whose collaborators are stubbed enough to exercise
    `_create_camera_img_manager` / `refresh_cameras` against an in-memory KV.

    Mirrors test_miot_proxy_lifecycle.py's pattern so we don't drift from
    the existing convention.
    """
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    reset_settings()
    monkeypatch.setattr(bl_module, "BIND_DEBOUNCE_SEC", 0.05)
    monkeypatch.setattr(
        ws_module, "dispatch_event",
        AsyncMock(return_value=True),
    )
    # refresh_cameras 末尾会把 cameras dict 喂给 to_jsonable_python 落 KV；
    # SimpleNamespace stub 不支持 pydantic 序列化，替换成 no-op 让测试聚焦
    # 在销毁循环和 manager 状态本身。
    monkeypatch.setattr(
        "miloco.miot.client.to_jsonable_python", lambda _cameras: {}
    )

    kv = _FakeKV()
    kv_repo = SimpleNamespace(
        get=kv.get,
        set=kv.set,
        delete=kv.delete,
    )
    proxy = MiotProxy(uuid="u", redirect_uri="http://x", kv_repo=kv_repo)

    miot_client = MagicMock()
    miot_client.register_lan_device_changed_async = AsyncMock()
    miot_client.unregister_lan_device_changed_async = AsyncMock()
    miot_client.create_camera_instance_async = AsyncMock()
    miot_client.get_cameras_async = AsyncMock(return_value={})
    proxy._miot_client = miot_client  # type: ignore[assignment]

    yield proxy, kv, miot_client

    reset_settings()


@pytest.mark.asyncio
async def test_create_camera_img_manager_denied_by_disabled(_scope_proxy_env):
    """`_create_camera_img_manager` 是纯建原语,本身不含 scope gate。

    scope gate(黑名单 / home 白名单)在调用方 refresh_cameras 层。直调 _create
    时即使相机在停用集也会尝试建(确保 gate 没误下沉到这一层);此处 instance
    返回 None 故 manager 不写入 dict。
    """
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps(["c1"]))

    miot_client.create_camera_instance_async = AsyncMock(return_value=None)
    cam = _camera("c1", home_id="H1")
    result = await proxy._create_camera_img_manager(cam)

    # create_camera_instance_async 仍然被调(不 gate)，但返回 None 时 manager=None
    miot_client.create_camera_instance_async.assert_called_once()
    assert result is None  # instance 为 None 时 handler 不建
    assert "c1" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_create_camera_img_manager_denied_by_home_filter(_scope_proxy_env):
    """home_id 不在启用集 → 同上：_create 不 gate(gate 在 refresh 层)，仍尝试建。"""
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    miot_client.create_camera_instance_async = AsyncMock(return_value=None)
    cam = _camera("c2", home_id="H2")  # H2 不在启用集
    result = await proxy._create_camera_img_manager(cam)

    miot_client.create_camera_instance_async.assert_called_once()
    assert result is None
    assert "c2" not in proxy._camera_img_managers  # instance=None 时不写入 dict


@pytest.mark.asyncio
async def test_create_camera_img_manager_denied_but_valid_instance_builds_manager(_scope_proxy_env):
    """denied + 有效 instance → _create 仍建 manager（_create 是 gate-free 原语）。

    钉住分层契约:scope gate 只在 refresh_cameras,_create_camera_img_manager
    本身不查黑名单/白名单。若有人把 gate 误下沉到 _create,该测试会失败。
    """
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps(["c1"]))

    mock_instance = MagicMock(spec=[
        "start_async", "register_decode_jpg_async", "register_decode_video_frame_async",
    ])
    mock_instance.start_async = AsyncMock()
    mock_instance.register_decode_jpg_async = AsyncMock()
    miot_client.create_camera_instance_async = AsyncMock(return_value=mock_instance)
    miot_client._camera_client = MagicMock()

    cam = _camera("c1", home_id="H1")
    cam.channel_count = 1  # CameraVisionHandler.__init__ 需要该字段
    result = await proxy._create_camera_img_manager(cam)

    # scope_denied 不再 gate:instance 有效 → manager 被建立
    miot_client.create_camera_instance_async.assert_called_once()
    assert result is not None
    assert "c1" in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_create_camera_img_manager_denied_by_home_filter_valid_instance_builds_manager(_scope_proxy_env):
    """home filter 变体：home 不在启用集 + 有效 instance → _create 仍建(gate 在 refresh 层)。"""
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    mock_instance = MagicMock()
    mock_instance.start_async = AsyncMock()
    mock_instance.register_decode_jpg_async = AsyncMock()
    miot_client.create_camera_instance_async = AsyncMock(return_value=mock_instance)
    miot_client._camera_client = MagicMock()

    cam = _camera("c2", home_id="H2")  # H2 不在启用集
    cam.channel_count = 1
    result = await proxy._create_camera_img_manager(cam)

    miot_client.create_camera_instance_async.assert_called_once()
    assert result is not None
    assert "c2" in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_destroys_scope_denied_existing_manager(_scope_proxy_env):
    """先有 manager + 后写停用集 + refresh → manager 被销毁（关就停）。

    设计:相机被关(in_use=false → 进黑名单)时 refresh 销毁其 manager,真正停掉
    native PPCS 会话+解码线程,不再拉流。destroy + unregister + dict 删除三件配对。
    """
    proxy, kv, miot_client = _scope_proxy_env

    cam = _camera("c1", home_id="H1")
    handler = MagicMock()
    handler.destroy = AsyncMock()
    handler.update_camera_info = AsyncMock()
    proxy._camera_img_managers["c1"] = handler
    miot_client.get_cameras_async = AsyncMock(return_value={"c1": cam})

    kv.set(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps(["c1"]))
    await proxy.refresh_cameras()

    handler.destroy.assert_awaited_once()
    miot_client.unregister_lan_device_changed_async.assert_awaited_once_with(did="c1")
    assert "c1" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_destroys_scope_out_of_home_manager(_scope_proxy_env):
    """切换家庭后旧家庭相机移出 scope → manager 被销毁；scope 内的保活。

    模拟 switch_home 切到 H2：H1 的 c1 home 不在白名单 → 销毁；H2 的 c2 → 保活。
    """
    proxy, kv, miot_client = _scope_proxy_env

    h1 = MagicMock()
    h1.destroy = AsyncMock()
    h1.update_camera_info = AsyncMock()
    h2 = MagicMock()
    h2.destroy = AsyncMock()
    h2.update_camera_info = AsyncMock()
    proxy._camera_img_managers["c1"] = h1
    proxy._camera_img_managers["c2"] = h2
    miot_client.get_cameras_async = AsyncMock(
        return_value={"c1": _camera("c1", home_id="H1"), "c2": _camera("c2", home_id="H2")}
    )

    # 已切到 H2：H1 相机移出 scope
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H2"]))
    await proxy.refresh_cameras()

    h1.destroy.assert_awaited_once()
    assert "c1" not in proxy._camera_img_managers
    h2.destroy.assert_not_awaited()
    assert "c2" in proxy._camera_img_managers
    h2.update_camera_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_cameras_skips_manager_for_denied_camera(_scope_proxy_env):
    """refresh_cameras 新建分支：相机在停用集(黑名单)时 continue，不建 manager。

    防回归钉：若有人移除黑名单 gate，create_camera_instance_async 调用次数
    会从 1 变为 2，测试立即失败。
    """
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))
    kv.set(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps(["c2"]))

    miot_client.get_cameras_async = AsyncMock(
        return_value={"c1": _camera("c1", home_id="H1"), "c2": _camera("c2", home_id="H1")}
    )
    miot_client.create_camera_instance_async = AsyncMock(return_value=None)

    await proxy.refresh_cameras()

    # c1 未拉黑 → 尝试建；c2 在黑名单 → continue 跳过
    assert miot_client.create_camera_instance_async.call_count == 1
    assert "c2" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_destroys_when_camera_removed_from_account(_scope_proxy_env):
    """摄像头从账号消失(cam is None) → destroy + unregister + dict 删除三件配对。

    destroy 触发路径之一(另两条:移出当前家庭 / 被关闭,见上面两个测试)。
    """
    proxy, kv, miot_client = _scope_proxy_env

    handler = MagicMock()
    handler.destroy = AsyncMock()
    proxy._camera_img_managers["c_gone"] = handler
    # get_cameras_async 返回空集 → "c_gone" 不在 cameras → cam is None
    miot_client.get_cameras_async = AsyncMock(return_value={})

    await proxy.refresh_cameras()

    handler.destroy.assert_awaited_once()
    miot_client.unregister_lan_device_changed_async.assert_awaited_once_with(did="c_gone")
    assert "c_gone" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_no_destroy_when_scope_allows(_scope_proxy_env):
    """对照组：scope 不拒绝时，已有 manager 不应被销毁（防误删保险栓）。

    refresh_cameras 原本的契约是「云端不存在才销毁」，我们扩展触发条件后
    必须保证「scope 允许 + 云端存在」的常态路径完全无副作用。
    """
    proxy, kv, miot_client = _scope_proxy_env

    cam = _camera("c1", home_id="H1")
    handler = MagicMock()
    handler.destroy = AsyncMock()
    handler.update_camera_info = AsyncMock()
    proxy._camera_img_managers["c1"] = handler
    miot_client.get_cameras_async = AsyncMock(return_value={"c1": cam})

    # 启用 H1 → c1 的 home 在启用集内 → 允许
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))
    await proxy.refresh_cameras()

    handler.destroy.assert_not_awaited()
    miot_client.unregister_lan_device_changed_async.assert_not_awaited()
    assert "c1" in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_skips_manager_for_disallowed_home(_scope_proxy_env):
    """refresh_cameras 新建分支：home_id 不在启用集时 continue，不建 manager。

    防回归钉：若有人移除 is_home_allowed continue 逻辑，create_camera_instance_async
    调用次数会从 1 变为 2，测试立即失败。
    """
    proxy, kv, miot_client = _scope_proxy_env
    # H1 在启用集，H2 不在
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    cam_allowed = _camera("c1", home_id="H1")
    cam_disallowed = _camera("c2", home_id="H2")
    miot_client.get_cameras_async = AsyncMock(
        return_value={"c1": cam_allowed, "c2": cam_disallowed}
    )
    miot_client.create_camera_instance_async = AsyncMock(return_value=None)

    await proxy.refresh_cameras()

    # c1 的 home 在白名单，尝试建 manager；c2 的 home 不在，continue 跳过
    assert miot_client.create_camera_instance_async.call_count == 1
    assert "c2" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_caps_managers_to_max(_scope_proxy_env, monkeypatch):
    """在线相机超过上限 → 只为前 N(按 did)建 manager，超额的不建（拉流=投喂口径）。"""
    monkeypatch.setattr("miloco.miot.filter.MAX_ENABLED_CAMERAS", 2)
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    miot_client.get_cameras_async = AsyncMock(
        return_value={
            "c1": _camera("c1", home_id="H1"),
            "c2": _camera("c2", home_id="H1"),
            "c3": _camera("c3", home_id="H1"),  # 第 3 台在线 → 超额，不建
        }
    )
    miot_client.create_camera_instance_async = AsyncMock(return_value=None)

    await proxy.refresh_cameras()

    # 只为 c1/c2 建，c3 超额跳过
    assert miot_client.create_camera_instance_async.call_count == 2
    assert "c3" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_destroys_overcap_existing_manager(_scope_proxy_env, monkeypatch):
    """已建 >MAX 个 manager（存量超额）→ refresh 收敛到 MAX，多的销毁。"""
    monkeypatch.setattr("miloco.miot.filter.MAX_ENABLED_CAMERAS", 2)
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    handlers = {}
    for did in ("c1", "c2", "c3"):
        h = MagicMock()
        h.destroy = AsyncMock()
        h.update_camera_info = AsyncMock()
        handlers[did] = h
        proxy._camera_img_managers[did] = h
    miot_client.get_cameras_async = AsyncMock(
        return_value={did: _camera(did, home_id="H1") for did in ("c1", "c2", "c3")}
    )

    await proxy.refresh_cameras()

    # 按 did 保留 c1/c2，c3 超额被销
    handlers["c3"].destroy.assert_awaited_once()
    assert "c3" not in proxy._camera_img_managers
    assert "c1" in proxy._camera_img_managers
    assert "c2" in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_destroy_failure_isolated(_scope_proxy_env):
    """批量销时某台 destroy 抛错不拖垮其余：失败的留在 dict 待重试，其余照常销。

    Phase 1 把 destroy 触发条件扩到关/移出家庭/离线/超额 → 切家庭等场景一次销 N 台。
    防回归钉：若移除 per-iteration try/except，c1 抛错会 break 整个循环，c2 不会被销。
    """
    proxy, kv, miot_client = _scope_proxy_env
    # 两台都移出 scope（白名单只含 H2，二者都在 H1）→ 都该销
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H2"]))

    h1 = MagicMock()
    h1.destroy = AsyncMock(side_effect=RuntimeError("destroy boom"))
    h1.update_camera_info = AsyncMock()
    h2 = MagicMock()
    h2.destroy = AsyncMock()
    h2.update_camera_info = AsyncMock()
    proxy._camera_img_managers["c1"] = h1
    proxy._camera_img_managers["c2"] = h2
    miot_client.get_cameras_async = AsyncMock(
        return_value={
            "c1": _camera("c1", home_id="H1"),
            "c2": _camera("c2", home_id="H1"),
        }
    )

    # 不应抛出（refresh 整体仍成功返回）
    result = await proxy.refresh_cameras()
    assert result is not None

    # c1 destroy 抛错 → 留在 dict 待下次 refresh 重试
    h1.destroy.assert_awaited_once()
    assert "c1" in proxy._camera_img_managers
    # c2 不受 c1 失败影响，照常销毁
    h2.destroy.assert_awaited_once()
    assert "c2" not in proxy._camera_img_managers


@pytest.mark.asyncio
async def test_refresh_cameras_offline_not_built(_scope_proxy_env):
    """离线相机不在投喂/拉流集 → 不建 manager（Phase 1：拉流=投喂，离线不建）。"""
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))

    miot_client.get_cameras_async = AsyncMock(
        return_value={
            "c1": _camera("c1", home_id="H1"),  # 在线
            "c2": _camera("c2", home_id="H1", online=False, lan_online=False),  # 离线
        }
    )
    miot_client.create_camera_instance_async = AsyncMock(return_value=None)

    await proxy.refresh_cameras()

    assert miot_client.create_camera_instance_async.call_count == 1  # 只为在线的 c1
    assert "c2" not in proxy._camera_img_managers


# ─── service.toggle_*: 写完 KV 后驱动 MIoT manager 收敛 ──────────────────────


@pytest.mark.asyncio
async def test_toggle_camera_triggers_refresh_then_sync_when_changed():
    """toggle_camera 写完 KV(changed=True) → 先 refresh_cameras 后 _sync_camera_adapter。

    refresh_cameras 按新黑名单建/销 camera manager(关掉的相机停 native 会话+解码),
    _sync_camera_adapter 再让 perception 按新 manager 集连/断。顺序不可换。
    KV 不变(同操作重复)时两者都跳过。
    """
    kv = _FakeKV()
    svc = _make_service(cameras={"c1": _camera("c1")}, kv=kv)

    call_order: list[str] = []
    svc._miot_proxy.refresh_cameras = AsyncMock(
        side_effect=lambda *a, **k: call_order.append("refresh")
    )
    svc._sync_camera_adapter = AsyncMock(
        side_effect=lambda *a, **k: call_order.append("sync")
    )

    await svc.toggle_camera([{"did": "c1", "in_use": False}])
    assert svc._miot_proxy.refresh_cameras.await_count == 1
    assert svc._sync_camera_adapter.await_count == 1
    assert call_order == ["refresh", "sync"]

    # 第二次相同操作 → KV 已含 c1，changed=False → 两者都不再调
    await svc.toggle_camera([{"did": "c1", "in_use": False}])
    assert svc._miot_proxy.refresh_cameras.await_count == 1
    assert svc._sync_camera_adapter.await_count == 1




@pytest.mark.asyncio
async def test_switch_home_triggers_refresh():
    """switch_home 始终 refresh_cameras（无论 KV 是否变化）。"""
    kv = _FakeKV()
    svc = _make_service(devices={"d1": _home("H1")}, kv=kv)
    proxy = svc._miot_proxy

    await svc.switch_home("H1")
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        if proxy.refresh_cameras.await_count >= 1:
            break
        await asyncio.sleep(0.02)
    assert proxy.refresh_cameras.await_count == 1

    # 重复切换 → 仍然 refresh
    await svc.switch_home("H1")
    deadline = asyncio.get_event_loop().time() + 1.0
    while asyncio.get_event_loop().time() < deadline:
        if proxy.refresh_cameras.await_count >= 2:
            break
        await asyncio.sleep(0.02)
    assert proxy.refresh_cameras.await_count == 2


# ─── CameraVisionHandler.destroy 走 manager 入口 (SDK _camera_map evict) ───


@pytest.mark.asyncio
async def test_handler_destroy_routes_through_manager_evict():
    """handler.destroy 调 manager.destroy_camera_async(did) 不直调 instance.destroy_async。

    这是 SDK _camera_map cache evict 的关键保证：
    - manager.destroy_camera_async(did) 内部 pop(did) + instance.destroy_async
    - 直调 instance.destroy_async 不 evict cache → 下次 create_camera_async
      "camera already exists" 短路返回已 free 的 instance → enable 拉不起流。

    不变量：handler.destroy 后只能看见走 manager 入口的 evict。
    """
    from miloco.miot.camera_handler import CameraVisionHandler

    cam_info = SimpleNamespace(
        did="d1",
        name="cam",
        channel_count=1,
        audio_codecs=[],
    )
    instance = MagicMock()
    instance.unregister_decode_jpg_async = AsyncMock()
    instance.unregister_raw_video_async = AsyncMock()
    instance.unregister_raw_audio_async = AsyncMock()
    instance.register_decode_jpg_async = AsyncMock()
    instance.destroy_async = AsyncMock()

    manager = MagicMock()
    manager.destroy_camera_async = AsyncMock()

    handler = CameraVisionHandler(
        cam_info,
        instance,
        manager,
        max_size=10,
        ttl=60,
    )

    await handler.destroy()

    # 走入 manager evict 入口（SDK 会里 pop _camera_map["d1"] 再 destroy_async）
    manager.destroy_camera_async.assert_awaited_once_with(did="d1")
    # 不能直调 instance.destroy_async——那样会跳过 cache evict
    instance.destroy_async.assert_not_awaited()
    # unregister callbacks 仍需调用（在 destroy_camera_async 之前，拆除 callback 引用）
    instance.unregister_decode_jpg_async.assert_awaited_once()
    instance.unregister_raw_video_async.assert_awaited_once()
    instance.unregister_raw_audio_async.assert_awaited_once()


# ─── authorize_with_code: 登录后自动选首个家庭（兜底） ────────────────────────────────


@pytest.mark.asyncio
async def test_authorize_with_code_auto_selects_first_home():
    """登录后 list_homes 兜底自动选第一个家庭。"""
    kv = _FakeKV()
    db_connector = MagicMock()
    db_connector.execute_update = MagicMock(return_value=0)
    db_connector.execute_query = MagicMock(return_value=[])
    proxy = SimpleNamespace(
        _kv_repo=SimpleNamespace(
            db_connector=db_connector, get=kv.get, set=kv.set, delete=kv.delete,
        ),
        get_miot_auth_info=AsyncMock(),
        refresh_cameras=AsyncMock(),
        get_devices=AsyncMock(return_value={"d1": _home("H1"), "d2": _home("H2")}),
        get_cameras=AsyncMock(return_value={}),
    )
    svc = MiotService(miot_proxy=proxy)
    svc._sync_camera_adapter = AsyncMock()  # type: ignore[assignment]
    svc._connected_camera_dids = lambda: set()  # type: ignore[assignment]
    svc._restart_perception_engine = AsyncMock()  # type: ignore[assignment]

    await svc.authorize_with_code(code="c", state="s")

    # 登录后自动选第一个家庭
    assert miot_filter.allowed_home_ids(kv) == {"H1"}


@pytest.mark.asyncio
async def test_list_homes_auto_selects_then_switch():
    """list_homes 自动选第一个家庭，手动 switch 可以切换。"""
    kv = _FakeKV()
    svc = _make_service(devices={"d1": _home("H1"), "d2": _home("H2")}, kv=kv)

    # 首次调用 list_homes → 自动选第一个家庭
    homes = await svc.list_homes()
    by_id = {h["home_id"]: h for h in homes}
    assert by_id["H1"]["in_use"] is True
    assert by_id["H2"]["in_use"] is False
    assert miot_filter.allowed_home_ids(kv) == {"H1"}

    # 手动切换到 H2
    await svc.switch_home("H2")
    assert miot_filter.allowed_home_ids(kv) == {"H2"}
    assert miot_filter.is_home_allowed(kv, "H2") is True
    assert miot_filter.is_home_allowed(kv, "H1") is False


# ─── 摄像头启用数量上限（MAX_ENABLED_CAMERAS）──────────────────────────────
#
# 测试相对 MAX_ENABLED_CAMERAS 构造场景，改上限后自动适配。LIMIT 为当前值，
# OVER = LIMIT + 1（恰好超一台）。

from miloco.miot.filter import MAX_ENABLED_CAMERAS as LIMIT  # noqa: E402


def _cam_dids(n: int) -> list[str]:
    """生成 n 个 did（零填充保证字典序 = 数值序）。"""
    return [f"c{i:03d}" for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_toggle_camera_enable_rejected_at_limit():
    """已满额时 enable 一台黑名单内的相机 → ValidationException。"""
    dids = _cam_dids(LIMIT + 1)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {d: _camera(d, home_id="H1") for d in dids}
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)
    # 前 LIMIT 台启用，最后一台在黑名单 → enable 它就超限
    kv.set(ScopeConfigKeys.CAMERA_BLACK_LIST_KEY, json.dumps([dids[-1]]))

    with pytest.raises(ValidationException, match="最多同时启用"):
        await svc.toggle_camera([{"did": dids[-1], "in_use": True}])


@pytest.mark.asyncio
async def test_toggle_camera_enable_already_enabled_not_counted():
    """满额时 enable 已启用的 camera（no-op）→ 不报错。"""
    dids = _cam_dids(LIMIT)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {d: _camera(d, home_id="H1") for d in dids}
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)
    # dids[0] 已在启用集 — enable 它是 no-op，不触发上限
    await svc.toggle_camera([{"did": dids[0], "in_use": True}])  # 不抛异常


@pytest.mark.asyncio
async def test_toggle_camera_disable_not_limited():
    """disable 不受上限限制。"""
    dids = _cam_dids(LIMIT)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {d: _camera(d, home_id="H1") for d in dids}
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)
    # 满额时 disable 第一台不受上限影响
    await svc.toggle_camera([{"did": dids[0], "in_use": False}])  # 不抛异常


@pytest.mark.asyncio
async def test_toggle_camera_enable_batch_over_limit():
    """批量 enable 把总数推过上限 → 报错。"""
    # LIMIT+2 台相机：LIMIT-1 台已启用，最后 3 台在黑名单；enable 其中 2 台 →
    # (LIMIT-1) + 2 = LIMIT+1 > LIMIT → 报错。要求 LIMIT>=1（恒成立）。
    total = LIMIT + 2
    dids = _cam_dids(total)
    blacklisted = dids[LIMIT - 1:]  # 最后 (total-(LIMIT-1)) = 3 台
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps(blacklisted),
    })
    cameras = {d: _camera(d, home_id="H1") for d in dids}
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)

    with pytest.raises(ValidationException, match="最多同时启用"):
        await svc.toggle_camera([
            {"did": blacklisted[0], "in_use": True},
            {"did": blacklisted[1], "in_use": True},
        ])


@pytest.mark.asyncio
async def test_toggle_camera_enable_count_excludes_other_homes():
    """上限计数只算当前启用家庭内的相机——其他家庭的相机不占额度。"""
    # 启用家庭 H1：满额 LIMIT 台未拉黑；未启用家庭 H2：另有 LIMIT+2 台未拉黑。
    # 若计数按全账号算会误报超限；正确实现只数 H1 的 LIMIT 台。
    h1_dids = [f"h1_{i:03d}" for i in range(LIMIT)]
    h2_dids = [f"h2_{i:03d}" for i in range(LIMIT + 2)]
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {d: _camera(d, home_id="H1") for d in h1_dids}
    cameras.update({d: _camera(d, home_id="H2") for d in h2_dids})
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)

    # H1 已满额，enable 一台 H1 已启用的相机（no-op）→ 不应因 H2 的相机误报超限
    await svc.toggle_camera([{"did": h1_dids[0], "in_use": True}])  # 不抛异常


@pytest.mark.asyncio
async def test_toggle_camera_atomic_swap_at_limit():
    """满额时同批「禁一台 + 启一台」原子换机 → 净额不变，应通过。"""
    # LIMIT 台在用（A 在其中）+ 1 台黑名单 B。换机：禁 A 启 B → 仍 LIMIT 台。
    enabled = _cam_dids(LIMIT)  # c001..cN，满额
    b = "c_new"
    a = enabled[0]
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps([b]),
    })
    cameras = {d: _camera(d, home_id="H1") for d in enabled}
    cameras[b] = _camera(b, home_id="H1")
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)

    # 禁 A 同时启 B：操作后 final_enabled = LIMIT 台，不应误拒
    res = await svc.toggle_camera([
        {"did": a, "in_use": False},
        {"did": b, "in_use": True},
    ])
    assert isinstance(res, list)
    by_did = {c["did"]: c for c in res}
    assert by_did[a]["in_use"] is False
    assert by_did[b]["in_use"] is True


@pytest.mark.asyncio
async def test_toggle_camera_swap_still_rejects_net_over_limit():
    """同批禁 1 启 2、净额超限 → 仍报错（换机放行不等于无上限）。"""
    # LIMIT 台在用 + 2 台黑名单；禁 1 启 2 → 净 LIMIT+1 > LIMIT → 报错。
    enabled = _cam_dids(LIMIT)
    b1, b2 = "c_new1", "c_new2"
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps([b1, b2]),
    })
    cameras = {d: _camera(d, home_id="H1") for d in enabled}
    cameras[b1] = _camera(b1, home_id="H1")
    cameras[b2] = _camera(b2, home_id="H1")
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)

    with pytest.raises(ValidationException, match="最多同时启用"):
        await svc.toggle_camera([
            {"did": enabled[0], "in_use": False},
            {"did": b1, "in_use": True},
            {"did": b2, "in_use": True},
        ])


# ─── awake（镜头开关）门 + 三态 ───────────────────────────────────────────────


def test_select_active_awake_gate():
    """select_active 的 awake_map：awake==False 的相机被排除；None/True/未给出放行。"""
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    cameras = {
        "c1": _camera("c1", home_id="H1"),  # awake True → 保留
        "c2": _camera("c2", home_id="H1"),  # awake False → 排除
        "c3": _camera("c3", home_id="H1"),  # awake None → 保留
    }
    awake = {"c1": True, "c2": False, "c3": None}
    got = set(miot_filter.select_active_camera_dids(kv, cameras, awake_map=awake))
    assert got == {"c1", "c3"}  # c2 镜头关被 gate 掉
    # 不给 awake_map → 全放行（向后兼容）
    assert set(miot_filter.select_active_camera_dids(kv, cameras)) == {
        "c1", "c2", "c3",
    }


def test_resolve_camera_switch_iids():
    """从 spec 定位 camera-control:on；双摄命中多个；indicator-light 排除；无则空。"""
    from miloco.miot.client import _resolve_camera_switch_iids

    single = {
        "prop.2.1": {"service_type_name": "camera-control", "type_name": "on"},
        "prop.4.1": {"service_type_name": "indicator-light", "type_name": "on"},
    }
    assert _resolve_camera_switch_iids(single) == [(2, 1)]

    dual = {
        "prop.2.22": {"service_type_name": "camera-control", "type_name": "on"},
        "prop.24.1": {"service_type_name": "camera-control", "type_name": "on"},
        "prop.25.1": {"service_type_name": "camera-control", "type_name": "on"},
    }
    assert set(_resolve_camera_switch_iids(dual)) == {(2, 22), (24, 1), (25, 1)}

    none = {"prop.3.1": {"service_type_name": "environment", "type_name": "temperature"}}
    assert _resolve_camera_switch_iids(none) == []


@pytest.mark.asyncio
async def test_list_camera_lens_off_not_in_use():
    """镜头关闭(awake=False)的相机：不进活跃集 → in_use=false（不显示为开）。"""
    cameras = {"c1": _camera("c1", home_id="H1")}  # 云端+局域网都好
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)
    # 镜头关闭
    svc._miot_proxy.read_cameras_awake = AsyncMock(  # type: ignore[assignment]
        side_effect=lambda dids, **kw: {"c1": False}
    )

    out = await svc.list_cameras_with_state()
    c1 = {c["did"]: c for c in out}["c1"]
    assert c1["cloud_online"] is True
    assert c1["lan_reachable"] is True
    assert c1["awake"] is False
    assert c1["in_use"] is False  # 镜头关 → 不活跃 → 不显示为开


@pytest.mark.asyncio
async def test_read_cameras_awake_or_combine(_scope_proxy_env):
    """双摄多开关 OR 合并：任一 on→True；全 off→False；无一 on 且有读失败→None。"""
    proxy, kv, miot_client = _scope_proxy_env
    proxy._device_info_dict = {  # type: ignore[attr-defined]
        "c1": SimpleNamespace(urn="urn:dual", model="dual"),
    }
    spec = {
        "prop.2.22": {"service_type_name": "camera-control", "type_name": "on"},
        "prop.24.1": {"service_type_name": "camera-control", "type_name": "on"},
    }
    proxy._fetch_device_spec = AsyncMock(return_value=spec)  # type: ignore[assignment]

    # 任一 on → True
    proxy.get_device_properties = AsyncMock(return_value=[  # type: ignore[assignment]
        {"did": "c1", "siid": 2, "piid": 22, "value": False, "code": 0},
        {"did": "c1", "siid": 24, "piid": 1, "value": True, "code": 0},
    ])
    assert (await proxy.read_cameras_awake(["c1"]))["c1"] is True
    assert proxy._camera_awake_cache["c1"] is True  # 回填缓存

    # 全 off → False
    proxy.get_device_properties = AsyncMock(return_value=[  # type: ignore[assignment]
        {"did": "c1", "siid": 2, "piid": 22, "value": False, "code": 0},
        {"did": "c1", "siid": 24, "piid": 1, "value": False, "code": 0},
    ])
    assert (await proxy.read_cameras_awake(["c1"]))["c1"] is False

    # 无一 on 且有读失败(code!=0) → None（不误判整机关闭）
    proxy.get_device_properties = AsyncMock(return_value=[  # type: ignore[assignment]
        {"did": "c1", "siid": 2, "piid": 22, "value": False, "code": 0},
        {"did": "c1", "siid": 24, "piid": 1, "code": -704},
    ])
    assert (await proxy.read_cameras_awake(["c1"]))["c1"] is None


@pytest.mark.asyncio
async def test_read_cameras_awake_cache_only(_scope_proxy_env):
    """cache_only：只读缓存、零云请求；命中返回、缺失→None。"""
    proxy, kv, miot_client = _scope_proxy_env
    proxy._device_info_dict = {  # type: ignore[attr-defined]
        "c1": SimpleNamespace(urn="urn:c1", model="m1"),
    }
    spec = {"prop.2.1": {"service_type_name": "camera-control", "type_name": "on"}}
    proxy._fetch_device_spec = AsyncMock(return_value=spec)  # type: ignore[assignment]
    proxy.get_device_properties = AsyncMock(  # type: ignore[assignment]
        return_value=[{"did": "c1", "siid": 2, "piid": 1, "value": True, "code": 0}]
    )
    # 先真读一次填缓存
    assert (await proxy.read_cameras_awake(["c1"]))["c1"] is True
    assert proxy.get_device_properties.await_count == 1
    # cache_only：c1 命中、c2 未缓存→None，不再打云
    out = await proxy.read_cameras_awake(["c1", "c2"], cache_only=True)
    assert out == {"c1": True, "c2": None}
    assert proxy.get_device_properties.await_count == 1


# ─── toggle_camera 三态开启门 + 可用集上限 ────────────────────────────────────


@pytest.mark.asyncio
async def test_toggle_enable_rejected_cloud_offline():
    """开启云端离线的相机 → 拒绝，文案含「米家云端离线」。"""
    cam = _camera("c1", home_id="H1", online=False)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(devices={"c1": cam}, cameras={"c1": cam}, kv=kv)
    with pytest.raises(ValidationException, match="米家云端离线"):
        await svc.toggle_camera([{"did": "c1", "in_use": True}])


@pytest.mark.asyncio
async def test_toggle_enable_rejected_lan_unreachable():
    """开启局域网不可达（云端在线）的相机 → 拒绝，文案含「局域网不可达」。"""
    cam = _camera("c1", home_id="H1", online=True, lan_online=False)
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(devices={"c1": cam}, cameras={"c1": cam}, kv=kv)
    with pytest.raises(ValidationException, match="局域网不可达"):
        await svc.toggle_camera([{"did": "c1", "in_use": True}])


@pytest.mark.asyncio
async def test_toggle_enable_rejected_lens_off():
    """开启镜头关闭（awake=False，云端+局域网都好）的相机 → 拒绝，含「镜头已关闭」。
    awake 走新鲜读，CLI/API 直连同样被挡（闸在后端）。"""
    cam = _camera("c1", home_id="H1")  # 云端+局域网都好
    kv = _FakeKV({ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"])})
    svc = _make_service(devices={"c1": cam}, cameras={"c1": cam}, kv=kv)
    svc._miot_proxy.read_cameras_awake = AsyncMock(  # type: ignore[assignment]
        side_effect=lambda dids, **kw: {"c1": False}
    )
    with pytest.raises(ValidationException, match="镜头已关闭"):
        await svc.toggle_camera([{"did": "c1", "in_use": True}])


@pytest.mark.asyncio
async def test_toggle_cap_counts_usable_offline_frees_slot():
    """上限数「可用集」：已启用相机里有一台离线时，它不占名额 → 仍可再开一台在线相机。"""
    dids = _cam_dids(LIMIT)  # c001..cN，全在线、未拉黑
    cameras = {d: _camera(d, home_id="H1") for d in dids}
    cameras[dids[0]] = _camera(dids[0], home_id="H1", online=False)  # 其中一台离线
    new = "c_new"
    cameras[new] = _camera(new, home_id="H1")  # 新的在线相机，初始被拉黑(关)
    kv = _FakeKV({
        ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["H1"]),
        ScopeConfigKeys.CAMERA_BLACK_LIST_KEY: json.dumps([new]),
    })
    svc = _make_service(devices=dict(cameras), cameras=cameras, kv=kv)

    # 未拉黑意图 = LIMIT 台，但其中 1 台离线 → 可用只有 LIMIT-1 → 开 new(在线)
    # 后可用 = LIMIT ≤ 上限 → 放行（离线那台把名额让了出来）。
    res = await svc.toggle_camera([{"did": new, "in_use": True}])
    by_did = {c["did"]: c for c in res}
    assert by_did[new]["in_use"] is True


@pytest.mark.asyncio
async def test_refresh_cameras_gap_fills_awake_for_current_home(_scope_proxy_env):
    """启动/新设备：refresh_cameras 对当前家庭、awake 缓存里还没有的相机补读并回填，
    不依赖 web/推送。已在缓存的不重复读；别的家庭的不补。"""
    proxy, kv, miot_client = _scope_proxy_env
    kv.set(ScopeConfigKeys.HOME_WHITE_LIST_KEY, json.dumps(["H1"]))
    miot_client.get_cameras_async = AsyncMock(return_value={
        "c1": _camera("c1", home_id="H1"),  # 已在缓存 → 不重复
        "c2": _camera("c2", home_id="H1"),  # 缺失 → 补读
        "c3": _camera("c3", home_id="H2"),  # 别的家庭 → 不补
    })
    miot_client.create_camera_instance_async = AsyncMock(return_value=None)
    proxy._device_info_dict = {  # type: ignore[attr-defined]
        "c1": SimpleNamespace(urn="urn:c1", model="m"),
        "c2": SimpleNamespace(urn="urn:c2", model="m"),
        "c3": SimpleNamespace(urn="urn:c3", model="m"),
    }
    spec = {"prop.2.1": {"service_type_name": "camera-control", "type_name": "on"}}
    proxy._fetch_device_spec = AsyncMock(return_value=spec)  # type: ignore[assignment]
    proxy.get_device_properties = AsyncMock(return_value=[  # type: ignore[assignment]
        {"did": "c2", "siid": 2, "piid": 1, "value": False, "code": 0},
    ])
    proxy._camera_awake_cache["c1"] = True  # 预置：已有

    await proxy.refresh_cameras()

    assert proxy._camera_awake_cache.get("c1") is True      # 未被重复读、保持
    assert proxy._camera_awake_cache.get("c2") is False     # 缺失 → 补读到 False
    assert "c3" not in proxy._camera_awake_cache            # 别的家庭 → 不补
