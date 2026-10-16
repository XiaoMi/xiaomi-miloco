"""probe_image.py 单元测试:httpx.MockTransport 抓真实上线的请求体,断言各 provider 格式
里确实带了图片块,并覆盖 supported / unsupported / failed / inconclusive 的状态映射。"""

from __future__ import annotations

import base64
import json

import cv2
import httpx
import numpy as np
import pytest
from miloco.perception.engine.omni import probe_image

OPENAI_BASE = "https://llm.example/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
KEY = "sk-test-secret-123"


def _transport(handler):
    return httpx.MockTransport(handler)


def _chat_ok(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _capture(response_factory):
    """返回 (handler, seen);seen 收集每次请求的 url / headers / json body。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": json.loads(request.content),
            }
        )
        return response_factory()

    return handler, seen


def _decode_jpeg(data_uri: str) -> tuple[int, int, tuple[int, int, int]]:
    """解出 data URI 里的 JPEG,返回 (w, h, 平均 RGB);JPEG 有损,颜色按容差比较。"""
    assert data_uri.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    assert raw[:3] == b"\xff\xd8\xff" and raw[-2:] == b"\xff\xd9"
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    b, g, r = (int(round(c)) for c in img.reshape(-1, 3).mean(axis=0))
    return img.shape[1], img.shape[0], (r, g, b)


def _close(actual: tuple[int, int, int], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - e) <= 12 for a, e in zip(actual, expected))


# ─── 内置图片 ────────────────────────────────────────────────────────────────


def test_build_probe_jpeg_is_valid_solid_color():
    uri = (
        "data:image/jpeg;base64,"
        + base64.b64encode(probe_image.build_probe_jpeg((10, 20, 30), 16)).decode()
    )
    w, h, rgb = _decode_jpeg(uri)
    assert (w, h) == (16, 16) and _close(rgb, (10, 20, 30))


@pytest.mark.parametrize("color", sorted(probe_image._COLORS))
def test_probe_colours_survive_jpeg_and_stay_distinct(color):
    # 四种候选色经 JPEG 编码后仍贴近原色,且彼此不会被混淆
    w, h, rgb = _decode_jpeg(probe_image.build_probe_messages(color)[0]["content"][0]["image_url"]["url"])
    assert (w, h) == (probe_image._IMAGE_SIZE, probe_image._IMAGE_SIZE)
    assert _close(rgb, probe_image._COLORS[color])
    for other, other_rgb in probe_image._COLORS.items():
        if other != color:
            assert not _close(rgb, other_rgb)


# ─── 模拟"看图模型":解码请求里真实发送的 JPEG,按其颜色作答 ───────────────────

_ZH = {"red": "红", "green": "绿", "blue": "蓝", "yellow": "黄"}


def _image_uri(body: dict) -> str:
    if "contents" in body:  # Gemini 原生
        parts = body["contents"][0]["parts"]
        inline = next(p["inline_data"] for p in parts if "inline_data" in p)
        assert inline["mime_type"] == "image/jpeg"
        return "data:image/jpeg;base64," + inline["data"]
    content = body["messages"][0]["content"]
    imgs = [b for b in content if b["type"] == "image_url"]
    assert len(imgs) == 1
    return imgs[0]["image_url"]["url"]


def _seen_colour(body: dict) -> str:
    _, _, rgb = _decode_jpeg(_image_uri(body))
    return min(
        probe_image._COLORS,
        key=lambda c: sum((a - b) ** 2 for a, b in zip(rgb, probe_image._COLORS[c])),
    )


def _vision(template: str = "{c}", *, fmt: str = "openai"):
    """返回 (handler, seen):handler 看请求里的图作答,seen 记录每次请求与所见颜色。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        colour = _seen_colour(body)
        seen.append({"url": str(request.url), "headers": dict(request.headers), "body": body, "colour": colour})
        text = template.format(c=colour, C=colour.upper(), zh=_ZH[colour])
        if fmt == "gemini":
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})
        if fmt == "sse":
            half = len(text) // 2
            sse = "".join(
                'data: {"choices":[{"delta":{"content":%s}}]}\n\n' % json.dumps(part)
                for part in (text[:half], text[half:])
            ) + "data: [DONE]\n\n"
            return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})
        return _chat_ok(text)

    return handler, seen


# ─── 请求格式:各 provider 的图片块(两次挑战) ──────────────────────────────


