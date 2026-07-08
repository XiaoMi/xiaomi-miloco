"""Tests for Omni Layer — Prompt Builder."""

from unittest.mock import patch

import numpy as np
from miloco.perception.engine.omni.prompt_builder import (
    _batch_video_has_audio,
    _encode_video,
    _render_examples,
    _resolve_route,
    build_prompt,
    build_query_prompt,
    build_stream_prompt,
    build_tier_c_verify_payload,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    FrameResolution,
    GateTrigger,
    IdentityPacket,
    IdentityTarget,
    MotionState,
    ObjectType,
    OmniContext,
    RuleCondition,
    SelectedFrame,
    TrackingBoxInfo,
)


def _mock_edge_packet() -> IdentityPacket:
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    return IdentityPacket(
        packet_id="ep-1",
        room_name="study-room",
        timestamp=1000.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=2),
        targets=[
            IdentityTarget(
                type=ObjectType.HUMAN_WITH_FACE,
                person_id="wangshihao",
                track_id=1,
                needs_omni_verify=False,
                box_info=[TrackingBoxInfo(frame_index=0, boxes={"human_body": (10, 10, 50, 80)})],
            ),
        ],
        scene_motion=MotionState.STATIC,
        frames=[
            SelectedFrame(
                frame_index=0,
                image=frame,
                resolution=FrameResolution.HIGH,
                crops=[],
            ),
        ],
        all_frames=[np.zeros((100, 100, 3), dtype=np.uint8)],
        audio_clip=np.zeros(100, dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
    )


class TestBuildPrompt:
    def test_complete_prompt(self):
        ep = _mock_edge_packet()
        ctx = OmniContext(
            room_name="书房",
            rule_conditions=[
                RuleCondition(
                    rule_id="reading_light",
                    rule_name="读书开灯",
                    query="当前是否有人在读书",
                ),
            ],
        )
        payload = build_prompt(ep, ctx)

        assert "家庭场景" in payload["system_prompt"]
        assert "wangshihao" in payload["user_content"]
        assert "位置: 书房" in payload["user_content"]  # room_name 作场景参考注入 U4
        assert "读书开灯" in payload["user_content"]  # 规则按 rule_name 渲染（# 待判断规则）
        assert payload["video_base64"] is not None
        assert payload["video_fps"] == ep.frame_info.fps

    def test_rule_rendered_by_name_without_evidence_suffix(self):
        """规则按 rule_name 渲染进「# 待判断规则」，不带已删除的 ｜允许证据= 后缀。"""
        ep = _mock_edge_packet()
        ctx = OmniContext(
            rule_conditions=[
                RuleCondition(
                    rule_id="help",
                    rule_name="[help] 求救",
                    query="用户呼救",
                ),
            ],
        )
        payload = build_prompt(ep, ctx)
        assert "[help] 求救：用户呼救" in payload["user_content"]
        assert "允许证据" not in payload["user_content"]

    def test_empty_context(self):
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)

        assert "wangshihao" in payload["user_content"]
        assert "上一次描述" not in payload["user_content"]

    def test_pending_dropped_from_roster(self):
        """pending（待确认）不进"已识别人物"名册——只在视频/"待识别 track"里出现。

        去先验重构后名册只放已定身份（confirmed 成员 / 已确认陌生人），pending /
        未识别一律剔除（历史行为是渲染成"已识别人物：待确认"）。
        """
        ep = _mock_edge_packet()
        ep.targets[0].needs_omni_verify = True
        ep.targets[0].person_id = "pending"
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)

        assert "待确认" not in payload["user_content"]
        # 唯一目标是 pending → 名册为空
        assert "已识别人物：无" in payload["user_content"]

    def test_crops_encoding(self):
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)
        assert isinstance(payload["crops"], list)

    def test_system_prompt_includes_schema(self):
        """Realtime 路径 system prompt 包含 JSON schema。"""
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)
        assert "speeches" in payload["system_prompt"]
        assert "suggestions" in payload["system_prompt"]
        assert "caption" in payload["system_prompt"]

    def test_system_prompt_includes_home_profile(self):
        """home_profile_loader 返回内容时注入到 system prompt。"""
        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
                   return_value="# Home Profile\n小明\n"):
            ep = _mock_edge_packet()
            ctx = OmniContext()
            payload = build_prompt(ep, ctx)
        assert "Home Profile" in payload["system_prompt"]
        assert "小明" in payload["system_prompt"]

    def test_pending_speech_user_content_only_data(self):
        """last_speech：数据 + gate-first 判断指令一起放 user_content（实测放 system 不拼接）；
        用"能否拼接"判断式而非"必须拼接"祈使式，既驱动拼接、又不被弱推理路径复读进 content。
        """
        ep = _mock_edge_packet()
        ctx = OmniContext(pending_speech=[{"speaker": "小明", "content": "打开"}])
        payload = build_prompt(ep, ctx)

        # 数据 + 判断指令都在 user_content
        assert "last_speech：打开" in payload["user_content"]
        assert "能否" in payload["user_content"]

        # 不用"必须拼接"这类祈使（会诱发过拼，且易被复读进 content）
        assert "必须拼接" not in payload["user_content"]
        assert "【重要】" not in payload["user_content"]

    def test_pending_speech_pointer_in_system_prompt(self):
        """speeches 字段说明保留指向 last_speech 续接说明的指针（具体判断在 user 段）。"""
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)
        assert "last_speech" in payload["system_prompt"]

    def test_last_context_not_injected_in_user_content(self):
        """停注入历史：user_content 不再出现 <last_caption> / <last_suggestions> 标签
        （回灌模型上轮结论会形成回声室、强化幻觉；caption 变化去重与 suggestion 事件链
        去重均下沉到代码）。旧的"对比上次场景/建议"祈使句同样不得出现。
        """
        ep = _mock_edge_packet()
        ctx = OmniContext(pending_speech=[{"speaker": "小明", "content": "打开"}])
        payload = build_prompt(ep, ctx)

        assert "<last_caption>" not in payload["user_content"]
        assert "<last_suggestions>" not in payload["user_content"]

        assert "对比" not in payload["user_content"]
        assert "遵循上述" not in payload["user_content"]
        assert "已提过的建议" not in payload["user_content"]

    def test_suggestion_dedup_is_code_side_not_prompt(self):
        """suggestion 去重已下沉代码（事件链按 event 语义匹配）：prompt 不再含 prev_id /
        last_suggestions；system prompt 只保留"对照规则去重"的指引。"""
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)

        sp = payload["system_prompt"]
        assert "prev_id" not in sp
        assert "<last_suggestions>" not in sp
        assert "对照「# 待判断规则」" in sp


