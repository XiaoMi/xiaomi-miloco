"""Suggestion 事件链去重用的句向量编码器。

把每条 suggestion 的 ``event`` 文本编码成归一化向量，事件链匹配时用余弦相似度判断
"是不是同一桩持续事件"——替代旧的精确字符串匹配（模型每窗对同一事件措辞会漂移，
精确匹配认不出 → 反复开新链刷屏）。

模型：bge-small-zh-v1.5（int8 量化，~24MB），随 ``perception/models/`` 分发。
强制 CPU EP：模型极小（短句 CPU ~10ms），且 int8 算子在 CoreML EP 上支持不全，
固定 CPU 避免 fallback 抖动。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

_MODEL_FILE = "bge-small-zh-v1.5-int8.onnx"
_TOKENIZER_FILE = "bge-small-zh-v1.5-tokenizer.json"
_MAX_TOKENS = 64  # 隐患事件都是短句，64 token 足够，超长截断


class EventEmbedder:
    """bge-small-zh 句向量编码器（CLS pooling + L2 归一化）。"""

    def __init__(self, models_dir: str | Path):
        from tokenizers import Tokenizer  # 延迟导入：缺依赖时由调用方降级处理

        from miloco.perception.inference.ort_utils import apply_kleidiai_opt_out

        models_dir = Path(models_dir)
        self._tok = Tokenizer.from_file(str(models_dir / _TOKENIZER_FILE))
        self._tok.enable_truncation(max_length=_MAX_TOKENS)
        # 与 speech_vad 同款:这条 session 不走 make_session 工厂、强制 CPU EP(int8 算子
        # CoreML 支持不全),故显式补 KleidiAI opt-out——ARM CPU EP 上 KleidiAI 也含 int8
        # GEMM 微内核,与 issue #429 同源、同平台条件。1.27 已上游根治,此为防御,保持
        # "所有自建 session 统一调 apply_kleidiai_opt_out"不变量(见其 docstring)。
        opts = ort.SessionOptions()
        apply_kleidiai_opt_out(opts)
        self._sess = ort.InferenceSession(
            str(models_dir / _MODEL_FILE),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._has_token_type = "token_type_ids" in {i.name for i in self._sess.get_inputs()}
        logger.info("EventEmbedder loaded (%s)", _MODEL_FILE)

    def embed(self, text: str) -> np.ndarray:
        """返回归一化句向量（1D float32）。"""
        enc = self._tok.encode(text or "")
        ids = np.asarray([enc.ids], dtype=np.int64)
        mask = np.asarray([enc.attention_mask], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if self._has_token_type:
            feed["token_type_ids"] = np.zeros_like(ids)
        out = self._sess.run(None, feed)[0]  # [1, T, H]
        vec = out[0, 0].astype(np.float32)  # CLS
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec
