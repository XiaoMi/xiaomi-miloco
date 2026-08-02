"""speech_vad.evaluate_speech 单测——含模型可用 / 缺失（优雅降级）两路。"""

from __future__ import annotations

import numpy as np
from miloco.perception.engine.config import GateConfig
from miloco.perception.engine.gate import speech_vad
from miloco.perception.engine.gate.speech_vad import evaluate_speech


class TestEvaluateSpeechDegrade:
    def test_disabled_returns_true(self):
        """关开关 → 不跑 VAD，恒判有人声（退回纯能量 gate 行为）。"""
        cfg = GateConfig(speech_vad_enabled=False)
        has, prob = evaluate_speech(np.zeros(16000, dtype=np.int16), cfg)
        assert has is True
        assert prob == 0.0

    def test_model_missing_returns_true(self, monkeypatch):
        """模型加载不到 → 优雅降级判有人声，绝不因 VAD 不可用吞掉真实语音。"""
        monkeypatch.setattr(speech_vad, "_get_session", lambda: None)
        cfg = GateConfig(speech_vad_enabled=True)
        has, prob = evaluate_speech(np.zeros(16000, dtype=np.int16), cfg)
        assert has is True

    def test_too_short_returns_false(self, monkeypatch):
        """音频不足一帧（512）→ 判无人声。"""
        monkeypatch.setattr(speech_vad, "_get_session", lambda: object())
        cfg = GateConfig(speech_vad_enabled=True)
        has, prob = evaluate_speech(np.zeros(100, dtype=np.int16), cfg)
        assert has is False


class TestVadSessionKleidiAIOptOut:
    """主线 1:自建 VAD session(不走 make_session)也必须补 KleidiAI opt-out(#429 加固)。"""

    def test_get_session_applies_kleidiai_opt_out(self, monkeypatch, tmp_path):
        import types

        import onnxruntime as ort
        from miloco.perception.inference import ort_utils

        # 不 skip:写个 0 字节 dummy 模型 + 把 models_dir 指过去,绕过 _get_session 的
        # is_file() 早退;stub InferenceSession 避免真加载。这样 CI(无真实模型)也真跑,
        # 强制守护"自建 session 必调 apply_kleidiai_opt_out"不变量(review 指出恒 skip 的缺口)。
        (tmp_path / speech_vad._MODEL_FILENAME).write_bytes(b"")
        monkeypatch.setattr(
            "miloco.config.get_settings",
            lambda: types.SimpleNamespace(
                directories=types.SimpleNamespace(models_dir=tmp_path)
            ),
        )
        # 重置单例强制重建(monkeypatch 会在测试后还原)
        monkeypatch.setattr(speech_vad, "_session", None)
        monkeypatch.setattr(speech_vad, "_load_failed", False)
        monkeypatch.setattr(ort, "InferenceSession", lambda *a, **k: object())
        # spy 共享 helper:确认自建 session 路径确实调用了它
        calls: list = []
        real = ort_utils.apply_kleidiai_opt_out

        def _spy(opts):
            calls.append(opts)
            return real(opts)

        monkeypatch.setattr(ort_utils, "apply_kleidiai_opt_out", _spy)

        sess = speech_vad._get_session()
        assert sess is not None, "应建成(stub)session"
        assert len(calls) == 1, "自建 VAD session 未调用 apply_kleidiai_opt_out"