class TestBuildStreamPrompt:
    def test_stream_schema_order(self):
        """流式路径 system prompt 字段顺序为 speeches → env_sounds → matched_rules → suggestions → caption。"""
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_stream_prompt(ep, ctx)
        sp = payload["system_prompt"]
        assert sp.index('"speeches"') < sp.index('"env_sounds"')
        assert sp.index('"env_sounds"') < sp.index('"matched_rules"')
        assert sp.index('"matched_rules"') < sp.index('"suggestions"')
        assert sp.index('"suggestions"') < sp.index('"caption"')

    def test_normal_schema_order(self):
        """非流式路径 system prompt 字段顺序为 caption 在前。"""
        ep = _mock_edge_packet()
        ctx = OmniContext()
        payload = build_prompt(ep, ctx)
        sp = payload["system_prompt"]
        assert sp.index('"caption"') < sp.index('"speeches"')


class TestBuildQueryPrompt:
    def test_system_prompt_includes_commonsense(self):
        """On-demand 路径 system prompt 包含 _COMMONSENSE。"""
        ep = _mock_edge_packet()
        payload = build_query_prompt([ep], "现在谁在家？")
        assert "通用常识" in payload["system_prompt"]

    def test_no_room_name_in_payload(self):
        """On-demand 路径 room_name 不出现在 user_content 或 system_prompt。"""
        ep = _mock_edge_packet()
        payload = build_query_prompt([ep], "现在谁在家？")
        assert "study-room" not in payload["user_content"]
        assert "study-room" not in payload["system_prompt"]


class TestDeviceHeaderRoster:
    """_build_device_header：名册按身份分桶（已识别人物 / 陌生人），含归一化位置，
    pending/未识别剔除，candidate_tids（本窗重审）剔除以去身份先验。"""

    @staticmethod
    def _target(track_id, person_id, bbox=None, suppress_as_prior=False):
        return IdentityTarget(
            type=ObjectType.HUMAN_WITH_FACE,
            person_id=person_id,
            track_id=track_id,
            needs_omni_verify=False,
            box_info=[],
            bbox_xyxy_norm=bbox,
            suppress_as_prior=suppress_as_prior,
        )

    def _packet(self, targets):
        ep = _mock_edge_packet()
        ep.targets = targets
        return ep

    def test_confirmed_member_renders_name_and_bbox(self):
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([self._target(1, "pid-uuid", (120, 200, 480, 900))])
        lines = _build_device_header([ep], label_lookup={"pid-uuid": "张三"})
        assert lines[0] == "已识别人物：张三[bbox=(120, 200, 480, 900)]"
        # 名册含 bbox → 附坐标系说明
        assert any("归一化到 [0, 1000]" in ln for ln in lines)

    def test_stranger_goes_to_separate_line(self):
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([
            self._target(1, "pid-uuid", (10, 20, 30, 40)),
            self._target(2, "unknown_3", (50, 60, 70, 80)),
        ])
        lines = _build_device_header([ep], label_lookup={"pid-uuid": "张三"})
        assert lines[0] == "已识别人物：张三[bbox=(10, 20, 30, 40)]"
        assert lines[1] == "陌生人：陌生人#3[bbox=(50, 60, 70, 80)]"

    def test_pending_and_none_dropped(self):
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([
            self._target(1, "pending", (10, 20, 30, 40)),
            self._target(2, "none", (50, 60, 70, 80)),
            self._target(3, "pending:pid-x", (1, 2, 3, 4)),
        ])
        lines = _build_device_header([ep], label_lookup={})
        assert lines == ["已识别人物：无"]

    def test_recheck_track_excluded_via_candidate_tids(self):
        """本窗在 candidates 里的 confirmed track 从名册剔除（避免身份先验锚定重审）。"""
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([
            self._target(1, "pid-a", (10, 20, 30, 40)),
            self._target(2, "pid-b", (50, 60, 70, 80)),
        ])
        lines = _build_device_header(
            [ep], label_lookup={"pid-a": "张三", "pid-b": "李四"},
            candidate_tids={2},
        )
        assert lines[0] == "已识别人物：张三[bbox=(10, 20, 30, 40)]"
        assert all("李四" not in ln for ln in lines)

    def test_reverted_track_excluded_via_suppress_as_prior(self):
        """翻身份黏旧名 track(suppress_as_prior)从名册剔除——即便不在 candidate_tids 里。

        coasting(本窗未派发)的翻转 track 不在 candidate_tids, 靠 suppress_as_prior 兜住,
        防黏住的旧名当先验把翻转翻不动。
        """
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([
            self._target(1, "pid-a", (10, 20, 30, 40)),
            self._target(2, "pid-b", (50, 60, 70, 80), suppress_as_prior=True),
        ])
        lines = _build_device_header(
            [ep], label_lookup={"pid-a": "张三", "pid-b": "李四"},
            candidate_tids=set(),       # 不靠 candidate_tids —— track 2 coasting 未派发
        )
        assert lines[0] == "已识别人物：张三[bbox=(10, 20, 30, 40)]"
        assert all("李四" not in ln for ln in lines)

    def test_coasting_no_bbox_degrades_to_name_only(self):
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        ep = self._packet([self._target(1, "pid-uuid", None)])
        lines = _build_device_header([ep], label_lookup={"pid-uuid": "张三"})
        # 无 bbox → 纯名 + 不附坐标系说明
        assert lines == ["已识别人物：张三"]

    def test_bbox_hint_only_when_roster_has_bbox(self):
        """名册有 bbox 才附坐标系说明；名册为空 / 全 coasting 时不附。"""
        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        # 空名册（仅 pending）→ 无说明
        ep_empty = self._packet([self._target(1, "pending", (1, 2, 3, 4))])
        assert all("归一化到 [0, 1000]" not in ln
                   for ln in _build_device_header([ep_empty], label_lookup={}))
        # 有 bbox 的成员 → 末尾一句说明
        ep_bbox = self._packet([self._target(1, "pid-x", (1, 2, 3, 4))])
        lines = _build_device_header([ep_bbox], label_lookup={"pid-x": "王五"})
        assert "归一化到 [0, 1000]" in lines[-1]


