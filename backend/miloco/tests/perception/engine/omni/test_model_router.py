import asyncio
import json
from types import SimpleNamespace

from miloco.config.settings import OmniModelSettings


def test_selects_video_model_before_image_model():
    from miloco.perception.engine.omni.model_router import select_visual_route

    profiles = [
        OmniModelSettings(
            label="image",
            model="vision",
            base_url="https://x/v1",
            api_key="sk-image",
            enabled=True,
            capabilities=["image"],
        ),
        OmniModelSettings(
            label="video",
            model="video",
            base_url="https://x/v1",
            api_key="sk-video",
            enabled=True,
            capabilities=["video", "audio"],
        ),
    ]

    route = select_visual_route(profiles)

    assert route.visual_mode == "video"
    assert route.primary.label == "video"
    assert route.audio is None


def test_selects_image_and_audio_models_when_no_video_model():
    from miloco.perception.engine.omni.model_router import select_visual_route

    profiles = [
        OmniModelSettings(
            label="image",
            model="vision",
            base_url="https://x/v1",
            api_key="sk-image",
            enabled=True,
            capabilities=["image"],
        ),
        OmniModelSettings(
            label="audio",
            model="audio",
            base_url="https://x/v1",
            api_key="sk-audio",
            enabled=True,
            capabilities=["audio"],
        ),
    ]

    route = select_visual_route(profiles)

    assert route.visual_mode == "frames"
    assert route.primary.label == "image"
    assert route.audio is not None
    assert route.audio.label == "audio"


def test_ignores_disabled_profiles_and_uses_first_enabled_match():
    from miloco.perception.engine.omni.model_router import select_model_for

    profiles = [
        OmniModelSettings(
            label="disabled",
            model="m1",
            base_url="https://x/v1",
            api_key="sk-1",
            enabled=False,
            capabilities=["audio"],
        ),
        OmniModelSettings(
            label="first",
            model="m2",
            base_url="https://x/v1",
            api_key="sk-2",
            enabled=True,
            capabilities=["audio"],
        ),
        OmniModelSettings(
            label="second",
            model="m3",
            base_url="https://x/v1",
            api_key="sk-3",
            enabled=True,
            capabilities=["audio"],
        ),
    ]

    selected = select_model_for(profiles, "audio")

    assert selected is not None
    assert selected.label == "first"


def test_merge_visual_and_audio_outputs_keeps_visual_fields_and_adds_audio_fields():
    from miloco.perception.engine.omni.omni import _merge_visual_audio_outputs
    from miloco.perception.types import (
        CaptionEntry,
        MatchedRule,
        RealtimePerceptionResult,
        Speech,
        Suggestion,
    )

    visual = RealtimePerceptionResult(
        caption=[CaptionEntry(area="客厅", description="有人在客厅活动")],
        matched_rules=[MatchedRule(rule_id="r1", rule_name="规则", reason="命中", hit=True)],
        suggestions=[Suggestion(event="冰箱门开着", action="检查冰箱门", urgency="low")],
        usage={"input_tokens": 10, "output_tokens": 2, "cached_tokens": 1, "audio_tokens": 0, "video_tokens": 8},
    )
    audio = RealtimePerceptionResult(
        speeches=[Speech(speaker="未知", content="打开灯", is_complete=True, needs_response=True)],
        env_sounds=["听到敲门声"],
        suggestions=[Suggestion(event="有人敲门", action="确认门口情况", urgency="medium")],
        usage={"input_tokens": 5, "output_tokens": 1, "cached_tokens": 0, "audio_tokens": 4, "video_tokens": 0},
    )

    merged = _merge_visual_audio_outputs(visual, audio)

    assert merged.caption == visual.caption
    assert merged.matched_rules == visual.matched_rules
    assert merged.speeches == audio.speeches
    assert merged.env_sounds == audio.env_sounds
    assert [s.event for s in merged.suggestions] == ["冰箱门开着", "有人敲门"]
    assert merged.usage == {
        "input_tokens": 15,
        "output_tokens": 3,
        "cached_tokens": 1,
        "audio_tokens": 4,
        "video_tokens": 8,
    }


