# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Admin controller
System status check interface
"""

import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool

from miloco.admin import log_pack as _log_pack_mod
from miloco.config import get_settings
from miloco.database.token_usage_repo import get_token_usage_repo
from miloco.manager import get_manager
from miloco.middleware import verify_token
from miloco.observability import debug as debug_mod
from miloco.schema.common_schema import NormalResponse
from miloco.utils.agent_config import update_shared_config

logger = logging.getLogger(name=__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

manager = get_manager()


@router.get("/status", summary="System Status", response_model=NormalResponse)
async def get_system_status(current_user: str = Depends(verify_token)):
    """
    Check system component status:
    - MiOT: whether logged in with valid token
    - SQLite: whether database is accessible
    - Perception model: whether a vision_understanding model is activated
    - Rule engine: whether running and how many rules are loaded
    """
    logger.info("Get system status API called - User: %s", current_user)

    # MiOT login status
    try:
        miot_ok = await manager.miot_proxy.check_token_valid()
    except Exception:
        miot_ok = False

    # SQLite status
    try:
        rule_service = manager.rule_service
        total_rules = rule_service._repo.count_all()
        enabled_rules = rule_service._repo.count_enabled()
        sqlite_ok = True
    except Exception:
        total_rules = 0
        enabled_rules = 0
        sqlite_ok = False

    # Perception status
    try:
        perception_status = manager.perception_service.engine_status()
        perception_ok = perception_status.running
    except Exception:
        perception_ok = False

    data = {
        "miot": {"ok": miot_ok},
        "sqlite": {"ok": sqlite_ok},
        "perception": {"ok": perception_ok},
        "rule_engine": {
            "total_rules": total_rules,
            "enabled_rules": enabled_rules,
        },
    }

    logger.info("System status retrieved: %s", data)
    return NormalResponse(
        code=0, message="System status retrieved successfully", data=data
    )


@router.get(
    "/token-usage",
    summary="Token Usage (raw events in [since, until])",
    response_model=NormalResponse,
)
async def get_token_usage(
    since: int | None = None,
    until: int | None = None,
    limit: int = 10000,
    current_user: str = Depends(verify_token),
):
    """Raw token-usage events in [since, until] (ms epoch). Defaults to today.

    ``limit`` caps the response size; ``truncated=true`` in the payload tells
    the client to narrow the window if the cap is hit. Up to ~3 days of data
    is queryable (older events have been rolled up to /token-usage/daily).
    """
    events, truncated = get_token_usage_repo().list_events(since, until, limit)
    return NormalResponse(
        code=0,
        message="ok",
        data={"events": events, "total": len(events), "truncated": truncated},
    )


@router.get(
    "/token-usage/daily",
    summary="Token Usage (daily rollup by date / model / type)",
    response_model=NormalResponse,
)
async def get_token_usage_daily(
    since: str | None = None,
    until: str | None = None,
    current_user: str = Depends(verify_token),
):
    """Daily rollup rows (date / model / type) combining historical + today's live."""
    rows = get_token_usage_repo().aggregate_daily(since, until)
    return NormalResponse(
        code=0, message="ok", data={"rows": rows, "total": len(rows)}
    )


@router.get(
    "/token-usage/buckets",
    summary="Token Usage (today, server-side bucketed by time / model / type)",
    response_model=NormalResponse,
)
async def get_token_usage_buckets(
    since: int | None = None,
    until: int | None = None,
    bin_minutes: int = Query(60, alias="bin", ge=1),
    current_user: str = Depends(verify_token),
):
    """Server-side bucketed aggregation for the "today" view (ms epoch window).

    ``bin`` is the bucket width in minutes. Response size is bounded by bucket
    count, so it never hits the raw-event cap regardless of activity — preferred
    over /token-usage for the today timeline.
    """
    rows = get_token_usage_repo().aggregate_buckets(since, until, bin_minutes)
    return NormalResponse(
        code=0, message="ok", data={"rows": rows, "total": len(rows)}
    )


@router.post(
    "/token-usage/clear",
    summary="清空全部 Token 用量(实时表 + 日聚合，不可恢复)",
    response_model=NormalResponse,
)
def clear_token_usage(current_user: str = Depends(verify_token)):
    """删除 token_usage + token_usage_daily 全部行，返回各表删除条数。供重置统计用。"""
    deleted = get_token_usage_repo().clear_all()
    return NormalResponse(code=0, message="ok", data={"deleted": deleted})


# ─── debug 开关(同步 runtime override + .debug_observability 文件 flag) ────────


