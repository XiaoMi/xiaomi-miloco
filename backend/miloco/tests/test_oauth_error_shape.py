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
    """这份响应会不会被判成「凭据被拒绝」——**调的是生产判据**，不复刻。

    钉的是形状识别：``error`` 藏在 ``result`` 里的真实形状能不能认出来。至于
    「认出来之后报哪个错误码」由请求体决定（续期与授权码兑换报不同的码），那条
    分支另有用例从真实的换取入口驱动，不在这里重复。

    返回码名只是为了让下面的断言读起来贴近调用方看到的东西；**不要**在这里按
    请求体去挑码——那就又变成一份会和实现分叉的复刻件了。
    """
    from miot.cloud import find_oauth_error

    if find_oauth_error(res_obj) is None:
        return None
    return MIoTErrorCode.CODE_OAUTH_INVALID_REFRESH_TOKEN.name


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


def test_flat_shaped_response_is_redacted_at_the_top_level_too():
    """令牌直接躺在顶层时也要脱敏——「没有 result」正是走进这条分支的原因。

    形状识别那一侧刻意不假设 error 只在 result 里；脱敏这一侧同样不能假设令牌
    只在 result 里。扁平响应恰恰因为缺 result 才被判成形状不合法，此时只下探
    result 等于把两枚令牌原文写进日志。
    """
    from miot.cloud import _redact_response

    at = "AT_flat_live_token_must_not_leak_0123456789"
    rt = "RT_flat_live_token_must_not_leak_0123456789"
    body = {"code": 0, "access_token": at, "refresh_token": rt}
    out = _redact_response(body, json.dumps(body))

    assert at not in out, "顶层的 access_token 原文进了异常消息"
    assert rt not in out, "顶层的 refresh_token 原文进了异常消息"
    # 排障需要的结构信息要留着
    assert "access_token" in out and "refresh_token" in out


def test_early_return_branches_never_log_a_raw_response_body():
    """令牌与业务接口的早退分支不许把响应体原样写进日志。

    这几条分支（401、非 200）此前把上游响应原样打出来，而本文件另一处早已认定
    「那里面可能有一枚可用的令牌」。它们的请求侧都已脱敏，响应侧留着裸的，是同一
    条日志里一半脱一半不脱——下一个读代码的人会据此推断响应体不必脱。

    按语法树判而不按行文本匹配：同一个值有 ``await http_res.text(...)`` 与先取出
    再传两种写法，按行匹配只覆盖其中一种。
    """
    import ast
    import pathlib

    import miot.cloud as mod

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    lines = src.splitlines()

    leaked: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_LOGGER"
        ):
            continue

        def _bare(n: ast.AST) -> bool:
            """这棵子树里有没有「没过脱敏的响应体」。"""
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in (
                "_redact_body",
                "_redact_response",
                "_redact",
                "_redact_map",
            ):
                return False
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "text"
            ):
                return True
            return any(_bare(c) for c in ast.iter_child_nodes(n))

        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if _bare(arg):
                leaked.append(f"L{node.lineno}: {lines[node.lineno - 1].strip()[:56]}")

    assert not leaked, "这些日志把响应体原样写出去了:\n" + "\n".join(leaked)


def test_redact_body_masks_credentials_in_an_unparsed_response():
    """尚未解析的响应体也要脱敏，解析不出时退回只报长度。"""
    from miot.cloud import _redact_body

    tok = "AT_early_branch_token_must_not_leak_0123456789"
    assert tok not in _redact_body(json.dumps({"access_token": tok, "code": 0}))
    # 非 JSON（网关错误页）：不留原文，只报长度
    html = f"<html>gateway error, token={tok}</html>"
    out = _redact_body(html)
    assert tok not in out and str(len(html)) in out


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
    # 导入实现，不复刻它——自己再算一遍的话，实现被回退测试照样绿。
    from miloco.utils.logger import log_safe

    hostile = "admin\n2026-01-01 00:00:00 - root - INFO - 伪造的日志行"
    safe = log_safe(hostile)
    assert "\n" not in safe and "\r" not in safe
    # 内容不丢，只是拼成一行——排障仍看得出发生了什么
    assert "admin" in safe and "伪造的日志行" in safe
    # 恒为 None 的今天也不能抛
    assert log_safe(None) == "None"


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


