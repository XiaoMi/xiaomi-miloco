"""米家授权健康度：瞬时故障与凭据失效必须分开处理。

钉住的行为（全部是过去出过事的点）：
- 刷新失败**不再清空** ``_oauth_info``——清空会让 ``is_operational`` 转 False，
  连带把感知侧的相机全部断开
- 超时 / 连接失败 / 5xx / 响应体不合法 = 瞬时故障，只累计次数，不进降级态
- 401 与响应体带 ``error`` 字段（如 96009）= 凭据被云端拒绝，立刻进降级态
- 任何一次刷新成功、或用户重新授权成功，都无条件回到 OK
- 同状态内的重复失败日志按间隔限频，状态迁移那条不限频
- 定时检查对瞬时故障会退避重试；对凭据失效立刻停手
- 降级态落 KV，进程重启后仍然可读
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.database.kv_repo import AuthConfigKeys
from miloco.miot.auth_state import (
    FAILURE_LOG_INTERVAL_SECONDS,
    RETRY_BACKOFF_SECONDS,
    MiotAuthHealth,
    MiotAuthState,
    is_permanent_auth_error,
)
from miot.error import MIoTErrorCode, MIoTOAuth2Error


class _FakeKV:
    """内存版 KVRepo。"""

    def __init__(self, initial: dict[str, str] | None = None):
        self._store: dict[str, str] = dict(initial or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._store.get(key, default)

    def set(self, key: str, value: str) -> bool:
        self._store[key] = value
        return True

    def delete(self, key: str) -> bool:
        self._store.pop(key, None)
        return True


def _make_proxy(kv: _FakeKV, *, expires_in: int = 600):
    """造一个只装了本测试关心的那几件东西的 MiotProxy。

    不走 ``__init__``：真实构造要连 SDK、起后台任务，跟本测试无关。
    """
    from miloco.miot.client import MiotProxy
    from miot.types import MIoTOauthInfo

    proxy = MiotProxy.__new__(MiotProxy)
    proxy._kv_repo = kv
    proxy._auth_health = proxy._load_auth_health()
    proxy._oauth_info = MIoTOauthInfo(
        access_token="at",
        refresh_token="rt",
        expires_ts=int(time.time()) + expires_in,
    )
    proxy._miot_client = MagicMock()
    proxy.refresh_miot_info = AsyncMock(return_value={})
    # 刷新串行化用的锁。真实构造在 __init__ 里建，这里绕过了 __init__，补上即可。
    proxy._token_refresh_lock = asyncio.Lock()
    # 与真实 __init__ 同款的启动检查：上一轮刷新若死在「请求已发、结果未知」
    # 那段，这里会把状态判为降级。测的就是这个方法本身。
    proxy._apply_interrupted_refresh_on_start()
    return proxy


# ─────────────── 错误分类 ───────────────


@pytest.mark.parametrize(
    "code,permanent",
    [
        (MIoTErrorCode.CODE_OAUTH_UNAUTHORIZED.value, True),
        (MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value, True),
        (MIoTErrorCode.CODE_TIMEOUT.value, False),
        (MIoTErrorCode.CODE_UNKNOWN.value, False),
        (MIoTErrorCode.CODE_UNAVAILABLE.value, False),
        (None, False),
    ],
)
def test_only_explicit_credential_rejection_is_permanent(code, permanent):
    """只有云端明确拒绝凭据才算永久失败，其余一律可重试。

    方向是刻意 fail-open 的：宁可晚一点告警，也不要因为一次网络故障就告诉
    住户「授权失效了」。
    """
    assert is_permanent_auth_error(code) is permanent


# ─────────────── 状态机 ───────────────


def test_transient_failure_does_not_degrade():
    h = MiotAuthHealth()
    h, _ = h.mark_failure(permanent=False, code=None, message="timeout")
    assert h.state is MiotAuthState.OK
    assert h.consecutive_failures == 1
    assert h.since_ts is None


def test_permanent_failure_degrades_and_records_since():
    h = MiotAuthHealth()
    h, _ = h.mark_failure(permanent=True, code=-10021, message="invalid refresh token")
    assert h.state is MiotAuthState.DEGRADED
    assert h.since_ts is not None
    assert h.error_code == -10021


def test_since_ts_is_kept_across_repeated_failures():
    """已经降级后反复失败，「自 X 时起」不能被刷新成最近一次。"""
    h, _ = MiotAuthHealth().mark_failure(permanent=True, code=-10021, message="x")
    first = h.since_ts
    for _ in range(3):
        h, _ = h.mark_failure(permanent=False, code=None, message="timeout")
    assert h.since_ts == first
    assert h.state is MiotAuthState.DEGRADED, "降级后遇到瞬时故障不应回到 OK"
    assert h.consecutive_failures == 4


def test_success_always_recovers():
    h, _ = MiotAuthHealth().mark_failure(permanent=True, code=-10021, message="x")
    h = h.mark_success()
    assert h.state is MiotAuthState.OK
    assert h.since_ts is None
    assert h.error_code is None
    assert h.consecutive_failures == 0
    assert h.last_success_ts is not None


# ─────────────── 刷新路径 ───────────────


@pytest.mark.asyncio
async def test_refresh_failure_keeps_oauth_info():
    """核心回归：刷新失败不许清空凭据。

    清空会让 ``is_operational`` 转 False，而它是感知侧相机发现的闸门——
    一次瞬时故障会连带把全部摄像头断开。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._miot_client.refresh_access_token_async = AsyncMock(
        side_effect=TimeoutError("read timeout")
    )

    result = await proxy.refresh_xiaomi_home_token_info()

    assert result is None
    assert proxy._oauth_info is not None, "刷新失败不应清空凭据"
    assert proxy.auth_health.state is MiotAuthState.OK, "超时是瞬时故障，不该降级"
    assert proxy.auth_health.consecutive_failures == 1


