"""Gemini 媒体控制探针 —— 验证 Gemini 是否真正遵从 Miloco 的推理视频语义
（fps / 分辨率），而非只接收字节后按自己的固定档位处理。

这是**手动探针脚本**（非 pytest 单测），需真实 Gemini key + 你自己的视频 clip。
它复用生产同款编码器 ``_encode_video_mp4`` 与真实 ``GeminiAdapter``，把请求打到
Gemini 原生 ``generateContent``，读回 ``usageMetadata`` 的 VIDEO/AUDIO/prompt token
分解——token 是 Gemini 侧「实际怎么处理这段视频」的唯一可观测证据。

零生产代码改动：``media_resolution`` 由本脚本在 body 上就地注入（生产未接该轴，
待实验确认映射语义后再定）。

--------------------------------------------------------------------------
为什么要 --fps 与 --meta-fps 解耦
--------------------------------------------------------------------------
- ``--fps N``      控制**编码进 mp4 的真实帧数**（6s × N fps = 6N 帧）。
- ``--meta-fps M`` 控制**告诉 Gemini 的 video_metadata.fps**（默认跟随 --fps）。

关键实验：固定一份高帧率字节（如 --fps 3），只改 --meta-fps 1 vs 3。
  - VIDEO token 随 meta-fps 变       → Gemini 真按 video_metadata.fps 抽帧（fps="exact/approximate"）。
  - VIDEO token 不变（只跟随真实帧数） → video_metadata.fps 被忽略，服务端按自己的档位处理（fps="unsupported"）。

--------------------------------------------------------------------------
验收矩阵（示例命令）
--------------------------------------------------------------------------
    export MILOCO_MODEL__OMNI__API_KEY=<gemini-key>
    B=https://generativelanguage.googleapis.com/v1beta

    # FPS 轴（编码帧数 + 声明 fps 同步变）
    for f in 1 2 3; do
      uv run python -m miloco.tests... --video clip.mp4 --duration 6 --fps $f --base-url $B
    done

    # FPS 是否被真正遵从（同一份字节，只改声明 fps）
    uv run python -m ...  --video clip.mp4 --fps 3 --meta-fps 1 --base-url $B
    uv run python -m ...  --video clip.mp4 --fps 3 --meta-fps 3 --base-url $B

    # 分辨率轴（本地像素短边）
    for s in 360 512 768 1080; do
      uv run python -m ...  --video clip.mp4 --fps 1 --short-edge $s --base-url $B
    done

    # 分辨率轴（Gemini token 档位）
    for r in low medium high; do
      uv run python -m ...  --video clip.mp4 --fps 1 --media-resolution $r --base-url $B
    done

每次记录 (fps, meta-fps, short_edge, media_resolution, frame_count, resolution)
→ (prompt_tokens, video_tokens, audio_tokens)，即可判定 Gemini 是否遵从各轴语义。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
from miloco.perception.engine.omni.prompt_builder import _encode_video_mp4
from miloco.perception.engine.omni.provider import GeminiAdapter

_DEFAULT_PROMPT = (
    "用中文简要描述这段视频里发生了什么，重点说清楚人/宠物/物体的动作与任何快速事件。"
)


def _decode_frames(video_path: str, duration_s: float, target_fps: int) -> tuple[list, int]:
    """读源视频前 ``duration_s`` 秒，按 ``target_fps`` 均匀抽帧，返回 (BGR 帧列表, 源 fps)。

    抽帧 stride = round(src_fps / target_fps)；与生产 pipeline「下采到 omni_fps」同构。
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[ERROR] 无法打开视频: {video_path}")
        sys.exit(1)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if src_fps <= 0:
        print("[WARN] 源视频无 fps 元数据，按 30 估算")
        src_fps = 30.0

    max_src_frames = int(duration_s * src_fps)
    stride = max(1, round(src_fps / target_fps))
    frames: list = []
    idx = 0
    while idx < max_src_frames:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            frames.append(frame)
        idx += 1
    cap.release()
    return frames, int(round(src_fps))


def _find_video_part(body: dict) -> dict | None:
    """在 Gemini body.contents[].parts[] 里定位含视频的 inline_data part。"""
    for content in body.get("contents", []):
        for part in content.get("parts", []):
            inline = part.get("inline_data")
            if isinstance(inline, dict) and str(inline.get("mime_type", "")).startswith("video/"):
                return part
    return None


def _sum_modality_tokens(usage_metadata: dict, modality: str) -> int:
    total = 0
    for entry in usage_metadata.get("promptTokensDetails") or []:
        if entry.get("modality") == modality and entry.get("tokenCount") is not None:
            total += int(entry["tokenCount"])
    return total