def test_the_guarded_scope_is_not_limited_to_files_that_already_sanitize():
    """守护范围里必须有「一处都没清洗过」的文件，否则这条护栏的方向是反的。

    这是踩过两次才写下的反向断言。按「谁 import 了清洗函数就在范围内」发现时，一个
    文件必须**先做对**才会被检查，而最需要检查的恰恰是一处都没做的那些——新增一个
    只打日志、不清洗的接口文件，护栏永远全绿。改成「谁定义了接口」之后这个缺口没了，
    但那是个容易被「简化」掉的判据，所以在这里钉一下：范围一旦缩回「只扫已清洗的」，
    这条立刻变红。

    不重复实现发现逻辑，只断言它的这个性质。
    """
    import ast
    import pathlib

    unsanitized = [
        m
        for m in _guarded_modules()
        if not any(
            isinstance(n, ast.ImportFrom)
            and n.module == "miloco.utils.logger"
            and any(a.name in ("log_safe", "cam_tag") for a in n.names)
            for n in ast.walk(
                ast.parse(pathlib.Path(m.__file__).read_text(encoding="utf-8"))
            )
        )
    ]
    assert unsanitized, (
        "扫描范围里全是「已经在清洗」的文件——那意味着一个零清洗的新接口文件"
        "永远进不来，护栏对它永久失明"
    )


def test_no_route_parameter_reaches_the_log_unsanitized():
    """路由函数的形参不许裸传进日志——不看名单，按结构判。

    这条比名单那条更本质：**被路由装饰器修饰的函数，它的形参按定义就是请求**，
    路径参数、查询参数、请求体、鉴权依赖注入的调用者标识都在里面。名单要靠人去
    想「这个值算不算外部可控」，而这里不需要想——凡是从那一层进来的，一律先剥。

    也因此它逮得住名单逮不住的：``person_id`` ``pet_id`` ``keep_days`` 这些谁都
    不会主动加进名单的形参名。声明成整数、布尔的那几个由框架在进入函数体之前完成
    校验转换，带换行的请求会被直接拒回，但类型标注是运行期承诺而非静态保证——一并
    纳入，改类型时不必回头补。它们原本落在数值占位符上，而字符串清洗配数值位会让
    整行日志在运行期被整条吞掉，所以占位符一并改成了 ``%s``。
    """
    import ast
    import pathlib

    ROUTE_DECORATORS = {
        "get", "post", "put", "patch", "delete",
        "head", "options", "websocket", "api_route",
    }
    EMITTERS = {"debug", "info", "warning", "error", "exception", "critical"}
    WRAPPERS = {"log_safe", "cam_tag", "_cam_tag"}

    def _is_route(fn):
        return any(
            isinstance(d.func if isinstance(d, ast.Call) else d, ast.Attribute)
            and (d.func if isinstance(d, ast.Call) else d).attr in ROUTE_DECORATORS
            for d in getattr(fn, "decorator_list", [])
        )

    def _bare(node, params):
        """这个实参里有哪些「路由形参、却没被清洗」的值（清洗包装的子树整棵跳过）。"""
        found = []
        # ``len(...)`` 的子树也整棵跳过：长度是语言保证的整数，不可能带换行。这条
        # 跳过的依据是语言语义，不是「信任某个辅助函数」——后者需要逐个补行为断言
        # 才不是空护栏，前者不需要。
        SAFE_CALLS = WRAPPERS | {"len"}

        def visit(n):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in SAFE_CALLS
            ):
                return
            if isinstance(n, ast.Name) and n.id in params:
                found.append(n.id)
                return
            for c in ast.iter_child_nodes(n):
                visit(c)

        visit(node)
        return found

    leaked = []
    for mod in _guarded_modules():
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        lines = src.splitlines()
        short = mod.__name__.removeprefix("miloco.")
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not (
                isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_route(fn)
            ):
                continue
            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            for n in ast.walk(fn):
                if not (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in EMITTERS
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "logger"
                ):
                    continue
                args = list(n.args) + [kw.value for kw in n.keywords]
                for arg in args:
                    for name in _bare(arg, params):
                        leaked.append(
                            f"{short}.py:L{n.lineno} [{name}]: "
                            f"{lines[n.lineno - 1].strip()}"
                        )
    assert not leaked, (
        "这些日志调用直接用了路由函数的形参（= 请求携带的值），必须先剥换行：\n"
        + "\n".join(leaked)
    )