class TestFormatTrackLine:
    """_format_track_line：去身份先验后只剩 track_id + bbox + face_visible。"""

    def test_no_status_only_bbox_and_face(self):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import _format_track_line

        line = _format_track_line(IdentityQueryItem(
            track_id=5, bbox_xyxy_norm=(100, 200, 300, 400), face_visible=True,
        ))
        assert "track_id=5" in line
        assert "bbox=(100, 200, 300, 400)" in line
        assert "face_visible=true" in line
        assert "状态=" not in line
        assert "疑似" not in line
        assert "待复核" not in line  # 默认非重审

    def test_recheck_renders_identically_no_marker(self):
        """重核 track 不再加"待复核"标记——与首次出现的 track 在 prompt 里不可区分（去先验更彻底）。"""
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import _format_track_line

        kwargs = dict(track_id=5, bbox_xyxy_norm=(100, 200, 300, 400), face_visible=False)
        recheck = _format_track_line(IdentityQueryItem(is_recheck=True, **kwargs))
        fresh = _format_track_line(IdentityQueryItem(is_recheck=False, **kwargs))
        assert "待复核" not in recheck
        assert recheck == fresh  # 重核与首次完全一致，无可区分标记
        assert "track_id=5" in recheck
        # 仍不含任何身份/姓名先验
        assert "状态=" not in recheck
        assert "疑似" not in recheck


def _audio_only_packet() -> IdentityPacket:
    """构造一个 audio route 入口条件齐全的 IdentityPacket。

    audio_clip 长度 ≥ AAC 单帧 1024 samples，保证 _encode_audio_only_mp4 可走通。
    """
    ep = _mock_edge_packet()
    ep.audio_clip = np.zeros(16000, dtype=np.int16)
    ep.trigger = GateTrigger(
        visual_changed=False,
        visual_change_score=0.05,
        audio_active=True,
        audio_energy_level=0.6,
    )
    return ep


def _video_route_packet() -> IdentityPacket:
    ep = _mock_edge_packet()
    ep.trigger = GateTrigger(
        visual_changed=True,
        visual_change_score=0.5,
        audio_active=True,
        audio_energy_level=0.6,
    )
    return ep


def _multimodal_user_content(messages: list[dict]) -> list[dict]:
    """取 fused messages 里那条多模态主 user 消息的 content（list 形态）。

    家庭档案现以纯文本 user 消息插在 system 与主 user 之间，按 content 是否为 list
    定位主 user 消息，避免硬编码索引随档案有无而漂移。
    """
    return next(
        m["content"] for m in messages
        if m["role"] == "user" and isinstance(m["content"], list)
    )


class TestResolveRoute:
    def test_audio_only_packets_resolve_audio(self):
        assert _resolve_route([_audio_only_packet()]) == "audio"

    def test_visual_changed_resolves_video(self):
        assert _resolve_route([_video_route_packet()]) == "video"

    def test_trigger_none_resolves_video(self):
        ep = _mock_edge_packet()
        assert ep.trigger is None
        assert _resolve_route([ep]) == "video"

    def test_batch_mixed_resolves_video(self):
        """batch 任一 device visual_changed=True → 整 batch 走 video。"""
        assert _resolve_route([_audio_only_packet(), _video_route_packet()]) == "video"

    def test_audio_inactive_resolves_video(self):
        ep = _audio_only_packet()
        ep.trigger = GateTrigger(
            visual_changed=False,
            visual_change_score=0.05,
            audio_active=False,
            audio_energy_level=0.0,
        )
        assert _resolve_route([ep]) == "video"

    def test_global_switch_off_forces_video(self):
        with patch(
            "miloco.perception.engine.omni.prompt_builder._AUDIO_ONLY_ENABLED",
            False,
        ):
            assert _resolve_route([_audio_only_packet()]) == "video"

    def test_empty_packets_resolves_video(self):
        assert _resolve_route([]) == "video"


class TestAudioRoutePayload:
    def test_audio_payload_shape(self):
        """audio route：payload 只有 audio_base64，没有 video_base64 / video_fps。"""
        ep = _audio_only_packet()
        payload = build_prompt(ep, OmniContext())
        assert "audio_base64" in payload
        assert payload["audio_base64"]  # 非空 base64
        assert "video_base64" not in payload
        assert "video_fps" not in payload

    def test_video_route_no_audio_field(self):
        """video route：payload 含 video_base64 / video_fps，不含 audio_base64。"""
        ep = _video_route_packet()
        payload = build_prompt(ep, OmniContext())
        assert "video_base64" in payload
        assert "video_fps" in payload
        assert "audio_base64" not in payload

    def test_audio_route_drops_visual_fields(self):
        """场景装配：audio route 的 system prompt 剥掉视觉字段（caption），video 保留。

        旧设计两 route 共享全量 system prompt；按场景装配后，audio 不再扛 caption 与视频
        观察任务。prefix cache 改为按场景各自命中——同 route 前缀稳定即可。
        """
        audio_payload = build_prompt(_audio_only_packet(), OmniContext())
        video_payload = build_prompt(_video_route_packet(), OmniContext())
        assert audio_payload["system_prompt"] != video_payload["system_prompt"]
        assert "## caption" not in audio_payload["system_prompt"]
        assert "## caption" in video_payload["system_prompt"]
        # audio user_content 末尾锚点也不带视觉措辞（_USER_REF_BOUNDARY_AUDIO）
        assert "视频" not in audio_payload["user_content"]
        assert "视频" in video_payload["user_content"]
        # 同 route 内 system prompt 稳定（prefix cache 友好）
        audio_payload2 = build_prompt(_audio_only_packet(), OmniContext())
        assert audio_payload["system_prompt"] == audio_payload2["system_prompt"]


class TestBuildMessagesContentBlocks:
    """omni_client._build_messages 块组装（audio vs video route）。"""

    def test_audio_route_emits_input_audio_block(self):
        from miloco.perception.engine.omni.omni_client import _build_messages

        ep = _audio_only_packet()
        payload = build_prompt(ep, OmniContext())
        messages = _build_messages(payload)
        user_blocks = messages[1]["content"]
        types = [b["type"] for b in user_blocks]

        assert "input_audio" in types
        assert "video_url" not in types
        audio_block = next(b for b in user_blocks if b["type"] == "input_audio")
        assert audio_block["input_audio"]["data"].startswith("data:audio/m4a;base64,")

    def test_video_route_emits_video_url_block(self):
        from miloco.perception.engine.omni.omni_client import _build_messages

        ep = _video_route_packet()
        payload = build_prompt(ep, OmniContext())
        messages = _build_messages(payload)
        user_blocks = messages[1]["content"]
        types = [b["type"] for b in user_blocks]

        assert "video_url" in types
        assert "input_audio" not in types


