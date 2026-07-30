"""EventEmbedder 自建 ONNX session 的 KleidiAI opt-out 覆盖。

与 speech_vad 同款:该 session 不走 make_session 工厂、强制 CPU EP,故须显式调
apply_kleidiai_opt_out(#429 加固,保持"所有自建 session 统一调本函数"不变量)。
"""

from __future__ import annotations

import types


class TestEmbedderKleidiAIOptOut:
    def test_init_applies_kleidiai_opt_out(self, monkeypatch):
        import onnxruntime as ort
        import tokenizers
        from miloco.perception.engine.omni import dedup_embedder
        from miloco.perception.inference import ort_utils

        # 不 skip:stub Tokenizer(Rust ext)与 InferenceSession → 无需真实模型/tokenizer
        # 文件,CI(无模型)也真跑,强制守护"自建 session 必调 apply_kleidiai_opt_out"
        # 不变量(review 指出恒 skip 的缺口)。
        class _FakeTok:
            @staticmethod
            def from_file(_path):
                return types.SimpleNamespace(enable_truncation=lambda **k: None)

        monkeypatch.setattr(tokenizers, "Tokenizer", _FakeTok)
        monkeypatch.setattr(
            ort,
            "InferenceSession",
            lambda *a, **k: types.SimpleNamespace(get_inputs=lambda: []),
        )
        # spy 共享 helper:确认自建 session 路径确实调用了它
        calls: list = []
        real = ort_utils.apply_kleidiai_opt_out

        def _spy(opts):
            calls.append(opts)
            return real(opts)

        monkeypatch.setattr(ort_utils, "apply_kleidiai_opt_out", _spy)

        dedup_embedder.EventEmbedder("/nonexistent-models-dir")
        assert len(calls) == 1, "EventEmbedder 自建 session 未调用 apply_kleidiai_opt_out"