def test_no_unsanitized_value_reaches_the_log_in_that_router():
    """名单上的值不许裸传进这一组日志。

    守的是**一份显式名单**，不是「这个文件里所有该清洗的值」。名单之外还有别的值
    也裸传进日志（例如 WS 文本帧原文），它们在主干上即如此、本次未纳入，这条护栏
    也不管它们——要扩大守护范围就往名单里加，别指望它自动发现。

    名单按**来源**分四类，加值时对号入座。分类而不是逐值列理由，是因为这份说明是
    维护者判断「我这个新值该不该加进来」的唯一依据：护栏只会告诉你「名单上的值裸传
    了」，不会告诉你「这个值该不该上名单」；判断不出来的最可能结果是不加，而这条护
    栏的有效性完全建立在名单的完整度上。

    - **路径与查询参数**：经过 URL 解码，百分号编码的换行会还原成真换行，是今天就
      真的可控的那一档。声明成整数、布尔的那几个由框架在进入函数体之前完成校验转
      换，带换行的请求会被直接拒回，但类型标注是运行期承诺而非静态保证——一并纳入，
      改类型时不必回头补。
    - **请求体里的自由字符串**：只被约束非空、不限字符集。带服务凭据的调用方塞一个
      含换行的值进去，日志里就多出一整行看起来完全正常的记录，按行切的采集器分不出
      真假。
    - **云端回传后再进日志的值**：住户在米家 App 里自己起的设备名与房间名，本机无从
      约束。
    - **鉴权依赖注入的调用者标识**：今天恒为 ``None``（两条鉴权依赖成功时都不返回
      值），钉它是为了「将来补成返回真实身份」那天不必回头逐处补。

    判据按 **AST 看实参**，不按行文本匹配：同一个值在这个文件里有好几种写法
    （占位符写法不同、单参数与多参数、单行与折行），按行匹配只覆盖其中一种。
    位置实参与关键字实参都看，也包括第 0 个——``logger.info(f"user={x}")`` 这种
    写法会把值拼进格式串本身。
    """
    import ast
    import pathlib

    # 范围由证据推导：谁在用清洗函数，谁就在守护范围内。
    MODULES = _guarded_modules()

    def _bare_names(node: ast.expr) -> list[str]:
        """这个实参里有哪些「必须先清洗、却没被清洗」的值。

        **向下遍历整棵子树**，不只看顶层——``extra={"user": current_user}`` 会把值
        埋进字典、f-string 会把它埋进 ``JoinedStr``，只看顶层就全漏过去了。
        """
        # 要守的名单，按来源分类见上。往里加名字即可扩大守护范围。
        BARE_NAMES = {
            "current_user",
            "camera_id",
            "did",
            "scene_id",
            "channel",
            "refresh",
            "duration_ms",
            # 授权入口请求体里的自由字符串，且那一行打在校验它之前
            "state",
            # 与 did 同为路径 / 查询参数，且常与它打在同一行
            "iid",
            # 住户在米家 App 里自取的设备名与房间名，同样与 did 打在同一行
            "device_name",
            "room",
            # 规则的标识与来源设备号：建规则的调用方自由填，schema 只约束形态不
            # 约束字符集，而规则每命中一次失败就打一行
            "rule_id",
            "source_did",
        }
        DOTTED_NAMES = {"request.notify"}
        # 清洗包装：这些调用的整棵子树都算清洗过。合成「相机.通道」那个函数拼装
        # 时已经过 log_safe；两个名字是同一个函数——接口层起了下划线别名。
        WRAPPERS = {"log_safe", "cam_tag", "_cam_tag"}

        found: list[str] = []

        def visit(n: ast.AST) -> None:
            # 已被清洗包装包住的，整棵子树都算清洗过——不往下看。
            # 注意不能用 ast.walk + continue：那是平铺遍历，跳过调用节点本身
            # 之后它的实参照样会被访问到。
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in WRAPPERS
            ):
                return
            # 名单判定：裸名字，以及 `对象.属性` 形式
            if isinstance(n, ast.Name) and n.id in BARE_NAMES:
                found.append(n.id)
                return
            if isinstance(n, ast.Attribute):
                if isinstance(n.value, ast.Name):
                    dotted = f"{n.value.id}.{n.attr}"
                    if dotted in DOTTED_NAMES:
                        found.append(dotted)
                        return
                # 名单按**属性末段**认，不要求写全点分名：同一个值在这些文件里
                # 既有 `did` 的裸写法、也有 `action.did` / `camera_channel.did`
                # 的取属性写法，只认裸写法的话，换个容器装一下就绕过了护栏。
                if n.attr in BARE_NAMES:
                    found.append(f"*.{n.attr}")
                    return
            for child in ast.iter_child_nodes(n):
                visit(child)

        visit(node)
        return found

    leaked = []
    for mod in MODULES:
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        lines = src.splitlines()
        short = mod.__name__.removeprefix("miloco.")
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "logger"
            ):
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            for arg in args:
                for name in _bare_names(arg):
                    leaked.append(
                        f"{short}.py:L{node.lineno} [{name}]: "
                        f"{lines[node.lineno - 1].strip()}"
                    )

    assert not leaked, "这些日志调用还在直接用未清洗的值:\n" + "\n".join(leaked)


