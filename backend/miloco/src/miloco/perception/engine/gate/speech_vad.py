"""Speech VAD (silero) — 判本窗音频里有没有真人声，门控 speeches 字段。

只在音频已过能量 gate（``audio_active``）时调用（见 ``run_gate``）：能量过线但 VAD
判无人声 → ``speech_active=False`` → 下游从 schema 剥掉 speeches（env_sounds / 喂音频
照旧）。根除模型在键鼠敲击 / 办公底噪上脑补"像指令的话"——纯能量分不开人声与机械
瞬态噪声，silero（神经网络 VAD）能。

模型缺失 / 加载失败 → 一律视作"有人声"（返回 ``True``）退回旧行为，绝不因 VAD 不可用
而吞掉真实语音。
"""

from __future__ import annotations

import logging
import threading

import numpy as np
from numpy.typing import NDArray

from miloco.perception.engine.config import GateConfig

logger = logging.getLogger(__name__)

_MODEL_FILENAME = "silero_vad.onnx"
_CHUNK = 512  # silero 16kHz 固定帧长
_CONTEXT = 64  # silero 16kHz 每帧前置的上一帧尾部 context（模型实际输入 = 64+512=576）
_SAMPLE_RATE = 16000

_lock = threading.Lock()
_inference_lock = threading.Lock()  # 保护全局 VAD session 并发 sess.run()（多设备并行推理）
_session = None  # ort.InferenceSession | None
_load_failed = False


def _get_session():
    """懒加载 silero session（进程级单例）。缺模型 / 加载失败 → None 且不再重试。"""
    global _session, _load_failed
    if _session is not None or _load_failed:
        return _session
    with _lock:
        if _session is not None or _load_failed:
            return _session
        try:
            import onnxruntime as ort

            from miloco.config import get_settings

            path = get_settings().directories.models_dir / _MODEL_FILENAME
            if not path.is_file():
                logger.warning(
                    "silero VAD 模型缺失(%s)，speeches VAD 门控停用，退回能量 gate 行为",
                    path,
                )
                _load_failed = True
                return None
            # silero 是小型有状态模型(单帧 <1ms)，直接走 CPU EP：CoreML 对其有状态算子
            # 支持差、易逐算子回落，CPU 已足够且与离线验证口径一致。
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            _session = ort.InferenceSession(
                str(path), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        except Exception:
            logger.exception("silero VAD 加载失败，speeches VAD 门控停用")
            _load_failed = True
    return _session


def evaluate_speech(
    audio_clip: NDArray[np.int16], config: GateConfig
) -> tuple[bool, float]:
    """返回 ``(has_speech, peak_prob)``。

    逐 512 样本帧跑 silero（state 在帧间承接），统计过阈帧数；过阈帧 >=
    ``speech_vad_min_speech_chunks`` 即判有人声。模型不可用 / 关闭 → ``(True, 0.0)``
    退回旧行为；音频过短 → ``(False, 0.0)``。
    """
    if not config.speech_vad_enabled:
        return True, 0.0
    sess = _get_session()
    if sess is None:
        return True, 0.0
    if audio_clip.size < _CHUNK:
        return False, 0.0

    pcm = audio_clip.astype(np.float32) / 32768.0
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr = np.array(_SAMPLE_RATE, dtype=np.int64)
    # silero 每帧输入 = 上一帧尾部 64 样本 context + 本帧 512 样本（首帧 context 补零），
    # 与官方 OnnxWrapper 口径一致；漏掉 context 会让概率恒接近 0。
    context = np.zeros((1, _CONTEXT), dtype=np.float32)
    above = 0
    peak = 0.0
    for i in range(0, pcm.size - _CHUNK + 1, _CHUNK):
        chunk = pcm[i : i + _CHUNK].reshape(1, _CHUNK)
        x = np.concatenate([context, chunk], axis=1)
        with _inference_lock:
            out, state = sess.run(
                ["output", "stateN"], {"input": x, "state": state, "sr": sr}
            )
        context = chunk[:, -_CONTEXT:]
        p = float(out[0, 0])
        if p > peak:
            peak = p
        if p >= config.speech_vad_threshold:
            above += 1
    return above >= config.speech_vad_min_speech_chunks, peak