class TestFusedAudioRoute:
    """build_fused_payload 在 audio route 下的降级行为。"""

    def test_fused_audio_skips_candidates_and_gallery(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _audio_only_packet()
        fused = build_fused_payload(
            packets=[ep],
            context=OmniContext(),
            candidates=[],
            gallery_snapshot={},
        )
        assert fused["candidate_track_ids"] == []

        user_blocks = _multimodal_user_content(fused["messages"])
        types = [b["type"] for b in user_blocks]
        assert "input_audio" in types
        assert "video_url" not in types
        assert "image_url" not in types

    def test_fused_video_route_unchanged(self):
        """video route 走原有 fused 路径，messages 含 video_url 块。"""
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _video_route_packet()
        fused = build_fused_payload(
            packets=[ep],
            context=OmniContext(),
            candidates=[],
            gallery_snapshot={},
        )
        user_blocks = _multimodal_user_content(fused["messages"])
        types = [b["type"] for b in user_blocks]
        assert "video_url" in types
        assert "input_audio" not in types


class TestMultimodalSanityCheck:
    """防 omni 服务端 400 Multimodal data corrupted: 非空但 size 异常小的 bytes
    入 payload 兜底过滤。"""

    def test_jpeg_block_rejects_too_short_bytes(self):
        import pytest
        from miloco.perception.engine.omni.prompt_builder import _jpeg_block

        with pytest.raises(ValueError, match="jpeg bytes too short"):
            _jpeg_block(b"")
        with pytest.raises(ValueError, match="jpeg bytes too short"):
            _jpeg_block(b"\xff\xd8\xff")  # 半截 JPEG SOI, < 100 bytes

    def test_jpeg_block_accepts_valid_size_bytes(self):
        from miloco.perception.engine.omni.prompt_builder import _jpeg_block

        # 100 bytes payload, _jpeg_block 不做内容合法性校验, 仅 size gate
        block = _jpeg_block(b"\xff\xd8" + b"\x00" * 200)
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_png_block_rejects_too_short_bytes(self):
        import pytest
        from miloco.perception.engine.omni.prompt_builder import _png_block

        with pytest.raises(ValueError, match="png bytes too short"):
            _png_block(b"")
        with pytest.raises(ValueError, match="png bytes too short"):
            _png_block(b"\x89PNG")  # 半截 PNG 签名, < 100 bytes

    def test_png_block_accepts_valid_size_bytes(self):
        from miloco.perception.engine.omni.prompt_builder import _png_block

        block = _png_block(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/png;base64,")


class TestSummarizeMultimodalPayload:
    """fused omni 400 错误时打的 payload 尺寸 summary helper。"""

    def test_summary_counts_text_and_sizes_blocks(self):
        from miloco.perception.engine.omni.omni import _summarize_multimodal_payload

        messages = [
            {"role": "system", "content": "system prompt str"},
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": "world"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,BBBBBB"}},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,CCCCCCCC"}},
                {"type": "input_audio", "input_audio": {"data": "data:audio/m4a;base64,DDDDDDDDDD"}},
            ]},
        ]
        summary = _summarize_multimodal_payload(messages)
        assert "text=2 blocks" in summary
        assert "#2:4b" in summary
        assert "#3:6b" in summary
        assert "#4:8b" in summary
        assert "#5:10b" in summary  # input_audio 块

    def test_summary_handles_no_multimodal(self):
        from miloco.perception.engine.omni.omni import _summarize_multimodal_payload

        messages = [{"role": "user", "content": [{"type": "text", "text": "x"}]}]
        summary = _summarize_multimodal_payload(messages)
        assert "image_url=[none]" in summary
        assert "video_url=[none]" in summary
        assert "input_audio=[none]" in summary