@pytest.mark.asyncio
async def test_refresh_credential_rejection_degrades():
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._miot_client.refresh_access_token_async = AsyncMock(
        side_effect=MIoTOAuth2Error(
            "oauth/get_token rejected, error=96009",
            MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN,
        )
    )

    result = await proxy.refresh_xiaomi_home_token_info()

    assert result is None
    assert proxy._oauth_info is not None, "即便凭据失效也不清空——感知要继续跑"
    assert proxy.auth_health.state is MiotAuthState.DEGRADED
    assert (
        proxy.auth_health.error_code
        == MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value
    )


@pytest.mark.asyncio
async def test_refresh_success_clears_degraded_state():
    from miot.types import MIoTOauthInfo

    kv = _FakeKV(
        {
            AuthConfigKeys.MIOT_AUTH_STATE_KEY: MiotAuthHealth(
                state=MiotAuthState.DEGRADED, since_ts=1, error_code=-10021
            ).model_dump_json()
        }
    )
    proxy = _make_proxy(kv)
    assert proxy.auth_health.is_degraded, "前置：从 KV 读回降级态"

    proxy._miot_client.refresh_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
    )
    # 不桩 reset_miot_token_info：健康度复位就在它里面，桩掉等于把被测的那一处
    # 挖走，用例会在实现坏掉时照样绿。

    result = await proxy.refresh_xiaomi_home_token_info()

    assert result is not None
    assert proxy.auth_health.state is MiotAuthState.OK


