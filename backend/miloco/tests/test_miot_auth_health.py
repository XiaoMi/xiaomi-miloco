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
from unittest.mock import AsyncMock, MagicMock, patch

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
    # 那段，这里**不判降级**，只安排下一次检查跳过临期早退、立刻验一次——那个
    # 现场分不出「令牌已死」与「请求压根没发出去」。这个取舍由
    # test_interrupted_refresh_schedules_verification_not_a_verdict 钉住。
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

    # 签名要跟着真实现走：它现在带一个「当初决定要换的是哪一枚」的关键字，
    # 桩不接就会在调用点撞 TypeError——那不是生产缺陷、是替身没跟上。
    async def _always_transient(**_kw):
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

    async def _permanent(**_kw):
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
async def test_refresh_loop_checks_before_its_first_sleep():
    """定时循环先查后睡——待验意向必须立刻被消费，不能等满一个周期。

    立起意向的是启动路径，消费它的只有这个循环。先睡满一个周期的话，那段时间里
    健康度仍是正常、界面仍显示已连，而手上那枚续期令牌可能其实已被云端作废——
    「结果未知就去验一次」这套设计的意义正好被那段延迟抵消掉。

    刻意不去桩 ``asyncio.sleep``：真的睡下去就永远等不到，而先查的话检查在第一个
    事件循环轮次里就发生。等得到就是先查，等不到就是先睡。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    checked = asyncio.Event()

    async def _check():
        checked.set()

    proxy._check_and_refresh_token = _check

    task = asyncio.create_task(proxy._start_token_refresh_task())
    try:
        await asyncio.wait_for(checked.wait(), timeout=1)
    finally:
        task.cancel()

    assert checked.is_set(), "首次检查排在睡眠之后，意向要等满一个周期才被消费"


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


@pytest.mark.asyncio
async def test_marker_survives_a_transient_failure():
    """瞬时失败后中断标记要留着——那一轮的结果仍然未知。

    超时或连不上意味着请求**可能已经抵达云端并轮换了令牌**，我们只是没收到回音。
    无条件清掉标记，等于把「收到了一个异常」当成「知道结果了」；此后若再被硬杀，
    内存里的待验意向随进程消失、库里的标记也已不在，下次启动就不再验，那枚可能
    已死的令牌要等自然临期才会被发现。
    """
    from miloco.database.kv_repo import AuthConfigKeys

    kv = _FakeKV()
    proxy = _make_proxy(kv)

    async def _timeout(refresh_token, persist=None):
        raise MIoTOAuth2Error("connection failed", MIoTErrorCode.CODE_UNAVAILABLE)

    proxy._miot_client.refresh_access_token_async = _timeout

    assert await proxy.refresh_xiaomi_home_token_info() is None
    assert not proxy.auth_health.is_degraded, "前置：连不上属瞬时故障"
    assert kv.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY) is not None, (
        "结果未知，标记不该被清掉"
    )


@pytest.mark.asyncio
async def test_marker_is_cleared_once_the_outcome_is_known():
    """反向：拿到结论就要清——成功与「云端明确拒绝」都是结论。

    与上一条成对：只验「瞬时不清」的话，把判据改成永不清也是绿的，标记会一直
    留着，每次重启都白白安排一次验证。
    """
    from miloco.database.kv_repo import AuthConfigKeys
    from miot.types import MIoTOauthInfo

    # 成功
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._miot_client.refresh_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
    )
    assert await proxy.refresh_xiaomi_home_token_info() is not None
    assert kv.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY) is None, "成功后应当清掉"

    # 云端明确拒绝
    kv2 = _FakeKV()
    proxy2 = _make_proxy(kv2)

    async def _rejected(refresh_token, persist=None):
        raise MIoTOAuth2Error(
            "invalid refresh token",
            MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN,
        )

    proxy2._miot_client.refresh_access_token_async = _rejected
    assert await proxy2.refresh_xiaomi_home_token_info() is None
    assert proxy2.auth_health.is_degraded, "前置：这是永久失效"
    assert kv2.get(AuthConfigKeys.MIOT_REFRESH_INFLIGHT_KEY) is None, (
        "已经拿到结论，标记不该继续留着"
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


# ─────────────── 云端令牌校验的结果合并 ───────────────


@pytest.mark.asyncio
async def test_token_check_is_coalesced_within_the_window():
    """同一枚令牌在窗口内只问云端一次。

    界面按固定间隔轮询绑定状态，而它背后是一次真实的云端校验；不合并的话，光是
    把页面开着就会给云端打上数千次请求，多开页签还成倍叠加，而降级态下那些请求
    全是失败的。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    calls: list[int] = []

    async def _check():
        calls.append(1)
        return True

    proxy._miot_client.check_token_async = _check

    assert await proxy.check_token_valid() is True
    assert await proxy.check_token_valid() is True
    assert await proxy.check_token_valid() is True

    assert len(calls) == 1, f"窗口内应当只问一次云端，实际 {len(calls)} 次"


