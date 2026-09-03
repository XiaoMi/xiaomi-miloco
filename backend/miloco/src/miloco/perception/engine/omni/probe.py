"""omni provider 连通性探测。

统一被 web preflight (admin/router.py) 与运行时 circuit_breaker HALF_OPEN 复用。
两阶段探测：GET /models 验鉴权+可达；再 max_tokens=1 chat 真校验模型。

返回统一形状 {ok, code, status?, latency_ms?, message}。探测类 code 与 spec §2 的集合一致;
``fetch_models`` 另有一个 ``list_unsupported``,不参与熔断、不属于该集合(见 error_classifier
的 docstring),别因为在那边找不到就补进去。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_ALLOWED_SCHEMES = ("http", "https")

# Gemini 原生 generateContent 协议的两条官方接入路径:
#   generativelanguage.googleapis.com   —— Gemini API
#   {区域前缀-}aiplatform.googleapis.com —— Vertex AI
# **两台主机下都是原生协议与 OpenAI 兼容协议并存**,主机名一样,只能靠 path 区分:
#   Gemini API  原生 版本段如 /v1beta      兼容 /v1beta/openai
#   Vertex      原生 路径含 /publishers/   兼容 路径含 /endpoints/openapi
# 兼容那条走 ``Authorization: Bearer``;误判成原生会给它发 x-goog-api-key,被拒后报成
# 「Key 无效」——正是本模块要消除的那类误报。
# 两侧策略**故意不同**:
#   Gemini API 侧用黑名单(只排除含 openai 段的路径)—— 原生的版本段会随 API 版本新增,白名单要跟着改;
#   Vertex 侧用白名单(只放行含 publishers 段的路径)—— 该主机路径形态多,黑名单挡不住未知的非原生路径。
_GEMINI_NATIVE_HOST = "generativelanguage.googleapis.com"
_GEMINI_OPENAI_PATH_SEG = "openai"
_VERTEX_HOSTS = frozenset({"aiplatform.googleapis.com"})
_VERTEX_HOST_SUFFIX = "-aiplatform.googleapis.com"
_VERTEX_NATIVE_PATH_SEG = "publishers"


def _parse_model_ids(payload: Any, *, prefer_native: bool) -> list[str]:
    """从模型列表响应里抽 model id,认识的几种形态都试。

    认识三种形态:Gemini 原生 ``{models:[{name}]}``、Vertex 的 ``{publisherModels:[{name}]}``
    (name 形如 ``publishers/google/models/xxx``)、OpenAI 兼容 ``{data:[{id}]}``。
    前缀归一**按形态各配一条**:两种原生形态各剥各的前缀,兼容形态的 id 原样透传
    (含 ``/`` 合法,如 ``accounts/<org>/models/<name>``,截断后 provider 认不出来)。
    ``prefer_native`` 只决定**先试哪一种**,不再是唯一依据:绑死单一形态时,端点的形态一旦判错
    (鉴权仍通过、只是响应形态与预期不符)就会解出空列表,却仍按 200 成功返回。

    产出保证:恒为字符串列表且元素均非空——调用方据此可直接排序,不必再过滤。顶层非 dict、
    列表位不是 list、条目不是 dict、标识是嵌套结构(dict/list)一律跳过而非抛出;标识为数字等
    标量时转成字符串。
    """
    if not isinstance(payload, dict):
        return []

    def _pick(key: str, field: str, norm: Callable[[str], str]) -> list[str]:
        items = payload.get(key)
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            raw = m.get(field)
            # 只对标量归一:标识为数字时转字符串,免得调用方排序混排类型抛异常。dict / list
            # 这类嵌套值 str() 之后是一串垃圾文本,会通过下面的非空过滤、被当成合法 model id
            # 收进下拉,故与其他异常形态一样跳过。bool 是 int 的子类,一并排除。
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            # 归一规则**按形态配**,不能写死一条套到三种上:兼容形态的 id 里含 ``/models/``
            # 是合法的(如 ``accounts/<org>/models/<name>``),截断后 provider 认不出来。
            mid = norm(str(raw))
            if mid:
                out.append(mid)
        return out

    native = [
        # Gemini 原生:name 形如 ``models/<id>``
        ("models", "name", lambda v: v.removeprefix("models/")),
        # Vertex:name 形如 ``publishers/<pub>/models/<id>``
        ("publisherModels", "name", lambda v: v.rsplit("/models/", 1)[-1]),
    ]
    # OpenAI 兼容:id 原样透传——含 ``/`` 是合法的,不做任何剥离
    compat = [("data", "id", lambda v: v)]
    for key, field, norm in (native + compat if prefer_native else compat + native):
        ids = _pick(key, field, norm)
        if ids:
            return ids
    return []


def _is_gemini_native_endpoint(base: str) -> bool:
    """按**主机名 + 路径**判是否 Gemini 原生 generateContent 端点。

    主机名取 ``urlparse().hostname`` 比对,不用 ``"…" in base_url`` 子串匹配——子串匹配会被
    ``https://evil.com/generativelanguage.googleapis.com`` 之类绕过(CodeQL 报的 incomplete
    URL substring sanitization)。两台主机都还要再过一道路径判断,用途见上方注释。
    """
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    # 按**路径段**比对而非子串:尾锚定会漏掉 ``…/openai/v1``(不少工具要求 base 以 /v1 结尾),
    # 裸子串又会把 ``…/myopenai`` 之类误判。两侧同一写法,不再一边尾锚定一边包含。
    # 与主机名一样做小写归一:URL 路径按 RFC 大小写敏感,但判定不该因大小写差异漏判——
    # 漏判的后果是给兼容端点发原生鉴权头,正是本模块要消除的那类误报。
    segments = parsed.path.lower().strip("/").split("/")
    if host == _GEMINI_NATIVE_HOST:
        return _GEMINI_OPENAI_PATH_SEG not in segments
    if host in _VERTEX_HOSTS or host.endswith(_VERTEX_HOST_SUFFIX):
        return _VERTEX_NATIVE_PATH_SEG in segments
    return False


def _normalize_base_url(base_url: str) -> tuple[str | None, str | None]:
    """校验 base_url 并归一化(去尾斜杠)。

    只挡非 http/https scheme(拒 file/gopher/ftp/data 等)。**不挡内网/链路本地 IP**——
    家用场景的自建 LLM (Ollama http://127.0.0.1:11434 / vLLM http://192.168.x.x:8000
    / Tailscale http://100.64.x.x) 就是常见 base_url,禁内网 = 禁自建。

    防"key 通过 base_url 外泄"靠的是 admin/router.py::_key_by_label 的跨 URL 凭证隔离
    (base_url 变了不沿用旧 key),不靠这里的 IP 黑名单。docstring 明说这点避免后续读者
    误以为这层做了 SSRF 防护。

    返回 (normalized, error_message);合法时 error 为 None。
    """
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            None,
            f"Base URL 协议非法（仅支持 http/https，实际: {scheme or 'empty'}）",
        )
    if not parsed.netloc:
        return None, "Base URL 缺少主机名"
    return base_url.rstrip("/"), None


async def probe_reachable(base_url: str) -> dict | None:
    """无 key 时判 Base URL 是否明显有问题;使 URL 错优先于「缺 key」暴露。

    - scheme 非法 / 网络错 → {code: unreachable, ...}
    - 2xx/3xx 或 401/403 → None(URL 没问题,问题在缺 key)
    - Gemini 原生端点的 404 → None(见下方注释)
    - 其他 4xx/5xx → {code: http_error, ...}

    **已知代价**:原生端点的 404 一律放行,而路径写错同样回 404,两者仅凭状态码分不开。
    于是同族主机上的路径笔误(如版本段敲多一个字母)在**未填 Key 阶段**会被上层的「缺 key」
    盖住,提示里一个字都不提地址。这是本函数契约的直接推论——它的既定职责就是让「缺 key」
    优先于地址错暴露,收紧就得牺牲那个契约。影响只到填 Key 之前:填上 Key 后走拉模型列表
    那条路,拿到的是并列「端点没有此方法」与「路径可能写错」两种可能的提示。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"code": "unreachable", "message": err}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models")
    except Exception as e:  # noqa: BLE001
        return {
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code < 400 or r.status_code in (401, 403):
        return None
    # Gemini 原生端点未必都有 models.list,404 只说明这条路径没有该方法,不代表
    # Base URL 写错;判 http_error 会让可用配置在「缺 key」之前先被报成地址错。
    if r.status_code == 404 and _is_gemini_native_endpoint(base):
        return None
    return {"code": "http_error", "message": f"服务返回异常（HTTP {r.status_code}）"}


async def fetch_models(base_url: str, api_key: str) -> dict[str, Any]:
    """拉取 provider 模型列表(GET /models)。

    模型下拉在「选定 model 之前」拉取,没有 model 可路由 adapter,故按 base_url 判 provider:
    Gemini 原生端点(见 ``_is_gemini_native_endpoint``)用 ``x-goog-api-key`` 鉴权;其余用
    ``Bearer``。响应形态不绑死判定结果——``_parse_model_ids`` 认识的几种形态都会试,判定只
    决定先试哪一种。
    (经代理转发的 Gemini 不含这些域名时,仍走 OpenAI 兼容分支——用户可手填 model 名兜底。)

    ``{models:[{name}]}`` 这个响应形态只在 Gemini API 那条路上验证过;生产在用的那条
    Vertex base_url 实测 GET /models 回 404,故走下方 ``list_unsupported`` 分支。

    状态码阶梯(本函数只在 api_key 非空时被调用,空 key 由 admin 侧转 ``probe_reachable``):

    ======  =================  ====================================================
    状态码  归类               依据
    ======  =================  ====================================================
    200     解析模型列表       各种响应形态都试,见 ``_parse_model_ids``
    404     list_unsupported   **仅原生端点**;其余一律 http_error——判据是端点级而非主机级,
                               故同主机下的兼容路径也走 http_error
    401/403 bad_key            坏 key / 受限 key 的典型返回,归「列不出来」会藏掉真问题
    其余    http_error         **含 400/429/5xx/3xx/非 200 的 2xx —— 见下方已知限制**
    ======  =================  ====================================================

    **400 不单独分类**:它既可能是 key 写错(``API_KEY_INVALID``),也可能与凭据无关
    (如出口 IP 地区不受支持的 ``FAILED_PRECONDITION``)。只凭数字码归进 Key 那一档,会把
    后者指到 API Key 字段、让用户反复重签一把有效凭据;要区分须读响应体的 ``error.status``。

    **已知限制**(改动前后一致,未收口):「其余」那一档统一走 http_error,前端据此挂到
    Base URL 字段——对 400/429/5xx/3xx 而言这个归因是错的。另外本函数按 base_url 判协议,
    而 ``provider.get_adapter`` 按 model 名判,两套判据对同一份配置可能得出不同结论。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "models": [], "message": err}
    is_gemini = _is_gemini_native_endpoint(base)
    headers = (
        {"x-goog-api-key": api_key}
        if is_gemini
        else {"Authorization": f"Bearer {api_key}"}
    )
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models", headers=headers)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "models": [],
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code == 200:
        # 解析与排序一起放进 try:响应体不是 JSON 时 r.json() 会抛,逃出去会让本接口 500。
        # 排序本身靠 _parse_model_ids「恒产非空字符串」的产出保证不抛,一并纳入只是防后续改动。
        try:
            models = sorted(_parse_model_ids(r.json(), prefer_native=is_gemini))
        except Exception:  # noqa: BLE001
            models = []
        return {"ok": True, "models": models}
    if r.status_code == 404 and is_gemini:
        # 无法仅凭 404 区分「该端点没有 models.list」与「Base URL 路径写错」,故文案并列
        # 两种可能,不把不确定说成确定。
        return {
            "ok": False,
            "code": "list_unsupported",
            "models": [],
            "message": "该 Base URL 未返回模型列表，请手动填写模型名（并确认 Base URL 路径无误）",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "models": [],
            "message": "API Key 无效或无权限",
        }
    return {
        "ok": False,
        "code": "http_error",
        "models": [],
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


class _FakeStatusResp:
    """占位:probe_chat 流式路径下,非 200 场景把 status_code 塞进"看起来像 httpx.Response"
    的最小对象里,复用下方 status_code 分支代码。仅用 status_code / json / text / headers
    四个属性。

    headers 包成 httpx.Headers 而非 plain dict:非流式路径的真实 Response.headers 是
    httpx.Headers(大小写不敏感);_probe_stream_chat 传上来的 dict(resp.headers) 已经把
    header 名小写化了(httpx 语义),若这里存成 plain dict,下方 429 分支的
    r.headers.get("Retry-After")(大写 R/A)会大小写敏感 miss → 恒 None,与非流式路径的
    大小写不敏感 hit 行为不一致,Qwen 撞 429 时会丢掉 server 明示的 Retry-After。
    """

    def __init__(self, status_code: int, json_body: dict, text: str, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = httpx.Headers(headers or {})

    def json(self) -> Any:
        return self._json


async def _probe_stream_chat(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    t0: float,
) -> tuple[int, int, bool, dict[str, str]]:
    """流式探测:开 SSE stream,读第一条 data 行就视为可达。返回
    (status_code, latency_ms, ok, resp_headers)。非 200 status 回带原始 response
    headers,让上层 429 分支能读 Retry-After —— 否则 forced-stream provider (Qwen)
    撞 429 时熔断退避会丢掉 server 明示的等待时长,与非流式路径 (MiMo) 行为不一致。"""
    async with client.stream(
        "POST", url, headers=headers, json=body,
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()  # 允许连接释放
            return resp.status_code, 0, False, dict(resp.headers)
        # 读到任一 data 行即算可达 (200 已经通过);不等 [DONE] 避免 max_tokens=1 下
        # provider 拖延 keep-alive 直到 _TIMEOUT。
        async for line in resp.aiter_lines():
            line = line.strip()
            if line.startswith("data: "):
                latency_ms = round((time.monotonic() - t0) * 1000)
                return 200, latency_ms, True, {}
        # 流开完无 data 行 → 视为 http_error(RECOVERABLE),返 500 让上层 http_error
        # 兜底分支处理(与 bad_response 都归 RECOVERABLE、cap 同为 _default 600s,
        # 运行时行为无差;此处选 http_error 是让 code 与状态码语义一致 —— 无 payload
        # 更像上游异常而非结构错)。
        return 500, 0, False, {}


async def probe_chat(model: str, base_url: str, api_key: str) -> dict[str, Any]:
    """极简 chat 探测(max_tokens=1)真校验模型是否可用。

    走 provider adapter 生成 body,兼容不同 provider 的强制要求(Qwen 强制
    stream=True + modalities=["text"])。之前硬编码非流式 body 打 Qwen 会被
    400/422 判成 rejected_authed,合法配置反而进 OPEN_CONFIG。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    # 延迟 import 避免 probe 被 wire 时循环拉起 provider (provider 只依赖标准库,
    # 但保险起见延后到函数内)。
    from miloco.perception.engine.omni.provider import get_adapter

    adapter = get_adapter(model)
    body = adapter.build_request_body(
        [{"role": "user", "content": "ping"}],
        model=model,
        max_tokens=1,
        temperature=0.0,
        top_p=1.0,
        stream=False,  # 请求非流式;adapter 若强制 stream=True (Qwen) 会覆盖
    )
    forced_stream = body.get("stream", False)
    url = adapter.endpoint(base, model, stream=forced_stream)
    # adapter.auth_headers 走 provider 特化 —— Gemini 用 ``x-goog-api-key`` 头,
    # OpenAI 兼容族用 ``Authorization: Bearer``。硬编码 Bearer 会对合法 Gemini
    # 配置误报失败(401)。
    headers = {
        **adapter.auth_headers(api_key),
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if forced_stream:
                # forced-stream provider (Qwen):走 SSE 流,读第一条有效 data chunk 就
                # 视为可达(不等 [DONE],避免 max_tokens=1 下 provider 拖延 keep-alive
                # 撑到超时)。任何一条 status_code / auth / model 错都会在开 stream 时
                # 直接抛,与非流式行为对齐。
                status_code, latency_ms, ok, resp_headers = await _probe_stream_chat(
                    client, url, headers, body, t0
                )
                if ok:
                    return {
                        "ok": True,
                        "code": "ok",
                        "status": status_code,
                        "latency_ms": latency_ms,
                        "message": "连接正常",
                    }
                # 非 200: 复用下方 status_code 分支;把 headers 一起塞进 _FakeStatusResp,
                # 429 分支能读 Retry-After,行为与非流式路径对齐。
                r = _FakeStatusResp(status_code, {}, "", resp_headers)
            else:
                r = await client.post(  # type: ignore[assignment]
                    url,
                    headers=headers,
                    json=body,
                )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    latency_ms = round((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        # status 200 不代表 body 合法:mock/中间层可能返 200 + 非 JSON。运行时 omni_client
        # 走 json.loads + 非 dict → bad_response,probe 需对齐,否则 mock/异常网关下 probe
        # 误判 ok,熔断状态被 record_probe_result(True) 复位 CLOSED,与真实调用行为背离。
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001 — 任何解码错都归 bad_response
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "omni 响应格式异常",
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "omni 响应格式异常",
            }
        return {
            "ok": True,
            "code": "ok",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "连接正常",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code == 404:
        return {
            "ok": False,
            "code": "not_found",
            "status": 404,
            "message": "模型或地址不存在",
        }
    if r.status_code in (400, 422):
        # OpenAI 兼容族此前有 GET /models 预检拦过 401/403，到这里 400 多为请求体/模型名被拒；
        # 但原生协议(Gemini)无预检、且部分 provider 对无效 key 就返回 400(而非 401/403)——
        # 故 400 也可能是 key 无效,无法仅凭 status code 区分,文案同时提示两种可能。
        return {
            "ok": False,
            "code": "rejected_authed",
            "status": r.status_code,
            "latency_ms": latency_ms,
            "message": "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
        }
    if r.status_code == 429:
        # 429 不加分支时会掉到 http_error 兜底,后果:上层用 http_error 走 _default cap
        # (600s)而非 rate_limited cap(60s),且丢 Retry-After header → backoff 无法尊重
        # server 明示的等待时长,可能过快复触发限流或过慢恢复。
        retry_after: float | None = None
        rah = r.headers.get("Retry-After")
        if rah:
            try:
                retry_after = float(rah)
            except ValueError:
                # HTTP-date 格式不解析,靠默认 backoff 兜底
                retry_after = None
        payload: dict[str, Any] = {
            "ok": False,
            "code": "rate_limited",
            "status": 429,
            "latency_ms": latency_ms,
            "message": "被 provider 限流",
        }
        if retry_after is not None:
            payload["retry_after_seconds"] = retry_after
        return payload
    return {
        "ok": False,
        "code": "http_error",
        "status": r.status_code,
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


async def probe_omni(model: str, base_url: str, api_key: str) -> dict[str, Any]:
    """两阶段探测:GET /models 预检 → 极简 chat 真校验。

    - GET /models 网络错 → unreachable
    - GET /models 401/403 → bad_key
    - GET /models 5xx → http_error
    - 其他(含 200 / 404 等) → 回退到 chat,以其结论为准

    非 OpenAI 兼容族(Gemini 等原生协议)没有等价的 GET /models 预检语义,直接走
    adapter 化的 chat 探测(``probe_chat`` 已按 provider 取 endpoint / 鉴权)。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    # 延迟 import 避免顶层循环依赖。
    from miloco.perception.engine.omni.provider import (
        OpenAICompatAdapter,
        get_adapter,
    )

    if not isinstance(get_adapter(model), OpenAICompatAdapter):
        return await probe_chat(model, base, api_key)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code >= 500:
        return {
            "ok": False,
            "code": "http_error",
            "status": r.status_code,
            "message": f"服务返回异常（HTTP {r.status_code}）",
        }
    return await probe_chat(model, base, api_key)