def _raw_response(payload: dict) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "prompt_tokens_details": {}},
    }


def test_run_omni_batch_uses_image_and_audio_profiles(monkeypatch):
    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni import omni as omni_mod
    from miloco.perception.engine.types import OmniContext

    profiles = [
        OmniModelSettings(
            label="vision",
            model="vision-model",
            base_url="https://vision/v1",
            api_key="sk-vision",
            enabled=True,
            capabilities=["image"],
        ),
        OmniModelSettings(
            label="audio",
            model="audio-model",
            base_url="https://audio/v1",
            api_key="sk-audio",
            enabled=True,
            capabilities=["audio"],
        ),
    ]
    monkeypatch.setattr(omni_mod, "get_live_profiles", lambda: profiles)

    build_calls: list[dict] = []

    def fake_build_batch_prompt(*args, **kwargs):
        build_calls.append(dict(kwargs))
        if kwargs.get("force_route") == "audio":
            return {"audio_base64": "audio", "system_prompt": "s", "user_content": "u"}
        return {"frame_images": [{"data": "img"}], "system_prompt": "s", "user_content": "u"}

    call_models: list[str] = []

    async def fake_call_omni(payload, config, type="realtime"):
        call_models.append(config.model)
        if config.model == "audio-model":
            return _raw_response({
                "speeches": [{"speaker": "未知", "content": "打开灯", "is_complete": True, "needs_response": True}],
                "env_sounds": "敲门声",
                "suggestions": [],
            })
        return _raw_response({"caption": "画面里有人", "matched_rules": [], "suggestions": []})

    monkeypatch.setattr(omni_mod, "build_batch_prompt", fake_build_batch_prompt)
    monkeypatch.setattr(omni_mod, "call_omni", fake_call_omni)

    packet = SimpleNamespace(trigger=SimpleNamespace(audio_active=True))
    output = asyncio.run(omni_mod.run_omni_batch([packet], OmniContext(), OmniConfig()))

    assert call_models == ["vision-model", "audio-model"]
    assert build_calls[0]["visual_mode"] == "frames"
    assert build_calls[1]["force_route"] == "audio"
    assert output.caption[0].description == "画面里有人"
    assert output.speeches[0].content == "打开灯"
    assert output.env_sounds == ["敲门声"]


def test_run_omni_batch_prefers_video_profile(monkeypatch):
    from miloco.perception.engine.config import OmniConfig
    from miloco.perception.engine.omni import omni as omni_mod
    from miloco.perception.engine.types import OmniContext

    profiles = [
        OmniModelSettings(
            label="image",
            model="image-model",
            base_url="https://image/v1",
            api_key="sk-image",
            enabled=True,
            capabilities=["image"],
        ),
        OmniModelSettings(
            label="video",
            model="video-model",
            base_url="https://video/v1",
            api_key="sk-video",
            enabled=True,
            capabilities=["video", "audio"],
        ),
    ]
    monkeypatch.setattr(omni_mod, "get_live_profiles", lambda: profiles)

    build_calls: list[dict] = []

    def fake_build_batch_prompt(*args, **kwargs):
        build_calls.append(dict(kwargs))
        return {"video_base64": "video", "system_prompt": "s", "user_content": "u"}

    call_models: list[str] = []

    async def fake_call_omni(payload, config, type="realtime"):
        call_models.append(config.model)
        return _raw_response({"caption": "视频画面", "matched_rules": [], "suggestions": []})

    monkeypatch.setattr(omni_mod, "build_batch_prompt", fake_build_batch_prompt)
    monkeypatch.setattr(omni_mod, "call_omni", fake_call_omni)

    packet = SimpleNamespace(trigger=SimpleNamespace(audio_active=True))
    output = asyncio.run(omni_mod.run_omni_batch([packet], OmniContext(), OmniConfig()))

    assert call_models == ["video-model"]
    assert build_calls[0]["visual_mode"] == "video"
    assert output.caption[0].description == "视频画面"