@pytest.mark.asyncio
async def test_token_check_cache_is_keyed_on_the_token_itself():
    """令牌一换，缓存立刻失效——「重新绑定后自动恢复」不许被缓存延后。

    缓存键取的是令牌本身而不是别的状态：靠「记得在 N 处失效」的缓存，漏掉一处
    就会把界面钉在过期的结论上，而这个 PR 的核心承诺正是「重新绑定后马上恢复」。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    calls: list[int] = []

    async def _check():
        calls.append(1)
        return True

    proxy._miot_client.check_token_async = _check

    # 用肯定结论来验键：否定结论只在降级态才缓存，拿它验键会把两件事混在一起
    assert await proxy.check_token_valid() is True
    assert await proxy.check_token_valid() is True
    assert len(calls) == 1, "同一枚令牌，应当走缓存"

    # 重新绑定：换了一枚新令牌
    proxy._oauth_info = MIoTOauthInfo(
        access_token="brand_new", refresh_token="rt2", expires_ts=9999999999
    )

    assert await proxy.check_token_valid() is True
    assert len(calls) == 2, "令牌已换，必须重新问云端"


@pytest.mark.asyncio
async def test_degraded_failure_is_coalesced():
    """**降级态下**失败的结论同样进缓存——那些注定失败的请求要被合并掉。"""
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    health, _ = proxy._auth_health.mark_failure(
        permanent=True, code=-10021, message="invalid refresh token"
    )
    proxy._auth_health = health
    calls: list[int] = []

    async def _check():
        calls.append(1)
        return False

    proxy._miot_client.check_token_async = _check

    assert await proxy.check_token_valid() is False
    assert await proxy.check_token_valid() is False

    assert len(calls) == 1, "失败结论没进缓存，降级态会持续打云端"


@pytest.mark.asyncio
async def test_transient_failure_is_not_cached():
    """未降级时的失败**不进缓存**——一次网络抖动不该把界面钉住一整个窗口。

    底层实现对超时、连不上同样返回假，与「云端明确拒绝」在返回值上分不出，而这
    正是本改动通篇在分的那两档。缓存住抖动那一次，住户手动刷新也绕不开——而那条
    自救路径本来是通的。与上一条成对：只验「降级态要合并」，把判据放宽成无差别
    缓存也是绿的。
    """
    kv = _FakeKV()
    proxy = _make_proxy(kv)
    assert not proxy.auth_health.is_degraded, "前置：未降级"
    results = [False, True]

    async def _check():
        return results.pop(0)

    proxy._miot_client.check_token_async = _check

    assert await proxy.check_token_valid() is False
    assert await proxy.check_token_valid() is True, "抖动的结论不该被缓存"


@pytest.mark.asyncio
async def test_rebinding_drops_a_pending_verification():
    """重绑之后不该再「验一次」——它走的是授权那条路，不经过定时检查。

    留着那份待验意向，下一个周期会跳过「还有半小时才过期」的早退，拿刚绑好的那枚
    一次性令牌白换一轮（又开一次崩溃窗口），日志还会指向一个并不存在的中断。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv, expires_in=86400 * 3)
    proxy._mark_refresh_inflight(proxy._oauth_info.refresh_token)
    reborn = _make_proxy(kv, expires_in=86400 * 3)
    assert reborn._verify_token_on_start, "前置：已安排验证"

    # 扫码重绑最终就是拿一对新令牌走落库这一处
    reborn.reset_miot_token_info(
        MIoTOauthInfo(
            access_token="rebound", refresh_token="rebound_rt", expires_ts=9999999999
        )
    )

    assert not reborn._verify_token_on_start, "拿到一对新令牌，上一轮的未知已无关"


@pytest.mark.asyncio
async def test_refresh_releases_the_lock_before_the_cleanup():
    """令牌落库之后临界区就结束了：善后不许还占着与扫码授权共用的那把锁。

    占着的话，住户恰好这时提交扫码拿到的授权码，要排队等「睡一会儿 + 全量拉取」
    跑完——设备多的家里是好几秒到十几秒，而那个码是一次性且有有效期的。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._miot_client.refresh_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="new", refresh_token="new_rt", expires_ts=9999999999
        )
    )
    held: list[bool] = []

    async def _cleanup():
        held.append(proxy._token_refresh_lock.locked())

    proxy.refresh_miot_info = _cleanup

    with patch("asyncio.sleep", new=AsyncMock()):
        assert await proxy.refresh_xiaomi_home_token_info() is not None

    assert held == [False], "善后跑的时候锁必须已经放开"


@pytest.mark.asyncio
async def test_authorize_releases_the_lock_before_the_cleanup():
    """授权这条路的善后同样不许占着与定时续期共用的那把锁。

    与续期那一侧同一条边界：锁只护到令牌落库为止，其后那一趟全量拉取（身份 /
    设备 / 相机 / 场景）与凭据正确性无关。两条路各自有用例，是因为它们是两个
    独立的入口——只钉一条，另一条改回锁内不会有任何反应。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    proxy._miot_client.get_access_token_async = AsyncMock(
        return_value=MIoTOauthInfo(
            access_token="at", refresh_token="rt", expires_ts=9999999999
        )
    )
    held: list[bool] = []

    async def _cleanup():
        held.append(proxy._token_refresh_lock.locked())

    proxy.refresh_miot_info = _cleanup

    assert await proxy.get_miot_auth_info(code="c", state="s") is not None
    assert held == [False], "善后跑的时候锁必须已经放开"