async def test_openai_compat_two_challenges_with_distinct_images():
    handler, seen = _vision()
    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert r["status"] == "supported" and r["reason"] == "answer_correct"
    assert r["challenges"] == 2 and len(seen) == 2
    assert seen[0]["colour"] == "red" and seen[1]["colour"] != "red"
    for req in seen:
        assert req["url"] == f"{OPENAI_BASE}/chat/completions"
        assert req["headers"]["authorization"] == f"Bearer {KEY}"
        body = req["body"]
        assert body["stream"] is False and body["max_tokens"] == probe_image._MAX_TOKENS
        w, h, _ = _decode_jpeg(_image_uri(body))
        assert (w, h) == (96, 96)
        assert any(b["type"] == "text" and "colour" in b["text"] for b in body["messages"][0]["content"])


async def test_qwen_forced_stream_two_challenges_read_sse():
    handler, seen = _vision("{C}", fmt="sse")
    r = await probe_image.probe_image_input(
        "qwen3.5-omni-flash", OPENAI_BASE, KEY, color="blue", transport=_transport(handler)
    )
    assert r["status"] == "supported" and r["challenges"] == 2
    assert [s["colour"] for s in seen][0] == "blue" and len({s["colour"] for s in seen}) == 2
    for req in seen:
        assert req["body"]["stream"] is True and req["body"]["modalities"] == ["text"]


async def test_gemini_native_two_challenges_inline_data():
    handler, seen = _vision("{C}", fmt="gemini")
    r = await probe_image.probe_image_input(
        "gemini-3-flash-preview", GEMINI_BASE, KEY, color="green", transport=_transport(handler)
    )
    assert r["status"] == "supported" and r["challenges"] == 2
    for req in seen:
        assert req["url"] == f"{GEMINI_BASE}/models/gemini-3-flash-preview:generateContent"
        assert req["headers"]["x-goog-api-key"] == KEY
        assert any("text" in p for p in req["body"]["contents"][0]["parts"])


# ─── 双挑战调度:提前停止 / 请求次数 / 结果分类 ──────────────────────────────


async def test_first_challenge_wrong_stops_after_one_request():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return _chat_ok("blue")

    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"
    assert r["challenges"] == 1 and len(calls) == 1


async def test_first_correct_second_wrong_is_inconclusive_after_two_requests():
    answers = iter(["red", "red"])  # 第二张换了颜色,仍答 red = 盲答
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return _chat_ok(next(answers))

    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"
    assert r["challenges"] == 2 and len(calls) == 2


async def test_second_challenge_rejecting_image_is_unsupported():
    responses = iter(
        [
            None,  # 第一张:按图作答
            httpx.Response(400, json={"error": {"message": "image input is not supported"}}),
        ]
    )
    vision, _ = _vision()

    def handler(request):
        nxt = next(responses)
        return vision(request) if nxt is None else nxt

    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert r["status"] == "unsupported" and r["reason"] == "image_rejected"
    assert r["challenges"] == 2


async def test_network_failure_on_first_challenge_stops():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("boom")

    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, transport=_transport(handler)
    )
    assert r["status"] == "failed" and r["reason"] == "unreachable"
    assert r["challenges"] == 1 and len(calls) == 1


@pytest.mark.parametrize("seed", range(20))
async def test_random_challenges_use_two_distinct_colours(seed):
    import random

    random.seed(seed)
    handler, seen = _vision()
    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, transport=_transport(handler)
    )
    assert r["status"] == "supported" and len(seen) == 2
    assert seen[0]["colour"] != seen[1]["colour"]


async def test_supported_latency_is_sum_of_both_requests():
    handler, seen = _vision()
    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert r["status"] == "supported" and isinstance(r["latency_ms"], int) and r["latency_ms"] >= 0


# ─── 状态映射 ────────────────────────────────────────────────────────────────


async def _run(response_or_exc, *, model="mimo-v2.6-flash", color="red"):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc

    return await probe_image.probe_image_input(
        model, OPENAI_BASE, KEY, color=color, transport=_transport(handler)
    )


@pytest.mark.parametrize(
    "template",
    [
        "{c}",
        "{C}.",
        "The dominant colour is {C}.",
        "a vibrant {c} color",
        "{zh}色",
        "这张图片是{zh}色的",
    ],
)
async def test_supported_answer_variants(template):
    handler, seen = _vision(template)
    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, transport=_transport(handler)
    )
    assert r["status"] == "supported" and r["http_status"] == 200
    assert r["challenges"] == 2 and len(seen) == 2


async def test_supported_with_content_part_list():
    seen: list[str] = []

    def handler(request):
        colour = _seen_colour(json.loads(request.content))
        seen.append(colour)
        return _chat_ok([{"type": "text", "text": colour}])

    r = await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, transport=_transport(handler)
    )
    assert r["status"] == "supported" and len(seen) == 2


