#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地视频 clip → omni 输入 → 模型结果（无需摄像头、无需后端进程）。

在没有摄像头时用本地视频文件固定输入画面，直接走感知引擎的 prompt 构建与 omni
调用链路，本地验证模型对「待判断规则」/ 场景理解的输出——规则调参、prompt 迭代、
模型对比都靠它。

用法示例：
  # rule_only（默认）：只判定规则命中
  python scripts/clip_omni.py ~/clip.mp4 \
      --rule "手扫地：人手出现在画面中" \
      --rule "床上看书自动开灯：有人坐在床上看书"

  # 完整感知（caption / suggestions / env_sounds，规则条件照常注入）
  python scripts/clip_omni.py ~/clip.mp4 --full

  # 临时覆盖 rule_only system prompt（等价于配置 rule_only_system_prompt，仅本次运行生效）
  python scripts/clip_omni.py ~/clip.mp4 --rule "比耶开灯：摆出比耶手势" \
      --system-prompt "你是一个规则判定引擎：只依据本轮画面判定规则，只输出 JSON。"

  # 省 token：rule_only 图片模式（只发窗口末帧 JPEG）
  python scripts/clip_omni.py ~/clip.mp4 --rule "手扫地：人手出现在画面中" --image

模型地址 / API Key 取自 $MILOCO_HOME/config.json（model.omni）或环境变量。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# ── 引导：让脚本能 import 仓库内的 miloco 包（不依赖安装） ───────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_SRC = _REPO_ROOT / "backend" / "miloco" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))


# =============================================================================
# 规则解析
# =============================================================================


def parse_rules(items: list[str]) -> list:
    """把 ``--rule "名称：判定条件"`` 解析为 RuleCondition 列表。

    分隔符支持中文冒号「：」与英文冒号「:」，取第一个；无分隔符时整串同时充当
    名称与条件。rule_id 用稳定前缀 ``local-N``（仅本工具内部用）。
    """
    from miloco.perception.engine.types import RuleCondition

    rules: list[RuleCondition] = []
    for i, item in enumerate(items):
        item = (item or "").strip()
        if not item:
            continue
        name = query = item
        for sep in ("：", ":"):
            if sep in item:
                name, query = item.split(sep, 1)
                break
        rules.append(
            RuleCondition(
                rule_id=f"local-{i}",
                rule_name=name.strip() or f"local-{i}",
                query=query.strip(),
            )
        )
    return rules


# =============================================================================
# clip → 输入
# =============================================================================


def load_snapshot(clip_path: str, room: str = "本地测试", fps: float | None = 1.0):
    """解码本地视频为 DeviceSnapshot（InputSlice）：BGR 帧 + 音频（按需抽帧）。"""
    from miloco.perception.types import PerceptionDevice
    from miloco.perception.utils import snapshot_from_video

    path = Path(clip_path).expanduser()
    if not path.exists():
        raise SystemExit(f"clip 不存在: {path}")
    device = PerceptionDevice(
        did="clip", name="本地测试", device_type="camera", room_name=room
    )
    try:
        snap = snapshot_from_video(str(path), device, target_fps=fps)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"解码 clip 失败: {exc}") from exc
    if not snap.video or not snap.video.frames:
        raise SystemExit(f"clip 没有可解码的画面帧: {path}")
    return snap


def packet_from_snapshot(snap, room: str = "本地测试", fps: float | None = 1.0):
    """从 snapshot 构建 IdentityPacket（无音频、无身份，供直接 build_prompt 用）。"""
    import numpy as np

    from miloco.perception.engine.types import (
        AudioAnalysis,
        AudioType,
        FrameInfo,
        GateTrigger,
        IdentityPacket,
        MotionState,
    )

    frames = [f.data for f in snap.video.frames]
    return IdentityPacket(
        packet_id="clip",
        room_name=room,
        timestamp=snap.start_timestamp,
        frame_info=FrameInfo(
            start_timestamp=snap.start_timestamp,
            end_timestamp=snap.end_timestamp,
            fps=int(fps or 1) or 1,
        ),
        targets=[],
        scene_motion=MotionState.STATIC,
        frames=[],
        all_frames=frames,
        # 无音频：不喂音频、不烧 token，也避免模型脑补人声/环境音
        audio_clip=np.zeros(1, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE, is_urgent=False, energy_level=0.0
        ),
        trigger=GateTrigger(
            visual_changed=True,
            visual_change_score=1.0,
            audio_active=False,
            audio_energy_level=0.0,
            speech_active=False,
        ),
    )


# =============================================================================
# 临时设置（仅本进程、运行后恢复）
# =============================================================================


