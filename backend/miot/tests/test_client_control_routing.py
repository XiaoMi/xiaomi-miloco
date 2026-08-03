# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Tests for the local↔cloud control routing in `miot.client.MIoTClient`.

Exercises `set_props_async` / `get_props_async` / `action_async` in isolation
by attaching a fake central hub + fake http client to a bare MIoTClient
instance (no OAuth / storage / network). Asserts the failure-classification
matrix:

  - not locally controllable / in cooldown  → cloud
  - local timeout (set/action)               → error, NO cloud, cooldown armed
  - local timeout (get)                      → cloud (idempotent), cooldown armed
  - gateway replied but unusable (-10041 / -10040):
        set / action                         → error, NO cloud, NO cooldown
        get                                  → cloud (idempotent), NO cooldown
  - exception / reply carrying no `code`     → cloud, NO cooldown
  - literal None reply:  set                 → cloud
                         action              → -10041 (result unknown), NO cloud (non-idempotent)
  - success                                  → returned as-is (no cloud)
  - device-level rejection:  set / action     → returned as-is
                             get              → cloud (any non-OK code)
  - cloud batch raises with local survivors  → local results kept, cloud=error
"""

from __future__ import annotations

import pytest
from miot.client import MIoTClient
from miot.error import MIoTErrorCode
from miot.types import (
    MIoTActionParam,
    MIoTGetPropertyParam,
    MIoTSetPropertyParam,
)

_TIMEOUT = MIoTErrorCode.CODE_TIMEOUT.value
_INTERNAL = MIoTErrorCode.CODE_INTERNAL_ERROR.value
# 本地"结果未知"码:网关已回包但不可用 → 写/动作绝不重发云端。
_UNKNOWN = MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value  # -10041
_BAD_JSON = MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value  # -10040
# 真实可达的"肯定没执行"形态:回包里连 code 都没有(client 判 code is None)。
# 注意不要用 -10001/-10040 冒充这类:前者本地无产出点,后者前提是网关已回包。
_NO_CODE_REPLY = {"message": "gateway said something unparseable"}


class FakeCentralHub:
    """Minimal stand-in for CentralHubManager used by the routing methods."""

    def __init__(self, controllable=None, cooldown=None, responses=None):
        self.enabled = True
        self._controllable = set(controllable or [])
        self._cooldown = set(cooldown or [])
        # did -> value/dict returned by set/get/action, or an Exception to raise
        self._responses = responses or {}
        self.cooled = []  # dids passed to note_local_failure
        self.local_calls = []  # dids that actually hit a local RPC

    def can_control(self, did):
        return did in self._controllable

    def in_local_cooldown(self, did):
        return did in self._cooldown

    def note_local_failure(self, did):
        self.cooled.append(did)

    def _resolve(self, did):
        self.local_calls.append(did)
        r = self._responses.get(did)
        if isinstance(r, Exception):
            raise r
        return r

    async def set_prop_async(self, did, siid, piid, value):
        return self._resolve(did)

    async def get_prop_async(self, did, siid, piid):
        return self._resolve(did)

    async def action_async(self, did, siid, aiid, in_list):
        return self._resolve(did)


class FakeHttpClient:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc
        self.set_calls = []
        self.get_calls = []
        self.action_calls = []

    async def set_props_async(self, params):
        self.set_calls.append(list(params))
        if self._raise:
            raise self._raise
        return [
            {"did": p.did, "siid": p.siid, "piid": p.piid, "code": 0} for p in params
        ]

    async def get_props_async(self, params):
        self.get_calls.append(list(params))
        if self._raise:
            raise self._raise
        return [
            {"did": p.did, "siid": p.siid, "piid": p.piid, "value": "cloud", "code": 0}
            for p in params
        ]

    async def action_async(self, param):
        self.action_calls.append(param)
        if self._raise:
            raise self._raise
        return {"did": param.did, "siid": param.siid, "aiid": param.aiid, "code": 0}


def _make_client(ch, http):
    client = object.__new__(MIoTClient)  # bypass heavy __init__
    client._central_hub = ch
    client._http_client = http
    return client


def _sp(did, value=1):
    return MIoTSetPropertyParam(did=did, siid=2, piid=1, value=value)


def _gp(did):
    return MIoTGetPropertyParam(did=did, siid=2, piid=1)


# ------------------------------------------------------------------ set_props


@pytest.mark.asyncio
async def test_set_not_controllable_goes_cloud():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0
    assert ch.local_calls == []  # never tried local
    assert len(http.set_calls) == 1


@pytest.mark.asyncio
async def test_set_in_cooldown_goes_cloud_without_local():
    ch = FakeCentralHub(controllable=["A"], cooldown=["A"])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.set_props_async([_sp("A")])
    assert ch.local_calls == []
    assert len(http.set_calls) == 1


@pytest.mark.asyncio
async def test_set_timeout_no_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == _TIMEOUT
    assert http.set_calls == []  # NO cloud retry on timeout
    assert ch.cooled == ["A"]  # cooldown armed


@pytest.mark.asyncio
async def test_set_other_failure_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": _NO_CODE_REPLY})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # cloud result
    assert len(http.set_calls) == 1
    assert ch.cooled == []  # non-timeout does not cool down


@pytest.mark.asyncio
async def test_set_exception_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": RuntimeError("boom")})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0
    assert len(http.set_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_set_success_returned_as_is_no_cloud():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 1}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # 1 normalized to 0
    assert http.set_calls == []


@pytest.mark.asyncio
async def test_set_device_rejection_returned_as_is():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": -704002000}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == -704002000
    assert http.set_calls == []


@pytest.mark.asyncio
async def test_set_mixed_batch_preserves_order():
    # A local-success, B not controllable (cloud), C local-timeout (error).
    ch = FakeCentralHub(
        controllable=["A", "C"],
        responses={"A": {"code": 0}, "C": {"code": _TIMEOUT}},
    )
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B"), _sp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    assert res[0]["code"] == 0
    assert res[1]["code"] == 0  # cloud
    assert res[2]["code"] == _TIMEOUT
    assert [p.did for p in http.set_calls[0]] == ["B"]  # only B went to cloud


@pytest.mark.asyncio
async def test_set_cloud_failure_keeps_local_survivors():
    # A succeeds locally, B needs cloud but cloud raises → A kept, B error.
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 0}})
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B")])
    assert res[0]["code"] == 0  # local survivor
    assert res[1]["code"] == _INTERNAL  # cloud error fill


@pytest.mark.asyncio
async def test_set_all_cloud_failure_raises():
    # Nothing handled locally → preserve cloud-only contract (raise).
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    with pytest.raises(RuntimeError):
        await client.set_props_async([_sp("A"), _sp("B")])


# ------------------------------------------------------------------ get_props


@pytest.mark.asyncio
async def test_get_local_success():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"value": 42, "code": 0}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == 42
    assert res[0]["code"] == 0
    assert http.get_calls == []


@pytest.mark.asyncio
async def test_get_timeout_falls_back_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"  # read fell back
    assert len(http.get_calls) == 1
    assert ch.cooled == ["A"]  # get timeout still cools down


@pytest.mark.asyncio
async def test_get_fast_error_cloud_no_cooldown():
    """get_prop 快速错误(网关拒绝,非超时)→ 云端、不冷却。

    用真实的设备级拒绝码(-704030013 属性不可读)而非 -10004:后者已收窄为"本地与
    云端都没给出结果",本地路径产不出它,拿它当"网关拒绝"的例子语义不对。
    """
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": -704030013}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"
    assert ch.cooled == []  # 快速错误不冷却


@pytest.mark.asyncio
async def test_get_exception_falls_back_cloud_no_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": RuntimeError("x")})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_get_cloud_failure_keeps_local_survivors():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"value": 7, "code": 0}})
    http = FakeHttpClient(raise_exc=RuntimeError("cloud down"))
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A"), _gp("B")])
    assert res[0]["value"] == 7
    assert res[1]["code"] == _INTERNAL


# -------------------------------------------------------------------- action


def _ap(did):
    return MIoTActionParam(did=did, siid=2, aiid=1, in_=[])


@pytest.mark.asyncio
async def test_action_timeout_no_cloud_and_cooldown():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _TIMEOUT}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == _TIMEOUT
    assert http.action_calls == []
    assert ch.cooled == ["A"]


@pytest.mark.asyncio
async def test_action_other_failure_falls_back_cloud():
    ch = FakeCentralHub(controllable=["A"], responses={"A": _NO_CODE_REPLY})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == 0  # cloud
    assert len(http.action_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_action_success_as_is():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": 0, "out": []}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == 0
    assert http.action_calls == []


@pytest.mark.asyncio
async def test_action_not_controllable_goes_cloud():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.action_async(_ap("A"))
    assert ch.local_calls == []
    assert len(http.action_calls) == 1


@pytest.mark.asyncio
async def test_central_hub_disabled_uses_cloud_directly():
    ch = FakeCentralHub(controllable=["A"])
    ch.enabled = False
    http = FakeHttpClient()
    client = _make_client(ch, http)
    await client.set_props_async([_sp("A")])
    assert ch.local_calls == []
    assert len(http.set_calls) == 1


# --------------------------------------------------------------- edge cases


@pytest.mark.asyncio
async def test_set_empty_params():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    assert await client.set_props_async([]) == []
    assert http.set_calls == []  # nothing to route


@pytest.mark.asyncio
async def test_get_empty_params():
    ch = FakeCentralHub(controllable=[])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    assert await client.get_props_async([]) == []


class ShortHttpClient(FakeHttpClient):
    """Cloud returns fewer results than requested (broker dropped some)."""

    async def set_props_async(self, params):
        self.set_calls.append(list(params))
        return [{"did": params[0].did, "siid": params[0].siid, "piid": params[0].piid, "code": 0}]


@pytest.mark.asyncio
async def test_set_short_cloud_result_backfilled_with_error():
    # Two not-locally-controllable dids → both cloud; cloud returns only 1
    # result → the missing slot must be filled with an internal-error code,
    # never left as None.
    ch = FakeCentralHub(controllable=[])
    http = ShortHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B")])
    assert len(res) == 2
    assert res[0]["code"] == 0
    assert res[1]["code"] == _INTERNAL  # backfilled, not None
    assert None not in res


# ------------------------------------------- cloud backfill keyed by identity


class ReorderingHttpClient(FakeHttpClient):
    """Cloud that answers correctly but in a different order than requested.

    The `/app/v2/miotspec/prop/{set,get}` contract makes no ordering promise;
    aggregating per device would be a legitimate implementation. Positional
    backfill silently mis-attributes results under such a cloud.
    """

    async def set_props_async(self, params):
        res = await super().set_props_async(params)
        # Tag each so a mis-attribution is observable, then reverse.
        for r in res:
            r["code"] = -704042011 if r["did"] == "B" else 0
        return list(reversed(res))

    async def get_props_async(self, params):
        res = await super().get_props_async(params)
        for r in res:
            r["value"] = f"v-{r['did']}"
        return list(reversed(res))


class DroppingHttpClient(FakeHttpClient):
    """Cloud that omits one entry entirely (did == 'B')."""

    async def set_props_async(self, params):
        res = await super().set_props_async(params)
        return [r for r in res if r["did"] != "B"]


@pytest.mark.asyncio
async def test_set_cloud_reordered_results_matched_by_key():
    """云端乱序返回时结果仍须归到正确的 did，而不是按下标错位。"""
    ch = FakeCentralHub(controllable=[])  # 全部走云端
    client = _make_client(ch, ReorderingHttpClient())
    res = await client.set_props_async([_sp("A"), _sp("B"), _sp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    # 只有 B 是失败码；错位的话失败码会挂到 A 或 C 身上。
    assert res[0]["code"] == 0
    assert res[1]["code"] == -704042011
    assert res[2]["code"] == 0


@pytest.mark.asyncio
async def test_get_cloud_reordered_results_matched_by_key():
    ch = FakeCentralHub(controllable=[])
    client = _make_client(ch, ReorderingHttpClient())
    res = await client.get_props_async([_gp("A"), _gp("B"), _gp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    assert [r["value"] for r in res] == ["v-A", "v-B", "v-C"]


@pytest.mark.asyncio
async def test_set_cloud_omitted_entry_gets_internal_error():
    """云端漏返回某条 → 该槽位填内部错误，其余条目不受影响。"""
    ch = FakeCentralHub(controllable=[])
    client = _make_client(ch, DroppingHttpClient())
    res = await client.set_props_async([_sp("A"), _sp("B"), _sp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    assert res[0]["code"] == 0
    assert res[1]["code"] == _INTERNAL  # 漏掉的那条
    assert res[2]["code"] == 0


@pytest.mark.asyncio
async def test_set_mixed_batch_reordered_cloud_keeps_local_results():
    """本地已定结果 + 云端乱序混合批次:本地那条不被云端条目覆盖。

    用一个设备级拒绝码(-704030023 属性不可写)标记 A 的本地结果——它既不在
    _LOCAL_OK_CODES 也不在 _LOCAL_FALLBACK_CODES,SDK 会原样保留,因此可与云端
    对 A 的 code=0 区分开(本地成功会被归一成 0,反而无法区分)。
    """
    device_reject = -704030023
    ch = FakeCentralHub(
        controllable=["A"], responses={"A": {"code": device_reject}}
    )
    http = ReorderingHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B"), _sp("C")])
    assert [r["did"] for r in res] == ["A", "B", "C"]
    assert res[0]["code"] == device_reject  # A 仍是本地那条,未被云端覆盖
    assert res[1]["code"] == -704042011
    assert res[2]["code"] == 0
    assert [p.did for p in http.set_calls[0]] == ["B", "C"]  # A 没发去云端


# ------------------------------------ -10041/-10040 = 结果未知,不得云端重发


@pytest.mark.asyncio
async def test_set_ambiguous_code_no_cloud_retry():
    """-10041(网关已回包但结构不认识)→ 写属性绝不云端重发,否则会执行两次。

    -10041 在本地设备接口里只有一个产生位置:mips_local 各方法末尾那个
    "Invalid result",而走到那里的前提正是网关**已经回包**——超时会先被
    {"error": {code: -10006}} 分支拦走。所以它是"结果未知",不是"肯定没执行"。

    注意别把 -10004 当成这一类:它已收窄为"本地与云端都没给出结果",本地产不出。
    """
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _UNKNOWN}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == _UNKNOWN  # 原样上报"结果未知"
    assert http.set_calls == []  # 关键:一次云端重发都没有
    assert ch.cooled == []  # 快速返回,不值得冷却(没有 5s 超时代价可省)


@pytest.mark.asyncio
async def test_action_ambiguous_code_no_cloud_retry():
    """累加型动作(窗帘 +10% / 音量 +1)双发用户会看到走两步,故同样不重发。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _UNKNOWN}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == _UNKNOWN
    assert res["did"] == "A"  # 字段与 timeout 分支一致
    assert http.action_calls == []
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_get_ambiguous_code_does_retry_cloud():
    """读是幂等的 → -10041 仍可云端重读,拿到确定值比返回未知更有用。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _UNKNOWN}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["code"] == 0  # 云端结果
    assert len(http.get_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_mixed_batch_ambiguous_does_not_leak_to_cloud():
    """混合批次里 -10041 的那条不进云端批次,其它该走云端的照走。"""
    ch = FakeCentralHub(
        controllable=["A", "B"],
        responses={"A": {"code": _UNKNOWN}, "B": _NO_CODE_REPLY},
    )
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A"), _sp("B")])
    assert res[0]["code"] == _UNKNOWN  # A 未重发
    assert res[1]["code"] == 0  # B 走了云端
    assert [p.did for p in http.set_calls[0]] == ["B"]  # 只有 B


@pytest.mark.asyncio
async def test_set_bad_json_reply_no_cloud_retry():
    """-10040(回包非合法 JSON)前提同样是网关已回包 → 写属性不得重发云端。

    这个码此前会被 mips_local 覆写成 -10004,现在原样上抛;它归入
    _LOCAL_AMBIGUOUS_CODES 而非"肯定没执行",因为请求确实到过网关。
    """
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _BAD_JSON}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == _BAD_JSON
    assert http.set_calls == []
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_action_bad_json_reply_no_cloud_retry():
    ch = FakeCentralHub(controllable=["A"], responses={"A": {"code": _BAD_JSON}})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == _BAD_JSON
    assert http.action_calls == []


@pytest.mark.asyncio
async def test_reply_without_code_falls_back_cloud():
    """"肯定没执行"在本地路径的真实形态:回包里连 code 都没有 → 可安全重发云端。

    回归上一版测试用 -10001 冒充这类的问题——那个码本地根本没有产出点,
    绿灯并不能证明生产上这条分支走得通。
    """
    ch = FakeCentralHub(controllable=["A"], responses={"A": _NO_CODE_REPLY})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # 云端结果
    assert len(http.set_calls) == 1
    assert ch.cooled == []


def test_local_fallback_codes_is_empty_by_design():
    """守住"空集是事实而非遗漏"这个结论。

    往里加码前必须先确认:该码产生时请求是否可能已经执行过。-10040 / -10041 都以
    "网关已回包"为前提,属于结果未知,加进来就会重新引入双发。
    """
    from miot.client import _LOCAL_AMBIGUOUS_CODES, _LOCAL_FALLBACK_CODES

    assert _LOCAL_FALLBACK_CODES == frozenset()
    assert _BAD_JSON in _LOCAL_AMBIGUOUS_CODES
    assert _UNKNOWN in _LOCAL_AMBIGUOUS_CODES
    assert not (_LOCAL_AMBIGUOUS_CODES & _LOCAL_FALLBACK_CODES)


# ------------------------------------------------- matrix gaps (per PR review)
#
# 上面的用例按操作分组;这一节专门补矩阵里此前**零覆盖**的格子。逐格对账后缺的是:
# action+异常、action+字面 None、set+字面 None、get+回包无 code、get/action 的
# "冷却中→云端"。其中 action+字面 None 是矩阵里唯一「set 与 action 不对称」的一行,
# 却一条测试都没有 —— 有人把它改成与 set 一致(回落云端)CI 会全绿,而非幂等动作的
# 双发防护就无声消失了。


@pytest.mark.asyncio
async def test_action_exception_falls_back_cloud_no_cooldown():
    """本地调用抛异常 = 请求明确没发出去 → 动作可安全重发云端,不冷却。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": RuntimeError("boom")})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == 0  # cloud
    assert len(http.action_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_action_literal_none_returns_result_unknown_no_cloud():
    """网关返回字面 None(内部吞了异常,连回包结构都拿不到):动作非幂等,绝不重发,
    报"结果未知"的 -10041,不是 -10004——后者已被收窄为"本地与云端都没给出结果"、
    与本地路由无关,复用它会让 is_result_unknown() 为假,规则引擎的非幂等冷却
    判据两侧都不成立,下一轮 tick 会把同一条动作再执行一遍。

    与 test_set_literal_none_falls_back_cloud 成对——这是 set/action 唯一不对称的
    一格,两条必须同时在,否则改动其一不会被发现。
    """
    ch = FakeCentralHub(controllable=["A"], responses={"A": None})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert res["code"] == _UNKNOWN
    assert http.action_calls == []
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_set_literal_none_falls_back_cloud():
    """写属性遇字面 None 走云端(写是"最终状态"语义,重发不会叠加)。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": None})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A")])
    assert res[0]["code"] == 0  # cloud
    assert len(http.set_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_get_reply_without_code_falls_back_cloud():
    """读属性回包连 code 都没有 → 防御性分支兜云端,不冷却。"""
    ch = FakeCentralHub(controllable=["A"], responses={"A": _NO_CODE_REPLY})
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert res[0]["value"] == "cloud"
    assert len(http.get_calls) == 1
    assert ch.cooled == []


@pytest.mark.asyncio
async def test_get_in_cooldown_goes_cloud_without_local():
    ch = FakeCentralHub(controllable=["A"], cooldown=["A"])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.get_props_async([_gp("A")])
    assert ch.local_calls == []  # 冷却窗口内不再花 5s 试本地
    assert res[0]["value"] == "cloud"
    assert len(http.get_calls) == 1


@pytest.mark.asyncio
async def test_action_in_cooldown_goes_cloud_without_local():
    ch = FakeCentralHub(controllable=["A"], cooldown=["A"])
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.action_async(_ap("A"))
    assert ch.local_calls == []
    assert res["code"] == 0
    assert len(http.action_calls) == 1


@pytest.mark.asyncio
async def test_backfill_handles_duplicate_iid_in_one_batch():
    """同一批次里重复 (did,siid,piid):两个槽位各拿一条云端结果,不能有一个被误报。

    请求体允许重复 iid(agent 可能发 ["prop.2.1", "prop.2.1"]),云端逐条回两条同键
    结果。回填若按"每键只消费一次"实现,第二个槽位会拿不到结果而被贴 -10004,
    把实际已执行的那条报成内部错误。
    """
    ch = FakeCentralHub(controllable=[])  # 全部走云端
    http = FakeHttpClient()
    client = _make_client(ch, http)
    res = await client.set_props_async([_sp("A", 1), _sp("A", 2)])
    assert [r["code"] for r in res] == [0, 0]
    assert all(r["did"] == "A" for r in res)
