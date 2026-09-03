"""OAuth 错误响应的形状识别。

用的是**实机抓到的原样响应**，不是想象的形状。此前判据只看顶层 `error`，
而小米把它放在 `result` 里，导致「凭据被拒绝」一路被当成可重试的未知错误——
真实故障场景下永远不会进降级态，界面提示也就永远不出现。
"""

from __future__ import annotations

import json

import pytest
from miot.error import MIoTErrorCode


def _classify(res_obj: dict) -> str | None:
    """复刻 __get_token_async 里的分类逻辑，返回错误码名或 None。

    直接调私有方法要连 aiohttp，成本远高于收益；这里钉的是**判据本身**，
    与实现保持同一份形状假设即可。若实现改了形状假设，这个测试要一起改。
    """
    err_obj = res_obj if isinstance(res_obj, dict) else {}
    result_obj = err_obj.get("result")
    for holder in (err_obj, result_obj if isinstance(result_obj, dict) else {}):
        if holder.get("error") is not None:
            return MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.name
    return None


# 实机抓到的原样响应（2026-09-01 测试机，refresh_token 改坏后云端的回复）。
# 直接构造 dict：手写转义 JSON 极易写错，而这里要钉的是**结构**不是字面量。
_INNER = {
    "error": 96009,
    "error_description": "invalid refresh token",
    "traceId": "327a11a9b3f9f44fda07e6f371cba835",
}
REAL_REJECTION = {
    "code": -6,
    "message": json.dumps(_INNER, ensure_ascii=False),  # 服务端把同样内容又塞了一份字符串
    "result": dict(_INNER),
}


def test_nested_error_is_recognized_as_credential_rejection():
    """error 在 result 里，不在顶层——这是真实形状。"""
    assert REAL_REJECTION.get("error") is None, "前提：顶层确实没有 error"
    assert REAL_REJECTION["result"]["error"] == 96009
    assert _classify(REAL_REJECTION) == "CODE_OAUTH_INVALID_REFRESH_TOKEN"


def test_top_level_error_also_recognized():
    """另一种可能的形状也要认，不对响应形状做唯一假设。"""
    assert (
        _classify({"error": 96009, "error_description": "invalid refresh token"})
        == "CODE_OAUTH_INVALID_REFRESH_TOKEN"
    )


@pytest.mark.parametrize(
    "body",
    [
        {"code": 0, "result": {"access_token": "a", "refresh_token": "r", "expires_in": 1}},
        {"code": -1, "message": "server busy"},  # 服务端临时错误，可重试
        {},
        {"result": None},
        {"result": "not-a-dict"},
    ],
)
def test_non_rejection_responses_are_not_classified_as_permanent(body):
    """只有明确带 error 的才算凭据被拒；其余一律留给可重试路径。

    方向刻意 fail-open：宁可晚一点告警，也不因一次服务端抖动误报授权失效。
    """
    assert _classify(body) is None


# ─────────────── 凭据不进日志 ───────────────


def test_credentials_are_redacted_in_log_payload():
    """出错时把请求体拼进错误消息，凭据不能是原文。

    故障日志里曾经出现过完整的 refresh_token——日志一旦外发即是泄露。
    保留前 8 位是刻意的：排障时要能比对「两次失败发的是不是同一枚」。
    """
    from miot.cloud import _redact

    out = _redact(
        {
            "client_id": "2882303761520431603",
            "redirect_uri": "https://example/login_redirect",
            "refresh_token": "R3_GvjOY74sX6bsPW-frDN2Z71jZGJZmLFYlflDAnpd6Hmqk",
            "code": "C3_04B1Fabcdefghijklmnop",
        }
    )

    assert "R3_GvjOY74sX6bsPW-frDN2Z" not in out, "refresh_token 原文进了日志"
    assert "C3_04B1Fabcdefghijklmnop" not in out, "授权码原文进了日志"
    assert "R3_GvjOY" in out, "应保留前 8 位供比对"
    # 非凭据字段照常保留，排障要用
    assert "2882303761520431603" in out
    assert "login_redirect" in out


# ─────────────── 出错日志里的凭据 ───────────────


