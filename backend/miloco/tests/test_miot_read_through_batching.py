# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""``datasource=2`` 的批量悬崖 —— 见 ``miot.cloud.READ_THROUGH_MAX_PROPS_PER_REQ``。

读真机是**全或无**的:一次请求里的属性数超过该设备能在约 4.2 秒内答完的数量,
整批返回 ``-704220043`` 且不带 ``updateTime``。于是一台健康在线的设备会被整台
报成"读不到" —— 把一个响亮的假阳性换成了一个安静的**假阴性**,正是这条修复要
避免的方向。而 ``GET /devices/{did}/status?iid=`` 收逗号分隔的多个 iid,真实家里
33 台设备中有 11 台可读属性 ≥15 个,一次点名就能踩上去。

用例测的是 ``miot`` 包(``backend/miot``),文件却放在这里:``backend/pyproject.toml``
的 ``norecursedirs = ["miot", ...]`` 把 ``backend/miot/tests`` 整个排除在收集之外
(那边是要真账号 OAuth 的联调用例),放过去等于不跑。
"""

import pytest
from miot.cloud import READ_THROUGH_MAX_PROPS_PER_REQ, MIoTHttpClient
from miot.types import MIoTGetPropertyParam

# 超时那批整批返回的码,仓库码表里渲染成「属性值不正确」。
_CLIFF_CODE = -704220043


def _params(n: int) -> list[MIoTGetPropertyParam]:
    """n 个互不相同的属性,piid 从 1 递增 —— 用来验证顺序。"""
    return [MIoTGetPropertyParam(did="dev1", siid=2, piid=i + 1) for i in range(n)]


def _echo(data: dict, code: int = 0) -> dict:
    """云端的正常返回形状:逐属性一行,原样回 did/siid/piid。"""
    return {
        "result": [
            {
                "did": p["did"],
                "siid": p["siid"],
                "piid": p["piid"],
                # value 取 piid,好让"顺序错了"这件事在断言里现形
                "value": p["piid"],
                "code": code,
                **({"updateTime": 1785408130} if code == 0 else {}),
            }
            for p in data["params"]
        ]
    }


def _make_client(responder):
    """构造一个 HTTP 往返被替换掉的 client;``responder(data) -> res_obj``。

    卡的接缝是 ``__mihome_api_post_async``(一次 HTTP 往返),不是更上层的封装 ——
    要验的正是"发出去几个请求、每个请求里装了几个属性"。私有方法被 name mangling
    改了名,所以这里用改名后的属性名遮蔽它。
    """
    client = MIoTHttpClient(cloud_server="cn", access_token="token")
    calls: list[dict] = []

    async def _post(url_path: str, data: dict, **kwargs):
        calls.append(data)
        return responder(data)

    setattr(client, "_MIoTHttpClient__mihome_api_post_async", _post)
    return client, calls


@pytest.mark.asyncio
async def test_read_through_is_split_below_the_cliff():
    """超过 chunk 的点名查询必须**全部成功返回**,而不是整批栽掉。

    实测悬崖因设备而异(空调 14→15,净化器/洗衣机 16→20),所以分批值取的是
    最低那条的一半;这里只钉住"确实按 chunk 拆了、每个属性恰好被问一次"。
    """
    params = _params(42)
    client, calls = _make_client(_echo)

    out = await client.get_props_async(params, datasource=2)

    assert [len(c["params"]) for c in calls] == [8, 8, 8, 8, 8, 2]
    assert all(c["datasource"] == 2 for c in calls), "每一批都得照样是读真机"
    asked = [(p["siid"], p["piid"]) for c in calls for p in c["params"]]
    assert asked == [(p.siid, p.piid) for p in params], "每个属性恰好被问一次,不重不漏"
    assert len(out) == 42
    assert all(r["code"] == 0 for r in out)


@pytest.mark.asyncio
async def test_batched_results_keep_request_order():
    """调用方按 iid 对号,分批不能打乱顺序。"""
    params = _params(20)
    client, _ = _make_client(_echo)

    out = await client.get_props_async(params, datasource=2)

    assert [(r["siid"], r["piid"]) for r in out] == [(p.siid, p.piid) for p in params]
    assert [r["value"] for r in out] == list(range(1, 21))


@pytest.mark.asyncio
async def test_one_failed_batch_does_not_take_the_others_down():
    """中间一批栽了,后面的批照发,而且那批的错误码**原样返回**。

    不吞掉、也不改写:只有云端真的返回过的码才算失败(#394),在本地合成或抹掉
    都会让调用方失去"这几个属性到底怎么了"的唯一线索。
    """
    params = _params(24)

    def responder(data: dict) -> dict:
        # 第 2 批(piid 9..16)整批超时
        if data["params"][0]["piid"] == 9:
            return _echo(data, code=_CLIFF_CODE)
        return _echo(data)

    client, calls = _make_client(responder)
    out = await client.get_props_async(params, datasource=2)

    assert len(calls) == 3, "中间那批失败不该让第 3 批发不出去"
    assert [r["code"] for r in out] == [0] * 8 + [_CLIFF_CODE] * 8 + [0] * 8
    assert len(out) == 24, "失败的批也要占位返回,不能悄悄少几行"
    assert all("updateTime" not in r for r in out[8:16]), "超时那批本来就没有 updateTime"


@pytest.mark.asyncio
async def test_cache_reads_are_not_split():
    """``ds=1`` 读云端缓存,没有这个悬崖(实测 15 个属性 61ms)。

    把它也拆细只会白白把面板那种全量冷查询变慢,还更容易撞上 MiOT 约 10 QPS 的限频。
    """
    client, calls = _make_client(_echo)

    await client.get_props_async(_params(150))

    assert len(calls) == 1
    assert calls[0]["datasource"] == 1
    assert len(calls[0]["params"]) == 150


@pytest.mark.asyncio
async def test_short_read_through_stays_a_single_request():
    """不到一批的点名查询不该平白多花一次往返。"""
    client, calls = _make_client(_echo)

    await client.get_props_async(_params(READ_THROUGH_MAX_PROPS_PER_REQ), datasource=2)

    assert len(calls) == 1


def test_chunk_size_stays_below_the_lowest_measured_cliff():
    """实测最低的一条悬崖在空调上:14 个属性成功,15 个整批返回 -704220043。

    悬崖位置因设备而异,而云端不给上限,所以这个值只能靠实测兜底 —— 谁要往上调,
    先去重测最慢的那台设备。
    """
    assert 1 <= READ_THROUGH_MAX_PROPS_PER_REQ <= 14