def apply_temp_settings(system_prompt: str | None, image_mode: bool):
    """临时改写感知引擎设置：rule_only system prompt 覆盖 / 图片输入模式。

    get_settings() 是进程级缓存单例，build_system_prompt 与媒体模式选择都实时读它，
    所以直接改 dict 即可本次生效。返回 ``restore()`` 恢复函数。
    """
    from miloco.config import get_settings

    engine = get_settings().perception.engine
    saved: dict[str, object] = {}
    if system_prompt is not None:
        saved["rule_only_system_prompt"] = engine.get("rule_only_system_prompt", "")
        engine["rule_only_system_prompt"] = system_prompt
    if image_mode:
        inp = engine.setdefault("input", {})
        saved["_input_rule_only_input"] = inp.get("rule_only_input", "video")
        inp["rule_only_input"] = "image"

    def restore() -> None:
        if "rule_only_system_prompt" in saved:
            engine["rule_only_system_prompt"] = saved["rule_only_system_prompt"]
        if "_input_rule_only_input" in saved:
            engine.setdefault("input", {})["rule_only_input"] = saved["_input_rule_only_input"]

    return restore


# =============================================================================
# 执行路径
# =============================================================================


async def run_rule_only(snap, ctx, config):
    """生产链路：run_pipeline（gate → rule_only packet → omni → 解析 matched_rules）。"""
    from miloco.perception.engine.pipeline import run_pipeline

    return await run_pipeline(snap, ctx, config)


async def run_full(ep, ctx, config):
    """完整感知链路：直接 build_prompt + call_omni（无身份/无 tracker）。"""
    from miloco.perception.engine.omni.omni_client import (
        call_omni,
        resolve_live_omni_config,
    )
    from miloco.perception.engine.omni.prompt_builder import build_prompt

    omni_cfg = resolve_live_omni_config(config.omni)
    payload = build_prompt(ep, ctx)
    raw = await call_omni(payload, omni_cfg)
    return payload, raw


# =============================================================================
# 结果渲染
# =============================================================================


def strip_json_fences(content: str) -> str:
    """去掉模型输出常见的 ```json ... ``` 围栏，返回纯 JSON 文本。"""
    content = (content or "").strip()
    if content.startswith("```"):
        first_nl = content.find("\n")
        if first_nl != -1:
            content = content[first_nl + 1 :]
        if content.endswith("```"):
            content = content[:-3]
    return content.strip()


def render_rule_only(rules, omni_output) -> list[str]:
    """把解析结果渲染成行：命中的规则（hit=true）标 ✅，其余标未命中。"""
    matched = {m.rule_id: m for m in (omni_output.matched_rules if omni_output else [])}
    lines: list[str] = []
    for rc in rules:
        m = matched.get(rc.rule_id)
        if m is not None:
            lines.append(f"✅ 命中    [{m.rule_name}]  {rc.query}")
            lines.append(f"   reason: {m.reason}")
        else:
            lines.append(f"⬜ 未命中  [{rc.rule_name}]  {rc.query}")
    return lines


