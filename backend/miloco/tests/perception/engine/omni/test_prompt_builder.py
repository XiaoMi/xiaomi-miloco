"""Tests for Omni Layer — Prompt Builder."""

import contextlib
from unittest.mock import patch

import numpy as np
import pytest
from miloco.perception.engine.omni.prompt_builder import (
    _batch_video_has_audio,
    _encode_video,
    _render_examples,
    _resolve_route,
    build_prompt,
    build_query_prompt,
    build_stream_prompt,
    build_system_prompt,
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
        assert payload["media_info"] is not None

    def test_camera_prompt_in_system_prompt(self):
        """camera_prompt 注入 system prompt 尾部，不在 user_content。"""
        ep = _mock_edge_packet()
        ctx = OmniContext(
            room_name="门口",
            camera_prompt="画面右侧是公共走廊电梯门，与本户无关；只有正中木色入户门才是本户。",
        )
        payload = build_prompt(ep, ctx)
        sp = payload["system_prompt"]
        assert "本摄像头须知" in sp  # 段头在 system prompt
        assert "公共走廊电梯门" in sp  # 用户自定义文本
        uc = payload["user_content"]
        assert "本摄像头须知" not in uc  # 不再出现在 user_content

    def test_no_camera_prompt_no_note(self):
        """未配置 camera_prompt → system prompt 不出现须知段。"""
        ep = _mock_edge_packet()
        payload = build_prompt(ep, OmniContext(room_name="门口"))
        assert "本摄像头须知" not in payload["system_prompt"]
        assert "本摄像头须知" not in payload["user_content"]

    def test_blank_camera_prompt_stripped_to_none(self):
        """空白 camera_prompt → strip 后为空 → 不注入 system prompt。"""
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(
            SceneDescriptor(route="video", has_identity=False, stream=False),
            camera_prompt="   ",
            include_home_profile=False,
        )
        assert "本摄像头须知" not in sp

    def test_build_system_prompt_with_camera_prompt(self):
        """build_system_prompt 直接传 camera_prompt → 尾部带 ## 本摄像头须知 段。"""
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(
            SceneDescriptor(route="video", has_identity=False, stream=False),
            camera_prompt="忽略窗外马路",
            include_home_profile=False,
        )
        assert "## 本摄像头须知" in sp
        assert "忽略窗外马路" in sp

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
        """audio route：payload 只有 audio_base64，没有 video_base64。"""
        ep = _audio_only_packet()
        payload = build_prompt(ep, OmniContext())
        assert "audio_base64" in payload
        assert payload["audio_base64"]
        assert "video_base64" not in payload

    def test_video_route_no_audio_field(self):
        """video route：payload 含 video_base64 + media_info，不含 audio_base64。"""
        ep = _video_route_packet()
        payload = build_prompt(ep, OmniContext())
        assert "video_base64" in payload
        assert "media_info" in payload
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
        from miloco.perception.engine.omni.provider import MiMoAdapter

        ep = _audio_only_packet()
        payload = build_prompt(ep, OmniContext())
        messages = _build_messages(payload, MiMoAdapter())
        user_blocks = messages[1]["content"]
        types = [b["type"] for b in user_blocks]

        assert "input_audio" in types
        assert "video_url" not in types
        audio_block = next(b for b in user_blocks if b["type"] == "input_audio")
        assert audio_block["input_audio"]["data"].startswith("data:audio/m4a;base64,")

    def test_video_route_emits_video_url_block(self):
        from miloco.perception.engine.omni.omni_client import _build_messages
        from miloco.perception.engine.omni.provider import MiMoAdapter

        ep = _video_route_packet()
        payload = build_prompt(ep, OmniContext())
        messages = _build_messages(payload, MiMoAdapter())
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
        b64, _info = _encode_video(_video_packet(audio_active=True))
        assert b64 is not None
        assert _mp4_has_audio_stream(b64)

    def test_audio_track_dropped_when_audio_inactive(self):
        b64, _info = _encode_video(_video_packet(audio_active=False))
        assert b64 is not None
        assert not _mp4_has_audio_stream(b64)

    def test_audio_track_included_when_trigger_none(self):
        """trigger=None（主动查询/旧路径）保持原行为：照常合成音频。"""
        ep = _mock_edge_packet()
        ep.audio_clip = np.zeros(16000, dtype=np.int16)
        assert ep.trigger is None
        b64, _info = _encode_video(ep)
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


def _adaptive_packet(
    *,
    person_id: str = "none",
    bbox_xyxy_norm: tuple[int, int, int, int] | None = None,
    frames: list[np.ndarray] | None = None,
    fps: int = 1,
    body_box: tuple[int, int, int, int] = (100, 100, 80, 120),
) -> IdentityPacket:
    """640x480 噪声帧(保证 crop 视频编码后过 size gate)+ 一个小 human_body 框(crop 面积达标)。

    默认 target 是 none(不进名册);传 confirmed pid + bbox_xyxy_norm 可让它以
    ``名[bbox=...]`` 进名册,用于验证 crop 生效时 bbox 坐标系说明的指向。
    frames / fps 可覆盖:验参考帧取末帧、crop 视频帧率跟随 frame_info.fps。
    body_box(像素 xywh)可覆盖:换大帧时默认框离画面角太近,_enforce_min_area 绕中心放大会
    被 clamp 截断而达不到 crop_min_area_ratio → 回退全景;此时需给一个居中的框。
    """
    if frames is None:
        np.random.seed(1234)
        frames = [np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8) for _ in range(5)]
    return IdentityPacket(
        packet_id="ep-adaptive",
        room_name="study-room",
        timestamp=1000.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=fps),
        targets=[
            IdentityTarget(
                type=ObjectType.HUMAN_BODY,
                person_id=person_id,
                track_id=1,
                needs_omni_verify=False,
                bbox_xyxy_norm=bbox_xyxy_norm,
                box_info=[TrackingBoxInfo(frame_index=0, boxes={"human_body": body_box})],
            ),
        ],
        scene_motion=MotionState.STATIC,
        frames=[SelectedFrame(frame_index=0, image=frames[0], resolution=FrameResolution.HIGH, crops=[])],
        all_frames=frames,
        audio_clip=np.zeros(100, dtype=np.int16),
        audio_analysis=AudioAnalysis(type=AudioType.SILENCE, is_urgent=False, energy_level=0.0),
    )


