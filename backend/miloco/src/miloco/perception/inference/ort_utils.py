"""ONNX Runtime session utilities — centralised thread control."""

from __future__ import annotations

import hashlib
import logging
import platform
import shutil
import threading
from pathlib import Path

import onnxruntime as ort

_LOGGER = logging.getLogger(__name__)

# Empirically tested: 4 threads gives the best balance of throughput and
# tail-latency stability on real workloads.  Higher counts look faster on
# synthetic benchmarks but suffer from scheduling jitter on real frames
# (e.g. 8 threads: avg 62ms but max 410ms vs 4 threads: avg 48ms, max 58ms).
_DEFAULT_NUM_THREADS = 4

# Apple Silicon 上 CPU EP 默认走 ArmKleidiAI::MlasConv,每次 Conv 推理分配
# native workspace 不归还,长跑 RSS 单调上涨。CoreML EP 走 ANE/GPU 绕开此路径。
# Intel Mac 上 CoreML EP 反而更慢,需要按 arch 区分。
_IS_APPLE_SILICON = (
    platform.system() == "Darwin" and platform.machine() == "arm64"
)

# CoreML EP 每建一个 InferenceSession 都会把 ONNX 子图序列化成一个 ~模型等大的
# 中间 .mlmodel 写进 $TMPDIR,且删除只挂在 C++ Execution 析构链上——进程被
# SIGKILL / session 对象不及时释放时文件永久遗留,长跑累积可撑爆磁盘(上游
# microsoft/onnxruntime#26023,至今无修复)。ModelCacheDirectory(>=1.21)把这些
# 文件从 $TMPDIR 挪到我们指定的持久目录并跨 session/进程复用,把"无界泄漏"收敛
# 成"每模型一份的有界 footprint"。cache 子目录按模型内容 hash 隔离,规避 ORT 自身
# cache key 不检测内容变更(原地换模型会复用旧编译产物)的坑。
_COREML_CACHE_DIRNAME = "coreml_cache"

# 兜底:cache 目录总大小超过 models_dir 下所有 onnx 之和的这个倍数时整目录清空
# 重建。全清是安全操作(下次 session 重编译一次而已,不会用错模型),故阈值可保守。
# 真机实测(ort 1.27.0):单模型 CoreML 编译产物约为源 onnx 的 ~2x(det 43MB →
# cache 89MB),稳态 det+reid 合计 total/base ≈ 1.4x;模型升级一次(旧目录暂留)
# 约 2.8x 仍 < 3x,连续两次以上升级累积才触发全清 —— 故 3x 余量足、稳态不误触发。
_CACHE_OVERSIZE_MULTIPLIER = 3

# 总量兜底清理进程内只跑一次(首个 CoreML session 创建前),避免边清边读。
# 用 threading.Event 表达「已清」这一次性标志:is_set / set 线程安全,配合下面的
# 锁做双检锁;比模块级 bool + global 更贴 once 语义,也让 CodeQL 不再把「本次
# 调用写、下次调用读」的跨调用持久 flag 按单次数据流误判为 unused global。
_cache_sweep_lock = threading.Lock()
_cache_swept = threading.Event()


def _ort_version_ge(major: int, minor: int) -> bool:
    try:
        parts = ort.__version__.split(".")
        return (int(parts[0]), int(parts[1])) >= (major, minor)
    except (ValueError, IndexError):
        return False


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
            _LOGGER.warning(
                "CoreML cache 兜底清理失败(忽略,不影响启动)", exc_info=True
            )
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
                _sweep_coreml_cache_if_oversized_once()
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

    opts = ort.SessionOptions()
    threads = num_threads if num_threads is not None else _DEFAULT_NUM_THREADS
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = threads

    # 兜底层: onnxruntime >= 1.25 加 PR #27136 引入的 opt-out。CoreML 不支持
    # 的算子会 fallback 到 CPU EP,默认仍走 ArmKleidiAI 继续小幅泄漏;另外覆盖
    # 非 Apple Silicon ARM 平台 (如 Linux ARM 部署)。
    if _ort_version_ge(1, 25):
        opts.add_session_config_entry("mlas.disable_kleidiai", "1")

    _LOGGER.info("ORT session providers=%s for %s", providers, model_path)
    return ort.InferenceSession(model_path, sess_options=opts, providers=providers)