@pytest.mark.asyncio
async def test_a_queued_refresh_does_not_burn_a_token_bound_while_it_waited():
    """排队期间住户完成重绑，这一轮不许再拿新令牌换一次。

    判定（临期 / 待验意向）都在锁外做，而这把锁与重新授权共用——排队期间世界可以
    变。真换一轮的代价不是「多一次请求」：一次性令牌被消费，且重新打开了「云端已
    轮换、本机未落库」那个窗口，而住户刚刚才把它关上。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    intended = proxy._oauth_info.refresh_token
    rotated: list[str] = []

    async def _rotate(refresh_token, persist=None):
        rotated.append(refresh_token)
        return MIoTOauthInfo(
            access_token="at2", refresh_token="rt2", expires_ts=9999999999
        )

    proxy._miot_client.refresh_access_token_async = _rotate

    # 让这一轮在锁上排队，期间模拟一次重绑换掉手上的凭据
    await proxy._token_refresh_lock.acquire()
    task = asyncio.ensure_future(
        proxy.refresh_xiaomi_home_token_info(expected_refresh_token=intended)
    )
    await asyncio.sleep(0)
    proxy.reset_miot_token_info(
        MIoTOauthInfo(
            access_token="rebound", refresh_token="rebound_rt", expires_ts=9999999999
        )
    )
    proxy._token_refresh_lock.release()
    result = await task

    assert rotated == [], "手上已不是当初要换的那一枚，不该再换"
    assert result is not None, "凭据是好的（别人刚换的），应判成成功而不是失败重试"
    assert proxy._oauth_info.refresh_token == "rebound_rt", "刚绑好的那枚必须原样留着"


@pytest.mark.asyncio
async def test_a_queued_refresh_still_runs_when_nothing_replaced_it():
    """反向：没人动过凭据时照常换。

    只验「变了就跳过」的话，把判据写成无条件跳过也是绿的——那等于把定时续期整个
    关掉，令牌会一路放到过期。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)
    intended = proxy._oauth_info.refresh_token
    rotated: list[str] = []

    async def _rotate(refresh_token, persist=None):
        rotated.append(refresh_token)
        info = MIoTOauthInfo(
            access_token="at2", refresh_token="rt2", expires_ts=9999999999
        )
        if persist:
            persist(info)
        return info

    proxy._miot_client.refresh_access_token_async = _rotate

    with patch("asyncio.sleep", new=AsyncMock()):
        assert await proxy.refresh_xiaomi_home_token_info(
            expected_refresh_token=intended
        ) is not None

    assert rotated == [intended], "凭据没被动过，就该照常换这一枚"


@pytest.mark.asyncio
async def test_the_retry_loop_stops_rotating_once_a_rebind_landed():
    """住户在退避 sleep 期间重绑，循环的下一次尝试不许再换。

    从真实入口 ``_check_and_refresh_token`` 驱动，而不是直接调下层：判定在循环
    **之前**做一次，而循环里每一次尝试都隔着一段退避（合计约 85 秒），窗口远比
    「排队等锁」那一瞬宽。只钉下层入口的话，调用侧忘了把「当初要换的是哪一枚」
    传下去，护栏照样全绿——这条就是钉那件事的。
    """
    from miot.types import MIoTOauthInfo

    kv = _FakeKV()
    proxy = _make_proxy(kv)  # 默认 600s 后到期 → 判定为需要刷新
    attempts: list[str] = []

    async def _first_fails_then_records(refresh_token, persist=None):
        attempts.append(refresh_token)
        if len(attempts) == 1:
            raise TimeoutError("read timeout")
        return MIoTOauthInfo(
            access_token="at2", refresh_token="rt2", expires_ts=9999999999
        )

    proxy._miot_client.refresh_access_token_async = _first_fails_then_records
    rebound = False

    async def _sleep_and_rebind(_sec):
        # 第一次退避期间，住户完成一次重绑
        nonlocal rebound
        if not rebound:
            rebound = True
            proxy.reset_miot_token_info(
                MIoTOauthInfo(
                    access_token="rebound",
                    refresh_token="rebound_rt",
                    expires_ts=9999999999,
                )
            )

    with patch("asyncio.sleep", new=_sleep_and_rebind):
        await proxy._check_and_refresh_token()

    assert len(attempts) == 1, (
        "重绑之后那次尝试仍然换了一轮——判定与执行之间的前提没被复核"
    )
    assert proxy._oauth_info.refresh_token == "rebound_rt", "刚绑好的那枚必须原样留着"