class DebugOverrideBody(BaseModel):
    enabled: StrictBool


@router.get("/debug", summary="Debug 开关状态", response_model=NormalResponse)
def get_debug_state(current_user: str = Depends(verify_token)):
    """返回 omni log debug 开关的当前状态。

    解析顺序: runtime override > 文件 flag > 默认 False。
    """
    return NormalResponse(code=0, message="ok", data=debug_mod.get_state())


@router.post(
    "/debug",
    summary="设置 Debug 开关(同步 runtime override + 文件 flag)",
    response_model=NormalResponse,
)
def set_debug_override(
    body: DebugOverrideBody, current_user: str = Depends(verify_token)
):
    """``enabled=true`` 开启并创建 .debug_observability;
    ``enabled=false`` 关闭并删除文件。重启后从文件 flag 恢复状态。

    每次调用无条件触发 ``omni_log.flush()``,保证 buffer 落盘。
    """
    debug_mod.set_runtime_override(body.enabled)
    return NormalResponse(code=0, message="ok", data=debug_mod.get_state())


@router.post(
    "/debug/log-pack",
    summary="打包 trace db / jsonl / log 到 $MILOCO_HOME/packs/",
    response_model=NormalResponse,
)
def post_log_pack(current_user: str = Depends(verify_token)):
    """LRU 保留最新 2 个;预扫描超 500MB 返 422 + 各组件 size 明细。"""
    try:
        result = _log_pack_mod.build_log_pack()
    except _log_pack_mod.LogPackSizeExceeded as e:
        raise HTTPException(status_code=422, detail=e.info)
    return NormalResponse(code=0, message="ok", data=result)


# ─── omni 模型配置(在「模型」页内读/写) ─────────────────────────────────────