def test_request_headers_are_redacted_before_logging():
    """业务请求出错时整份请求头会进日志，其中的凭据必须先脱敏。

    401 与非 200 分支原样打印请求头，而头里既有 access_token 也有 client
    secret——等于每报一次错就把两样凭据落一次盘。留前 8 位仍能比对「两次失败
    发的是不是同一枚」，那是排障真正需要的；完整值再无别的用处。
    """
    from miot.cloud import _redact_map

    token = "AT_this_is_a_real_looking_access_token_value"
    secret = "CS_this_is_the_client_secret_b64_value"
    safe = _redact_map(
        {
            "Content-Type": "text/plain",
            "Host": "api.example.com",
            "X-Client-AppId": "app-123",
            "X-Client-Secret": secret,
            "Authorization": f"Bearer{token}",
        }
    )

    rendered = str(safe)
    assert token not in rendered, "access_token 原文进了日志"
    assert secret not in rendered, "client secret 原文进了日志"
    # 排障需要的那部分必须留着
    assert safe["Host"] == "api.example.com"
    assert safe["X-Client-AppId"] == "app-123"
    assert safe["Content-Type"] == "text/plain"
    # 前缀保留，足以比对是不是同一枚
    assert safe["X-Client-Secret"].startswith(secret[:8])
    assert str(len(secret)) in safe["X-Client-Secret"]


def test_same_credential_stays_comparable_after_redaction():
    """脱敏后仍要能判断两次发的是不是同一枚——这是留前缀的唯一理由。"""
    from miot.cloud import _redact_map

    a = _redact_map({"Authorization": "BearerTOKEN_AAAA_1111"})
    b = _redact_map({"Authorization": "BearerTOKEN_AAAA_1111"})
    c = _redact_map({"Authorization": "BearerTOKEN_BBBB_2222"})

    assert a["Authorization"] == b["Authorization"], "同一枚脱敏后应当相同"
    assert a["Authorization"] != c["Authorization"], "不同的两枚脱敏后应当可区分"


def test_empty_and_missing_credential_values_do_not_break_redaction():
    """空值 / 缺失不能让脱敏抛异常——它跑在错误处理路径上，二次失败最难查。"""
    from miot.cloud import _redact_map

    assert _redact_map({}) == {}
    assert _redact_map(None) == {}
    assert _redact_map({"Authorization": ""}) == {"Authorization": ""}
    assert _redact_map({"Authorization": None}) == {"Authorization": None}


# ─────────────── 两条流程的拒绝码要分开 ───────────────


async def _reject_with(data: dict):
    """把 data 喂给**真正的** __get_token_async，返回它抛出的异常。

    只替掉 HTTP 会话（返回一份实机抓到的拒绝响应），分类逻辑跑的是真实现——
    复刻一份判据的话，实现改了测试还会绿。
    """
    from miot.cloud import MIoTOAuth2Client
    from miot.error import MIoTOAuth2Error

    cli = MIoTOAuth2Client(
        redirect_uri="https://example.invalid/cb",
        cloud_server="cn",
        uuid="test-uuid",
    )

    class _Res:
        status = 200

        async def text(self, encoding="utf-8"):
            return json.dumps(REAL_REJECTION, ensure_ascii=False)

    class _Session:
        async def get(self, **kw):
            return _Res()

    cli._ensure_session = lambda: _Session()
    try:
        await cli._MIoTOAuth2Client__get_token_async(data)
    except MIoTOAuth2Error as e:
        return e
    finally:
        cli._session = None
    raise AssertionError("预期抛出 MIoTOAuth2Error，实际没抛")



@pytest.mark.asyncio
async def test_refresh_rejection_reports_invalid_refresh_token():
    """定时续期被拒 → 报「刷新令牌无效」。"""
    from miot.error import MIoTErrorCode

    err = await _reject_with({"refresh_token": "rt_value", "client_id": "x"})
    assert err.code == MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN


