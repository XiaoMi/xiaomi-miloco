# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""按一次画面观测生成一段文本（规则生成式动作用）。

与主动查询（``on_demand_perceive``）是两条链路：这里只取每个源最近一帧、
缩成 JPEG crop，直接调一次 omni；不带视频、不写主动查询日志、不占引擎推理锁。
与 ``pet/observe.py`` 的「自定义 system prompt + crops + call_omni + 解析
JSON」同源。

输出契约由本模块持有：模型只负责填 ``{"result": "<text>"}``，调用方只配语义
prompt，不关心 JSON 格式。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

import cv2
import numpy as np

from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import response_parser
from miloco.perception.engine.omni.omni_client import (
    call_omni,
    resolve_live_omni_config,
)
from miloco.utils.common import safe_log

if TYPE_CHECKING:
    from miloco.perception.collect.collector import MultimodalCollector

logger = logging.getLogger(__name__)

# 一次生成最多看几路；控制 payload 与网关压力（显式配置也受同一上限约束）。
MAX_SOURCES = 3
SHORT_EDGE = 640
LONG_EDGE = 1280
JPEG_QUALITY = 85

# 提示词要求「不超过 max_chars 个字」，但模型常把标点排除在「字」外（一句
# 35 字的话实际会有近 40 个字符），也可能轻微超写。硬截在 max_chars 会把
# 最后一句话切在词中间，TTS 听起来就是播报不全；先留 5 个字符的软容差。
TRUNCATION_MARGIN = 5

# 为了收在完整的一句话上，软容差之外再看最多 5 个字符找句末标点（35 字
# 档位的最终硬上限是 45）。找不到句末标点时仍退回 max_chars + 5，不把
# 无标点的长句无条件放宽。
SENTENCE_LOOKAHEAD = 5
SENTENCE_ENDS = frozenset("。！？!?")
SENTENCE_CLOSERS = frozenset("”’」』）)】》〉")

# 固定的 JSON 契约。prompt 里只能写「生成什么」，格式要求写在这里，避免每个
# 调用方各写一份、解析器还得兼容多种字段名。
GENERATE_SYSTEM_PROMPT = """你是画面观察与文案生成助手。
只根据画面中可见的信息生成文案；看不清的特征不要猜，不要编造画面里没有的内容。
只输出一个 JSON 对象，不要 markdown、不要解释、不要思考过程：
{{"result":"<不超过 {max_chars} 个字的文案>"}}
画面中没有合适的目标、或无法按要求生成时，输出 {{"result":""}}。"""


class GenerateError(Exception):
    """生成失败。``code`` 是规则日志里稳定可检索的分类。"""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


def _downscale(frame: np.ndarray) -> np.ndarray:
    """短边不超过 640、长边不超过 1280；不放大。

    只缩不放：小图放大既不会增加信息，还会把 payload 撑大。
    """
    h, w = frame.shape[:2]
    if h <= 0 or w <= 0:
        raise GenerateError("no_frame", "empty frame")
    scale = min(1.0, SHORT_EDGE / min(h, w), LONG_EDGE / max(h, w))
    if scale >= 1.0:
        return frame
    return cv2.resize(
        frame,
        (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def _jpeg_b64(frame: np.ndarray) -> str:
    ok, buf = cv2.imencode(
        ".jpg", np.ascontiguousarray(frame),
        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
    )
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else ""


def _truncate_for_tts(text: str, max_chars: int) -> str:
    """按 ``max_chars`` 截断，优先收尾在完整的一句上。

    软上限是 ``max_chars + TRUNCATION_MARGIN``：模型把标点排除在「字」外
    时通常会多写几个字符，落在这里的文案原样保留。

    超出软上限时，在 ``max_chars + TRUNCATION_MARGIN + SENTENCE_LOOKAHEAD``
    以内倒着找最后一个句末标点；找到就收在它（及其后的收尾引号/括号）之后。
    句末标点早于 ``max(2, max_chars // 2)`` 的不采用——宁可硬切，也不为
    对齐一个很早的句号把文案砍得过短。一个句末标点都没有时退回软上限硬切。

    返回值长度不超过 ``max_chars + TRUNCATION_MARGIN + SENTENCE_LOOKAHEAD``。
    """
    soft = max_chars + TRUNCATION_MARGIN
    if len(text) <= soft:
        return text

    window = text[: soft + SENTENCE_LOOKAHEAD]
    floor = max(2, max_chars // 2)
    for i in range(len(window) - 1, floor - 1, -1):
        if window[i] not in SENTENCE_ENDS:
            continue
        end = i + 1
        # 句号后紧跟的右引号/右括号一起保留，避免 TTS 文本里落单。
        while end < len(window) and window[end] in SENTENCE_CLOSERS:
            end += 1
        return window[:end].rstrip()
    return text[:soft].rstrip()


async def generate_text(
    collector: MultimodalCollector,
    *,
    prompt: str,
    sources: list[str],
    max_chars: int = 30,
) -> str:
    """取 ``sources`` 的最近帧生成一段文本。

    失败一律抛 :class:`GenerateError`，``code`` 取值：

    - ``no_source``：没有可看的相机
    - ``no_frame``：所有源都取不到最近帧（或编码失败）
    - ``omni_error``：模型调用超时 / 熔断 / 网络错误
    - ``invalid_json``：返回不是可解析的 ``{"result": "<str>"}``
    - ``empty_result``：画面里没有合适目标，模型按契约返回空串
    """
    if not sources:
        raise GenerateError("no_source")

    crops: list[dict[str, str]] = []
    for did in sources[:MAX_SOURCES]:
        frame = collector.peek_latest_frame(did)
        if frame is None:
            logger.debug("generate: no frame for did=%s", safe_log(did))
            continue
        try:
            b64 = _jpeg_b64(_downscale(frame))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "generate: encode failed for did=%s: %s", safe_log(did), safe_log(e)
            )
            continue
        if b64:
            crops.append({"media_type": "image/jpeg", "data": b64})
    if not crops:
        raise GenerateError("no_frame")

    payload = {
        "system_prompt": GENERATE_SYSTEM_PROMPT.format(max_chars=max_chars),
        "user_content": prompt,
        "crops": crops,
    }
    # 温度固定 0.5：感知默认 0.1 对「夸夸」这类生成偏确定，同一件衣服容易每次
    # 一模一样；0.7 又可能换来措辞漂移。model/base_url/api_key 跟随当前实时设置。
    config = resolve_live_omni_config(
        OmniConfig(temperature=0.5, max_completion_tokens=512)
    )
    try:
        raw = await call_omni(payload, config, type="on_demand")
    except Exception as e:  # noqa: BLE001
        logger.warning("generate: omni call failed: %s", safe_log(e))
        raise GenerateError("omni_error", str(e)) from e

    content = ""
    try:
        content = response_parser.parse_query_response(raw)
        data = json.loads(response_parser.extract_json(content))
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("generate: omni returned non-JSON: %r", content[:200])
        raise GenerateError("invalid_json") from e
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        raise GenerateError("invalid_json")

    # 压平空白：模型可能把换行写进 TTS 文本，播报会多出停顿。
    text = " ".join(data["result"].split())
    if not text:
        raise GenerateError("empty_result")
    return _truncate_for_tts(text, max_chars)
