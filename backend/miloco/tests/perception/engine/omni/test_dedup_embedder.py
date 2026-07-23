"""EventEmbedder 自建 ONNX session 的 KleidiAI opt-out 覆盖。

与 speech_vad 同款:该 session 不走 make_session 工厂、强制 CPU EP,故须显式调
apply_kleidiai_opt_out(#429 加固,保持"所有自建 session 统一调本函数"不变量)。
"""

from __future__ import annotations

import types

import pytest


class TestEmbedderKleidiAIOptOut:
    def test_init_applies_kleidiai_opt_out(self, monkeypatch):
        import onnxruntime as ort
        from miloco.config import get_settings
        from miloco.perception.engine.omni import dedup_embedder
        from miloco.perception.inference import ort_utils

        models_dir = get_settings().directories.models_dir
        if not (models_dir / dedup_embedder._MODEL_FILE).is_file() or not (
            models_dir / dedup_embedder._TOKENIZER_FILE
        ).is_file():
            pytest.skip("bge 模型/tokenizer 缺失")

        # spy 共享 helper:确认自建 session 路径确实调用了它(dedup_embedder 内为函数内
        # lazy import,patch 模块属性能被调用点重新解析命中)
        calls: list = []
        real = ort_utils.apply_kleidiai_opt_out

        def _spy(opts):
            calls.append(opts)
            return real(opts)

        monkeypatch.setattr(ort_utils, "apply_kleidiai_opt_out", _spy)
        # stub 真实 session,避免加载 int8 模型;__init__ 会读 get_inputs()
        monkeypatch.setattr(
            ort,
            "InferenceSession",
            lambda *a, **k: types.SimpleNamespace(get_inputs=lambda: []),
        )

        dedup_embedder.EventEmbedder(models_dir)
        assert len(calls) == 1, "EventEmbedder 自建 session 未调用 apply_kleidiai_opt_out"