@pytest.mark.asyncio
async def test_successful_refresh_zeroes_transient_failure_count():
    """瞬时故障累计的次数，刷新成功后必须归零。

    复位点若加上「仅在降级时才复位」这类前置判断，这条会红：state 一直是 OK、
    判断不成立，计数却留在原处，退避节奏与排障读到的都是一个虚高的数。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    for _ in range(2):
        health, _ = proxy._auth_health.mark_failure(
            permanent=False, code=None, message="timeout"
        )
        proxy._set_auth_health(health)
    assert proxy.auth_health.state is MiotAuthState.OK, "前置：瞬时故障不该进降级"
    assert proxy.auth_health.consecutive_failures == 2, "前置：计数已累计"

    proxy._miot_client.refresh_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
    )

    assert await proxy.refresh_xiaomi_home_token_info() is not None
    assert proxy.auth_health.consecutive_failures == 0


@pytest.mark.asyncio
async def test_rebind_clears_degraded_state():
    """重新授权成功必须当场解除降级态。

    只在定时刷新成功时复位是不够的：重新绑定拿到的是刚签发的新令牌，下一次刷新
    要等到它临近过期才触发，中间界面会一直挂着「授权已失效」——住户刚做完重新
    绑定却看不到任何变化。
    """
    from miot.types import MIoTOauthInfo

    degraded, _ = MiotAuthHealth().mark_failure(
        permanent=True, code=-10021, message="invalid refresh token"
    )
    kv = _FakeKV({AuthConfigKeys.MIOT_AUTH_STATE_KEY: degraded.model_dump_json()})
    proxy = _make_proxy(kv)
    assert proxy.auth_health.is_degraded, "前置：处于降级态"

    proxy._miot_client.get_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
    )

    await proxy.get_miot_auth_info(code="c", state="s")

    assert proxy.auth_health.state is MiotAuthState.OK


@pytest.mark.asyncio
async def test_rebind_clears_degraded_even_if_later_step_fails():
    """换票成功、新令牌已落库，但 SDK 后续那些网络步骤抛错——健康度也必须已复位。

    换票函数内部在落库之后还要取账号身份、重连长连接，任一步失败都会让它整体
    抛出。复位若排在它返回之后，就会留下「新令牌在库里且完全可用」与「健康度
    仍是降级」并存：定时续期看这枚令牌远未临期便直接早退，要等它临近过期才有下
    一次刷新去纠正，量级是天；期间感知与下发一直停着，重启也照样读回降级态。所以
    复位点必须跟落库绑在一起。
    """
    from miot.error import MIoTHttpError
    from miot.types import MIoTOauthInfo

    degraded, _ = MiotAuthHealth().mark_failure(
        permanent=True, code=-10021, message="invalid refresh token"
    )
    kv = _FakeKV({AuthConfigKeys.MIOT_AUTH_STATE_KEY: degraded.model_dump_json()})
    proxy = _make_proxy(kv)
    assert proxy.auth_health.is_degraded, "前置：处于降级态"

    async def _exchange(code, state, persist=None):
        # 与 SDK 同序：先落库，再做会抛的副作用
        persist(
            MIoTOauthInfo(
                access_token="new", refresh_token="new_rt", expires_ts=9999999999
            )
        )
        raise MIoTHttpError("invalid http response(user)")

    proxy._miot_client.get_access_token_async = _exchange

    with pytest.raises(MIoTHttpError):
        await proxy.get_miot_auth_info(code="c", state="s")

    assert proxy.auth_health.state is MiotAuthState.OK
    assert proxy.is_operational, "令牌可用，感知与下发不该继续停着"
    assert kv.get(AuthConfigKeys.MIOT_AUTH_STATE_KEY), "复位要落库，重启后不能读回降级"
    assert json.loads(kv.get(AuthConfigKeys.MIOT_AUTH_STATE_KEY))["state"] == "ok"


# ─────────────── 重复失败的日志限频 ───────────────


def test_state_transition_always_logs():
    """状态迁移那条每次故障只出现一次，是排障要找的那条，不受限频。"""
    _, should_log = MiotAuthHealth(
        last_failure_log_ts=int(time.time())  # 刚打过，仍然要放行
    ).mark_failure(permanent=True, code=-10021, message="x")
    assert should_log is True


def test_repeated_failure_in_same_state_is_throttled():
    """同状态内的重复失败按间隔限频，否则每天近 300 条同义行。"""
    now = int(time.time())
    degraded = MiotAuthHealth(
        state=MiotAuthState.DEGRADED, since_ts=now - 7200, last_failure_log_ts=now
    )
    _, should_log = degraded.mark_failure(permanent=True, code=-10021, message="x")
    assert should_log is False, "刚打过日志，本次应被限频"


def test_throttle_opens_again_after_interval():
    now = int(time.time())
    degraded = MiotAuthHealth(
        state=MiotAuthState.DEGRADED,
        since_ts=now - 7200,
        last_failure_log_ts=now - FAILURE_LOG_INTERVAL_SECONDS - 1,
    )
    health, should_log = degraded.mark_failure(
        permanent=True, code=-10021, message="x"
    )
    assert should_log is True
    assert health.last_failure_log_ts is not None


def test_throttle_timestamp_not_advanced_when_suppressed():
    """被限频时不能刷新时间戳，否则窗口会被每次失败无限推后、永远不再打日志。"""
    now = int(time.time())
    marked = now - 10
    degraded = MiotAuthHealth(
        state=MiotAuthState.DEGRADED, since_ts=now - 7200, last_failure_log_ts=marked
    )
    health, should_log = degraded.mark_failure(permanent=True, code=-10021, message="x")
    assert should_log is False
    assert health.last_failure_log_ts == marked


# ─────────────── 持久化 ───────────────


def test_degraded_state_survives_restart():
    """重启后立刻可读，否则重启到首次刷新之间界面会误报「一切正常」。"""
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    health, _ = MiotAuthHealth().mark_failure(
        permanent=True, code=-10021, message="x"
    )
    proxy._set_auth_health(health)

    stored = json.loads(kv.get(AuthConfigKeys.MIOT_AUTH_STATE_KEY))
    assert stored["state"] == "degraded"

    reborn = _make_proxy(kv)  # 模拟新进程重新加载
    assert reborn.auth_health.is_degraded


def test_corrupt_stored_state_falls_back_to_ok():
    kv = _FakeKV({AuthConfigKeys.MIOT_AUTH_STATE_KEY: "not-json"})
    proxy = _make_proxy(kv)
    assert proxy.auth_health.state is MiotAuthState.OK


# ─────────────── 定时检查的重试 ───────────────


@pytest.mark.asyncio
async def test_transient_failure_retries_with_backoff(monkeypatch):
    """瞬时故障要在本轮内退避重试——过去这里的实际重试次数是 0。"""
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._oauth_info.expires_ts = 0  # 强制进入刷新分支

    calls = 0

    async def _always_transient():
        nonlocal calls
        calls += 1
        h, _ = proxy.auth_health.mark_failure(
            permanent=False, code=None, message="timeout"
        )
        proxy._set_auth_health(h)
        return None

    proxy.refresh_xiaomi_home_token_info = _always_transient
    slept: list[float] = []

    async def _fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    await proxy._check_and_refresh_token()

    assert calls == len(RETRY_BACKOFF_SECONDS) + 1
    assert slept == list(RETRY_BACKOFF_SECONDS)


@pytest.mark.asyncio
async def test_permanent_failure_stops_retrying(monkeypatch):
    """凭据被拒绝时重试无用，必须立刻停手，别刷屏也别拖时间。"""
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._oauth_info.expires_ts = 0

    calls = 0

    async def _permanent():
        nonlocal calls
        calls += 1
        h, _ = proxy.auth_health.mark_failure(
            permanent=True, code=-10021, message="invalid refresh token"
        )
        proxy._set_auth_health(h)
        return None

    proxy.refresh_xiaomi_home_token_info = _permanent
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    await proxy._check_and_refresh_token()

    assert calls == 1


@pytest.mark.asyncio
async def test_no_refresh_when_token_still_fresh():
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._oauth_info.expires_ts = 2**31 - 1  # 远未到期
    proxy.refresh_xiaomi_home_token_info = AsyncMock()

    await proxy._check_and_refresh_token()

    proxy.refresh_xiaomi_home_token_info.assert_not_called()


# ─────────────── 中断的刷新：重启后安排验证，不直接定论 ───────────────


def test_interrupted_refresh_schedules_verification_not_a_verdict():
    """上一轮刷新结果未知时，重启后安排验一次，**不判永久失效**。

    标记是在发请求**之前**写下的，所以它只说明「这一轮结果未知」：请求抵达云端
    之后被硬杀，旧令牌确实作废；在那之前（连接、握手、发送）被硬杀，手上这枚
    完好无损——两种现场一模一样，判据分不开。

    两侧代价严重不对称，所以只能选不定论：误判一次，住户的一次断电就换来「全停
    且必须手动重绑」，而定时续期在临期窗口外直接早退，这个误判要等到令牌自然临期
    才有机会纠正；而放弃提前定论，真死时也只多花一轮退避。
    """
    from miloco.database.kv_repo import AuthConfigKeys

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    # 模拟：发请求前记下了指纹，然后进程没能走完
    proxy._mark_refresh_inflight(proxy._oauth_info.refresh_token)
    assert kv.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY) is not None

    reborn = _make_proxy(kv)  # 新进程重新加载
    assert not reborn.auth_health.is_degraded, (
        "结果未知不等于令牌已死，不该在启动时就判永久失效"
    )
    assert reborn._verify_token_on_start, "应当安排下一次检查立刻验一次"
    assert kv.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY) is None, (
        "标记应当被消费掉，不能反复触发"
    )


@pytest.mark.asyncio
async def test_scheduled_check_verifies_immediately_after_interruption():
    """安排了验证之后，定时检查不许因为「远未临期」早退。

    早退的话这一轮就白过，而唯一能纠正误判的路径正是一次成功的续期——令牌远未
    临期时要等好几天才会有下一次。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv, expires_in=86400 * 3)  # 远未临期
    proxy._mark_refresh_inflight(proxy._oauth_info.refresh_token)
    reborn = _make_proxy(kv, expires_in=86400 * 3)
    assert reborn._verify_token_on_start, "前置：已安排验证"

    refreshed: list[str] = []

    async def _refresh(refresh_token, persist=None):
        refreshed.append(refresh_token)
        info = MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
        if persist:
            persist(info)
        return info

    reborn._miot_client.refresh_access_token_async = _refresh

    await reborn._check_and_refresh_token()

    assert refreshed, "远未临期也必须验这一次"
    assert not reborn.auth_health.is_degraded, "验证成功即视为恢复"
    assert not reborn._verify_token_on_start, "验过一次就该落下，不必每轮都跳早退"


