# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Admin controller
System status check interface
"""

import logging
import time
from typing import Literal

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
    if m.omni.label == label and m.omni.api_key:
        return m.omni.api_key
    for p in m.omni_profiles:
        if p.label == label and p.api_key:
            return p.api_key
    return ""


def _full_omni_payload() -> dict:
    """{active, profiles}：均 api_key 打码;profiles 标记哪套 active(按档案名 label 匹配)。"""
    m = get_settings().model
    active = m.omni
    return {
        "active": {
            "label": active.label,
            "model": active.model,
            "base_url": active.base_url,
            "api_key_masked": _mask_api_key(active.api_key),
            "has_key": bool(active.api_key),
        },
        "profiles": [
            {
                "label": p.label,
                "model": p.model,
                "base_url": p.base_url,
                "api_key_masked": _mask_api_key(p.api_key),
                "has_key": bool(p.api_key),
                "active": p.label == active.label,
            }
            for p in m.omni_profiles
        ],
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
    # activate=true 显式设为当前;或编辑的就是当前生效那套 → 同步刷新 active(改 key/model 即时生效)
    if body.activate or get_settings().model.omni.label == (orig or label):
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


@router.post(
    "/omni-config/delete",
    summary="删除一套已存档案(不影响当前生效配置)",
    response_model=NormalResponse,
)
def delete_omni_config(body: OmniSelectBody, current_user: str = Depends(verify_token)):
    label = body.label.strip()
    profiles = [p for p in _profiles_as_dicts() if p["label"] != label]
    update_shared_config(model={"omni_profiles": profiles})
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
            "message": "已连上且鉴权通过，但探测请求被模型拒绝（Key 大概率有效）",
        }
    return {"ok": False, "code": "http_error", "status": r.status_code, "message": f"HTTP {r.status_code}: {r.text[:160]}"}


async def _probe_omni(model: str, base_url: str, api_key: str) -> dict:
    """轻量探测：GET {base_url}/models 验证鉴权 + 可达性（零 token、与模型无关）。

    200→连接正常（顺带看模型在不在列表）；401/403→Key 无效；连接异常→Base URL 不可达；
    404/405（不支持 /models）→ 回退到极简 chat 探测。
    """
    base_url = base_url.rstrip("/")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "unreachable", "message": f"无法连接 Base URL（{type(e).__name__}）"}
    latency_ms = round((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        found = False
        try:
            ids = {m.get("id") for m in (r.json().get("data") or [])}
            found = model in ids
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "code": "ok_model_found" if found else "ok",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "连接正常，模型可用" if found else "连接正常",
        }
    if r.status_code in (401, 403):
        return {"ok": False, "code": "bad_key", "status": r.status_code, "message": "API Key 无效或无权限"}
    if r.status_code in (404, 405):
        return await _probe_chat(model, base_url, api_key)
    return {"ok": False, "code": "http_error", "status": r.status_code, "message": f"HTTP {r.status_code}: {r.text[:160]}"}


@router.post(
    "/omni-config/test",
    summary="测试 omni 配置连通性（GET /models 探测，不写库、不计用量）",
    response_model=NormalResponse,
)
async def test_omni_config(
    body: OmniTestBody, current_user: str = Depends(verify_token)
):
    """用表单值（缺省回退当前已保存配置）做一次轻量探测，返回 {ok, status, latency_ms, message}。"""
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
        "message": f"HTTP {r.status_code}: {r.text[:160]}",
    }


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
        return NormalResponse(
            code=0, message="ok", data={"ok": False, "code": "no_key", "models": [], "message": "未配置 API Key"}
        )
    return NormalResponse(code=0, message="ok", data=await _fetch_models(base_url, api_key))


# ---- 音视频质量预设 ----


class QualityPresetBody(BaseModel):
    preset: Literal["default", "high"]


@router.get(
    "/perception-quality",
    summary="获取音视频质量预设",
    response_model=NormalResponse,
)
async def get_perception_quality(current_user: str = Depends(verify_token)):
    """获取当前音视频质量预设及可用选项。"""
    from miloco.perception.quality import get_quality_params, list_presets

    settings = get_settings()
    current_preset = settings.perception.quality.preset
    params = get_quality_params(current_preset)
    return NormalResponse(
        code=0,
        message="ok",
        data={
            "current": current_preset,
            "params": {
                "video_short_edge": params.video_short_edge,
                "audio_sample_rate": params.audio_sample_rate,
                "camera_video_quality": "HIGH" if params.camera_video_quality == 3 else "LOW",
            },
            "presets": list_presets(),
        },
    )


@router.put(
    "/perception-quality",
    summary="设置音视频质量预设",
    response_model=NormalResponse,
)
async def set_perception_quality(
    body: QualityPresetBody,
    current_user: str = Depends(verify_token),
):
    """设置音视频质量预设，需重启服务生效。"""
    from miloco.perception.quality import get_quality_params, list_presets

    preset = body.preset.strip().lower()
    available = list_presets()
    if preset not in available:
        return NormalResponse(
            code=1001,
            message=f"无效预设: {preset}，可用: {list(available.keys())}",
            data=None,
        )

    # 写入 config.json（原子写 + 文件锁防并发）
    import fcntl
    import json
    import os
    import tempfile

    from miloco.utils.paths import miloco_home

    config_path = miloco_home() / "config.json"
    lock_path = config_path.with_suffix(".lock")

    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            config = {}
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)

            if "perception" not in config:
                config["perception"] = {}
            if "quality" not in config["perception"]:
                config["perception"]["quality"] = {}
            config["perception"]["quality"]["preset"] = preset

            # 原子写：先写临时文件再 rename
            fd, tmp_path = tempfile.mkstemp(
                dir=str(config_path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                os.rename(tmp_path, str(config_path))
            except BaseException:
                os.unlink(tmp_path)
                raise
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)

    params = get_quality_params(preset)
    return NormalResponse(
        code=0,
        message=f"已设置为 {preset}，重启服务后生效",
        data={
            "preset": preset,
            "params": {
                "video_short_edge": params.video_short_edge,
                "audio_sample_rate": params.audio_sample_rate,
                "camera_video_quality": "HIGH" if params.camera_video_quality == 3 else "LOW",
            },
            "restart_required": True,
        },
    )