def test_cam_tag_actually_strips_newlines():
    """拼装函数自己必须真的剥换行——护栏是按**名字**信任它的。

    裸传护栏把 ``_cam_tag(...)`` 的整棵子树当成清洗过（相机标识与通道号在那里面
    是裸的）。这种按名字的信任只有在「名字背后的行为被钉住」时才成立：否则把它
    内部的清洗去掉，护栏照样全绿，而外部可控的相机标识就一路裸进日志了。
    """
    from miloco.utils.logger import cam_tag as _cam_tag

    hostile = "cam-1\n2026-01-01 00:00:00 - root - INFO - 伪造的日志行"
    out = _cam_tag(hostile, 0)

    assert "\n" not in out and "\r" not in out, "拼装结果里还有换行"
    # 内容不丢，只是拼成一行——排障仍看得出发生了什么
    assert "cam-1" in out and "伪造的日志行" in out
    assert out.endswith(".0"), "通道号仍要拼在后面"


def _guarded_modules():
    """护栏的扫描范围：**定义了接口的文件** ∪ **已经在用清洗函数的文件**。

    前一半是关键的一半，也是这条护栏改过两次才站住的地方。第一版把范围写成一份
    人工维护的模块清单，往新文件里加了清洗却忘了加进清单，护栏照样全绿。第二版
    改成「谁 import 了清洗函数就在范围内」，方向是反的——**一个文件必须先做对才
    会被检查，而最需要检查的恰恰是一处都没做的那些**：新增一个只打日志、不清洗的
    接口文件，护栏永远全绿。所以改为按「谁定义了接口」发现：请求携带的值就是从
    那一层进来的，那一层无条件进范围，与它有没有开始清洗无关。

    后一半是回退防护：一个文件一旦开始清洗，就不许再退回去，哪怕它不在接口层
    （规则执行器就是这样进来的——路径参数会下传到它那里再打日志）。

    **已知残留**：值从接口层继续往下传、在更深的层打日志的那些位置不在范围内
    （设备状态对齐、长连接监听、感知流水线等处）。那些位置的值多数来自云端而非
    请求，且要覆盖它们得先把清洗改成「在绑定处剥」而不是「在日志处包」——那是另
    一件事。范围写在这里，不写成「全仓都守住了」。
    """
    import ast
    import importlib
    import pathlib

    import miloco

    SANITIZERS = {"log_safe", "cam_tag"}
    # FastAPI 的路由装饰器；websocket 也算——它的路径参数同样经 URL 解码
    ROUTE_DECORATORS = {
        "get", "post", "put", "patch", "delete",
        "head", "options", "websocket", "api_route",
    }
    root = pathlib.Path(miloco.__file__).parent
    mods = []
    for f in sorted(root.rglob("*.py")):
        if "tests" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 仓库里不该有，有也不该让护栏挂掉
            continue
        # 判据按 AST 看，不按行文本匹配：这一条是踩过才写下的——按精确子串找导入
        # 语句时，给同一行加了第二个名字，那个文件当场静默掉出扫描范围。
        sanitizes = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "miloco.utils.logger"
            and any(a.name in SANITIZERS for a in n.names)
            for n in ast.walk(tree)
        )
        defines_routes = any(
            isinstance(d.func if isinstance(d, ast.Call) else d, ast.Attribute)
            and (d.func if isinstance(d, ast.Call) else d).attr in ROUTE_DECORATORS
            for n in ast.walk(tree)
            for d in getattr(n, "decorator_list", [])
        )
        if not (sanitizes or defines_routes):
            continue
        rel = f.relative_to(root).with_suffix("")
        mods.append(importlib.import_module("miloco." + ".".join(rel.parts)))
    assert mods, "一个模块都没发现——发现逻辑坏了，护栏会静默失效"
    return mods