class TestFusedHomeProfileInjection:
    """fused 路径下家庭档案改为 system 之后、主 user 之前的独立 user 消息。"""

    def _patch_profile(self, value: str):
        return patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value=value,
        )

    def test_video_route_injects_profile_as_user_message(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        profile = "# 家庭档案\n\n## 家庭成员\nProfileSentinelMember"
        with self._patch_profile(profile):
            fused = build_fused_payload(
                packets=[_video_route_packet()],
                context=OmniContext(),
                candidates=[],
                gallery_snapshot={},
            )
        messages = fused["messages"]
        # system → 家庭档案 user → 主 user
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == profile
        assert messages[2]["role"] == "user"
        assert isinstance(messages[2]["content"], list)
        # 不占用 system prompt
        assert "ProfileSentinelMember" not in messages[0]["content"]

    def test_audio_route_injects_profile_as_user_message(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        profile = "# 家庭档案\n\n## 家庭成员\nProfileSentinelMember"
        with self._patch_profile(profile):
            fused = build_fused_payload(
                packets=[_audio_only_packet()],
                context=OmniContext(),
                candidates=[],
                gallery_snapshot={},
            )
        messages = fused["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": profile}
        assert isinstance(messages[2]["content"], list)
        assert "ProfileSentinelMember" not in messages[0]["content"]

    def test_no_profile_message_when_empty(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with self._patch_profile(""):
            fused = build_fused_payload(
                packets=[_video_route_packet()],
                context=OmniContext(),
                candidates=[],
                gallery_snapshot={},
            )
        messages = fused["messages"]
        # 档案为空时不插入额外 user 消息，退化为 system + user
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert isinstance(messages[1]["content"], list)


class TestBuildTierCVerifyPayload:
    """build_tier_c_verify_payload: 同人校验 payload 构造 + 空图守卫(无效输入返回 None)。"""

    @staticmethod
    def _crop(h: int = 80, w: int = 50) -> np.ndarray:
        return np.full((h, w, 3), 128, dtype=np.uint8)

    def test_valid_inputs_build_two_image_payload(self):
        out = build_tier_c_verify_payload(
            self._crop(), self._crop(40, 40), [self._crop()], [self._crop(40, 40)],
        )
        assert out is not None
        assert len(out["crops"]) == 2          # QUERY + GALLERY 各一张
        assert out["system_prompt"]
        assert all(c["media_type"] == "image/png" for c in out["crops"])

    def test_empty_query_body_returns_none(self):
        out = build_tier_c_verify_payload(
            np.zeros((0, 0, 3), dtype=np.uint8), self._crop(), [self._crop()], [self._crop()],
        )
        assert out is None

    def test_none_query_body_returns_none(self):
        out = build_tier_c_verify_payload(None, self._crop(), [self._crop()], [self._crop()])
        assert out is None

    def test_empty_gallery_returns_none(self):
        # gallery 两侧皆空 → hstack 无图 → gallery_img None → None
        out = build_tier_c_verify_payload(self._crop(), self._crop(), [], [])
        assert out is None


class TestSceneAssembly:
    """field_registry + SceneDescriptor 驱动的按场景 system prompt 装配。"""

    def _sp(self, **kw):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import build_system_prompt

        return build_system_prompt(SceneDescriptor(**kw), include_home_profile=False)

    def test_identity_field_present_only_when_has_identity(self):
        """漂移修复：有身份候选时 identities 进 schema + 字段说明；无候选时不出现。"""
        with_id = self._sp(route="video", has_identity=True)
        without_id = self._sp(route="video", has_identity=False)
        assert '"identities"' in with_id
        assert "## identities" in with_id
        # 字段本身缺席（"identities" 作为词仍可能出现在别的字段交叉引用里，故断言字段标记）
        assert '"identities"' not in without_id
        assert "## identities" not in without_id

    def test_identity_first_in_schema(self):
        """有身份候选时 identities 置于 schema 最前（先识别）。"""
        sp = self._sp(route="video", has_identity=True)
        assert sp.index('"identities"') < sp.index('"caption"')

    def test_audio_scene_drops_visual_fields(self):
        sp = self._sp(route="audio", has_identity=False)
        assert "## caption" not in sp
        assert '"caption"' not in sp
        assert '"identities"' not in sp
        assert "## identities" not in sp
        assert "## speeches" in sp
        assert "音频" in sp

    def test_audio_scene_is_audio_native(self):
        """audio 路由 prompt 音频原生化：剥离 matched_rules（规则判断本质需视觉）、无正向视觉
        指令、含无画面声明。

        纯音频轮曾出现模型脑补 "画面中看到 X 进玄关" + 声明 video 证据 + 误触发 welcome
        规则。现 audio 路由直接去掉 matched_rules 字段（从源头消灭该幻觉），只做 speeches /
        env_sounds / suggestions。
        """
        sp = self._sp(route="audio", has_identity=False)
        # matched_rules 整字段（及规则判断任务）在 audio 路由消失
        assert "## matched_rules" not in sp
        assert '"matched_rules"' not in sp
        assert "规则判断" not in sp
        # 无正向视觉指令；显式声明本轮无画面；通用常识切到音频版
        assert "画面里看到某人" not in sp
        assert "本轮只有音频" in sp
        assert "不得声明 video 证据" in sp
        assert "本轮仅音频" in sp
        # video 路由仍完整保留 matched_rules（未被波及）
        sp_v = self._sp(route="video", has_identity=False)
        assert "## matched_rules" in sp_v

    def test_stream_orders_speeches_before_caption(self):
        sp = self._sp(route="video", has_identity=False, stream=True)
        assert sp.index('"speeches"') < sp.index('"caption"')

    def test_workflow_and_reminder_merged_into_field_spec(self):
        """独立「# 工作流程」「# 提醒判定」段均已取消，全部内联进「# 字段说明」各字段块。"""
        sp = self._sp(route="video", has_identity=True)
        assert "# 工作流程" not in sp
        assert "# 提醒判定" not in sp
        assert "# 字段说明" in sp
        # suggestions 的触发/urgency 逻辑现内联在 ## suggestions
        assert "## suggestions" in sp
        assert "urgency" in sp


class TestReadonlyHistoryMessage:
    """Phase 3: fused video 路径把历史参考抽到独立「只读历史」user 消息，主 user 只剩本轮事实。"""

    def _patch_no_profile(self):
        return patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value="",
        )

    def test_pending_speech_emits_readonly_message(self):
        """只剩 pending_speech 这类客观跨窗事实会触发独立「只读历史」消息（last_caption /
        last_suggestions 已停止注入）。"""
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ctx = OmniContext(
            pending_speech=[{"speaker": "小明", "content": "帮我"}],
            rule_conditions=[RuleCondition(rule_id="r1", rule_name="x", query="有人读书")],
        )
        with self._patch_no_profile():
            fused = build_fused_payload(
                packets=[_video_route_packet()], context=ctx, candidates=[], gallery_snapshot={},
            )
        messages = fused["messages"]
        # system → 待判断规则 user(str) → 只读历史 user(str) → 主 user(list)
        str_users = [m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str)]
        rule_msg = next(c for c in str_users if "待判断规则" in c)
        hist_msg = next(c for c in str_users if "上一窗未说完的话" in c)
        # 规则在独立段、按 rule_name 渲染（非历史）
        assert "有人读书" in rule_msg
        # 历史段只含 pending_speech，不含已停止注入的 last_caption/last_suggestions
        assert "帮我" in hist_msg
        assert "<last_caption>" not in hist_msg
        assert "<last_suggestions>" not in hist_msg
        # 主 user（本轮事实）不含历史
        main = _multimodal_user_content(messages)
        main_text = "\n".join(b.get("text", "") for b in main if b.get("type") == "text")
        assert "帮我" not in main_text

    def test_no_history_no_readonly_message(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with self._patch_no_profile():
            fused = build_fused_payload(
                packets=[_video_route_packet()], context=OmniContext(), candidates=[], gallery_snapshot={},
            )
        messages = fused["messages"]
        # 空 context + 无档案：退化为 system + 主 user，无只读历史消息
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert isinstance(messages[1]["content"], list)


# =============================================================================
# Section 7.2 B 矩阵 — _is_audio_only hold 短路
# =============================================================================
import miloco.perception.engine.omni.prompt_builder as _pb_mod  # noqa: E402
from miloco.perception.engine.omni.prompt_builder import _is_audio_only  # noqa: E402


class _FakeIdentityPacket:
    """最小满足 _is_audio_only 访问 .trigger 的桩。"""
    def __init__(self, trigger):
        self.trigger = trigger


def _trigger(visual: bool, audio: bool, hold: bool) -> GateTrigger:
    return GateTrigger(
        visual_changed=visual,
        visual_change_score=1.0 if visual else 0.0,
        audio_active=audio,
        audio_energy_level=1.0 if audio else 0.0,
        hold=hold,
    )


class TestIsAudioOnlyHoldShortCircuit:
    """B 矩阵 — hold 标志位短路 audio-only 路由判定。"""

    def test_B1_single_packet_hold_blocks_audio_only(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", True)
        packets = [_FakeIdentityPacket(_trigger(visual=False, audio=True, hold=True))]
        assert _is_audio_only(packets) is False

    def test_B2_single_packet_normal_audio_only(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", True)
        packets = [_FakeIdentityPacket(_trigger(visual=False, audio=True, hold=False))]
        assert _is_audio_only(packets) is True

    def test_B3_batch_any_hold_blocks(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", True)
        packets = [
            _FakeIdentityPacket(_trigger(visual=False, audio=True, hold=True)),
            _FakeIdentityPacket(_trigger(visual=False, audio=True, hold=False)),
        ]
        assert _is_audio_only(packets) is False

    def test_B4_batch_all_no_hold_audio_only(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", True)
        packets = [
            _FakeIdentityPacket(_trigger(visual=False, audio=True, hold=False)),
            _FakeIdentityPacket(_trigger(visual=False, audio=True, hold=False)),
        ]
        assert _is_audio_only(packets) is True

    def test_B5_trigger_none_not_audio_only(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", True)
        packets = [_FakeIdentityPacket(None)]
        assert _is_audio_only(packets) is False

    def test_B6_global_disabled_returns_false(self, monkeypatch):
        monkeypatch.setattr(_pb_mod, "_AUDIO_ONLY_ENABLED", False)
        packets = [_FakeIdentityPacket(_trigger(visual=False, audio=True, hold=False))]
        assert _is_audio_only(packets) is False


def _mp4_has_audio_stream(b64_mp4: str) -> bool:
    """解码 base64 mp4，判断是否含音频轨。"""
    import base64
    import io

    import av

    raw = base64.b64decode(b64_mp4)
    with av.open(io.BytesIO(raw)) as container:
        return len(container.streams.audio) > 0


def _video_packet(audio_active: bool, speech_active: bool | None = None) -> IdentityPacket:
    """video 路由 packet（visual_changed=True），音频长度足够编码 AAC 轨。

    speech_active 默认跟随 audio_active（无 VAD 关注时维持"有音频即有 speeches"语义）；
    需要单独验证 VAD 门控时显式传入。"""
    ep = _video_route_packet()
    ep.audio_clip = np.zeros(16000, dtype=np.int16)
    ep.trigger.audio_active = audio_active
    ep.trigger.speech_active = audio_active if speech_active is None else speech_active
    return ep


class TestEncodeVideoAudioGating:
    """video 路由按 audio gate 结果决定是否把音频轨编进 mp4（反语音幻觉）。"""

    def test_audio_track_included_when_audio_active(self):
        b64 = _encode_video(_video_packet(audio_active=True))
        assert b64 is not None
        assert _mp4_has_audio_stream(b64)

    def test_audio_track_dropped_when_audio_inactive(self):
        b64 = _encode_video(_video_packet(audio_active=False))
        assert b64 is not None
        assert not _mp4_has_audio_stream(b64)

    def test_audio_track_included_when_trigger_none(self):
        """trigger=None（主动查询/旧路径）保持原行为：照常合成音频。"""
        ep = _mock_edge_packet()
        ep.audio_clip = np.zeros(16000, dtype=np.int16)
        assert ep.trigger is None
        b64 = _encode_video(ep)
        assert b64 is not None
        assert _mp4_has_audio_stream(b64)


class TestSpeechFieldsGatedByAudio:
    """video 路由没发音频时，schema 必须剥掉 speeches / env_sounds——否则模型会就着
    画面脑补人声（实测 audio_tokens=0 仍幻觉出"帮我把那个文件发一下"）。

    断言用 schema 字面量（``"speeches":[{"speaker"`` / ``"env_sounds":"环境音描述"``），
    避开 system prompt 里「输出实例」「总原则」对字段名的非 schema 提及。
    """

    _SPEECH_LIT = '"speeches":[{"speaker"'
    _ENV_LIT = '"env_sounds":"环境音描述"'

    def test_video_no_audio_route_drops_speech_fields(self):
        from miloco.perception.engine.omni.field_registry import (
            SceneDescriptor,
            render_schema,
        )

        schema = render_schema(SceneDescriptor(route="video", has_audio=False))
        assert "speeches" not in schema
        assert "env_sounds" not in schema
        assert "caption" in schema  # 视觉字段不受影响

    def test_video_with_audio_keeps_speech_fields(self):
        from miloco.perception.engine.omni.field_registry import (
            SceneDescriptor,
            render_schema,
        )

        schema = render_schema(SceneDescriptor(route="video", has_audio=True))
        assert "speeches" in schema and "env_sounds" in schema

    def test_build_prompt_no_audio_omits_speeches_schema(self):
        sp = build_prompt(_video_packet(audio_active=False), OmniContext())["system_prompt"]
        assert self._SPEECH_LIT not in sp
        assert self._ENV_LIT not in sp

    def test_build_prompt_audio_active_keeps_speeches_schema(self):
        sp = build_prompt(_video_packet(audio_active=True), OmniContext())["system_prompt"]
        assert self._SPEECH_LIT in sp

    def test_build_prompt_trigger_none_keeps_speeches_schema(self):
        """trigger=None（主动查询/旧路径）保持原行为：schema 含 speeches。"""
        ep = _mock_edge_packet()
        assert ep.trigger is None
        assert self._SPEECH_LIT in build_prompt(ep, OmniContext())["system_prompt"]


class TestSpeechFieldsGatedByVAD:
    """音频过了能量 gate 但 VAD 判无人声（speech_active=False）时：只剥 speeches，
    保留 env_sounds / caption；principle、任务行、实例 A 都不再提"转录/人声"，避免重新
    诱导模型在键鼠 / 底噪上脑补"像指令的话"（实测留着 speeches 字段就幻觉）。"""

    _SPEECH_LIT = '"speeches":[{"speaker"'
    _ENV_LIT = '"env_sounds":"环境音描述"'

    def test_schema_drops_only_speeches_when_no_speech(self):
        from miloco.perception.engine.omni.field_registry import (
            SceneDescriptor,
            render_schema,
        )

        schema = render_schema(
            SceneDescriptor(route="video", has_audio=True, has_speech=False)
        )
        assert "speeches" not in schema
        assert "env_sounds" in schema  # 非人声音频事件仍要
        assert "caption" in schema

    def test_build_prompt_no_speech_omits_speeches_keeps_env(self):
        sp = build_prompt(
            _video_packet(audio_active=True, speech_active=False), OmniContext()
        )["system_prompt"]
        assert self._SPEECH_LIT not in sp
        assert self._ENV_LIT in sp
        # 任务行不再提"转录"，避免重新诱导脑补人声
        assert "转录" not in sp

    def test_build_prompt_speech_active_keeps_speeches(self):
        sp = build_prompt(
            _video_packet(audio_active=True, speech_active=True), OmniContext()
        )["system_prompt"]
        assert self._SPEECH_LIT in sp

    def test_identity_example_dropped_when_no_speech(self):
        """实例 A 的输出含 speeches（needs_response 指令），无人声时不附——否则与剥掉的
        schema 矛盾、并重新诱导脑补指令。"""
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import _render_examples

        out = _render_examples(
            SceneDescriptor(route="video", has_audio=True, has_speech=False, has_identity=True)
        )
        assert "实例 A" not in out
        assert "实例 B" in out  # 无 speeches，照常附

    def test_pending_speech_does_not_force_speeches_when_vad_silent(self):
        """本轮 VAD 判无人声时，即便挂着 <pending_speech>（上窗的半句）也照剥 speeches：
        没真听到延续就不该补全，否则模型会就着噪声脑补出一个完成句。pending 的拼接靠的是
        本轮真有延续语音（VAD 自然会过），而非"有 pending 就强留字段"。"""
        ep = _video_packet(audio_active=True, speech_active=False)
        ctx = OmniContext(pending_speech=[{"speaker": "未知", "content": "打开"}])
        sp = build_prompt(ep, ctx)["system_prompt"]
        assert self._SPEECH_LIT not in sp

    def test_pending_speech_stitched_when_vad_has_speech(self):
        """本轮真有人声（VAD 过）+ 挂着 pending → 保留 speeches，模型可把 <pending_speech>
        '打开' 与本轮 '空调' 拼成 '打开空调'。"""
        ep = _video_packet(audio_active=True, speech_active=True)
        ctx = OmniContext(pending_speech=[{"speaker": "未知", "content": "打开"}])
        sp = build_prompt(ep, ctx)["system_prompt"]
        assert self._SPEECH_LIT in sp


class TestPendingStitchPromptWording:
    """last_speech 续接：数据与 gate-first 判断指令捆在 user 段（实测正样本 6/6 拼对、
    多数无关句不乱拼）。锁住 prompt 形态：last_speech 块在 user 段、用"能否拼接"判断式、
    旧的"不得复制"禁令与具体例子都已移除（例子会被字面套用、不泛化）。"""

    def _build(self):
        ep = _video_packet(audio_active=True, speech_active=True)
        return build_prompt(ep, OmniContext(pending_speech=[{"speaker": "未知", "content": "打开"}]))

    def test_last_speech_block_in_user_content(self):
        out = self._build()
        assert "last_speech：打开" in out["user_content"]
        # gate-first：判断"能否"拼接、连贯完整才拼；且不得用 last_speech 改写本轮
        assert "能否" in out["user_content"]
        assert "不能用 last_speech 改写本轮" in out["user_content"]

    def test_no_forbidding_or_example_leftover(self):
        out = self._build()
        whole = out["system_prompt"] + out["user_content"]
        assert "不得复制其文本" not in whole  # 旧最高优先级禁令已移除（它会压住续接）
        assert "打开台灯" not in whole  # 不带具体拼接例子——例子会被字面套用、无法泛化


class TestBatchVideoHasAudio:
    """_batch_video_has_audio 按"首个有 frames 的设备"的音频门控结果判定，
    与 _encode_batch_video 选设备口径一致（has_audio 必须反映实际合进 mp4 的那台设备）。"""

    def test_single_device_audio_active(self):
        assert _batch_video_has_audio([_video_packet(audio_active=True)]) is True

    def test_single_device_audio_inactive(self):
        assert _batch_video_has_audio([_video_packet(audio_active=False)]) is False

    def test_uses_first_framed_device_not_any(self):
        """首个有 frames 的设备 audio_active=False → False，即使后面有 active 设备
        （只有首个设备的音视频会被合进 mp4，has_audio 必须跟它走，不是"任一有音频"）。"""
        first = _video_packet(audio_active=False)
        second = _video_packet(audio_active=True)
        assert _batch_video_has_audio([first, second]) is False

    def test_skips_frameless_leading_device(self):
        """首个 packet 无 frames → 顺延到下一个有 frames 的设备（与 _encode_batch_video 一致）。"""
        frameless = _video_packet(audio_active=True)
        frameless.all_frames = []
        framed = _video_packet(audio_active=True)
        assert _batch_video_has_audio([frameless, framed]) is True

    def test_no_framed_device_returns_false(self):
        ep = _video_packet(audio_active=True)
        ep.all_frames = []
        assert _batch_video_has_audio([ep]) is False

    def test_empty_packets_returns_false(self):
        assert _batch_video_has_audio([]) is False

    def test_trigger_none_treated_as_audio_present(self):
        """trigger=None（主动查询/旧路径）视为有音频，保持原行为。"""
        ep = _mock_edge_packet()  # all_frames 非空、trigger=None
        assert ep.trigger is None
        assert _batch_video_has_audio([ep]) is True


class TestExamplesGatedByAudio:
    """无音频时也撤掉「# 输出实例」——例句输出含 speeches/env_sounds，schema 已剥离，
    留着会与 schema 自相矛盾、并可能诱导模型照搬音频字段。"""

    def test_render_examples_dropped_when_no_audio(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        assert _render_examples(SceneDescriptor(route="video", has_audio=False)) == ""

    def test_render_examples_kept_when_audio(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        out = _render_examples(SceneDescriptor(route="video", has_audio=True))
        assert "# 输出实例" in out

    def test_build_prompt_no_audio_drops_env_sounds_example(self):
        """端到端：无音频时整份 system prompt 不再出现例句里的音频字段示范。"""
        sp = build_prompt(_video_packet(audio_active=False), OmniContext())["system_prompt"]
        assert "# 输出实例" not in sp
        assert "重物倒地声" not in sp  # _EXAMPLE_CHAIN 里的 env_sounds 示范

    def test_build_prompt_audio_active_keeps_example(self):
        sp = build_prompt(_video_packet(audio_active=True), OmniContext())["system_prompt"]
        assert "# 输出实例" in sp


class TestNoAudioPromptDropsAudioFieldRefs:
    """video 无音频时，整份 system prompt 不再提被剥掉的音频字段——任务列表的
    「音频理解：…才转录」与总原则的「写进 speeches/env_sounds」都随 has_audio 门控。"""

    def _sys_prompt(self, has_audio: bool) -> str:
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import build_system_prompt

        return build_system_prompt(
            SceneDescriptor(route="video", has_audio=has_audio), include_home_profile=False,
        )

    def test_no_audio_strips_audio_field_refs(self):
        sp = self._sys_prompt(has_audio=False)
        for w in ("speeches", "env_sounds", "音频理解", "转录"):
            assert w not in sp, f"无音频 prompt 不应再出现 {w!r}"

    def test_audio_present_keeps_audio_field_refs(self):
        sp = self._sys_prompt(has_audio=True)
        assert "speeches" in sp
        assert "env_sounds" in sp
        assert "音频理解" in sp


# =============================================================================
# 身份库为空 → identity 精简为 no_person-only（matching_moot / identity_match_disabled）
# =============================================================================
class TestIdentityMatchDisabled:
    """库空时 identities 字段改精简版（只判 unknown/no_person、不做成员匹配）。

    这是"gallery 为空不该激活成员匹配"修复的 prompt 侧断言：schema 的 name 域收成
    <unknown|no_person>、字段说明砍掉全套面部匹配规则、gallery 段整段不渲染；no_person
    判定链路不动（name 仍走 no_person）。库非空（默认 matching_moot=False）行为不变。
    """

    _MATCH_ONLY_MARKER = "本人正向吻合"   # 只在完整版 IDENTITY spec 里出现
    _NO_MATCH_MARKER = "不做成员匹配"     # 只在精简版 IDENTITY_NO_MATCH spec 里出现

    def test_render_schema_name_domain_collapses(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import render_schema

        slim = render_schema(SceneDescriptor(
            route="video", has_identity=True, identity_match_disabled=True))
        full = render_schema(SceneDescriptor(
            route="video", has_identity=True, identity_match_disabled=False))
        assert '"name":"<unknown|no_person>"' in slim
        assert "姓名" not in slim
        assert '"name":"<姓名|unknown|no_person>"' in full

    def test_render_field_spec_drops_matching_rules(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import render_field_spec

        slim = render_field_spec(SceneDescriptor(
            route="video", has_identity=True, identity_match_disabled=True))
        full = render_field_spec(SceneDescriptor(
            route="video", has_identity=True, identity_match_disabled=False))
        # 精简版：有 no_person 判据、无成员匹配规则
        assert self._NO_MATCH_MARKER in slim
        assert self._MATCH_ONLY_MARKER not in slim
        assert "no_person" in slim
        # 完整版：保留匹配规则（回归保护）
        assert self._MATCH_ONLY_MARKER in full
        assert self._NO_MATCH_MARKER not in full

    def _candidate(self):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        return IdentityQueryItem(
            track_id=7, bbox_xyxy_norm=(100, 100, 200, 400), face_visible=False)

    def test_matching_moot_skips_gallery_block_keeps_track_list(self):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value="",
        ):
            fused = build_fused_payload(
                packets=[_video_route_packet()], context=OmniContext(),
                candidates=[self._candidate()], gallery_snapshot={},
                matching_moot=True,
            )
        messages = fused["messages"]
        system_prompt = messages[0]["content"]
        main = _multimodal_user_content(messages)
        main_text = "\n".join(b.get("text", "") for b in main if b.get("type") == "text")

        # 主 user：无 gallery 段（连"库为空"占位文本都不塞），但待识别 track 列表仍在
        assert "<gallery>" not in main_text
        assert "库为空" not in main_text
        assert "待识别 track" in main_text
        # system prompt：用精简版 identity spec
        assert self._NO_MATCH_MARKER in system_prompt
        assert self._MATCH_ONLY_MARKER not in system_prompt

    def test_default_matching_moot_false_keeps_empty_gallery_placeholder(self):
        """回归保护：matching_moot 默认 False 时，库空仍走旧行为（塞"库为空"占位 + 完整匹配 spec）。"""
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value="",
        ):
            fused = build_fused_payload(
                packets=[_video_route_packet()], context=OmniContext(),
                candidates=[self._candidate()], gallery_snapshot={},
            )
        messages = fused["messages"]
        system_prompt = messages[0]["content"]
        main = _multimodal_user_content(messages)
        main_text = "\n".join(b.get("text", "") for b in main if b.get("type") == "text")
        assert "库为空" in main_text
        assert self._MATCH_ONLY_MARKER in system_prompt

    # ---- follow-up（PR #407 code review）：库空时「任务描述 / 示例」也须收敛，非只 gallery/字段说明 ----
    _TASK_MATCH_MARKER = "对照图片库"    # 只在完整版「# 任务」身份行里出现
    _EXAMPLE_A_MARKER = "## 实例 A"       # 只在成员匹配 few-shot（实例 A）里出现

    def test_task_list_slim_when_matching_moot(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import _render_task_list

        slim = _render_task_list(SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=True))
        assert self._TASK_MATCH_MARKER not in slim
        assert "库中哪一位" not in slim
        assert "不做成员匹配" in slim

    def test_task_list_full_when_gallery_present(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import _render_task_list

        full = _render_task_list(SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=False))
        assert self._TASK_MATCH_MARKER in full

    def test_example_a_dropped_when_matching_moot(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import _render_examples

        # has_speech=True：未修复时实例 A 本会注入，确保断言有意义
        out = _render_examples(SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=True))
        assert self._EXAMPLE_A_MARKER not in out   # 成员匹配 few-shot 不注入
        assert "实例 B" in out                       # 通用观察 few-shot 照常
        # 库空实例 B 用泛称版：无成员铺垫的窗口不示范 caption 叫专名
        assert "小明" not in out
        assert "某人坐在电脑前" in out

    def test_example_a_present_when_gallery_present(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import _render_examples

        out = _render_examples(SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=False))
        assert self._EXAMPLE_A_MARKER in out
        # 库非空用带名版实例 B（专名由实例 A 的 gallery 铺垫），非泛称版
        assert "小明坐在电脑前" in out

    def test_system_prompt_no_member_matching_leak_when_moot(self):
        """整类 catch-all：库空 system prompt 不得残留任何成员匹配内容（任务描述 /
        字段说明规则 / few-shot 示例），且带精简版标记。has_speech=True 让实例 A 在
        未修复时本会注入——确保 absence 断言不是空断言。将来新增身份 prompt 段若漏 gate，
        这条会红。"""
        from miloco.perception.engine.omni.field_registry import SceneDescriptor
        from miloco.perception.engine.omni.prompt_builder import build_system_prompt

        moot = SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=True)
        sp = build_system_prompt(moot, include_home_profile=False)
        for leak in (self._TASK_MATCH_MARKER, "库中哪一位",
                     self._EXAMPLE_A_MARKER, self._MATCH_ONLY_MARKER):
            assert leak not in sp, f"库空 system prompt 残留成员匹配内容: {leak!r}"
        assert self._NO_MATCH_MARKER in sp

        # 正向对照：库非空同场景，上述 marker 确实存在（证明 absence 断言非空断言）
        full = build_system_prompt(SceneDescriptor(
            route="video", has_identity=True, has_audio=True, has_speech=True,
            identity_match_disabled=False), include_home_profile=False)
        assert self._TASK_MATCH_MARKER in full
        assert self._EXAMPLE_A_MARKER in full
        assert self._MATCH_ONLY_MARKER in full
