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

from local_vision.engine import MageVLEngine

logger = logging.getLogger(__name__)

_engine: MageVLEngine | None = None


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine
    # checkpoint 解析放进构造参数里算,但它可能抛(路径写错)—— 那时不能让进程
    # 起不来:起不来就没有 /health,用户看到的是"连不上",而真正的原因
    # (目录不存在)只在终端里一闪而过。交给加载线程去撞,落进 load_error。
    _engine = MageVLEngine(
        checkpoint=_env("LOCAL_VISION_CHECKPOINT", "microsoft/Mage-VL"),
        device=_env("LOCAL_VISION_DEVICE", "cuda:0"),
        video_backend=_env("LOCAL_VISION_BACKEND", "codec"),
        num_frames=int(_env("LOCAL_VISION_NUM_FRAMES", "32")),
        max_pixels=int(_env("LOCAL_VISION_MAX_PIXELS", "150000")),
        attn_impl=_env("LOCAL_VISION_ATTN", "sdpa"),
    )
    # 权重加载要几十秒。放后台线程,让服务立刻开始应答 —— 否则 /health 在加载
    # 期间根本连不上,miloco 侧那条"正在加载模型"的等待态永远走不到,用户冷启动
    # 时只会看到"服务不可达",分不清是没装好还是在加载。
    # 上一次进程若被中途杀掉,/tmp 里会留下它正在用的视频段(清理在 finally 里,
    # SIGKILL 时不执行)。开机扫一次 —— 那是家里的画面,不该无限期留在磁盘上。
    from local_vision.video import sweep_stale_segments

    sweep_stale_segments()

    threading.Thread(target=_load_engine, name="lv-load", daemon=True).start()
    yield
    _engine = None


def _load_engine() -> None:
    e = _engine
    if e is None:
        return
    try:
        e.load()
    except Exception as err:  # noqa: BLE001 —— 加载失败只让服务停在 loading 态,不崩进程
        # 但必须把原因挂到 /health 上。不挂的话,"权重路径打错了"与"还在加载"
        # 在接口上完全同形,而前者永远不会好:miloco 会一直显示「正在加载模型,
        # 稍后再试」,用户就照做——等一辈子。
        e.load_error = f"{type(err).__name__}: {err}"[:300]
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
_MAX_INFLIGHT = max(1, int(os.environ.get("LOCAL_VISION_MAX_INFLIGHT", "5")))
_inflight = threading.Semaphore(_MAX_INFLIGHT)


def require_token(request: Request) -> None:
    """配了 token 就强制校验;没配就不校验(此时服务必须只绑环回地址)。"""
    expected = os.environ.get("LOCAL_VISION_TOKEN", "")
    if not expected:
        return
    got = request.headers.get("authorization", "")
    # 常数时间比较:普通 != 会因短路而泄漏前缀匹配长度。
    # 必须先 encode:compare_digest 对非 ASCII str 抛 TypeError —— token 里带一个
    # 中文字符就会让每次鉴权变成未捕获 500,而 /health 仍报 ok,排查时完全对不上。
    if not hmac.compare_digest(got.encode("utf-8"), f"Bearer {expected}".encode()):
        raise HTTPException(status_code=401, detail="invalid or missing token")


#: 单次请求允许的规则条数。生成预算按条数放大并封顶,条数过多时末尾的判定会被
#: 截断,而 fail-closed 会把它们变成静默的未命中。请求侧先挡住,好过事后追查。
MAX_RULES = 64


class RuleSpec(BaseModel):
    name: str = Field(default="", max_length=200)
    query: str = Field(default="", max_length=2000)


class RosterEntry(BaseModel):
    """调用方**已经认好**的一个人:名字 + 他在画面里的位置。

    这不是让边车去认人 —— 认人由调用方自己完成(miloco 侧用 ReID 比对身份库)。
    边车只把名字贴到位置上。契约对第三方实现开放:任何能给出「谁在哪」的上游都
    可以填这个字段,边车不关心它是怎么算出来的。
    """

    name: str = Field(default="", max_length=64, description="人物姓名;留空的条目会被丢弃")
    bbox: list[int] = Field(
        default_factory=list,
        description="(x1, y1, x2, y2),归一化到 [0,1000](左上 0,0)。非法框整条丢弃",
    )


#: 单次请求视频段的字节上限(解码后)。miloco 默认 4s 窗、CRF 28、短边 512,
#: 实测每段几十 KB;给到 64 MiB 已是三个数量级的余量。
#: 不设上限的话:pydantic 会先把整个 body 收进内存,base64 解码再复制一份,而这
#: 两步都发生在并发闸(_inflight)**之前** —— perceive 是同步 def,跑在 anyio 的
#: 线程池里(默认 40 个),几十个超大 body 可以同时驻留,与 _MAX_INFLIGHT 无关;
#: 随后 write_temp_video 还会把它落盘,连磁盘一起打满。
MAX_ROSTER = 16
MAX_VIDEO_BYTES = 64 * 1024 * 1024
_MAX_VIDEO_B64_CHARS = (MAX_VIDEO_BYTES * 4) // 3 + 8