@pytest.mark.parametrize("answer", ["red", "crimson"])
async def test_synonym_passes_first_challenge(answer):
    # 固定回答:第一张(red)判对 → 才会发第二张;第二张换了颜色仍答同一个词 → inconclusive
    r = await _run(_chat_ok(answer), color="red")
    assert r["challenges"] == 2
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"


@pytest.mark.parametrize(
    "message",
    [
        "Image input is not supported by this model",
        "unknown variant `image_url`, expected `text`",
        "This model does not support multimodal content",
        "vision is not enabled for model",
    ],
)
@pytest.mark.parametrize("status_code", [400, 422])
async def test_unsupported_when_provider_rejects_image(status_code, message):
    r = await _run(httpx.Response(status_code, json={"error": {"message": message}}))
    assert r["status"] == "unsupported" and r["reason"] == "image_rejected"
    assert r["http_status"] == status_code


async def test_unsupported_gemini_style_error_body():
    r = await _run(
        httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "Unable to process input image.",
                    "status": "INVALID_ARGUMENT",
                }
            },
        ),
        model="gemini-3-flash-preview",
    )
    assert r["status"] == "unsupported"


@pytest.mark.parametrize(
    "message",
    [
        "Invalid image format: expected png",
        "Image is too large",
        "Failed to decode image data",
        "image dimensions exceed the maximum resolution",
        "Multimodal data is corrupted: image",
    ],
)
async def test_rejecting_this_image_is_inconclusive_not_unsupported(message):
    # 报错指向这张图本身(格式/尺寸/解码) → 模型其实收图,不能判「不支持图片」
    r = await _run(httpx.Response(400, json={"error": {"message": message}}))
    assert r["status"] == "inconclusive" and r["reason"] == "image_invalid"


async def test_wrong_visual_answer_is_never_unsupported():
    # 看错颜色 = 视觉理解存疑,不等于不支持图片输入
    r = await _run(_chat_ok("blue"), color="red")
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"


async def test_400_invalid_api_key_is_failed_not_unsupported():
    r = await _run(
        httpx.Response(400, json={"error": {"message": "API key not valid. image"}}),
        model="gemini-3-flash-preview",
    )
    assert r["status"] == "failed" and r["reason"] == "bad_key"


async def test_400_without_image_hint_is_inconclusive():
    r = await _run(
        httpx.Response(400, json={"error": {"message": "max_tokens too small"}})
    )
    assert r["status"] == "inconclusive" and r["reason"] == "rejected_unknown"


@pytest.mark.parametrize(
    ("resp", "reason"),
    [
        (httpx.Response(401, text="nope"), "bad_key"),
        (httpx.Response(403, text="nope"), "bad_key"),
        (httpx.Response(404, text="nope"), "not_found"),
        (httpx.Response(429, text="slow down"), "rate_limited"),
        # 400/422 之外的 4xx(如 415)不进图片细分,与图片能力无关 → failed
        (httpx.Response(415, json={"error": {"message": "unsupported media type: image"}}), "http_error"),
        (httpx.Response(500, text="boom"), "http_error"),
        (httpx.Response(503, text="boom"), "http_error"),
    ],
)
async def test_failed_statuses(resp, reason):
    r = await _run(resp)
    assert r["status"] == "failed" and r["reason"] == reason


async def test_failed_timeout_and_unreachable():
    t = await _run(httpx.ReadTimeout("slow"))
    assert t["status"] == "failed" and t["reason"] == "timeout"
    u = await _run(httpx.ConnectError("dns"))
    assert u["status"] == "failed" and u["reason"] == "unreachable"


async def test_failed_invalid_scheme():
    r = await probe_image.probe_image_input("m", "ftp://x/v1", KEY)
    assert r == {"status": "failed", "reason": "invalid_url", "challenges": 0}  # 未发请求


async def test_inconclusive_wrong_answer():
    r = await _run(_chat_ok("blue"), color="red")
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"


@pytest.mark.parametrize(
    "answer",
    [
        "red or blue",
        "red and white",
        "orange-red",
        "It is not red.",
        "Not blue, it's red.",
        "Maybe red?",
        "Probably red",
        "I can't see images, but I would guess red.",
        "Sorry, I am unable to view images.",
        "可能是红色",
        "不是红色",
    ],
)
async def test_inconclusive_negated_hedged_or_multi_colour(answer):
    # 否定 / 含糊 / 多色 / 其他颜色:看图能力存疑,不判 supported,也不判 unsupported
    r = await _run(_chat_ok(answer), color="red")
    assert r["status"] == "inconclusive" and r["reason"] == "wrong_answer"


async def test_truncated_empty_answer_is_distinguished():
    # 推理模型把 max_tokens 耗在思考上 → content 空 + finish_reason=length
    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
    )
    r = await _run(resp)
    assert r["status"] == "inconclusive" and r["reason"] == "answer_truncated"