@pytest.mark.asyncio
async def test_all_transient_failures_keep_the_verification_intent():
    """四次尝试全是连接失败 = 「仍然不知道」，意向位必须留着，下个周期再验。

    这条与断电现场高度相关，不是两件独立的事：断电把进程硬杀，重启后光猫拨号
    通常比机器起得慢，这一轮四次尝试（合计一轮退避）全灭是常态。若在发起验证时
    就把意向位清掉，验证等于从未发生却被当成已完成——之后每个周期都因远未临期
    早退，健康度停在正常，而那枚刷新令牌可能其实已被云端消费。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv, expires_in=86400 * 3)
    proxy._mark_refresh_inflight(proxy._oauth_info.refresh_token)
    reborn = _make_proxy(kv, expires_in=86400 * 3)
    assert reborn._verify_token_on_start, "前置：已安排验证"

    tries: list[int] = []

    async def _conn_error(refresh_token, persist=None):
        tries.append(1)
        raise MIoTOAuth2Error("connection failed", MIoTErrorCode.CODE_UNAVAILABLE)

    reborn._miot_client.refresh_access_token_async = _conn_error
    # 退避 sleep 直接跳过，用例不必真等
    import miloco.miot.client as mc

    orig_sleep = mc.asyncio.sleep

    async def _no_sleep(_s):
        return None

    mc.asyncio.sleep = _no_sleep
    try:
        await reborn._check_and_refresh_token()
    finally:
        mc.asyncio.sleep = orig_sleep

    assert len(tries) > 1, "前置：瞬时故障应当在本轮内退避重试"
    assert not reborn.auth_health.is_degraded, "连接失败是瞬时故障，不该判降级"
    assert reborn._verify_token_on_start, (
        "本轮没拿到结论，意向位不能消费——否则验证再也不会发生"
    )


@pytest.mark.asyncio
async def test_verification_that_gets_rejected_does_degrade():
    """反向：令牌真的已死时，这次验证要如期把它判成降级。

    不定论换来的只是「慢一次往返」，不是「永远不报」。这条与上一条成对——只验
    「不误判」，把判据改成永不降级也是绿的。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv, expires_in=86400 * 3)
    proxy._mark_refresh_inflight(proxy._oauth_info.refresh_token)
    reborn = _make_proxy(kv, expires_in=86400 * 3)

    async def _rejected(refresh_token, persist=None):
        raise MIoTOAuth2Error(
            "oauth/get_token rejected, error=96009",
            MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN,
        )

    reborn._miot_client.refresh_access_token_async = _rejected

    await reborn._check_and_refresh_token()

    assert reborn.auth_health.is_degraded, "云端明确拒绝了，这一轮就该置降级"
    assert reborn.auth_health.error_code == (
        MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value
    )