def _mask_api_key(key: str) -> str:
    """打码 api_key:只回前 3 + … + 后 4 位,既能确认"配了哪把 key"又不泄漏全文。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "…" + key[-2:]
    return f"{key[:3]}…{key[-4:]}"


def _key_by_label(label: str, provided: str | None) -> str:
    """provided 非空用它;否则取该 label 档案(或当前生效配置)已存的 key。"""
    if provided and provided.strip():
        return provided.strip()
    if not label:
        return ""
    m = get_settings().model
    # 命中当前生效配置(含 label 为空、按展示 label 合成的「当前生效行」)→ 回退其 key。
    if m.omni.api_key and label in (m.omni.label, _active_display_label()):
        return m.omni.api_key
    for p in m.omni_profiles:
        if p.label == label and p.api_key:
            return p.api_key
    return ""


def _active_display_label() -> str:
    """当前生效配置用于「列表展示 / 编辑 / 删除」的稳定 label。

    omni.label 可能为空(env 或手改 config.json 直填 key、未走 web 档案流程的态),此时
    回退为 ``model @ base_url`` —— 与前端档案命名一致,保证合成的「当前生效行」label 非空,
    可被编辑 / 测试 / 删除按 label 正确定位(否则空 label 会使 upsert 报 400、删除 was_active
    误判为 False 而静默无效)。仅在有 key 时有展示意义。
    """
    m = get_settings().model.omni
    return m.label or f"{m.model} @ {m.base_url}"


def _full_omni_payload() -> dict:
    """{active, profiles}：均 api_key 打码;profiles 标记哪套 active(按档案名 label 匹配)。

    当前生效配置(active)并不一定已存档进 omni_profiles —— 默认状态(omni_profiles 为空、
    omni 是默认 MiMo)或历史遗留场景下,active 不在档案列表里。此时若直接返回 profiles,
    前端列表就看不到「当前生效模型」(只有折叠态标题栏读 active 能看到),造成「配没配好」
    的困惑。故在 active 未出现在档案列表时,把它作为一条合成档案补到列表头部(标 active)。
    """
    m = get_settings().model
    active = m.omni
    profiles = [
        {
            "label": p.label,
            "model": p.model,
            "base_url": p.base_url,
            "api_key_masked": _mask_api_key(p.api_key),
            "has_key": bool(p.api_key),
            "active": p.label == active.label,
        }
        for p in m.omni_profiles
    ]
    # 仅当 active 真有 key(确有模型在跑)、且未出现在档案列表时才合成补入并标 active。
    # 无 key 态(出厂未配 / 删当前生效后回到未配)不合成 —— 列表呈现为空 + 顶部「未配 key」
    # 警告,清楚表达「没有模型在跑」,而不是显示一条诡异的无 key 行。
    if active.api_key and not any(p["active"] for p in profiles):
        profiles.insert(0, {
            "label": _active_display_label(),  # 空 label 回退 model@base_url,保证可编辑/删除
            "model": active.model,
            "base_url": active.base_url,
            "api_key_masked": _mask_api_key(active.api_key),
            "has_key": True,
            "active": True,
        })
    return {
        "active": {
            "label": active.label,
            "model": active.model,
            "base_url": active.base_url,
            "api_key_masked": _mask_api_key(active.api_key),
            "has_key": bool(active.api_key),
        },
        "profiles": profiles,
    }


def _profiles_as_dicts() -> list[dict]:
    return [
        {"label": p.label, "model": p.model, "base_url": p.base_url, "api_key": p.api_key}
        for p in get_settings().model.omni_profiles
    ]


class OmniConfigBody(BaseModel):
    label: str  # 档案名 = 唯一 id(非空);base_url/api_key/model 都是它的可改属性
    base_url: str
    model: str
    api_key: str | None = None  # 留空 = 沿用该档案原 key(不被打码值覆盖)
    original_label: str | None = None  # 正在编辑的档案原名(支持改名/定位);None=新增
    activate: bool = True  # True=同时设为当前生效;False=只入列表(激活由 /activate 负责)


class OmniSelectBody(BaseModel):
    """按档案名(label)定位一套档案。"""

    label: str


@router.get(
    "/omni-config",
    summary="读取 omni 配置(当前生效 active + 已存档案 profiles，api_key 打码)",
    response_model=NormalResponse,
)
def get_omni_config(current_user: str = Depends(verify_token)):
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.put(
    "/omni-config",
    summary="保存一套 omni 配置(upsert 档案;activate=true 时设为当前，默认 true)",
    response_model=NormalResponse,
)
def put_omni_config(body: OmniConfigBody, current_user: str = Depends(verify_token)):
    """保存(新增/更新)一套档案到列表。

    - 档案名(label)= 唯一 id,非空;base_url / api_key / model 均为该档案可改属性。
    - ``original_label`` 标识正在编辑的档案(支持改名);为空表示新增。
    - ``api_key`` 留空 = 沿用该档案原 key(不被打码值覆盖)。
    - 重名(label 与"别的"档案相同)→ 409。
    - ``activate``=true(默认)同时设为当前生效;false 只入列表、不切换当前(激活走
      ``/activate``,即列表的「启用」)。但**正在编辑的就是当前生效那套时**,无论 activate
      与否都同步刷新 ``model.omni``,使改 key/model 即时对运行中的感知生效。
    - 写 config.json,感知下个推理周期热生效。env ``MILOCO_MODEL__OMNI__*`` 优先级更高会盖过。
    """
    label = body.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="档案名不能为空")
    base_url = body.base_url.strip()
    model = body.model.strip()
    orig = (body.original_label or "").strip()
    profiles = _profiles_as_dicts()
    target = next((p for p in profiles if p["label"] == orig), None) if orig else None
    clash = next((p for p in profiles if p["label"] == label and p is not target), None)
    if clash:
        raise HTTPException(status_code=409, detail=f"档案名「{label}」已存在")
    key = _key_by_label(orig or label, body.api_key)
    entry = {"label": label, "base_url": base_url, "model": model, "api_key": key}
    if target:
        profiles[profiles.index(target)] = entry
    else:
        profiles.append(entry)
    update: dict = {"omni_profiles": profiles}
    # activate=true 显式设为当前;或编辑的就是当前生效那套(含空 label 当前生效的合成展示 label)→
    # 同步刷新 active(改 key/model 即时生效)。复用 _label_is_active,与删除/停用同一处判定。
    # tgt 由非空 label 或 orig 组成,恒非空,故 _label_is_active 的 bool 守卫不影响语义。
    tgt = orig or label
    if body.activate or _label_is_active(tgt):
        update["omni"] = entry
    update_shared_config(model=update)
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.post(
    "/omni-config/activate",
    summary="切换当前生效配置为某套已存档案",
    response_model=NormalResponse,
)
def activate_omni_config(body: OmniSelectBody, current_user: str = Depends(verify_token)):
    label = body.label.strip()
    for p in get_settings().model.omni_profiles:
        if p.label == label:
            update_shared_config(
                model={
                    "omni": {
                        "label": p.label,
                        "model": p.model,
                        "base_url": p.base_url,
                        "api_key": p.api_key,
                    }
                }
            )
            return NormalResponse(code=0, message="ok", data=_full_omni_payload())
    raise HTTPException(status_code=404, detail="档案不存在")


def _label_is_active(label: str) -> bool:
    """label 是否指向当前生效配置(含空 label 当前生效的合成展示 label)。"""
    omni = get_settings().model.omni
    return bool(label) and (
        label == omni.label or (bool(omni.api_key) and label == _active_display_label())
    )


async def _soft_stop_best_effort(action: str) -> None:
    """重置当前生效配置后软停感知:关引擎 + 降回 no_omni_api_key,保留 tick 自愈循环。
    best-effort —— 配置落盘是主操作,软停失败仅告警(下次后端重启生效),不阻断整体。"""
    try:
        await manager.perception_service.stop_to_unconfigured()
    except Exception as e:  # noqa: BLE001
        logger.warning("%s当前生效模型后软停感知失败(将于重启后生效): %s", action, e)


@router.post(
    "/omni-config/delete",
    summary="删除一套已存档案;删的是当前生效那套时,回到「未配模型」态并软停感知",
    response_model=NormalResponse,
)
async def delete_omni_config(body: OmniSelectBody, current_user: str = Depends(verify_token)):
    """删除一套档案。删的若是当前生效模型,则把当前生效配置重置为「未配」(清空 key)并软停
    感知 —— 等价于回到初始未配模型态:感知停下,等重新配置并启用模型后由 tick 自愈自动拉起。
    """
    from miloco.config.settings import OmniModelSettings

    label = body.label.strip()
    was_active = _label_is_active(label)
    profiles = [p for p in _profiles_as_dicts() if p["label"] != label]
    update: dict = {"omni_profiles": profiles}
    if was_active:
        # 删当前生效模型 → 当前生效配置重置为出厂未配态(MiMo 默认 + 空 key)。
        update["omni"] = OmniModelSettings().model_dump()
    update_shared_config(model=update)
    if was_active:
        await _soft_stop_best_effort("删除")
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


@router.post(
    "/omni-config/deactivate",
    summary="停用当前生效模型:回到「未配模型」态并软停感知,但保留所有档案(可再启用)",
    response_model=NormalResponse,
)
async def deactivate_omni_config(body: OmniSelectBody, current_user: str = Depends(verify_token)):
    """停用当前生效模型:当前生效配置重置为「未配」(清空 key)+ 软停感知,但**不删除档案**。
    与 delete 的区别:delete 会移除该档案,deactivate 仅停用、档案保留,可随后再「启用」恢复。
    """
    from miloco.config.settings import OmniModelSettings

    if _label_is_active(body.label.strip()):
        update_shared_config(model={"omni": OmniModelSettings().model_dump()})
        await _soft_stop_best_effort("停用")
    return NormalResponse(code=0, message="ok", data=_full_omni_payload())


class OmniTestBody(BaseModel):
    # 皆可省略 —— 省略则回退当前生效配置;无 key 时按 label 取该档案已存 key。
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    label: str | None = None


async def _probe_chat(model: str, base_url: str, api_key: str) -> dict:
    """回退探测：少数服务不支持 GET /models 时，发一次极简非流式 chat（自测本次耗时）。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "unreachable", "message": f"无法连接 Base URL（{type(e).__name__}）"}
    latency_ms = round((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        return {"ok": True, "code": "ok", "status": 200, "latency_ms": latency_ms, "message": "连接正常"}
    if r.status_code in (401, 403):
        return {"ok": False, "code": "bad_key", "status": r.status_code, "message": "API Key 无效或无权限"}
    if r.status_code == 404:
        return {"ok": False, "code": "not_found", "status": 404, "message": "模型或地址不存在"}
    if r.status_code in (400, 422):
        # 鉴权已过、仅请求体被该模型拒（如只支持流式）→ Key 大概率有效。
        return {
            "ok": False,
            "code": "rejected_authed",
            "status": r.status_code,
            "latency_ms": latency_ms,
            "message": "已连接，但拒绝了模型请求（模型名可能错误）",
        }
    return {"ok": False, "code": "http_error", "status": r.status_code, "message": f"服务返回异常（HTTP {r.status_code}）"}


async def _probe_omni(model: str, base_url: str, api_key: str) -> dict:
    """验证「这套配置能否真正调用该模型」。

    先 GET /models 做廉价的鉴权 + 可达性预检(快速失败、省 token):连不上→unreachable,
    401/403→bad_key,5xx→http_error。预检通过后,用一次极简 chat(``max_tokens=1``)**真正
    探测该模型是否可用**——不依赖「模型是否出现在 /models 列表」这种弱判据(列表常不全,
    在不在列表都不等于能不能调通)。chat 探测结果(ok / not_found / rejected_authed / …)即最终结论。
    """
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "unreachable", "message": f"无法连接 Base URL（{type(e).__name__}）"}
    if r.status_code in (401, 403):
        return {"ok": False, "code": "bad_key", "status": r.status_code, "message": "API Key 无效或无权限"}
    if r.status_code >= 500:
        return {"ok": False, "code": "http_error", "status": r.status_code, "message": f"服务返回异常（HTTP {r.status_code}）"}
    # 鉴权/可达性 OK → 用极简 chat 真正验证该模型(在不在 /models 列表都以此为准)。
    return await _probe_chat(model, base, api_key)


@router.post(
    "/omni-config/test",
    summary="测试 omni 配置连通性（鉴权/可达预检 + 极简 chat 真校验，max_tokens=1 极少量 token，不写库、不计入 miloco 用量统计）",
    response_model=NormalResponse,
)
async def test_omni_config(
    body: OmniTestBody, current_user: str = Depends(verify_token)
):
    """用表单值（缺省回退当前已保存配置）做两阶段探测：先 GET /models 验鉴权/可达，再发一次
    max_tokens=1 的极简 chat 真正验证该模型可用（消耗极少量 token，不计入 miloco 用量统计）。
    返回 {ok, code, status, latency_ms, message}。"""
    omni = get_settings().model.omni
    model = (body.model or omni.model).strip()
    base_url = (body.base_url or omni.base_url).strip()
    api_key = _key_by_label((body.label or omni.label or "").strip(), body.api_key)
    if not api_key:
        return NormalResponse(
            code=0,
            message="ok",
            data={"ok": False, "code": "no_key", "message": "未配置 API Key"},
        )
    result = await _probe_omni(model, base_url, api_key)
    return NormalResponse(code=0, message="ok", data=result)


async def _fetch_models(base_url: str, api_key: str) -> dict:
    """拉取 provider 模型列表(GET /models)。成功返回 {ok, models:[id...]}。"""
    base_url = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "unreachable", "models": [], "message": f"无法连接 Base URL（{type(e).__name__}）"}
    if r.status_code == 200:
        try:
            ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:  # noqa: BLE001
            ids = []
        return {"ok": True, "models": sorted(ids)}
    if r.status_code in (401, 403):
        return {"ok": False, "code": "bad_key", "models": [], "message": "API Key 无效或无权限"}
    return {
        "ok": False,
        "code": "http_error",
        "models": [],
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


async def _probe_reachable(base_url: str) -> dict | None:
    """无 key 时判 Base URL 是否「明显有问题」,使 URL 错优先于「缺 key」暴露(而非被短路成「未配置」)。

    - 连接失败(DNS/拒连/超时/URL 非法)→ unreachable
    - 2xx/3xx,或 401/403(地址对、只是需要 key)→ None(URL 没问题,问题在缺 key)
    - 其余(404/405/4xx/5xx,如填错地址命中 openresty 404 页)→ http_error(地址/端点大概率不对)
    """
    url = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{url}/models")
    except Exception as e:  # noqa: BLE001
        return {"code": "unreachable", "message": f"无法连接 Base URL（{type(e).__name__}）"}
    if r.status_code < 400 or r.status_code in (401, 403):
        return None
    return {"code": "http_error", "message": f"服务返回异常（HTTP {r.status_code}）"}


class OmniModelsBody(BaseModel):
    base_url: str
    api_key: str | None = None
    label: str | None = None


@router.post(
    "/omni-config/models",
    summary="拉取某 Base URL 下可用模型列表(供模型下拉)",
    response_model=NormalResponse,
)
async def list_omni_models(
    body: OmniModelsBody, current_user: str = Depends(verify_token)
):
    """用 base_url + key(留空则按 label 取该档案已存 key)请求 GET /models,返回模型 id 列表。"""
    base_url = body.base_url.strip()
    api_key = _key_by_label((body.label or "").strip(), body.api_key)
    if not api_key:
        # URL 本身错优先于「缺 key」暴露:无 key 时先探可达性,连不上→报 URL 错;能连上才报缺 key。
        reach = await _probe_reachable(base_url)
        if reach is not None:
            return NormalResponse(
                code=0, message="ok",
                data={"ok": False, "code": reach["code"], "models": [], "message": reach["message"]},
            )
        return NormalResponse(
            code=0, message="ok", data={"ok": False, "code": "no_key", "models": [], "message": "未配置 API Key"}
        )
    return NormalResponse(code=0, message="ok", data=await _fetch_models(base_url, api_key))
