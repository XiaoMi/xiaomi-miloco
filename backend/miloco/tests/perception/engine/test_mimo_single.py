"""MiMo API 单条测试 — 从 testdata 中选一条，手动控制参数逐步排查。

用法：
    cd server/packages/perception-engine/src

    # 测试窗口 #2（无音频），默认参数
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_single --window 2

    # 测试窗口 #3（有音频），不送音频
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_single --window 3 --no-audio

    # 自定义参数
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_single --window 1 --max-tokens 2048 --temperature 1.0

    # 不带 thinking 参数
    MILOCO_MODEL__OMNI__API_KEY=... uv run python -m engine.tests.test_mimo_single --window 1 --no-thinking
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

TESTDATA_PATH = Path(__file__).parent.parent / "testcases" / "mimo_api_testdata.json"


def _build_messages(entry: dict, *, strip_audio: bool = False) -> list[dict]:
    messages = [{"role": "system", "content": entry["system_prompt"]}]
    content = [{"type": "text", "text": entry["user_content"]}]

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


async def run_single(args):
    if not TESTDATA_PATH.exists():
        print(f"[ERROR] 测试数据不存在: {TESTDATA_PATH}")
        print("  先运行: python -m engine.tests.test_mimo_api prepare <video>")
        sys.exit(1)

    with open(TESTDATA_PATH, encoding="utf-8") as f:
        test_data = json.load(f)

    entry = None
    for e in test_data:
        if e["window_idx"] == args.window:
            entry = e
            break
    if entry is None:
        print(
            f"[ERROR] 窗口 #{args.window} 不存在 (可选: {[e['window_idx'] for e in test_data]})"
        )
        sys.exit(1)

    api_key = os.environ.get("MILOCO_MODEL__OMNI__API_KEY", "")
    if not api_key:
        print("[ERROR] MILOCO_MODEL__OMNI__API_KEY 未设置")
        sys.exit(1)

    has_audio = entry.get("audio_base64") is not None
    strip_audio = args.no_audio or not has_audio
    messages = _build_messages(entry, strip_audio=strip_audio)

    # Build request body
    body: dict = {
        "model": args.model,
        "messages": messages,
        "max_tokens": args.max_tokens,
        "stream": False,
    }
    if args.temperature is not None:
        body["temperature"] = args.temperature
    if args.top_p is not None:
        body["top_p"] = args.top_p
    if not args.no_thinking:
        body["thinking"] = {"type": "disabled"}

    # Print request info
    audio_status = "NO" if strip_audio else f"YES({entry['audio_duration_ms']}ms)"
    print(f"\n{'=' * 70}")
    print(f"窗口 #{entry['window_idx']} ({entry['window_start_s']:.0f}s)")
    print(f"  audio_type={entry['audio_type']}")
    print(f"  images={entry['image_count']} audio={audio_status}")
    print("\n--- 请求参数 ---")
    body_info = {k: v for k, v in body.items() if k != "messages"}
    print(f"  {json.dumps(body_info, ensure_ascii=False)}")
    print("\n--- system_prompt ---")
    print(f"  {entry['system_prompt']}")
    print("\n--- user_content ---")
    for line in entry["user_content"].split("\n"):
        print(f"  {line}")

    # Send request
    print("\n--- 发送请求 ---")
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{args.base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            json=body,
        )
    elapsed = (time.monotonic() - t0) * 1000

    print(f"  status: {resp.status_code}")
    print(f"  elapsed: {elapsed:.0f}ms")

    try:
        data = resp.json()
    except Exception:
        print("  [ERROR] 无法解析 JSON 响应")
        print(f"  body: {resp.text[:500]}")
        return

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    finish = data.get("choices", [{}])[0].get("finish_reason", "")
    usage = data.get("usage", {})
    reasoning = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    audio_tokens = usage.get("prompt_tokens_details", {}).get("audio_tokens", 0)

    print("\n--- 响应 ---")
    print(f"  finish_reason: {finish}")
    print(f"  prompt_tokens: {usage.get('prompt_tokens', 0)}")
    print(f"  completion_tokens: {usage.get('completion_tokens', 0)}")
    print(f"  reasoning_tokens: {reasoning}")
    print(f"  audio_tokens: {audio_tokens}")
    print(f"  content_len: {len(content)}")

    print("\n--- content ---")
    if content:
        print(content)
    else:
        print("  (empty)")

    # Try parse
    if content:
        try:
            _parsed = json.loads(content)
            print("\n--- JSON 解析: ✓ 直接解析成功 ---")
        except json.JSONDecodeError:
            from miloco.perception.engine.omni.response_parser import extract_json

            extracted = extract_json(content)
            try:
                _parsed = json.loads(extracted)
                print("\n--- JSON 解析: ✓ extract_json 成功 ---")
            except json.JSONDecodeError:
                print("\n--- JSON 解析: ✗ 失败 ---")
                print(f"  extracted: {extracted[:200]}")


def parse_args():
    parser = argparse.ArgumentParser(description="MiMo API 单条测试")
    parser.add_argument("--window", type=int, required=True, help="窗口编号")
    parser.add_argument("--no-audio", action="store_true", help="不发送音频")
    parser.add_argument(
        "--no-thinking", action="store_true", help="不发送 thinking:disabled"
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--top-p", type=float, default=None)
    parser.add_argument("--model", default="mimo-v2.6-flash")
    parser.add_argument("--base-url", default="https://api.xiaomimimo.com/v1")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run_single(parse_args()))