class TestAdaptiveResolution:
    """Smart Crop(智能裁切增强)在 fused 路径的接线。

    激活看 crop_enhance 双闸(enabled=ops 灰度闸 AND user_enabled=用户开关),**与
    video_short_edge 正交** —— 分辨率档只决定编码短边(全景 / crop / 参考帧),不再决定裁不裁。
    """

    def _patches(self, *, short_edge: int = 512, enabled: bool = True, user_enabled: bool = True):
        from miloco.perception.engine.config import CropEnhanceConfig

        return (
            patch(
                "miloco.perception.engine.omni.prompt_builder._get_video_short_edge",
                return_value=short_edge,
            ),
            patch(
                "miloco.perception.engine.omni.crop_enhance.crop_enhance_config_from_settings",
                return_value=CropEnhanceConfig(enabled=enabled, user_enabled=user_enabled),
            ),
        )

    def _content(self, packet=None, **kwargs):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        fused = build_fused_payload(
            packets=[packet or _adaptive_packet()], context=OmniContext(),
            gallery_snapshot={}, **kwargs,
        )
        return _multimodal_user_content(fused["messages"])

    def _has_ref(self, content) -> bool:
        return any(
            b.get("type") == "text" and "全景场景参考" in b.get("text", "")
            for b in content
        )

    def _bbox_note(self, content) -> str:
        return next(
            (b["text"] for b in content
             if b.get("type") == "text" and "bbox=(x1, y1, x2, y2)" in b.get("text", "")),
            "",
        )

    def test_on_adds_ref_before_video(self):
        p1, p2 = self._patches()
        with p1, p2:
            content = self._content(candidates=[])
        assert self._has_ref(content)
        img_idx = next(
            i for i, b in enumerate(content)
            if b.get("type") == "image_url" and "image/jpeg" in b["image_url"]["url"]
        )
        vid_idx = next(i for i, b in enumerate(content) if b.get("type") == "video_url")
        assert img_idx < vid_idx  # 全景参考帧在 crop 视频之前

    def test_skipped_when_candidates_present(self):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        p1, p2 = self._patches()
        with p1, p2:
            content = self._content(candidates=[IdentityQueryItem(track_id=1)])
        assert not self._has_ref(content)  # 有身份候选 → 不 crop(bbox 锚定全景)

    def test_off_when_user_switch_off(self):
        p1, p2 = self._patches(user_enabled=False)  # ops 闸开、用户开关关
        with p1, p2:
            content = self._content(candidates=[])
        assert not self._has_ref(content)

    def test_off_when_ops_gate_off(self):
        p1, p2 = self._patches(enabled=False)  # 用户开关开,但 ops 灰度闸压死
        with p1, p2:
            content = self._content(candidates=[])
        assert not self._has_ref(content)

    @pytest.mark.parametrize("short_edge", [360, 512, 768, 1080])
    def test_on_at_any_resolution(self, short_edge):
        # 与 video_short_edge 正交:任何分辨率档下双闸开都要裁(旧哨兵设计只有 0 档才裁)。
        p1, p2 = self._patches(short_edge=short_edge)
        with p1, p2:
            content = self._content(candidates=[])
        assert self._has_ref(content)

    # 与 _adaptive_packet 默认 body_box=(100,100,80,120)(xywh)自洽的名册 bbox:
    # xyxy=(100,100,180,220) 按 640x480 帧走 identity.engine._normalize_bbox_to_1000
    # (round(v*1000/dim))→ (156, 208, 281, 458)。生产里这两者同源(都读
    # box_info[-1].boxes["human_body"]),测试数据也必须自洽,否则 remap 会正确地判定
    # "框在 crop 区域外"而退化为纯名,断言就测不到换算本身。
    _SELF_CONSISTENT_BBOX = (156, 208, 281, 458)

    def _roster_text(self, content) -> str:
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")

    def test_bbox_remapped_into_crop_coords_when_cropped(self):
        # crop 生效时名册 bbox 必须换算进 crop 坐标系 —— 不换算就是拿全景坐标读局部画面
        # (方向性错误,会把姓名贴到 crop 中央那个人身上)。
        # 固定 region 以便硬编码期望值,避免"用被测代码算期望值"的同义反复;几何精度本身
        # 由 test_crop_enhance 的纯函数测试坐实,这里验的是 region/frame_size 的**接线**。
        from unittest.mock import patch as _patch

        # region=(50,50,350,350) → 300x300;帧 640x480。
        # x: 156*640/1000=99.84 →(-50)/300*1000 → 166;281*640/1000=179.84 → 433
        # y: 208*480/1000=99.84 →(-50)/300*1000 → 166;458*480/1000=219.84 → 566
        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, _patch(
            "miloco.perception.engine.omni.crop_enhance.compute_crop_region",
            return_value=(50, 50, 350, 350),
        ):
            content = self._content(packet=member, candidates=[])
        assert self._has_ref(content)  # 确认本窗确实 crop 了
        roster = self._roster_text(content)
        assert "[bbox=(166, 166, 433, 566)]" in roster  # 换算后的 crop 坐标
        assert "[bbox=(156, 208, 281, 458)]" not in roster  # 全景原坐标不得残留
        # 括注是唯一一句「上文坐标读视频、别读这张全景图」的防混淆措辞。反面断言在
        # test_bbox_dropped_when_outside_crop_region;正面这条不补的话,把 anchor_hint
        # 恒置空的改动不会让任何测试变红。
        guide = next(b["text"] for b in content
                     if b.get("type") == "text" and "全景场景参考" in b.get("text", ""))
        assert "上文 bbox 对应放大后的视频" in guide

    def test_bbox_dropped_when_outside_crop_region(self):
        # 框落在 crop 区域外(理论上不该发生:区域是这些框的并集;但 identity 侧有一条
        # boxes.get("human") 回退键不在 _MAIN_BOX_TYPES 里)→ 退化为纯名。
        # 宁可只给姓名不给位置,也不能输出与画面错配的坐标。
        from unittest.mock import patch as _patch

        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, _patch(
            "miloco.perception.engine.omni.crop_enhance.compute_crop_region",
            return_value=(400, 300, 640, 480),  # 与 bbox 像素范围(100-180, 100-220)无交集
        ):
            content = self._content(packet=member, candidates=[])
        roster = self._roster_text(content)
        assert "已识别人物：p-alice" in roster
        assert "[bbox=" not in roster  # 坐标整个撤掉,不留错值
        assert self._bbox_note(content) == ""  # 名册无 bbox 且无候选 → 说明句也不发
        # 参考图引导语还在(图块本身没失败),但不得指一个 prompt 里不存在的「上文 bbox」
        guide = next(b["text"] for b in content
                     if b.get("type") == "text" and "全景场景参考" in b.get("text", ""))
        assert "上文 bbox" not in guide

    def test_remap_disabled_for_multi_packet(self):
        # crop 区域只属于「首个有帧设备」,套到设备 2..N 的名册上是跨设备错坐标。
        # 当前 fused 恒单 packet,故这是防御:多 packet 时整体弃用换算、退化为纯名。
        from unittest.mock import patch as _patch

        from miloco.perception.engine.omni.prompt_builder import _build_device_header

        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        called: list[tuple] = []

        def _remap(b):
            called.append(b)
            return (1, 2, 3, 4)

        single = _build_device_header([member], bbox_remap=_remap)
        assert "[bbox=(1, 2, 3, 4)]" in "".join(single)  # 单设备:换算照常生效
        assert called == [self._SELF_CONSISTENT_BBOX]

        called.clear()
        with _patch.object(_pb_mod.logger, "warning") as warn:
            multi = _build_device_header([member, member], bbox_remap=_remap)
        assert called == []  # 多设备:换算回调根本不该被调用
        joined = "".join(multi)
        # bbox 必须整个撤掉。特别是**不能**回落到全景原值 (156, 208, 281, 458) —— 走到这里
        # 说明 crop 已生效、主视频是局部画面,全景坐标配 crop 画面正是要根除的错配。
        assert "[bbox=(156, 208, 281, 458)]" not in joined
        assert "[bbox=(1, 2, 3, 4)]" not in joined
        assert "已识别人物：p-alice" in joined
        assert warn.call_args and "roster_bbox_remap_multi_packet" in warn.call_args[0][0]

    def test_bbox_note_anchors_video_last_frame_both_paths(self):
        # bbox 恒锚**视频最后一帧**:crop 时坐标已换算成 crop 坐标系,不 crop 时视频本就是
        # 全景 —— 两条路径同一句话。「最后一帧」必须写明:bbox 只标末帧位置而视频跨整个
        # 窗口,不说清模型会拿它去读中间帧(该模糊在接 Smart Crop 之前就存在)。
        from unittest.mock import patch as _patch

        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        for user_enabled, region_patch in (
            (True, _patch(
                "miloco.perception.engine.omni.crop_enhance.compute_crop_region",
                return_value=(50, 50, 350, 350),
            )),
            (False, contextlib.nullcontext()),
        ):
            p1, p2 = self._patches(user_enabled=user_enabled)
            with p1, p2, region_patch:
                content = self._content(packet=member, candidates=[])
            assert self._has_ref(content) is user_enabled  # crop 与否符合预期
            roster = self._roster_text(content)
            if user_enabled:
                assert "[bbox=(166, 166, 433, 566)]" in roster  # 换算进 crop 坐标系
            else:
                # 零回归:不裁切时坐标必须原样保留。crop 关闭是双闸灰度下绝大多数用户
                # 所在的路径,措辞断言对"坐标有没有被动过"零敏感,必须单独钉住。
                assert "[bbox=(156, 208, 281, 458)]" in roster
            note = self._bbox_note(content)
            assert "最后一帧" in note
            assert "视频里的人" in note
            assert "全景参考图" not in note  # 参考图不再充当 bbox 锚点

    def test_bbox_note_emitted_for_candidate_only_bbox(self):
        # 名册一个 bbox 都没有、但「待识别 track」行带 bbox 时,坐标系说明句仍必须发 ——
        # 否则模型拿到一串坐标却没有 [0,1000] / 末帧的口径说明。这条钉住 bbox_note_emitted
        # 里的 `or bool(candidates)` 项:名册扫的是 "[bbox="(带方括号),而 track 行由
        # _format_track_line 渲染成 "bbox="(无方括号)扫不到,少了该项就会漏发。
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        # 有候选 → 身份门本就关掉 crop,故不需要 _patches。
        content = self._content(
            candidates=[IdentityQueryItem(track_id=1, bbox_xyxy_norm=(100, 200, 300, 400))]
        )
        roster = self._roster_text(content)
        assert "已识别人物：无" in roster
        assert "[bbox=" not in roster  # 名册侧确实一个 bbox 都没有
        assert "bbox=(100, 200, 300, 400)" in roster  # track 行带 bbox
        assert "最后一帧" in self._bbox_note(content)  # 说明句照发

    def test_ref_block_failure_drops_guidance_but_keeps_bbox(self):
        # 契约防御:_jpeg_block 抛错时,引导语与图块**同进同退**,不留悬空指代。
        # 但 bbox 说明**不受影响** —— 坐标已换算进 crop 坐标系、锚的是视频而非参考图,
        # 参考图在不在都不改变它的正确性。这正是换算方案相对"锚全景参考图"的净简化。
        # (调用方 _maybe_encode_adaptive 已用同一条件校验过 ref jpeg,故实际不可达。)
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, _patch(
            "miloco.perception.engine.omni.crop_enhance.compute_crop_region",
            return_value=(50, 50, 350, 350),
        ), _patch.object(pb, "_jpeg_block", side_effect=ValueError("too small")):
            content = self._content(packet=member, candidates=[])
        assert not self._has_ref(content)  # 引导语不能单独留下
        assert not any(
            b.get("type") == "image_url" and "image/jpeg" in b["image_url"]["url"]
            for b in content
        )
        # bbox 仍是换算后的 crop 坐标,说明句照发(锚视频,与参考图无关)
        assert "[bbox=(166, 166, 433, 566)]" in self._roster_text(content)
        assert "最后一帧" in self._bbox_note(content)
        assert any(b.get("type") == "video_url" for b in content)  # 视频仍在,不整窗失败

    def _ref_bytes(self, content) -> bytes:
        import base64

        return base64.b64decode(
            next(
                b["image_url"]["url"].split(",", 1)[1]
                for b in content
                if b.get("type") == "image_url" and "image/jpeg" in b["image_url"]["url"]
            )
        )

    def test_ref_frame_is_last_not_first(self):
        # 参考帧必须取末帧:它与 crop 视频的末帧同一时刻,「全景 → 放大」的对照才成立;
        # 取首帧会在窗内有人移动时让两张画面错开。(bbox 不再锚这张图,已换算进 crop 坐标系。)
        from miloco.perception.engine.identity.gallery_composite import (
            encode_jpeg_bytes,
        )
        from miloco.perception.engine.omni.prompt_builder import _resize_short_edge

        np.random.seed(7)
        frames = [np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames)  # 首末帧为不同随机帧 → 编码可区分
        p1, p2 = self._patches(short_edge=512)
        with p1, p2:
            content = self._content(packet=pkt, candidates=[])
        ref_bytes = self._ref_bytes(content)
        assert ref_bytes == encode_jpeg_bytes(_resize_short_edge(frames[-1], 512))
        assert ref_bytes != encode_jpeg_bytes(_resize_short_edge(frames[0], 512))

    def test_ref_frame_short_edge_follows_resolution(self):
        # 参考帧短边跟用户分辨率档,不是硬编码 512 —— 否则用户降档也省不下参考帧的字节。
        from miloco.perception.engine.identity.gallery_composite import (
            encode_jpeg_bytes,
        )
        from miloco.perception.engine.omni.prompt_builder import _resize_short_edge

        np.random.seed(11)
        frames = [np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames)
        p1, p2 = self._patches(short_edge=256)  # 480 短边 → 会被缩到 256(512 档下不缩)
        with p1, p2:
            content = self._content(packet=pkt, candidates=[])
        ref_bytes = self._ref_bytes(content)
        assert ref_bytes == encode_jpeg_bytes(_resize_short_edge(frames[-1], 256))
        assert ref_bytes != encode_jpeg_bytes(frames[-1])  # 旧行为(硬编码 512 → 不缩)已改

    def test_pushes_crop_meta_with_wh_order(self):
        """写侧 frame_size 必须是 (w, h)。

        numpy shape 是 (h, w),这里最容易写反;而读侧 `frame_size_wh` 直接被前端当
        svg viewBox 用 —— 传成 (h, w) 时框还在、只是位置和缩放整体错位,是最难靠肉眼
        发现的那类 bug。写侧此前完全没有断言,顺序写反全套测试仍全绿。
        """
        seen: dict = {}
        frames = [np.zeros((360, 640, 3), dtype=np.uint8) for _ in range(3)]
        pkt = _adaptive_packet(frames=frames)  # 360 高 × 640 宽,非正方形才能验出顺序
        p1, p2 = self._patches()
        with p1, p2, patch(
            "miloco.perception.snapshot_context.push_crop_meta",
            side_effect=lambda **kw: seen.update(kw),
        ):
            self._content(packet=pkt, candidates=[])
        assert seen, "crop 生效时必须 push 一次 crop 元数据"
        assert seen["frame_size"] == (640, 360)  # (w, h) 而非 shape 的 (360, 640)
        x1, y1, x2, y2 = seen["region"]
        assert 0 <= x1 < x2 <= 640
        assert 0 <= y1 < y2 <= 360
        assert seen["short_edge"] > 0

    def test_crop_short_edge_budget_scales_with_resolution(self):
        # crop 短边预算 = 分辨率档 × 360/512(用户档的 70%),保住「像素开销 ≈ 同档全景」不变量;
        # 512 档必须精确落回 360,与本特性接入前的评测口径字节一致。
        from miloco.perception.engine.config import CropEnhanceConfig
        from miloco.perception.engine.omni.prompt_builder import _crop_short_edge_budget

        cfg = CropEnhanceConfig()
        assert [_crop_short_edge_budget(cfg, se) for se in (360, 512, 768, 1080)] == [
            253, 360, 540, 759,
        ]
        # (crop_se / pano_se)² 恒 ≈ crop_max_area_ratio(0.49),与档位无关
        for se in (360, 512, 768, 1080):
            assert abs((_crop_short_edge_budget(cfg, se) / se) ** 2 - 0.494) < 0.01

    def test_crop_video_short_edge_capped_by_budget(self):
        # 集成:1080 档下 crop 编码短边 = min(crop 区域原短边, 759),不再被固定 360 压死。
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        # 1080p 帧 + 居中 500x500 框 → 扩展后区域 900x800(占 34.7%,过面积双限),
        # 短边 800 > 预算 759 → 预算成为实际生效的上限。
        np.random.seed(13)
        frames = [np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames, body_box=(710, 290, 500, 500))
        p1, p2 = self._patches(short_edge=1080)
        with p1, p2, _patch.object(pb, "_encode_video_mp4", wraps=pb._encode_video_mp4) as spy:
            content = self._content(packet=pkt, candidates=[])
        assert self._has_ref(content)  # 确认确实走了 crop 分支
        assert spy.call_args_list[-1].kwargs["short_edge"] == 759

    def test_crop_video_upscaled_to_budget_when_region_smaller(self):
        # 区域比预算小(512 档默认小框 → 区域短边约 148)→ **放大**到预算 360,不再只缩不放。
        # 离线对照:720p 源下这类窗口占 57%,原生裁切 +0.6pp、放大到预算 +7.8pp。
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        p1, p2 = self._patches(short_edge=512)
        with p1, p2, _patch.object(pb, "_encode_video_mp4", wraps=pb._encode_video_mp4) as spy:
            content = self._content(candidates=[])
        assert self._has_ref(content)
        assert spy.call_args_list[-1].kwargs["short_edge"] == 360

    def test_upscale_capped_by_original_frame_long_edge(self):
        # 放大上限:放大后长边 ≤ crop 前原图长边。造一个极扁区域(长宽比 > 帧长边/预算),
        # 使预算不再是生效上限 —— 此时短边应被回压到 frame_long × short/long。
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        # 640x360 帧(长边 640)+ 很扁的框 → 扩展后区域长宽比约 5:1。放大到预算 360 会让
        # 长边达 1800 ≫ 640,故上限生效。
        np.random.seed(29)
        frames = [np.random.randint(0, 256, (360, 640, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames, body_box=(120, 160, 400, 40))
        p1, p2 = self._patches(short_edge=512)
        with p1, p2, _patch.object(pb, "_encode_video_mp4", wraps=pb._encode_video_mp4) as spy:
            content = self._content(packet=pkt, candidates=[])
        assert self._has_ref(content)
        cropped = spy.call_args_list[-1].args[0]
        ch, cw = cropped[0].shape[:2]
        se = spy.call_args_list[-1].kwargs["short_edge"]
        long_after = max(ch, cw) * se / min(ch, cw)
        assert long_after <= max(frames[0].shape[:2]) + 1  # 取整误差留 1px
        assert se < 360  # 确认是上限而非预算在起作用
        assert se >= min(ch, cw)  # 上限只限制放大,绝不反过来引入缩小

    def test_upscale_uses_lanczos_downscale_keeps_area(self):
        # 放大/缩小走不同重采样核:INTER_AREA 是区域平均,放大时退化成近似最近邻,故只用于缩小。
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        frames = [np.random.randint(0, 256, (100, 200, 3), dtype=np.uint8) for _ in range(3)]
        audio = np.zeros(0, dtype=np.int16)
        with _patch.object(pb.cv2, "resize", wraps=pb.cv2.resize) as spy:
            pb._encode_video_mp4(frames, audio, 16000, fps=1, short_edge=200)  # 放大 2x
            assert all(c.kwargs["interpolation"] == pb.cv2.INTER_LANCZOS4 for c in spy.call_args_list)
        with _patch.object(pb.cv2, "resize", wraps=pb.cv2.resize) as spy:
            pb._encode_video_mp4(frames, audio, 16000, fps=1, short_edge=50)  # 缩小 0.5x
            assert all(c.kwargs["interpolation"] == pb.cv2.INTER_AREA for c in spy.call_args_list)

    def test_crop_video_uses_frame_info_fps(self):
        # crop 视频帧率必须跟随 frame_info.fps(下采样后真实间隔),不是硬编码——否则调 omni_fps 就慢放。
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        pkt = _adaptive_packet(fps=2)
        p1, p2 = self._patches()
        with p1, p2, _patch.object(pb, "_encode_video_mp4", wraps=pb._encode_video_mp4) as spy:
            self._content(packet=pkt, candidates=[])
        # reorder 后裁切命中则只编 crop 一次(全景不再预编);该次必须用 frame_info.fps=2
        assert spy.call_count >= 1
        assert all(c.kwargs.get("fps") == 2 for c in spy.call_args_list)

    def test_non_fused_never_crops(self):
        # 非 fused/legacy 路径不接 crop:即便 adaptive 全开,也恒走全景、crops 为空(零回归)。
        from miloco.perception.engine.omni.prompt_builder import build_batch_prompt

        p1, p2 = self._patches()
        with p1, p2:
            payload = build_batch_prompt([_adaptive_packet()], OmniContext())
        assert payload["crops"] == []
        assert payload.get("video_base64")
