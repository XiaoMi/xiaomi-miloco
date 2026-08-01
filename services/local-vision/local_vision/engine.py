"""Mage-VL 推理引擎封装 —— 边车进程内唯一持有 GPU / torch 的地方。

为什么是边车而不是进主包:miloco 后端的目标硬件是 Mac mini / 树莓派这类
CPU-only 机器,把 torch + CUDA 拉进它的依赖树对绝大多数用户是纯负担。本服务
独立进程、独立依赖、可以跑在另一台带显卡的机器上,miloco 只通过 HTTP 认识它。

为什么不用 SGLang 的 OpenAI 兼容端点:那条路把视频按帧当 image_url 发
(见模型自带 inference.py 的 run_online),既拿不到 codec-native 的
token 削减,也拿不到流式门控。本引擎走进程内 offline API,两者都保住。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from local_vision.prompts import DEFAULT_SCENE_ASK, build_prompt, parse_response
from local_vision.video import pick_backend, probe_frame_count, sample_frames

logger = logging.getLogger(__name__)


class MageVLEngine:
    """加载一次、串行复用的 Mage-VL 推理器。

    单卡单模型,并发请求用锁串行化 —— 让 GPU 排队远好过 OOM。
    """

    def __init__(
        self,
        checkpoint: str,
        device: str = "cuda:0",
        video_backend: str = "codec",
        num_frames: int = 32,
        max_pixels: int = 150_000,
        attn_impl: str = "sdpa",
    ) -> None:
        self.checkpoint = checkpoint
        self.device = device
        self.video_backend = video_backend
        self.num_frames = num_frames
        self.max_pixels = max_pixels
        self.attn_impl = attn_impl
        self._lock = threading.Lock()
        self._model = None
        self._processor = None
        self._gate_ok = False
        self._gate_error: str | None = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def load(self) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        t0 = time.time()
        # codec 通路依赖外部二进制,启动期就定位好并把结果反映到 backend 上,
        # 免得每段推理才发现不可用。
        if self.video_backend == "codec" and ensure_codec_tool_on_path() is None:
            logger.warning(
                "cv-preinfer not found; codec backend unavailable, falling back to frames. "
                "Install codec-video-prep or set CV_PREINFER_BIN."
            )
            self.video_backend = "frames"
        logger.info("loading %s onto %s", self.checkpoint, self.device)
        self._processor = AutoProcessor.from_pretrained(
            self.checkpoint, trust_remote_code=True
        )
        self._model = (
            AutoModelForCausalLM.from_pretrained(
                self.checkpoint,
                trust_remote_code=True,
                dtype=torch.bfloat16,
                attn_implementation=self.attn_impl,
            )
            .to(self.device)
            .eval()
        )
        # 门控是可选能力:老 checkpoint 没有这个方法时降级为「不做门控」,
        # 而不是让整个服务起不来。
        self._gate_ok = hasattr(self._model, "streammind_gate_forward_segments")
        logger.info(
            "loaded in %.1fs (gate=%s)", time.time() - t0,
            "on" if self._gate_ok else "unavailable",
        )

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def gate_available(self) -> bool:
        return self._gate_ok

    @property
    def gate_error(self) -> str | None:
        return self._gate_error

    # ── 推理 ─────────────────────────────────────────────────────────────

    def _build_inputs(self, video_path: str, prompt: str, backend: str):
        messages = [{"role": "user", "content": [
            {"type": "video"}, {"type": "text", "text": prompt},
        ]}]
        text = self._processor.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        if backend == "codec":
            return self._processor(
                text=[text],
                videos=[video_path],
                video_backend="codec",
                max_pixels=self.max_pixels,
                # engine=hevc 是「传统编解码」通路(对 H.264/HEVC 都走这条);
                # patch=16 与 Mage-ViT 的 16x16 预测块对齐,不可随意改。
                codec_config={
                    "engine": "hevc",
                    "target_canvas": self.num_frames,
                    "patch": 16,
                },
                return_tensors="pt",
                padding=True,
            )
        return self._processor(
            text=[text],
            videos=[sample_frames(video_path, self.num_frames)],
            return_tensors="pt",
            padding=True,
        )

    def _to_device(self, inputs: dict) -> dict:
        out = {k: (v.to(self.device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        if "pixel_values" in out:
            out["pixel_values"] = out["pixel_values"].to(self._model.dtype)
        return out

    def _gate_probability(self, inputs: dict) -> float | None:
        """跑 StreamMind 门控,返回该段「值得开口」的概率。

        取每段最后一个视觉 token 位置的 logits 做 softmax(与官方 streaming
        脚本一致:按 image_grid_thw 的时间维累加求段边界)。门控不可用或
        计算失败时返回 None —— 门控是加分项,不该拖垮主感知。
        """
        if not self._gate_ok:
            return None
        import torch

        visual = {
            k: inputs[k]
            for k in ("pixel_values", "image_grid_thw", "patch_positions")
            if k in inputs
        }
        if "image_grid_thw" not in visual:
            return None
        try:
            with torch.inference_mode():
                logits = self._model.streammind_gate_forward_segments([visual])[0]
            boundary = int(visual["image_grid_thw"][:, 0].sum()) - 1
            probs = torch.softmax(logits[boundary].float(), dim=-1)
            return float(probs[1])
        except Exception as e:  # noqa: BLE001 —— 门控失败不影响 caption
            # 首次失败即熄灯:门控的失败因(缺 mamba_ssm 这类可选编译扩展)是
            # 环境级的,不会自愈。继续每段重试只会白烧时间,且 /health 会一直
            # 谎报「门控可用」误导排查。
            logger.warning("gate forward failed, disabling gate: %s", e)
            self._gate_ok = False
            self._gate_error = str(e)
            return None

    def perceive(
        self,
        video_path: str,
        rules: list[dict] | None = None,
        scene_ask: str | None = None,
        max_new_tokens: int = 256,
        want_gate: bool = True,
    ) -> dict:
        """对一段视频做一次感知,返回 caption + 逐条规则判定 + 门控概率。"""
        if not self.ready:
            raise RuntimeError("engine not loaded")
        rules = rules or []
        prompt = build_prompt(scene_ask or DEFAULT_SCENE_ASK, rules)
        # 段太短时 codec 分组凑不齐,先探帧数决定实际后端(降级而非报错)。
        backend = pick_backend(self.video_backend, probe_frame_count(video_path))

        import torch

        with self._lock:
            t0 = time.time()
            inputs = self._to_device(self._build_inputs(video_path, prompt, backend))
            t_prep = (time.time() - t0) * 1000

            gate_p = self._gate_probability(inputs) if want_gate else None

            t1 = time.time()
            with torch.inference_mode():
                out = self._model.generate(
                    **inputs, max_new_tokens=max_new_tokens, do_sample=False
                )
            t_gen = (time.time() - t1) * 1000
            raw = self._processor.tokenizer.decode(
                out[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True
            ).strip()

        caption, hits = parse_response(raw, rules)
        return {
            "caption": caption,
            "rule_hits": hits,
            "gate_p": gate_p,
            "raw": raw,
            "backend": backend,
            "timing_ms": {
                "prepare": round(t_prep, 1),
                "generate": round(t_gen, 1),
                "total": round(t_prep + t_gen, 1),
            },
        }


def ensure_codec_tool_on_path() -> str | None:
    """让 codec 通路能找到 ``cv-preinfer``,无论服务是怎么被拉起的。

    codec-video-prep 把 ``cv-preinfer`` 装在解释器同级的 bin/ 下。以
    ``/path/to/.venv/bin/python -m local_vision`` 这种**不激活 venv** 的方式
    启动时(systemd / supervisor / nohup 都是如此),该目录不在 PATH 上,
    模型侧会直接报「需要外部 cv-preinfer」而让整条 codec 通路失效 —— 且报错
    发生在推理时而非启动时,极难联想到是 PATH 问题。这里在启动阶段就把它定位好。
    """
    import shutil
    import sys

    if os.environ.get("CV_PREINFER_BIN"):
        return os.environ["CV_PREINFER_BIN"]
    found = shutil.which("cv-preinfer")
    if found:
        return found
    candidate = Path(sys.executable).parent / "cv-preinfer"
    if candidate.is_file():
        os.environ["CV_PREINFER_BIN"] = str(candidate)
        os.environ["PATH"] = f"{candidate.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        logger.info("located cv-preinfer at %s", candidate)
        return str(candidate)
    return None


def resolve_checkpoint(path: str) -> str:
    """本地目录直接用;否则当成 HF repo id 交给 transformers 自己解析。"""
    p = Path(path).expanduser()
    return str(p) if p.is_dir() else path