def test_marker_not_matching_current_token_is_not_degraded():
    """指纹对不上说明新令牌其实存下来了，只是标记没来得及清——不该判降级。"""
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._mark_refresh_inflight("some_other_token_that_was_replaced")
    # 手上已经是换过的新令牌
    proxy._oauth_info = MIoTOauthInfo(
        access_token="at2", refresh_token="rt2", expires_ts=9999999999
    )

    reborn = _make_proxy(kv)
    assert not reborn.auth_health.is_degraded


def test_marker_stores_fingerprint_not_the_token():
    """标记里不能出现凭据原文——它的用途只是比对是不是同一枚。"""
    from miloco.database.kv_repo import AuthConfigKeys

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    secret = "R3_super_secret_refresh_token_value"
    proxy._mark_refresh_inflight(secret)

    raw = kv.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY)
    assert secret not in raw, "标记里存了凭据原文"
    assert "fp" in raw and "ts" in raw


# ─────────────── 永久失效即停，瞬时故障不停 ───────────────


def test_is_operational_false_after_permanent_rejection():
    """云端明确拒绝凭据后判为不可用——感知据此停下。

    拉取账号下的相机列表本身就要有效令牌，拒绝之后那一步会 401、拿不到列表，
    感知没有相机可跑。与其空转到访问令牌自然到期，不如当场停下并告知住户。
    """
    from miot.error import MIoTErrorCode

    proxy = _make_proxy(_FakeKV())
    assert proxy.is_operational, "前置条件：一开始应当可用"

    health, _ = proxy._auth_health.mark_failure(
        permanent=True,
        code=MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value,
        message="invalid refresh token",
    )
    proxy._set_auth_health(health)

    assert not proxy.is_operational, "永久失效后应判为不可用"
    assert proxy.is_authenticated, "凭据仍然存在——两个判据不是一回事"


def test_transient_failure_keeps_operational():
    """一次超时不该打掉感知——瞬时故障与永久失效是两档。

    把瞬时故障也算作不可用的话，一次网络抖动就会让整套感知停摆，而那时凭据
    其实好好的。这一条守的正是这个边界，改判据时别把它一起改掉。
    """
    proxy = _make_proxy(_FakeKV())

    for _ in range(3):
        health, _ = proxy._auth_health.mark_failure(
            permanent=False, code=None, message="timeout"
        )
        proxy._set_auth_health(health)
        assert proxy.is_operational, "瞬时故障不该让感知停下"


def test_operational_recovers_after_success():
    """重新授权或续期成功后恢复可用。"""
    from miot.error import MIoTErrorCode

    proxy = _make_proxy(_FakeKV())
    health, _ = proxy._auth_health.mark_failure(
        permanent=True,
        code=MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value,
        message="rejected",
    )
    proxy._set_auth_health(health)
    assert not proxy.is_operational

    proxy._set_auth_health(proxy._auth_health.mark_success())
    assert proxy.is_operational, "恢复后应当重新可用"
