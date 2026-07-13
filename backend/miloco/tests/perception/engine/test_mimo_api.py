"""MiMo API 独立测试脚本 — 隔离 pipeline，直接测试 API 在不同参数组合下的行为。

用法：
    cd server/packages/perception-engine/src

    # 一步到位：提取数据 + 跑测试
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_api all testcases/open_light.mp4

    # 分步：先提取数据
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_api prepare testcases/open_light.mp4

    # 再跑测试（可反复跑，不用重新提取）
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_api run

    # 只跑指定窗口
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_api run --windows 1,3,6
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TESTDATA_PATH = Path(__file__).parent.parent / "testcases" / "mimo_api_testdata.json"
RESULTS_PATH = Path(__file__).parent.parent / "testcases" / "mimo_api_results.json"

DEFAULT_MODEL = "xiaomi/mimo-v2.5"
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MAX_TOKENS = 1024


# ---------------------------------------------------------------------------
# Step 1: Prepare — extract test data from video
# ---------------------------------------------------------------------------


async def prepare(video_path: str):
    """Run Gate → Edge on each window, extract prompts/images/audio, save to JSON."""
    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.gate.gate import run_gate
    from miloco.perception.engine.identity.identity import run_identity
    from miloco.perception.engine.identity.tracking_service import (
        create_tracking_service,
    )
    from miloco.perception.engine.omni.prompt_builder import build_prompt
    from miloco.perception.engine.types import OmniContext, RuleCondition

    from .run_stream_test import simulate_stream

    config = PerceptionConfig()
    windows = simulate_stream(video_path, 3, 3)
    tracking = create_tracking_service("real")
    context = OmniContext(
        rule_conditions=[
            RuleCondition(
                rule_id="reading_light",
                rule_name="读书开灯",
                query="当前是否有人在读书",
            )
        ],
    )

    test_data: list[dict] = []

    for i, (input_slice, win_start) in enumerate(windows):
        gate_packet, _, _, _, _ = await run_gate(input_slice, config.gate)
        if gate_packet is None:
            print(f"  窗口 #{i + 1} ({win_start:.0f}s): SKIPPED (gate)")
            continue

        identity_packet = await run_identity(
            gate_packet, config.identity, tracking
        )

        # Build prompt payload (same as pipeline)
        payload = build_prompt(identity_packet, context)

        # Audio info
        audio_b64 = payload.get("audio_base64")
        audio_duration_ms = 0
        if audio_b64:
            wav_data = base64.b64decode(audio_b64)
            with wave.open(io.BytesIO(wav_data), "rb") as wf:
                audio_duration_ms = int(wf.getnframes() / wf.getframerate() * 1000)

        entry = {
            "window_idx": i + 1,
            "window_start_s": win_start,
            "speech_state": identity_packet.speech_state,
            "audio_type": identity_packet.audio_analysis.type.value,
            "audio_energy": round(identity_packet.audio_analysis.energy_level, 6),
            "system_prompt": payload["system_prompt"],
            "user_content": payload["user_content"],
            "images": payload.get("images", []),  # list of {data, media_type}
            "audio_base64": audio_b64,
            "audio_duration_ms": audio_duration_ms,
            "image_count": len(payload.get("images", [])),
        }
        test_data.append(entry)

        has_audio = "YES" if audio_b64 else "NO"
        print(
            f"  窗口 #{i + 1} ({win_start:.0f}s): "
            f"speech={identity_packet.speech_state} audio_type={identity_packet.audio_analysis.type.value} "
            f"images={entry['image_count']} audio={has_audio}({audio_duration_ms}ms)"
        )

    TESTDATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TESTDATA_PATH, "w", encoding="utf-8") as f:
        json.dump(test_data, f, ensure_ascii=False, indent=2)

    print(f"\n  已保存 {len(test_data)} 条测试数据到 {TESTDATA_PATH}")


# ---------------------------------------------------------------------------
# Step 2: Run — send API requests with different parameter combos
# ---------------------------------------------------------------------------

# Test configurations: (label, description, body_overrides, strip_audio)
TEST_CONFIGS = [
    (
        "A",
        "无音频",
        {},
        True,  # strip audio
    ),
    (
        "B",
        "有音频+全参数",
        {
            "temperature": 0.3,
            "top_p": 0.95,
            "thinking": {"type": "disabled"},
        },
        False,
    ),
    (
        "C",
        "有音频+无thinking",
        {
            "temperature": 0.3,
            "top_p": 0.95,
        },
        False,
    ),
    (
        "D",
        "有音频+无temp/top_p",
        {
            "thinking": {"type": "disabled"},
        },
        False,
    ),
    (
        "E",
        "有音频+最小参数",
        {},
        False,
    ),
]


def _build_messages(entry: dict, *, strip_audio: bool = False) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": entry["system_prompt"]}]

    content: list[dict] = [{"type": "text", "text": entry["user_content"]}]

    for img in entry.get("images", []):
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"},
            }
        )

    if not strip_audio and entry.get("audio_base64"):
        content.append(
            {
                "type": "input_audio",
                "input_audio": {"data": entry["audio_base64"], "format": "wav"},
            }
        )

    messages.append({"role": "user", "content": content})
    return messages


async def _call_api(
    messages: list[dict],
    model: str,
    base_url: str,
    api_key: str,
    max_tokens: int,
    extra_body: dict,
) -> dict:
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        **extra_body,
    }

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=body,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

    status = resp.status_code
    try:
        data = resp.json()
    except Exception:
        data = {}

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    finish = data.get("choices", [{}])[0].get("finish_reason", "")
    usage = data.get("usage", {})
    reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    audio_tokens = usage.get("prompt_tokens_details", {}).get("audio_tokens", 0)

    return {
        "status": status,
        "finish_reason": finish,
        "completion_tokens": usage.get("completion_tokens", 0),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "reasoning_tokens": reasoning,
        "audio_tokens": audio_tokens,
        "elapsed_ms": round(elapsed_ms),
        "content": content,
        "content_len": len(content),
    }


async def run_tests(window_filter: list[int] | None = None):
    """Load test data and run API tests for each window × config combo."""
    if not TESTDATA_PATH.exists():
        print(f"[ERROR] 测试数据不存在: {TESTDATA_PATH}")
        print("  请先运行: python -m engine.tests.test_mimo_api prepare <video>")
        sys.exit(1)

    with open(TESTDATA_PATH, encoding="utf-8") as f:
        test_data = json.load(f)

    api_key = os.environ.get("MILOCO_MODEL__OMNI__API_KEY", "")
    if not api_key:
        print("[ERROR] MILOCO_MODEL__OMNI__API_KEY 未设置")
        sys.exit(1)

    all_results: list[dict] = []

    for entry in test_data:
        win_idx = entry["window_idx"]
        if window_filter and win_idx not in window_filter:
            continue

        has_audio = entry.get("audio_base64") is not None
        audio_ms = entry.get("audio_duration_ms", 0)

        print(f"\n{'=' * 70}")
        print(
            f"窗口 #{win_idx} ({entry['window_start_s']:.0f}s) | "
            f"speech={entry['speech_state']} | audio_type={entry['audio_type']} | "
            f"送音频={'YES' if has_audio else 'NO'}({audio_ms}ms)"
        )
        print("-" * 70)

        for label, desc, extra_body, strip_audio in TEST_CONFIGS:
            # Skip audio tests for windows without audio
            if not strip_audio and not has_audio:
                continue

            messages = _build_messages(entry, strip_audio=strip_audio)

            try:
                result = await _call_api(
                    messages=messages,
                    model=DEFAULT_MODEL,
                    base_url=DEFAULT_BASE_URL,
                    api_key=api_key,
                    max_tokens=DEFAULT_MAX_TOKENS,
                    extra_body=extra_body,
                )
            except Exception as e:
                result = {
                    "status": 0,
                    "finish_reason": "error",
                    "completion_tokens": 0,
                    "prompt_tokens": 0,
                    "reasoning_tokens": 0,
                    "audio_tokens": 0,
                    "elapsed_ms": 0,
                    "content": str(e),
                    "content_len": 0,
                }

            ok = result["content_len"] > 0 and result["status"] == 200
            mark = "✓" if ok else "✗"
            content_preview = (
                result["content"][:80].replace("\n", "\\n")
                if result["content"]
                else "(empty)"
            )

            # Check if content is valid JSON
            json_ok = False
            if ok:
                try:
                    json.loads(result["content"])
                    json_ok = True
                except (json.JSONDecodeError, ValueError):
                    # Try extracting JSON
                    from miloco.perception.engine.omni.response_parser import (
                        extract_json,
                    )

                    try:
                        json.loads(extract_json(result["content"]))
                        json_ok = True
                    except Exception:
                        pass

            json_mark = " JSON✓" if json_ok else " JSON✗" if ok else ""

            print(
                f"  [{label}] {desc:<18} {mark} "
                f"status={result['status']} "
                f"tokens={result['prompt_tokens']}→{result['completion_tokens']} "
                f"reasoning={result['reasoning_tokens']} "
                f"audio_tok={result['audio_tokens']} "
                f"({result['elapsed_ms']}ms){json_mark}"
            )
            if not ok or not json_ok:
                print(f"      content: {content_preview}")

            all_results.append(
                {
                    "window_idx": win_idx,
                    "test": label,
                    "test_desc": desc,
                    "has_audio": not strip_audio and has_audio,
                    "extra_body_keys": list(extra_body.keys()),
                    **result,
                }
            )

    # Save results
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n完整结果已保存到 {RESULTS_PATH}")

    # Summary
    print(f"\n{'=' * 70}")
    print("汇总")
    print("-" * 70)
    for label, desc, _, _ in TEST_CONFIGS:
        subset = [r for r in all_results if r["test"] == label]
        if not subset:
            continue
        ok_count = sum(1 for r in subset if r["content_len"] > 0 and r["status"] == 200)
        json_count = sum(
            1
            for r in subset
            if r["content_len"] > 0 and r["status"] == 200 and _is_json(r["content"])
        )
        avg_ms = sum(r["elapsed_ms"] for r in subset) / len(subset) if subset else 0
        print(
            f"  [{label}] {desc:<18} {ok_count}/{len(subset)} 有内容  {json_count}/{len(subset)} 有效JSON  avg={avg_ms:.0f}ms"
        )


def _is_json(content: str) -> bool:
    try:
        json.loads(content)
        return True
    except (json.JSONDecodeError, ValueError):
        from miloco.perception.engine.omni.response_parser import extract_json

        try:
            json.loads(extract_json(content))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description="MiMo API 独立测试")
    parser.add_argument(
        "command",
        choices=["prepare", "run", "all"],
        help="prepare=提取数据, run=跑测试, all=两步一起",
    )
    parser.add_argument("video", nargs="?", help="视频路径 (prepare/all 时必需)")
    parser.add_argument(
        "--windows", type=str, default="", help="只测指定窗口，逗号分隔 (如 1,3,6)"
    )
    return parser.parse_args()


async def main():
    args = parse_args()

    window_filter = None
    if args.windows:
        window_filter = [int(x) for x in args.windows.split(",")]

    if args.command in ("prepare", "all"):
        if not args.video:
            print("[ERROR] prepare/all 模式需要指定视频路径")
            sys.exit(1)
        print(f"\n[准备] 从 {args.video} 提取测试数据...")
        await prepare(args.video)

    if args.command in ("run", "all"):
        print("\n[测试] 开始 MiMo API 测试...")
        await run_tests(window_filter)


if __name__ == "__main__":
    asyncio.run(main())