class PerceiveRequest(BaseModel):
    video_b64: str = Field(
        description="视频段(mp4/h264)的 base64", max_length=_MAX_VIDEO_B64_CHARS
    )
    scene_ask: str | None = Field(
        default=None, max_length=4000, description="场景描述的提问;缺省用内置中文提问"
    )
    rules: list[RuleSpec] = Field(
        default_factory=list, description="要逐条判定的规则", max_length=MAX_RULES
    )
    # 这几项与 video_b64 同在一个 body 里,同样在并发闸**之前**被收取与解析 ——
    # 只给视频封顶而放任它们无界,分析和防护就对不上了。
    camera_note: str = Field(
        default="", max_length=2000,
        description="该机位的自定义说明;作为补充,不取代任务提问",
    )
    max_new_tokens: int = Field(default=256, ge=16, le=1024)
    want_gate: bool = Field(default=True, description="是否计算 StreamMind 门控概率")
    # 复读硬禁的 n。默认按"有无规则"自适应(见 engine),但主动查询这条路上提问是
    # agent 现编的自由文本,答案里合理重复一个短句的可能性高得多 —— 让调用方能
    # 按用途放宽,而不是被一个为定时描述调出来的值管着。
    ngram_guard: int | None = Field(default=None, ge=0, le=128)
    # codec 的 token 预算(画布数)。调用方按自己的成本取舍决定,边车不替它猜 ——
    # 缺省时按实际帧数能填满的档位来。
    codec_target_canvas: int | None = Field(default=None, ge=4, le=64)
    # 「谁在画面的哪个位置」。上限与 prompts._ROSTER_MAX_PERSONS 同量级 —— 与
    # rules 同理,这也是个在并发闸之前就被收取解析的列表,不能无界。
    roster: list[RosterEntry] = Field(
        default_factory=list, max_length=MAX_ROSTER, description="调用方已认好的人物名册",
    )
    osd_watermark: bool = Field(
        default=False,
        description=(
            "该机位是否把日期时间烧进了画面像素。True 时提问里挂一句「忽略时间水印」。"
            "**默认 False 是有意的**:这句话会压掉屋里真实存在的钟(挂钟/微波炉/闹钟),"
            "实测 30 段配对 8/30 → 1/30(McNemar p=0.039)。没水印的机位不该付这个代价"
        ),
    )


class RuleHit(BaseModel):
    """一条规则的判定结果。

    契约的关键在于**调用方怎么把它认回自己的规则**:miloco 按 ``name`` 查;
    ``name`` 为空、或查不到对应规则时,这条判定会被**丢弃**。没有下标兜底 ——
    按位置猜的后果是"厨房明火"背上"有人跌倒"的结论,宁可丢掉。

    所以第三方实现必须做到:``name`` 原样回填请求里的 ``rules[i].name``。
    顺序不必严格保证(按名字查),但建议逐条返回(含未命中的),这样调用方能把
    "模型说不" 与 "模型没给判定" 区分开。
    """

    name: str = Field(default="", description="与请求 rules[i].name 一致;留空会被丢弃")
    hit: bool = Field(default=False, description="是否命中")
    reason: str = Field(default="", description="判定依据,进事件文本给 agent 看")


class PerceiveResponse(BaseModel):
    caption: str
    rule_hits: list[RuleHit]
    # 必须声明,否则 pydantic 默认 extra="ignore" 会把它在边界上丢掉 ——
    # "模型输出被复读吃光/格式被带偏"就与"模型确实说不"变得无法区分,而前者会让
    # 该相机的规则被整体推成未命中(state 规则甚至会因此触发 on_exit 动作)。
    unparsed_rules: int = 0
    # 生成是否顶到 max_new_tokens 上限。截断会把末尾的「规则N:」整段吃掉,而
    # fail-closed 会把它变成"全部未命中" —— 与"模型确实说不"同形。必须报出来。
    truncated: bool = False
    gate_p: float | None
    backend: str
    timing_ms: dict
    raw: str


@app.get("/health")
def health(request: Request) -> dict:
    """无需鉴权,供 miloco 探活与「测试连接」使用。

    但**要把鉴权结论报出来**。探活不校验凭证的话,配错 token 的部署会一路绿灯:
    /health 通过 → miloco 判定就绪 → 每一窗推理 401 → 感知静默停摆,而界面上
    那行探活始终是绿的。所以这里用与 require_token 相同的比较得出 auth_ok,
    让调用方能在**切换那一刻**就拒绝,而不是等第一次推理失败。
    仍不返 401:探活得能区分「服务没起来」和「起来了但凭证不对」。
    """
    e = _engine
    expected = os.environ.get("LOCAL_VISION_TOKEN", "")
    auth_ok = True
    if expected:
        got = request.headers.get("authorization", "")
        auth_ok = hmac.compare_digest(got.encode("utf-8"), f"Bearer {expected}".encode())
    return {
        "auth_required": bool(expected),
        "auth_ok": auth_ok,
        "status": "ok" if (e and e.ready) else "loading",
        "model_loaded": bool(e and e.ready),
        "gate_available": bool(e and e.gate_available),
        # 门控熄灯的原因(如缺 mamba_ssm)直接暴露出来,免得排查时对着
        # gate_p=null 猜半天。
        "gate_error": e.gate_error if e else None,
        # 加载失败的原因。非空 = 这个"loading"永远不会变成 ready,别再等了。
        "load_error": e.load_error if e else None,
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
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"video payload exceeds {MAX_VIDEO_BYTES} bytes",
        )

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
            ngram_guard=body.ngram_guard,
            codec_target_canvas=body.codec_target_canvas,
            roster=[r.model_dump() for r in body.roster],
            osd_watermark=body.osd_watermark,
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
