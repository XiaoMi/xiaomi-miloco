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
import logging
import os
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
    _engine.load()
    yield
    _engine = None


app = FastAPI(title="miloco local-vision", version="0.1.0", lifespan=lifespan)


def require_token(request: Request) -> None:
    """配了 token 就强制校验;没配就不校验(此时服务必须只绑环回地址)。"""
    expected = os.environ.get("LOCAL_VISION_TOKEN", "")
    if not expected:
        return
    got = request.headers.get("authorization", "")
    if got != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid or missing token")


class RuleSpec(BaseModel):
    name: str = ""
    query: str = ""


class PerceiveRequest(BaseModel):
    video_b64: str = Field(description="视频段(mp4/h264)的 base64")
    scene_ask: str | None = Field(default=None, description="场景描述的提问;缺省用内置中文提问")
    rules: list[RuleSpec] = Field(default_factory=list, description="要逐条判定的规则")
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

    from local_vision.video import write_temp_video

    path = write_temp_video(data)
    try:
        out = e.perceive(
            str(path),
            rules=[r.model_dump() for r in body.rules],
            scene_ask=body.scene_ask,
            max_new_tokens=body.max_new_tokens,
            want_gate=body.want_gate,
        )
    except Exception as err:  # noqa: BLE001 —— 单次推理失败不该拖垮常驻服务
        logger.exception("perceive failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {err}") from err
    finally:
        path.unlink(missing_ok=True)
        # codec 通路会在临时文件旁留下 cv-preinfer 的中间产物,一并清掉。
        for leftover in path.parent.glob(f"{path.stem}*"):
            if leftover != path:
                try:
                    leftover.unlink()
                except OSError:
                    pass

    return PerceiveResponse(**out)