def render_full(content: str) -> list[str]:
    """把 --full 的原始模型 JSON 渲染成可读行。"""
    try:
        data = json.loads(strip_json_fences(content))
    except json.JSONDecodeError:
        return [content]

    lines: list[str] = []
    if data.get("caption"):
        lines.append("📝 caption:")
        for c in data["caption"]:
            if isinstance(c, dict):
                lines.append(f"   - {c.get('description') or c.get('text') or c}")
            else:
                lines.append(f"   - {c}")
    if data.get("speeches"):
        lines.append("🗣 speeches:")
        for sp in data["speeches"]:
            speaker = sp.get("speaker", "") if isinstance(sp, dict) else ""
            content_t = sp.get("content", "") if isinstance(sp, dict) else sp
            lines.append(f"   - [{speaker}] {content_t}")
    if data.get("env_sounds"):
        lines.append("🔊 env_sounds: " + ", ".join(data["env_sounds"]))
    if data.get("matched_rules"):
        lines.append("🎯 matched_rules:")
        for m in data["matched_rules"]:
            if isinstance(m, dict) and m.get("hit"):
                lines.append(f"   - ✅ [{m.get('rule_name')}] {m.get('reason')}")
    if data.get("suggestions"):
        lines.append("⚠️ suggestions:")
        for s in data["suggestions"]:
            if isinstance(s, dict):
                lines.append(
                    f"   - [{s.get('urgency')}] {s.get('event')} → {s.get('action')}"
                )
    if not lines:
        lines.append(content)
    return lines


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clip_omni",
        description="本地视频 clip → omni 输入 → 模型结果（无需摄像头）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="模型地址 / API Key 取自 $MILOCO_HOME/config.json（model.omni）或环境变量。",
    )
    parser.add_argument("clip", help="本地视频文件路径（mp4 等，cv2 可解码即可）")
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="名称：判定条件",
        help="待判断规则，可重复传；如 --rule \"手扫地：人手出现在画面中\"",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="完整感知模式（caption/suggestions/env_sounds 等）；默认只出 matched_rules",
    )
    parser.add_argument(
        "--image",
        action="store_true",
        help="rule_only 输入改为窗口末帧 JPEG（省 token）；默认发 mp4 视频",
    )
    parser.add_argument(
        "--system-prompt",
        help="临时覆盖 rule_only system prompt（等价于配置 rule_only_system_prompt，仅本次运行）",
    )
    parser.add_argument("--time", help="注入当前时间（HH:MM:SS），可选")
    parser.add_argument("--room", default="本地测试", help="房间名（默认 本地测试）")
    parser.add_argument("--fps", type=float, default=1.0, help="抽帧帧率（默认 1）")
    parser.add_argument("--json", dest="raw_json", action="store_true", help="额外打印模型原始输出 JSON")
    parser.add_argument("--verbose", action="store_true", help="打印 system prompt 与 user content")
    args = parser.parse_args(argv)

    rules = parse_rules(args.rule)
    snap = load_snapshot(args.clip, room=args.room, fps=args.fps)

    from miloco.perception.engine.config import PerceptionConfig
    from miloco.perception.engine.types import OmniContext

    config = PerceptionConfig(rule_only=not args.full)
    restore = apply_temp_settings(args.system_prompt, args.image)
    try:
        ctx = OmniContext(
            rule_only=not args.full,
            rule_conditions=rules,
            current_time=args.time,
            room_name=args.room,
        )

        if args.full:
            ep = packet_from_snapshot(snap, room=args.room, fps=args.fps)
            payload, raw = asyncio.run(run_full(ep, ctx, config))
            if args.verbose:
                print("═══ system prompt ═══")
                print(payload["system_prompt"])
                print("\n═══ user content ═══")
                print(payload["user_content"])
            print("\n═══ 模型结果 ═══")
            content = (
                (raw.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            for line in render_full(content):
                print(line)
            if args.raw_json:
                print("\n═══ 原始输出 ═══")
                print(content)
            usage = raw.get("usage")
            if usage:
                print(
                    f"\n[usage] prompt={usage.get('prompt_tokens')} "
                    f"completion={usage.get('completion_tokens')} "
                    f"video={usage.get('prompt_tokens_details', {}).get('video_tokens')} "
                    f"cached={usage.get('prompt_tokens_details', {}).get('cached_tokens')}"
                )
        else:
            result = asyncio.run(run_rule_only(snap, ctx, config))
            if result.skipped:
                print("pipeline skipped（gate 未放行，静态 clip 理论不会发生）")
            out = result.omni_output
            if args.verbose:
                # run_pipeline 已解析；要打印 prompt 需重建 payload（同一 ctx），仅调试用
                ep = packet_from_snapshot(snap, room=args.room, fps=args.fps)
                from miloco.perception.engine.omni.prompt_builder import build_prompt

                payload = build_prompt(ep, ctx)
                print("═══ system prompt ═══")
                print(payload["system_prompt"])
                print("\n═══ user content ═══")
                print(payload["user_content"])
            print("\n═══ 模型结果 ═══")
            for line in render_rule_only(rules, out):
                print(line)
            if not rules:
                print("（未传 --rule，仅打印命中的规则）")
                for m in (out.matched_rules if out else []):
                    print(f"✅ 命中    [{m.rule_name}]  {m.reason}")
            if args.raw_json and out is not None:
                print("\n═══ 原始输出 ═══")
                print(out.model_dump_json(indent=2))
            if result.timing:
                print(
                    "\n[timing] gate_ms=%s identity_ms=%s omni_ms=%s total_ms=%s"
                    % (
                        result.timing.get("gate_ms"),
                        result.timing.get("identity_ms"),
                        result.timing.get("omni_ms"),
                        result.timing.get("total_ms"),
                    )
                )
            if out is not None and out.usage:
                print(
                    f"[usage] prompt={out.usage.get('prompt_tokens')} "
                    f"completion={out.usage.get('completion_tokens')} "
                    f"video={out.usage.get('video_tokens')} "
                    f"cached={out.usage.get('cached_tokens')}"
                )
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 调用失败: {exc}", file=sys.stderr)
        if args.verbose:
            raise
        return 1
    finally:
        restore()


if __name__ == "__main__":
    raise SystemExit(main())