async def run(args) -> None:
    api_key = os.environ.get("MILOCO_MODEL__OMNI__API_KEY", "")
    if not api_key:
        print("[ERROR] MILOCO_MODEL__OMNI__API_KEY 未设置")
        sys.exit(1)
    if not Path(args.video).exists():
        print(f"[ERROR] 视频不存在: {args.video}")
        sys.exit(1)

    frames, src_fps = _decode_frames(args.video, args.duration, args.fps)
    if not frames:
        print("[ERROR] 未解出任何帧")
        sys.exit(1)

    # 生产同款编码：BGR 帧 + 空音频（本探针聚焦视频 token 轴）→ mp4，按 short_edge 缩放、按 fps 编码。
    video_b64, media_info = _encode_video_mp4(
        frames,
        np.empty(0, dtype=np.int16),
        sample_rate=16000,
        fps=args.fps,
        short_edge=args.short_edge,
    )
    if not video_b64 or media_info is None:
        print("[ERROR] 编码失败")
        sys.exit(1)

    adapter = GeminiAdapter()
    messages = [
        {"role": "system", "content": "你是家庭场景视频理解助手。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": args.prompt},
                adapter.build_video_block(video_b64, media_info),
            ],
        },
    ]
    body = adapter.build_request_body(
        messages,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
    )

    # --- 解耦注入：video_metadata.fps 覆盖 + media_resolution（生产未接该轴，仅探针用）---
    video_part = _find_video_part(body)
    meta_fps = args.meta_fps if args.meta_fps is not None else args.fps
    if video_part is not None:
        video_part["video_metadata"] = {"fps": meta_fps}
    if args.media_resolution:
        body["generationConfig"]["mediaResolution"] = (
            f"MEDIA_RESOLUTION_{args.media_resolution.upper()}"
        )

    url = adapter.endpoint(args.base_url, args.model, stream=False)
    headers = {"Content-Type": "application/json", **adapter.auth_headers(api_key)}

    print(f"\n{'=' * 72}")
    print("--- 本地编码参数 ---")
    print(f"  源视频 fps≈{src_fps} | duration={args.duration}s")
    print(f"  encode_fps={args.fps} → frame_count={media_info.frame_count}")
    print(f"  short_edge={args.short_edge} → resolution={media_info.video_width}x{media_info.video_height}")
    print(f"  video_b64_len={len(video_b64)} (~{len(video_b64) * 3 // 4 // 1024}KB)")
    print("--- 发给 Gemini 的媒体控制参数 ---")
    print(f"  video_metadata.fps={meta_fps}")
    print(f"  mediaResolution={body['generationConfig'].get('mediaResolution', '(default)')}")
    print(f"  model={args.model}")
    print(f"  endpoint={url}")

    print("\n--- 发送请求 ---")
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
    elapsed = (time.monotonic() - t0) * 1000
    print(f"  status={resp.status_code} elapsed={elapsed:.0f}ms")
    if resp.status_code != 200:
        print(f"  [ERROR] {resp.text[:800]}")
        return

    raw = resp.json()
    um = raw.get("usageMetadata", {}) or {}
    video_tokens = _sum_modality_tokens(um, "VIDEO")
    audio_tokens = _sum_modality_tokens(um, "AUDIO")
    text_tokens = _sum_modality_tokens(um, "TEXT")

    print("\n--- usageMetadata（Gemini 侧实际处理证据）---")
    print(f"  promptTokenCount     = {um.get('promptTokenCount')}")
    print(f"  candidatesTokenCount = {um.get('candidatesTokenCount')}")
    print(f"  totalTokenCount      = {um.get('totalTokenCount')}")
    print(f"  cachedContentToken   = {um.get('cachedContentTokenCount')}")
    print(f"  ├─ VIDEO tokens = {video_tokens}")
    print(f"  ├─ AUDIO tokens = {audio_tokens}")
    print(f"  └─ TEXT  tokens = {text_tokens}")
    print(f"  promptTokensDetails(raw) = {json.dumps(um.get('promptTokensDetails'), ensure_ascii=False)}")

    # 经真实 adapter 反解析回 OpenAI 形态，验证下游可消费
    parsed = adapter.parse_response(raw)
    content = parsed["choices"][0]["message"]["content"] if parsed["choices"] else ""
    print("\n--- 模型输出 ---")
    print(content[:800] if content else "  (empty)")

    # 一行 CSV，便于把多次运行汇总成矩阵
    print("\n--- CSV ---")
    print("encode_fps,meta_fps,short_edge,media_resolution,frame_count,resolution,prompt_tokens,video_tokens,audio_tokens")
    print(
        f"{args.fps},{meta_fps},{args.short_edge},{args.media_resolution or 'default'},"
        f"{media_info.frame_count},{media_info.video_width}x{media_info.video_height},"
        f"{um.get('promptTokenCount')},{video_tokens},{audio_tokens}"
    )


def parse_args():
    p = argparse.ArgumentParser(description="Gemini 媒体控制探针（fps/分辨率验收）")
    p.add_argument("--video", required=True, help="源视频文件路径")
    p.add_argument("--duration", type=float, default=6.0, help="取前 N 秒（默认 6）")
    p.add_argument("--fps", type=int, default=1, help="编码进 mp4 的目标帧率")
    p.add_argument(
        "--meta-fps", type=int, default=None,
        help="告诉 Gemini 的 video_metadata.fps；默认跟随 --fps。设置后与编码帧数解耦，用于验证 fps 是否被真正遵从",
    )
    p.add_argument("--short-edge", type=int, default=512, help="本地编码像素短边")
    p.add_argument(
        "--media-resolution", choices=["low", "medium", "high"], default=None,
        help="Gemini generationConfig.mediaResolution 档位（token 预算，非像素）",
    )
    p.add_argument("--prompt", default=_DEFAULT_PROMPT)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--temperature", type=float, default=0.1)
    p.add_argument("--top-p", type=float, default=0.95)
    p.add_argument("--model", default="gemini-3-flash-preview")
    p.add_argument("--base-url", default="https://generativelanguage.googleapis.com/v1beta")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
