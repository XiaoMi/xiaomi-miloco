"""ONNX Runtime session utilities — centralised thread control."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import threading
from pathlib import Path

import onnxruntime as ort

_LOGGER = logging.getLogger(__name__)

# 固定线程数控尾延迟。实测(真实帧):8 线程 avg 62ms/max 410ms,远差于 4 线程的
# avg 48ms/max 58ms——synthetic bench 上更高线程更快,但真实帧受调度抖动拖累。
# detector/reid 走此值;dedup/vad 用 TINY_MODEL_THREADS(见 perception/inference/tuning.py,
# 特意不放这里:本模块 module 级 import onnxruntime,只放常量会拖着 ORT 一起被引)。
_DEFAULT_NUM_THREADS = 4

# Apple Silicon 上 CPU EP 默认走 ArmKleidiAI::MlasConv,每次 Conv 推理分配
# native workspace 不归还,长跑 RSS 单调上涨。CoreML EP 走 ANE/GPU 绕开此路径。
# Intel Mac 上 CoreML EP 反而更慢,需要按 arch 区分。
_IS_APPLE_SILICON = platform.system() == "Darwin" and platform.machine() == "arm64"


# Linux x86_64 默认启用 OpenVINO EP 做硬件加速:N100/Alder Lake-N 等 Intel CPU
# 可用 OpenVINO CPU EP 的 AVX2/VNNI 优化;AMD 上走通用 oneDNN 路径,未做基准但能跑。
# 依赖层 marker(PEP 508 无法表达 CPU 厂商)同样把所有 Linux x86_64 装上
# onnxruntime-openvino wheel,既然体积代价已付,就让它默认也吃到加速;AMD 上若实测
# 更慢,可用 MILOCO_DISABLE_OPENVINO=1 一键退回 CPU EP。两层口径重新一致。
#
# device_type 固定 "CPU":N100 无独立 GPU,集成 UHD 与 CPU 共享内存带宽,GPU 插件
# 收益有限;OpenVINO CPU 插件的 AVX2/VNNI 微内核已能拿到主要加速。use_gpu=True
# 且轮子无 CUDA EP(落到这里)时改 "AUTO",让有 Arc/较新核显的机器自行吃 GPU,
# 无可用 GPU 时 OpenVINO 自行退回 CPU,不会因机器没独显而建不出 session。
_IS_X86_LINUX = platform.system() == "Linux" and platform.machine() in (
    "x86_64",
    "AMD64",
)

# CoreML EP 每建一个 InferenceSession 都会把 ONNX 子图序列化成一个 ~模型等大的
# 中间 .mlmodel 写进 $TMPDIR,且删除只挂在 C++ Execution 析构链上——进程被
# SIGKILL / session 对象不及时释放时文件永久遗留,长跑累积可撑爆磁盘(上游
# microsoft/onnxruntime#26023,至今无修复)。ModelCacheDirectory(>=1.21)把这些
# 文件从 $TMPDIR 挪到我们指定的持久目录并跨 session/进程复用,把"无界泄漏"收敛
# 成"每模型一份的有界 footprint"。cache 子目录按模型内容 hash 隔离,规避 ORT 自身
# cache key 不检测内容变更(原地换模型会复用旧编译产物)的坑。
_COREML_CACHE_DIRNAME = "coreml_cache"

# OpenVINO EP 同款编译缓存:跨 session/进程复用图编译产物,降低重启启动时延
# (N100 低功耗核上 det+reid 编译是秒级阻塞)。与 CoreML cache 分目录、互不干扰。
_OPENVINO_CACHE_DIRNAME = "openvino_cache"

# 兜底:cache 目录总大小超过 models_dir 下所有 onnx 之和的这个倍数时整目录清空
# 重建。全清是安全操作(下次 session 重编译一次而已,不会用错模型),故阈值可保守。
# 真机实测(ort 1.27.0):单模型 CoreML 编译产物约为源 onnx 的 ~2x(det 43MB →
# cache 89MB),稳态 det+reid 合计 total/base ≈ 1.4x;模型升级一次(旧目录暂留)
# 约 2.8x 仍 < 3x,连续两次以上升级累积才触发全清 —— 故 3x 余量足、稳态不误触发。
# OpenVINO 编译产物与源 onnx 同量级,沿用同一阈值。
_CACHE_OVERSIZE_MULTIPLIER = 3

# 总量兜底清理进程内每条 cache 路径各只跑一次(首个 session 创建前),避免边清边读。
# 用 threading.Event 表达「已清」这一次性标志:is_set / set 线程安全,配合下面的
# 锁做双检锁;按 cache_dirname 隔离(CoreML / OpenVINO 各自独立,互不干扰)。
_cache_sweep_lock = threading.Lock()
_cache_swept: dict[str, threading.Event] = {}


def _sweep_cache_if_oversized_once(cache_dirname: str) -> None:
    """进程内一次:指定 cache 目录总量超阈值则整目录清空重建。失败只告警不阻断启动。

    CoreML / OpenVINO 各自的编译缓存都挂此兜底逻辑:目录名 cache_dirname 隔离,
    互不影响。与 _model_cache_dir / _openvino_cache_dir 走同一 workspace_dir 根,
    避免清理路径与实际缓存路径分叉。
    """
    evt = _cache_swept.setdefault(cache_dirname, threading.Event())
    if evt.is_set():
        return
    with _cache_sweep_lock:
        evt = _cache_swept.setdefault(cache_dirname, threading.Event())
        if evt.is_set():
            return
        try:
            from miloco.config import get_settings

            dirs = get_settings().directories
            root = dirs.workspace_dir / cache_dirname
            if not root.is_dir():
                return
            models_dir = dirs.models_dir
            base = (
                sum(p.stat().st_size for p in models_dir.glob("*.onnx"))
                if models_dir.is_dir()
                else 0
            )
            if base <= 0:
                # 基准算不出(模型目录缺失)时不敢清,避免误删。
                return
            total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            if total > base * _CACHE_OVERSIZE_MULTIPLIER:
                shutil.rmtree(root, ignore_errors=True)
                root.mkdir(parents=True, exist_ok=True)
                _LOGGER.warning(
                    "%s cache %s 膨胀到 %.0fMB > %dx onnx总和(%.0fMB),已整清重建",
                    cache_dirname,
                    root,
                    total / 1e6,
                    _CACHE_OVERSIZE_MULTIPLIER,
                    base / 1e6,
                )
        except Exception:
            _LOGGER.warning(
                "%s cache 兜底清理失败(忽略,不影响启动)", cache_dirname, exc_info=True
            )
        finally:
            # set 移到清理完成之后:并发 make_session 的其它线程在快速路径见到未 set
            # 时会进锁阻塞,直到 rmtree 结束才放行,兑现「首个 session 创建前清完」
            # 屏障、杜绝边清边读;用 finally 保证即便清理体抛异常也只 set 一次、不重试。
            evt.set()


def _ort_version_ge(major: int, minor: int) -> bool:
    try:
        parts = ort.__version__.split(".")
        return (int(parts[0]), int(parts[1])) >= (major, minor)
    except (ValueError, IndexError):
        return False


def _openvino_disabled() -> bool:
    """环境变量 MILOCO_DISABLE_OPENVINO=1/true/yes 时强制禁用 OpenVINO EP。

    用于现场排障:OpenVINO 在某些模型/驱动组合上可能行为异常,留一条
    不重启改代码的逃生通道。
    """
    import os

    val = os.environ.get("MILOCO_DISABLE_OPENVINO", "").strip().lower()
    return val in ("1", "true", "yes")


def apply_kleidiai_opt_out(opts: "ort.SessionOptions") -> None:
    """onnxruntime >= 1.25 起关闭 ArmKleidiAI(PR #27136 引入的 opt-out)。

    Apple Silicon / ARM 的 CPU EP 默认走 ArmKleidiAI::MlasConv,每次 Conv 推理分配的
    native workspace 不归还 → 长跑 RSS 单调上涨(issue #429 同源)。1.27 上游已根治,
    此 opt-out 是防御层:覆盖 CoreML 不支持算子回落 CPU EP 的部分,以及非 Apple Silicon
    的 ARM 平台(如 Linux ARM 部署)。

    **单一来源**:所有自建 ONNX session(make_session 工厂 + speech_vad 等直建 session)
    统一调本函数,避免版本门槛(1.25)与 config key 字符串在多处各存一份、改一处漏一处。
    """
    if _ort_version_ge(1, 25):
        opts.add_session_config_entry("mlas.disable_kleidiai", "1")


def _hash_model_file(model_path: str) -> str:
    """模型文件内容的 sha256 前 16 位,作 cache 子目录名。

    用内容而非路径:模型换代(原地覆盖同名文件)hash 变→新目录,天然规避复用旧
    编译产物;同一模型(重启/跨机复制)hash 稳→复用同一目录。
    """
    h = hashlib.sha256()
    with open(model_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _coreml_cache_root() -> Path:
    # 惰性 import:config 不反向依赖本模块,函数内 import 既避免任何 import cycle,
    # 也沿用 detector/human_reid 里 make_session 的惰性风格。
    from miloco.config import get_settings

    return get_settings().directories.workspace_dir / _COREML_CACHE_DIRNAME


def _model_cache_dir(model_path: str) -> Path:
    """该模型的独立 CoreML cache 目录(coreml_cache/<content-hash>/),已确保存在。"""
    cache_dir = _coreml_cache_root() / _hash_model_file(model_path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _openvino_cache_dir(model_path: str) -> Path:
    """该模型的 OpenVINO 编译缓存目录(openvino_cache/<content-hash>/),已确保存在。

    与 CoreML 路径同款按内容 hash 分目录:模型原地换代 → hash 变 → 新目录,
    天然规避复用旧编译产物。
    """
    from miloco.config import get_settings

    cache_dir = (
        get_settings().directories.workspace_dir
        / _OPENVINO_CACHE_DIRNAME
        / _hash_model_file(model_path)
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _sweep_coreml_cache_if_oversized_once() -> None:
    """进程内一次:cache 目录总量超阈值则整目录清空重建。失败只告警不阻断启动。"""
    if _cache_swept.is_set():
        return
    with _cache_sweep_lock:
        if _cache_swept.is_set():
            return
        try:
            from miloco.config import get_settings

            dirs = get_settings().directories
            # 缓存根与 _model_cache_dir 走同一来源(_coreml_cache_root),避免两处
            # 拼路径:将来改缓存根布局时只改一处就会让 sweep 与实际缓存目录分叉、
            # 清错 / 清不到。
            root = _coreml_cache_root()
            if not root.is_dir():
                return
            models_dir = dirs.models_dir
            base = (
                sum(p.stat().st_size for p in models_dir.glob("*.onnx"))
                if models_dir.is_dir()
                else 0
            )
            if base <= 0:
                # 基准算不出(模型目录缺失)时不敢清,避免误删。
                return
            total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            if total > base * _CACHE_OVERSIZE_MULTIPLIER:
                shutil.rmtree(root, ignore_errors=True)
                root.mkdir(parents=True, exist_ok=True)
                _LOGGER.warning(
                    "CoreML cache %s 膨胀到 %.0fMB > %dx onnx总和(%.0fMB),已整清重建",
                    root,
                    total / 1e6,
                    _CACHE_OVERSIZE_MULTIPLIER,
                    base / 1e6,
                )
        except Exception:
            _LOGGER.warning("CoreML cache 兜底清理失败(忽略,不影响启动)", exc_info=True)
        finally:
            # set 移到清理完成之后:并发 make_session 的其它线程在快速路径见到未 set
            # 时会进锁阻塞,直到 rmtree 结束才放行,兑现「首个 session 创建前清完」
            # 屏障、杜绝边清边读;用 finally 保证即便清理体抛异常也只 set 一次、不重试。
            _cache_swept.set()


def make_session(
    model_path: str,
    *,
    use_gpu: bool = False,
    num_threads: int | None = None,
) -> ort.InferenceSession:
    """Create an InferenceSession with thread-count control.

    Args:
        model_path: Path to the ONNX model file.
        use_gpu: Whether to prefer CUDA execution provider.
        num_threads: Number of intra/inter-op threads. ``None`` uses the
            module default (4).
    """
    providers = ["CPUExecutionProvider"]
    available = ort.get_available_providers()
    # 提前算:OpenVINO 分支要把同一个值透传给 EP 侧线程池,不能等到
    # SessionOptions 才算(EP 侧默认 8,与本文件顶部"4 线程控尾延迟"的实测
    # 结论不一致;不显式对齐会让模型主体按 8 线程跑、尾延迟恶化)。
    threads = num_threads if num_threads is not None else _DEFAULT_NUM_THREADS
    if use_gpu and "CUDAExecutionProvider" in available:
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    # Apple Silicon 即使 use_gpu=False 也走 CoreML — 主要目的是绕开 CPU EP
    # 上 ArmKleidiAI 的 workspace 内存泄漏 (不是为性能,顺带也快)。
    #
    # CoreML EP (FP16) vs CPU EP (FP32) 的数值漂移在本仓库的 detector / reid
    # 模型上实测业务阈值内可忽略 — detector top conf |Δ| ≤ 1.3e-4 远小于
    # 0.5 阈值;reid same-input cosine ≥ 0.999998,cross-pair cosine drift
    # p95 = 4e-4 — 故未加 provider_options 钉死 MLComputeUnits。
    elif _IS_APPLE_SILICON and "CoreMLExecutionProvider" in available:
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
        # ModelCacheDirectory 需 onnxruntime >= 1.21;更低版本(如本地测试用的
        # 1.19)不认该 option,退回不带 cache 的 plain CoreML(与旧行为一致,
        # 不 break)。cache 目录准备的任何异常也一律优雅退回——cache 是优化,
        # 绝不能因它拖垮感知推理。
        if _ort_version_ge(1, 21):
            cache_dir = None
            try:
                _sweep_cache_if_oversized_once(_COREML_CACHE_DIRNAME)
                cache_dir = _model_cache_dir(model_path)
            except Exception:
                _LOGGER.warning(
                    "CoreML cache 目录准备失败,退回无 cache 模式", exc_info=True
                )
            if cache_dir is not None:
                providers = [
                    (
                        "CoreMLExecutionProvider",
                        {"ModelCacheDirectory": str(cache_dir)},
                    ),
                    "CPUExecutionProvider",
                ]
    elif _IS_APPLE_SILICON:
        # 自构 / 精简版 wheel 可能不带 CoreML EP,此时静默退回 CPU EP 会让本
        # 模块的内存修复彻底失效。WARNING 级别醒目,避免长跑几小时才发现 RSS
        # 还在涨,人却以为"在 Mac 上就一定走 CoreML"。
        _LOGGER.warning(
            "Apple Silicon detected but CoreMLExecutionProvider not in %s; "
            "falling back to CPU EP — KleidiAI workspace leak will reappear. "
            "Check onnxruntime wheel build options.",
            available,
        )
    # Linux x86_64 默认走 OpenVINO EP 做硬件加速:AVX2/VNNI 优化卷积/GEMM,
    # 无需代码感知。依赖层 marker 同样把所有 Linux x86_64 装上 onnxruntime-openvino
    # wheel(PEP 508 无法表达 CPU 厂商),既然体积代价已付,就让它默认也吃到加速;
    # AMD 上若实测更慢,用 MILOCO_DISABLE_OPENVINO=1 一键退回 CPU EP。
    # 若用户显式禁用(环境变量)或 runtime 未带 OpenVINO EP,则静默回退 CPU EP。
    elif (
        _IS_X86_LINUX
        and "OpenVINOExecutionProvider" in available
        and not _openvino_disabled()
    ):
        # OpenVINO EP 用自己的线程池,不读 SessionOptions.intra_op_num_threads
        # (EP 侧默认 8);不显式对齐,本文件顶部"4 线程控尾延迟"的实测结论在
        # x86 路径上只对 CPU EP 回落算子生效,模型主体不受约束。
        # load_config 是 ORT 1.23+ 的推荐写法(num_of_threads 已 deprecated);
        # 键名 INFERENCE_NUM_THREADS 控制 CPU 设备的推理线程数。
        # device_type 默认 CPU(N100 无独显,核显与 CPU 抢内存带宽,GPU 插件
        # 收益有限);use_gpu=True 且轮子无 CUDA EP(落到这里)时改 AUTO,让
        # 有 Arc/较新核显的机器自行吃 GPU,无可用 GPU 时 OpenVINO 自行退回 CPU。
        ov_opts: dict = {
            "device_type": "AUTO" if use_gpu else "CPU",
            "load_config": json.dumps({"CPU": {"INFERENCE_NUM_THREADS": str(threads)}}),
        }
        # cache_dir 复用编译产物,降低重启启动时延;与 CoreML 同款按内容 hash
        # 分目录。缓存是优化,准备失败只告警、退回无 cache 模式,绝不拖垮推理。
        try:
            _sweep_cache_if_oversized_once(_OPENVINO_CACHE_DIRNAME)
            ov_opts["cache_dir"] = str(_openvino_cache_dir(model_path))
        except Exception:
            _LOGGER.warning(
                "OpenVINO cache 目录准备失败,退回无 cache 模式", exc_info=True
            )
        providers = [
            ("OpenVINOExecutionProvider", ov_opts),
            "CPUExecutionProvider",
        ]
    elif _IS_X86_LINUX and "OpenVINOExecutionProvider" not in available:
        # 与 Apple Silicon 缺 CoreML 同款提示:本机是 Linux x86_64 却没有
        # OpenVINO EP,通常是解释器为 Python 3.14(无 cp314 wheel)或手动装了
        # 标准 onnxruntime。静默退回 CPU EP 会让人误以为加速已生效,故显式告警。
        _LOGGER.warning(
            "Linux x86_64 detected but OpenVINOExecutionProvider not in %s; "
            "falling back to CPU EP — 推理未获 OpenVINO 加速。"
            "常见原因:Python 3.14 无 onnxruntime-openvino wheel。",
            available,
        )

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = threads

    # 兜底层:关闭 KleidiAI(见 apply_kleidiai_opt_out)。CoreML 不支持的算子会
    # fallback 到 CPU EP、默认仍走 ArmKleidiAI 小幅泄漏,故 CoreML 路径也补这条。
    apply_kleidiai_opt_out(opts)

    _LOGGER.info("ORT session providers=%s for %s", providers, model_path)
    return ort.InferenceSession(model_path, sess_options=opts, providers=providers)
