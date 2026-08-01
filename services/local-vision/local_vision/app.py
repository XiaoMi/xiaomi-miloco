"""local-vision 边车的 HTTP 门面。

契约刻意做得**与模型无关**:送一段视频 + 想问的话 + 要判定的规则,拿回场景
描述、逐条判定和门控概率。任何满足这个契约的实现都能替换 Mage-VL,miloco 侧
不需要改一行代码。

鉴权与 miloco 后端同构:配置了 token 就要求 ``Authorization: Bearer <token>``;
未配置则只监听环回地址(见 README),不做隐式放行。
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from local_vision.engine import MageVLEngine, resolve_checkpoint

logger = logging.getLogger(__name__)

_engine: MageVLEngine | None = None


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    _engine = MageVLEngine(
        checkpoint=resolve_checkpoint(_env("LOCAL_VISION_CHECKPOINT", "microsoft/Mage-VL")),
        device=_env("LOCAL_VISION_DEVICE", "cuda:0"),
        video_backend=_env("LOCAL_VISION_BACKEND", "codec"),
        num_frames=int(_env("LOCAL_VISION_NUM_FRAMES", "32")),
        max_pixels=int(_env("LOCAL_VISION_MAX_PIXELS", "150000")),
        attn_impl=_env("LOCAL_VISION_ATTN", "sdpa"),
    )
    # 权重加载要几十秒。放后台线程,让服务立刻开始应答 —— 否则 /health 在加载
    # 期间根本连不上,miloco 侧那条"正在加载模型"的等待态永远走不到,用户冷启动
    # 时只会看到"服务不可达",分不清是没装好还是在加载。
    threading.Thread(target=_load_engine, name="lv-load", daemon=True).start()
    yield
    _engine = None


def _load_engine() -> None:
    e = _engine
    if e is None:
        return
    try:
        e.load()
    except Exception:  # noqa: BLE001 —— 加载失败只让服务停在 loading 态,不崩进程
        logger.exception("model load failed; service stays unready")


app = FastAPI(title="miloco local-vision", version="0.1.0", lifespan=lifespan)

# 同时在飞的推理请求上限。单卡串行推理,排队本身没问题 —— 但 miloco 是**每台相机
# 每个窗口各发一个请求**,多相机部署下窗口比推理快时队列会无限涨:请求全都占着
# FastAPI 的线程池等 GPU 锁,池子占满后连 /health 都开始超时,miloco 于是判定
# 边车挂了、切进重连循环 —— 一个纯粹由排队引起的假故障。
# 超限直接回 503:让 miloco 把这一窗当"本轮没结论"跳过(它本来就会这么处理),
# 比让它等一个已经过期的答案好。
# 默认值必须 >= miloco 允许同时启用的摄像头数(MAX_ENABLED_CAMERAS=4)——miloco
# 每窗把所有相机并发发过来,上限低于相机数时**同一批**相机每次都抢不到槽位、
# 每窗 503,它们上的规则于是永久不被评估,而且静默。GPU 锁本来就串行化推理,
# 这个上限只是给排队封顶(4 × ~1.5s 远小于客户端 60s 超时)。
# 默认 5 = 相机上限(4)+ 1 —— 多出来的那个留给主动查询。miloco 的主动查询同样会
# 把所有相机并发发过来,如果上限正好等于相机数,一次用户提问只要撞上一个正在进行的
# 实时窗口就会全部 503,agent 那头拿到空答案。
_MAX_INFLIGHT = int(os.environ.get("LOCAL_VISION_MAX_INFLIGHT", "5"))
_inflight = threading.Semaphore(_MAX_INFLIGHT)


def require_token(request: Request) -> None:
    """配了 token 就强制校验;没配就不校验(此时服务必须只绑环回地址)。"""
    expected = os.environ.get("LOCAL_VISION_TOKEN", "")
    if not expected:
        return
    got = request.headers.get("authorization", "")
    # 常数时间比较:普通 != 会因短路而泄漏前缀匹配长度。
    if not hmac.compare_digest(got, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class RuleSpec(BaseModel):
    name: str = ""
    query: str = ""


class PerceiveRequest(BaseModel):
    video_b64: str = Field(description="视频段(mp4/h264)的 base64")
    scene_ask: str | None = Field(default=None, description="场景描述的提问;缺省用内置中文提问")
    rules: list[RuleSpec] = Field(default_factory=list, description="要逐条判定的规则")
    camera_note: str = Field(default="", description="该机位的自定义说明;作为补充,不取代任务提问")
    max_new_tokens: int = Field(default=256, ge=16, le=1024)
    want_gate: bool = Field(default=True, description="是否计算 StreamMind 门控概率")


class PerceiveResponse(BaseModel):
    caption: str
    rule_hits: list[dict]
    gate_p: float | None
    backend: str
    timing_ms: dict
    raw: str


@app.get("/health")
def health() -> dict:
    """无需鉴权,供 miloco 探活与「测试连接」使用。"""
    e = _engine
    return {
        "status": "ok" if (e and e.ready) else "loading",
        "model_loaded": bool(e and e.ready),
        "gate_available": bool(e and e.gate_available),
        # 门控熄灯的原因(如缺 mamba_ssm)直接暴露出来,免得排查时对着
        # gate_p=null 猜半天。
        "gate_error": e.gate_error if e else None,
        "device": e.device if e else None,
        "backend": e.video_backend if e else None,
    }


@app.post("/v1/perceive", response_model=PerceiveResponse, dependencies=[Depends(require_token)])
def perceive(body: PerceiveRequest) -> PerceiveResponse:
    e = _engine
    if e is None or not e.ready:
        raise HTTPException(status_code=503, detail="engine not ready")
    try:
        data = base64.b64decode(body.video_b64, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=422, detail=f"video_b64 is not valid base64: {err}") from err
    if not data:
        raise HTTPException(status_code=422, detail="empty video payload")

    if not _inflight.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="busy: too many in-flight inferences, retry next window",
        )

    # acquire 之后的一切都必须在 try 里:临时文件写失败(磁盘满 / /tmp 只读)
    # 若发生在 try 之外,槽位就永远收不回来,几次之后服务永久回 503,只能重启。
    path = None
    try:
        from local_vision.video import write_temp_video

        path = write_temp_video(data)
        out = e.perceive(
            str(path),
            rules=[r.model_dump() for r in body.rules],
            scene_ask=body.scene_ask,
            camera_note=body.camera_note,
            max_new_tokens=body.max_new_tokens,
            want_gate=body.want_gate,
        )
    except Exception as err:  # noqa: BLE001 —— 单次推理失败不该拖垮常驻服务
        logger.exception("perceive failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {err}") from err
    finally:
        _inflight.release()
        if path is not None:
            # 整段清理裹起来:/tmp 变只读或目录消失时,不该让一次**已经成功**的
            # 推理以未捕获异常收场。
            try:
                path.unlink(missing_ok=True)
                # codec 通路会在临时文件旁留下 cv-preinfer 的中间产物,一并清掉。
                for leftover in path.parent.glob(f"{path.stem}*"):
                    if leftover != path:
                        try:
                            leftover.unlink()
                        except OSError:
                            pass
            except OSError as e:
                logger.warning("temp cleanup failed for %s: %s", path, e)

    return PerceiveResponse(**out)