def test_no_half_sanitized_log_call():
    """同一条日志里不许「一半脱一半不脱」。

    这是被反复挑出的形状：一行里设备标识包了、房间名没包，下一个读代码的人合理
    的推断是「房间名不需要包」，缺口就此固化。**按名字列危险值的护栏结构上抓不住
    它**——只认已经想到的名字，没想到的一律放过，所以每轮都能再冒出一个。

    这一条改成按**格式占位符**判：被 ``%s`` 承接的非常量实参都要过清洗（那是
    字符串位），被数值占位符承接的不必——那些值按契约就不是字符串，包成字符串
    反而会让整行日志在运行期被吞掉。

    只作用于「已经有实参过了清洗」的调用：那说明写它的人已经意识到这一行有外部
    值，漏下的就是缺口。完全没有清洗的调用不在此列，那属于范围问题，由上面那条
    显式名单守。
    """
    import ast
    import pathlib
    import re

    WRAPPERS = {
        "log_safe",
        "cam_tag",
        "_cam_tag",
        "_redact",
        "_redact_map",
        "_redact_response",
        "_redact_body",
    }
    SPEC = re.compile(r"%[-+ #0-9.*]*([a-zA-Z])")

    def _wrapped(a: ast.expr) -> bool:
        return (
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Name)
            and a.func.id in WRAPPERS
        )

    def _is_const(a: ast.expr) -> bool:
        """整棵子树里没有任何未清洗的名字/属性引用。"""
        if isinstance(a, ast.Constant):
            return True
        found: list[ast.AST] = []

        def visit(n: ast.AST) -> None:
            if _wrapped(n):
                return
            if isinstance(n, (ast.Name, ast.Attribute)):
                found.append(n)
                return
            for c in ast.iter_child_nodes(n):
                visit(c)

        visit(a)
        return not found

    bad: list[str] = []
    for mod in _guarded_modules():
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        lines = src.splitlines()
        short = mod.__name__.removeprefix("miloco.")
        for node in ast.walk(ast.parse(src)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            fmt = node.args[0].value
            if not isinstance(fmt, str):
                continue
            args = node.args[1:]
            if not any(_wrapped(a) for a in args):
                continue
            specs = SPEC.findall(fmt.replace("%%", ""))
            if len(specs) != len(args):
                continue  # 个数对不上由另一条护栏管
            for spec, arg in zip(specs, args):
                if spec == "s" and not _wrapped(arg) and not _is_const(arg):
                    bad.append(
                        f"{short}.py:L{node.lineno}: {lines[node.lineno - 1].strip()[:52]}"
                    )

    assert not bad, (
        "这些日志一半脱一半不脱——%s 位上还有未清洗的值:\n" + "\n".join(sorted(set(bad)))
    )


def test_log_format_placeholders_match_their_arguments():
    """每个日志调用的占位符个数必须与实参个数一致。

    这条守的是「改格式串时漏改实参」这一类。它值得单独存在，因为出事的方式是
    **静默**的：``%d`` 套上字符串会让 logging 在生产环境里吞掉错误、那条日志整行
    消失，而涉事的几条路径（录制片段、两个音视频 WebSocket 端点）没有测试覆盖，
    不会有任何用例变红。

    只检查格式串是字面量的调用；f-string 与预先拼好的消息不在此列（它们没有
    延迟格式化，本来也不会有这个问题）。
    """
    import ast
    import pathlib
    import re

    bad: list[str] = []
    for mod in _guarded_modules():
        src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
        short = mod.__name__.removeprefix("miloco.")
        for node in ast.walk(ast.parse(src)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            fmt = node.args[0].value
            if not isinstance(fmt, str):
                continue
            # %% 是转义的百分号，不占位
            holders = len(re.findall(r"%[-+ #0-9.]*[a-zA-Z]", fmt.replace("%%", "")))
            supplied = len(node.args) - 1
            if holders != supplied:
                bad.append(
                    f"{short}.py:L{node.lineno}: 占位 {holders} 个、实参 "
                    f"{supplied} 个 — {fmt[:56]!r}"
                )

    assert not bad, "日志格式串与实参个数不匹配（生产环境会静默丢掉这几行）:\n" + "\n".join(bad)


def test_a_successful_response_carrying_error_zero_is_not_a_rejection():
    """响应里带一个「没有错误」的错误字段，不能把一次成功的续期判成永久失效。

    归类拒绝用的判据是「那个键的值不是 None」，而用 0 表示「没有错误」是很常见的
    接口约定。这条判据若排在成功判定之前，一次**已经换回新令牌**的续期会被判成凭据
    失效——设备控制与感知全停、住户被要求重新扫码，而手上那对新令牌根本没被用上。
    先认成功、认不出来再归类失败，这条即成立。
    """
    import asyncio
    import json as _json

    from miot.cloud import MIoTOAuth2Client

    payload = {
        "code": 0,
        "error": 0,  # ← 「没有错误」，不是错误
        "result": {
            "access_token": "at-new",
            "refresh_token": "rt-new",
            "expires_in": 86400,
        },
    }

    class _Res:
        status = 200

        async def text(self, encoding="utf-8"):
            return _json.dumps(payload)

    class _Session:
        async def get(self, **kw):
            return _Res()

    async def _run():
        # 客户端要在事件循环内构造（它在 __init__ 里建锁）
        cli = MIoTOAuth2Client(
            redirect_uri="https://example.invalid/cb",
            cloud_server="cn",
            uuid="test-uuid",
        )
        cli._ensure_session = lambda: _Session()
        return await cli.refresh_access_token_async(refresh_token="rt-old")

    info = asyncio.run(_run())

    assert info is not None, "凑齐了可用令牌对就是成功，不该被归类成拒绝"
    assert info.access_token == "at-new"
    assert info.refresh_token == "rt-new"


def test_stripping_newlines_keeps_a_separator():
    """剥换行要换成空格，不能直接删掉。

    删掉会把「关灯\n晚安」拼成「关灯晚安」，与一个真就叫那个名字的任务在日志里再也
    分不开，按名字 grep 会互相串；多行的异常消息同样会被拼成一串没有分隔的文字。
    而「不让它伪造出额外整行」这个目的，换成空格一样达成——这条钉的就是「别为了
    省一个字符把两个名字粘起来」。
    """
    from miloco.utils.logger import log_safe

    out = log_safe("关灯\n晚安")
    assert "\n" not in out and "\r" not in out, "整行伪造仍然要挡住"
    assert out == "关灯 晚安", "换成空格，而不是删掉"
    assert log_safe("a\r\nb") == "a b", "回车换行一起来时也只留一个分隔"