@pytest.mark.parametrize(
    ("finish_reason", "reason"), [("length", "answer_truncated"), ("stop", "empty_answer")]
)
async def test_stream_finish_reason_is_kept_for_truncation(finish_reason, reason):
    # Qwen 被 adapter 强制流式:空内容 + 末 chunk finish_reason=length 也要识别为截断
    sse = (
        'data: {"choices":[{"delta":{"content":""}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"%s"}]}\n\n'
        "data: [DONE]\n\n" % finish_reason
    )
    r = await _run(
        httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"}),
        model="qwen3.5-omni-flash",
    )
    assert r["status"] == "inconclusive" and r["reason"] == reason
    assert r["challenges"] == 1


@pytest.mark.parametrize(
    ("finish", "reason"), [("MAX_TOKENS", "answer_truncated"), ("STOP", "empty_answer")]
)
async def test_gemini_native_max_tokens_is_truncation(finish, reason):
    # Gemini 非流式:可见文本为空 + 原生 finishReason=MAX_TOKENS → answer_truncated
    resp = httpx.Response(
        200,
        json={"candidates": [{"content": {"parts": []}, "finishReason": finish}]},
    )
    r = await _run(resp, model="gemini-3-flash-preview")
    assert r["status"] == "inconclusive" and r["reason"] == reason


async def test_early_stop_reports_accumulated_latency(monkeypatch):
    # 第一张答对、第二张被拒:latency_ms 是两次之和,而不是只有第二张
    results = iter(
        [
            {"status": "supported", "reason": "answer_correct", "http_status": 200, "latency_ms": 1200},
            {"status": "unsupported", "reason": "image_rejected", "http_status": 400, "latency_ms": 3400},
        ]
    )

    async def fake_challenge(*a, **k):
        return next(results)

    monkeypatch.setattr(probe_image, "_run_challenge", fake_challenge)
    r = await probe_image.probe_image_input("mimo-v2.6-flash", OPENAI_BASE, KEY)
    assert r["status"] == "unsupported" and r["challenges"] == 2
    assert r["latency_ms"] == 4600


async def test_early_stop_without_latency_keeps_it_absent(monkeypatch):
    results = iter(
        [
            {"status": "supported", "reason": "answer_correct", "http_status": 200, "latency_ms": 1200},
            {"status": "failed", "reason": "timeout"},
        ]
    )

    async def fake_challenge(*a, **k):
        return next(results)

    monkeypatch.setattr(probe_image, "_run_challenge", fake_challenge)
    r = await probe_image.probe_image_input("mimo-v2.6-flash", OPENAI_BASE, KEY)
    assert r == {"status": "failed", "reason": "timeout", "challenges": 2}


async def test_question_is_natural_and_does_not_list_candidates():
    handler, seen = _vision()
    await probe_image.probe_image_input(
        "mimo-v2.6-flash", OPENAI_BASE, KEY, color="red", transport=_transport(handler)
    )
    assert len(seen) == 2
    for req in seen:  # 两次挑战都不把候选颜色告诉模型
        body = req["body"]
        text = " ".join(
            b["text"] for b in body["messages"][0]["content"] if b["type"] == "text"
        ).lower()
        for colour in probe_image._COLORS:
            assert colour not in text
        assert body["max_tokens"] >= 64


@pytest.mark.parametrize("content", ["", "   ", None])
async def test_inconclusive_empty_answer(content):
    r = await _run(_chat_ok(content))
    assert r["status"] == "inconclusive" and r["reason"] == "empty_answer"


async def test_inconclusive_unparseable_200():
    r = await _run(httpx.Response(200, text="<html>gateway</html>"))
    assert r["status"] == "inconclusive" and r["reason"] == "bad_response"


async def test_inconclusive_stream_without_data():
    r = await _run(
        httpx.Response(200, content=": keep-alive\n\n"), model="qwen3.5-omni-flash"
    )
    assert r["status"] == "inconclusive" and r["reason"] == "bad_response"


async def test_stream_non_200_is_classified():
    r = await _run(
        httpx.Response(400, json={"error": {"message": "image not supported"}}),
        model="qwen3.5-omni-flash",
    )
    assert r["status"] == "unsupported"


async def test_result_never_echoes_response_text_or_key():
    secret_msg = "leak-me-please image " + KEY
    r = await _run(httpx.Response(400, json={"error": {"message": secret_msg}}))
    dumped = json.dumps(r)
    assert KEY not in dumped and "leak-me" not in dumped
    assert set(r) <= {"status", "reason", "http_status", "latency_ms", "challenges"}