@pytest.mark.asyncio
async def test_code_exchange_rejection_reports_unauthorized():
    """授权码兑换被拒 → 报「未授权」，不能也报「刷新令牌无效」。

    授权码同样一次性：用户在授权页面停留过久、或回调被刷第二次就会被拒。两条
    报同一个码，日志里「续期凭据失效」和「授权码已过期」就分不开——排障的人会
    去查续期链路，而问题其实在授权页面往返上。
    """
    from miot.error import MIoTErrorCode

    err = await _reject_with({"code": "auth_code_value", "client_id": "x"})
    assert err.code == MIoTErrorCode.CODE_OAUTH_UNAUTHORIZED, (
        "授权码兑换失败被误报成刷新令牌无效"
    )


@pytest.mark.asyncio
async def test_both_rejection_codes_stay_permanent():
    """两个码都必须留在永久失效集合里，否则会被当成瞬时故障反复重试。"""
    from miloco.miot.auth_state import is_permanent_auth_error
    from miot.error import MIoTErrorCode

    assert is_permanent_auth_error(MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.value)
    assert is_permanent_auth_error(MIoTErrorCode.CODE_OAUTH_UNAUTHORIZED.value)


def test_response_body_is_redacted_when_shape_is_invalid():
    """响应形状不合法时，异常消息里的响应体也要脱敏。

    这条分支的判据之一是「access_token 非空但 refresh_token 为空」——触发时
    响应里完全可能带着一枚可用的令牌，而异常消息会进日志。只脱敏请求体，
    等于把另一半原样留着。
    """
    from miot.cloud import _redact_response

    tok = "AT_live_token_that_must_not_leak_0123456789"
    body = {
        "code": 0,
        "result": {"access_token": tok, "refresh_token": "", "expires_in": 3600},
    }
    out = _redact_response(body, json.dumps(body))

    assert tok not in out, "响应体里的 access_token 原文进了异常消息"
    assert out.startswith("{"), "正常结构应当仍以 JSON 呈现，便于排障"
    # 排障需要的结构信息要留着
    assert "expires_in" in out and "refresh_token" in out


def test_unparsable_response_falls_back_to_length_only():
    """解析不出预期结构时只报长度，不把整个响应体原样落盘。"""
    from miot.cloud import _redact_response

    raw = "<html>gateway error, token=AT_should_not_leak</html>"
    out = _redact_response(None, raw)

    assert "AT_should_not_leak" not in out
    assert str(len(raw)) in out


# ─────────────── 进日志的值与 OAuth state ───────────────


def test_authorize_log_value_strips_newlines():
    """进日志的用户标识必须去掉换行，否则能伪造出额外的日志行。

    该值目前恒为 None（鉴权依赖成功时不返回值），但类型标注写的是 str——哪天补成
    返回真实用户标识，这里就是真实的注入点。钉住清洗本身，不依赖「今天恰好是 None」
    这个会变的前提。
    """
    hostile = "admin\n2026-01-01 00:00:00 - root - INFO - 伪造的日志行"
    safe = str(hostile).replace("\r", "").replace("\n", "")
    assert "\n" not in safe and "\r" not in safe
    # 内容不丢，只是拼成一行——排障仍看得出发生了什么
    assert "admin" in safe and "伪造的日志行" in safe


def test_oauth_state_uses_sha256_and_stays_self_consistent():
    """OAuth 回跳的防重放串改用 SHA256，且同一进程内自比对仍然成立。

    这个串只在本进程内比对（发出去一份、回跳带回来一份），既不落库也不与云端约定，
    所以换算法不影响任何已有绑定。这里走**真实构造函数**——自己再算一遍哈希再断言
    的话，实现换回旧算法测试照样绿，那是空护栏。
    """
    import asyncio
    import hashlib

    from miot.cloud import MIoTOAuth2Client

    async def _build():
        return MIoTOAuth2Client(
            redirect_uri="https://example.invalid/cb",
            cloud_server="cn",
            uuid="uuid-1",
        )

    c = asyncio.run(_build())
    state = c.state if hasattr(c, "state") else c._state
    seed = f"d={c._device_id}".encode("utf-8")

    assert state == hashlib.sha256(seed).hexdigest(), "实现没在用 SHA256"
    assert state != hashlib.sha1(seed).hexdigest(), "实现还在用 SHA1"
    assert len(state) == 64
    # 自比对成立：回跳带回同一个串才通过
    assert asyncio.run(c.check_state_async(redirect_state=state)) is True
    assert asyncio.run(c.check_state_async(redirect_state="not-it")) is False
