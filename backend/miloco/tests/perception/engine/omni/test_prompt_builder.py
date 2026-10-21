"""Tests for Omni Layer — Prompt Builder."""

import contextlib
from unittest.mock import patch

import cv2
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


class TestQueryPersonGrounding:
    """查询的人名、定位与历史线索须服从实际提供的画面。"""

    @staticmethod
    def _packet(pid="member", bbox=(100, 100, 400, 900), *, verify=False, suppress=False, color=60):
        ep = _mock_edge_packet()
        ep.targets = [IdentityTarget(
            type=ObjectType.HUMAN_WITH_FACE, person_id=pid, track_id=7,
            needs_omni_verify=verify, suppress_as_prior=suppress,
            box_info=[], bbox_xyxy_norm=bbox,
        )]
        ep.all_frames = [np.full((100, 100, 3), color, dtype=np.uint8)]
        return ep

    @staticmethod
    def _assert_policy(payload):
        sp = payload["system_prompt"]
        assert "直接输出自然语言答案" in sp
        policy = sp.split("# 人物在场与称呼\n", 1)[1].split("# 通用常识", 1)[0]
        assert "名册姓名 / bbox 是定位线索，不代表当前有人" in policy
        assert "椅子、家具、衣物及人体误检框均不算人" in policy
        assert "只据本轮提供的视频判断" in policy
        assert "家庭档案和问题里的人名都不证明本轮有人" in policy
        assert "只有画面确认给出的 bbox 对应真人、且该条为已确认成员线索时，才可沿用对应姓名" in policy
        assert "视频最后一帧的位置，坐标归一化到 [0, 1000]" in policy
        assert "确认有人但无法对应姓名时用“某人 / 陌生人”" in policy
        assert "不把 ID 当人名输出" in policy
        assert "不从家庭档案、历史描述或问题里给未确认身份的目标补姓名" in policy
        assert "背影、侧身、局部、遮挡或模糊的真人仍算有人" in policy
        assert "分不清是人还是物时说明“无法确认”，不得据此断言无人" in policy
        assert "本轮未提供有效视频的设备不能用于确认当前人员" in policy
        assert "不把其他机位的检测结果当作可见事实" in policy
        assert "identities" not in sp
        assert "caption" not in sp
        assert "matched_rules" not in sp
        assert payload["crops"] == []

    def _build(self, packets, last_caption=None):
        with (
            patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""),
            patch("miloco.perception.engine.omni.prompt_builder._effective_panorama_short_edge", return_value=128),
        ):
            payload = build_query_prompt(
                packets, "当前画面里是谁？", last_caption,
                {"member": "当前成员", "other": "其他机位成员"},
            )
        self._assert_policy(payload)
        return payload

    @pytest.mark.parametrize("pid,bbox,verify,suppress,authorized", [
        ("member", (100, 100, 400, 900), False, False, True),
        ("member", None, False, False, False),
        ("member", (100, 100, 400, 900), True, False, False),
        ("member", (100, 100, 400, 900), False, True, False),
        ("pending", (100, 100, 400, 900), False, False, False),
        ("pending:member", (100, 100, 400, 900), False, False, False),
        ("none", (100, 100, 400, 900), False, False, False),
        ("", (100, 100, 400, 900), False, False, False),
        ("unknown", (100, 100, 400, 900), False, False, False),
        ("unknown_1", (100, 100, 400, 900), False, False, False),
        ("unknown-scope-1", (100, 100, 400, 900), False, False, False),
    ])
    def test_name_requires_current_position_and_stable_identity(self, pid, bbox, verify, suppress, authorized):
        ep = self._packet(pid, bbox, verify=verify, suppress=suppress)
        payload = self._build([ep])
        text = payload["user_content"]
        assert payload["video_base64"]
        assert "检测定位线索（不代表当前有人）" in text
        assert ("当前成员" in text) is authorized
        assert ("已确认成员线索：当前成员[bbox=(100, 100, 400, 900)]" in text) is authorized
        assert ("不提供可沿用的身份标签" in text) is (not authorized)
        assert "pending" not in text
        assert "unknown" not in text
        assert ep.targets[0].person_id == pid

    @staticmethod
    def _video_color(payload):
        import base64
        import io

        import av

        with av.open(io.BytesIO(base64.b64decode(payload["video_base64"]))) as video:
            frames = list(video.decode(video=0))
        assert frames
        return float(np.mean(frames[-1].to_ndarray(format="bgr24")))

    @pytest.mark.parametrize("first_has_frames", [True, False])
    def test_only_encoded_camera_can_supply_names_and_history(self, first_has_frames):
        first = self._packet(color=40)
        second = self._packet("other", color=210)
        if not first_has_frames:
            first.all_frames = []
        payload = self._build([first, second], "旧场景中的历史姓名")
        text = payload["user_content"]
        assert ("当前成员" in text) is first_has_frames
        assert ("其他机位成员" in text) is (not first_has_frames)
        assert text.count("已确认成员线索：") == 1
        assert "本轮未提供该设备视频，不据其检测结果判断当前人物" in text
        assert ("上一窗场景参考（仅历史，不证明本轮事实）：旧场景中的历史姓名" in text) is first_has_frames
        assert ("旧场景中的历史姓名" in text) is first_has_frames
        assert abs(self._video_color(payload) - (40 if first_has_frames else 210)) < 8

    def test_source_follows_successful_encoding_not_frame_presence(self):
        import miloco.perception.engine.omni.prompt_builder as builder

        first = self._packet(color=40)
        second = self._packet("other", color=210)
        real_encode = builder._encode_video

        def reject_first(packet, short_edge):
            return (None, None) if packet is first else real_encode(packet, short_edge=short_edge)

        with patch.object(builder, "_encode_video", side_effect=reject_first):
            payload = self._build([first, second], "首镜头的历史姓名")
        assert "当前成员" not in payload["user_content"]
        assert "其他机位成员" in payload["user_content"]
        assert "首镜头的历史姓名" not in payload["user_content"]
        assert abs(self._video_color(payload) - 210) < 8

    @pytest.mark.parametrize("has_packets", [False, True])
    def test_no_video_does_not_supply_names_or_history(self, has_packets):
        ep = self._packet()
        ep.all_frames = []
        payload = self._build([ep] if has_packets else [], "历史成员仍坐着")
        assert payload["video_base64"] is None
        text = payload["user_content"]
        assert "本轮未提供有效视频，无法确认当前人员或场景" in text
        assert "当前成员" not in text
        assert "历史成员仍坐着" not in text
        assert "已确认成员线索" not in text

    def test_no_detections_does_not_assert_empty_scene(self):
        ep = self._packet()
        ep.targets = []
        payload = self._build([ep])
        assert payload["video_base64"]
        assert "未检测到目标（不代表画面无人）" in payload["user_content"]

    def test_empty_video_result_does_not_authorize_identity_metadata(self):
        import miloco.perception.engine.omni.prompt_builder as builder

        with patch.object(builder, "_encode_video", return_value=("", None)):
            payload = self._build([self._packet()], "历史成员仍坐着")
        assert not payload["video_base64"]
        assert "本轮未提供有效视频" in payload["user_content"]
        assert "当前成员" not in payload["user_content"]
        assert "历史成员仍坐着" not in payload["user_content"]


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
        # 有 bbox 的成员 → 坐标系说明句 + 紧随其后的定位语义句
        ep_bbox = self._packet([self._target(1, "pid-x", (1, 2, 3, 4))])
        lines = _build_device_header([ep_bbox], label_lookup={"pid-x": "王五"})
        assert any("归一化到 [0, 1000]" in ln for ln in lines)
        # 名册是定位线索,不是"当前有人"的断言 —— 空椅人体误检被名册安上姓名即由此而来
        assert any("不得沿用姓名" in ln for ln in lines)


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


class TestNoMediaBlockWarning:
    """拼不出本窗媒体块时必须留痕——生产默认路径（fused + query）逐 route 覆盖。

    与 fused video route 那条（``test_empty_window_guard.py::TestFusedNoMediaBlockWarning``）
    共用 ``event=fused_no_media_block``，按一个 event 名 grep 就能盖住全部 route。
    这类分支不在任何主路径上，不实际执行到就等于没写。
    """

    def test_fused_audio_route_warns_when_audio_unencodable(self, caplog):
        """audio route 编不出音频 → 打 warning。

        ``_encode_audio_only_mp4`` 对不足一个 AAC 帧的采样返回 None，此时 audio route 的
        user_content 里一个媒体块都没有（这条路不渲染参考图），模型手里只剩时间和房间名。
        """
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _audio_only_packet()
        ep.audio_clip = np.zeros(512, dtype=np.int16)  # < 一个 AAC 帧 → 编不出

        with caplog.at_level("WARNING"):
            fused = build_fused_payload(
                packets=[ep],
                context=OmniContext(room_name="厨房"),
                candidates=[],
                gallery_snapshot={},
            )

        blocks = fused["messages"][-1]["content"]
        assert not [b for b in blocks if b.get("type") == "input_audio"]
        msg = next(
            r.getMessage() for r in caplog.records
            if "fused_no_media_block" in r.getMessage()
        )
        assert "route=audio" in msg
        assert "room=厨房" in msg

    def test_fused_audio_unencodable_marks_encode_failure_diagnostics(self):
        """fused audio route 编不出媒体块 → 图中不会出现 encode 未知 + request OK。"""
        from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload
        from miloco.perception.flow_context import flow_diagnostics_scope

        diagnostics = PerDeviceFlowDiagnostics(
            device_id="camera-1",
            room_name="Living Room",
            trace_id="trace-1",
            device_trace_id="device-trace-1",
            observed_at=1,
        )
        ep = _audio_only_packet()
        ep.audio_clip = np.zeros(512, dtype=np.int16)  # < 一个 AAC 帧 → 编不出

        with flow_diagnostics_scope(diagnostics):
            build_fused_payload(
                packets=[ep],
                context=OmniContext(room_name="厨房"),
                candidates=[],
                gallery_snapshot={},
            )

        assert diagnostics.media_encode_status.value == "error"
        assert diagnostics.omni_request_status.value == "skipped"

    def test_batch_audio_unencodable_marks_encode_failure_diagnostics(self):
        """batch audio route 编不出媒体块 → 同样标记 encode ERROR + request SKIPPED。"""
        from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
        from miloco.perception.engine.omni.prompt_builder import build_batch_prompt
        from miloco.perception.flow_context import flow_diagnostics_scope

        diagnostics = PerDeviceFlowDiagnostics(
            device_id="camera-1",
            room_name="Living Room",
            trace_id="trace-1",
            device_trace_id="device-trace-1",
            observed_at=1,
        )
        ep = _audio_only_packet()
        ep.audio_clip = np.zeros(512, dtype=np.int16)

        with flow_diagnostics_scope(diagnostics):
            build_batch_prompt(
                identity_packets=[ep],
                context=OmniContext(room_name="厨房"),
            )

        assert diagnostics.media_encode_status.value == "error"
        assert diagnostics.omni_request_status.value == "skipped"

    def test_query_route_warns_when_video_unencodable(self, caplog):
        """query 路径拼不出 video 块 → 打 warning。

        query 拼不出有效视频时须保留可观测 warning，人物线索的隔离由查询护栏另测。
        """
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt

        ep = _mock_edge_packet()
        ep.all_frames = []  # 没有可编码的视频帧

        with caplog.at_level("WARNING"):
            payload = build_query_prompt([ep], "厨房现在怎么样")

        assert payload["video_base64"] is None
        msg = next(
            r.getMessage() for r in caplog.records
            if "fused_no_media_block" in r.getMessage()
        )
        assert "route=query" in msg
        assert "room=study-room" in msg


class TestAudioSizeGate:
    """音频尺寸闸收口在 ``_encode_audio_m4a`` 出口（见 ``_MIN_AUDIO_B64_LEN``）。

    图片模式新增的三个音频附加点与 audio route 共用这一个编码函数, 闸只写在某个
    调用点上会漏掉其余调用点 —— 漏掉的表现是把"非空但损坏的极短 m4a"发给 omni,
    换回 400 Multimodal data is corrupted、整轮失败。
    """

    def test_too_short_audio_is_dropped_and_not_archived(self, monkeypatch, caplog):
        """产物尺寸不达标 → 既不入 payload 也不落盘。"""
        from miloco.observability.context import (
            DeviceContext,
            reset_device_context,
            set_device_context,
        )
        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload
        from miloco.perception.snapshot_context import (
            OmniEventArtifacts,
            event_artifacts_scope,
        )

        # 把阈值拉到不可能达到, 模拟"PyAV 产出非空但损坏的极短 m4a"。
        # 真实编码产物恒远大于 500（单个 AAC 帧实测 b64 已 1680+）, 正常路径不受影响。
        monkeypatch.setattr(pb, "_MIN_AUDIO_B64_LEN", 10**9)

        ep = _video_route_packet()
        # _mock_edge_packet 的 audio_clip 只有 100 采样, 不足一个 AAC 帧会走"编不出来"
        # 那条早退路径, 到不了尺寸闸 —— 这里给足采样, 让闸成为唯一拦截点。
        ep.audio_clip = np.zeros(16000, dtype=np.int16)

        artifacts = OmniEventArtifacts()
        token = set_device_context(
            DeviceContext(device_trace_id="t", device_id="cam_a", room_name="r")
        )
        try:
            with event_artifacts_scope(artifacts), caplog.at_level("WARNING"):
                fused = build_fused_payload(
                    packets=[ep],
                    context=OmniContext(room_name="厨房"),
                    candidates=[],
                    gallery_snapshot={},
                    visual_input_mode="image",
                )
        finally:
            reset_device_context(token)

        types = [b["type"] for b in _multimodal_user_content(fused["messages"])]
        assert "input_audio" not in types
        # 落盘产物 = 实际请求: 没发出去的音频不留回放数据
        assert artifacts.image_audio == {}
        assert any(
            "audio_m4a_too_short" in r.getMessage() for r in caplog.records
        ), "尺寸闸拦下音频时要留痕（带 size）, 否则线上只能看到 omni 400"

    def test_audio_route_too_short_falls_back_to_text_only(self, monkeypatch, caplog):
        """audio route 同样被这一个闸收口 —— 尺寸不达标退化成 text-only, 不是报错。"""
        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        monkeypatch.setattr(pb, "_MIN_AUDIO_B64_LEN", 10**9)

        with caplog.at_level("WARNING"):
            fused = build_fused_payload(
                packets=[_audio_only_packet()],  # audio_clip 16000 采样, 过得了早退
                context=OmniContext(room_name="厨房"),
                candidates=[],
                gallery_snapshot={},
            )

        blocks = _multimodal_user_content(fused["messages"])
        assert not [b for b in blocks if b.get("type") == "input_audio"]
        assert any("fused_no_media_block" in r.getMessage() for r in caplog.records)


class TestAudioRouteVersusVisualMode:
    """纯音频路由不随视觉模式开关变语义。

    ``_build_messages`` 对"image 模式 + 无帧且无音频"抛 ValueError; 而音频编不出来
    (采样不足一个 AAC 帧)在 audio route 上是**既有降级路径**——退化成纯文本问模型。
    模式开关是给视觉表达做 A/B 的, 不该把一个与视觉无关的窗口从降级变成整轮失败。
    """

    def test_audio_route_keeps_video_semantics_in_image_mode(self):
        from miloco.perception.engine.omni.omni_client import _build_messages
        from miloco.perception.engine.omni.prompt_builder import build_prompt
        from miloco.perception.engine.omni.provider import MiMoAdapter

        ep = _audio_only_packet()
        ep.audio_clip = np.zeros(512, dtype=np.int16)  # < 一个 AAC 帧 → 编不出音频

        payload = build_prompt(ep, OmniContext(), visual_input_mode="image")
        assert payload["visual_input_mode"] == "video"
        # 不抛 ValueError: 退化成纯文本, 与 video 模式下的同一窗口行为一致
        messages = _build_messages(payload, MiMoAdapter())
        assert [b["type"] for b in messages[1]["content"]] == ["text"]

    def test_audio_route_still_sends_audio_in_image_mode(self):
        """音频编码正常时 audio route 照常发 input_audio —— 覆盖 mode 不该误伤它。"""
        from miloco.perception.engine.omni.omni_client import _build_messages
        from miloco.perception.engine.omni.prompt_builder import build_prompt
        from miloco.perception.engine.omni.provider import MiMoAdapter

        ep = _audio_only_packet()  # audio_clip 16000 采样, 编得出
        payload = build_prompt(ep, OmniContext(), visual_input_mode="image")
        messages = _build_messages(payload, MiMoAdapter())
        types = [b["type"] for b in messages[1]["content"]]
        assert "input_audio" in types
        assert "image_url" not in types


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

    def test_fused_image_route_emits_image_blocks(self):
        """image route 必须能完成 fused 装配并输出图片块。"""
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        fused = build_fused_payload(
            packets=[_video_route_packet()],
            context=OmniContext(),
            candidates=[],
            gallery_snapshot={},
            visual_input_mode="image",
        )
        user_blocks = _multimodal_user_content(fused["messages"])
        types = [b["type"] for b in user_blocks]
        assert "image_url" in types
        assert "video_url" not in types


class TestImagePromptAdaptation:
    """图片模式的措辞改写(``_adapt_visual_prompt`` + 替换表)。"""

    # 逐字取自 prompt 正文的那两句: 注意"最后一帧"是 **markdown 加粗**的
    _BBOX_BOLD = (
        "上方已识别人物、陌生人及待识别 track 中的 bbox=(x1, y1, x2, y2) 均为"
        "视频**最后一帧**中归一化到 [0, 1000] 区间的位置"
    )

    def test_bolded_last_frame_anchor_is_adapted(self):
        """加粗的「视频**最后一帧**」要改成「**最后一张主画面图片**」。

        替换表按序 str.replace, 最后一条单字"视频"垫底会把漏网的都吃掉 —— 键少写两个
        星号就成了死条目, 该处会被垫底那条改出"按时间排列的画面图片**最后一帧**",
        图片序列里并没有"帧"。所以这里对着**带加粗的原文**断言, 而不是自己拼一句。
        """
        from miloco.perception.engine.omni.prompt_builder import _adapt_visual_prompt

        out = _adapt_visual_prompt(self._BBOX_BOLD, "image")
        assert "**最后一张主画面图片**" in out
        assert "视频" not in out
        assert "最后一帧" not in out

    def test_video_mode_text_is_untouched(self):
        """video 模式逐字节原样返回 —— 存量视频用户看到的 prompt 不能变。"""
        from miloco.perception.engine.omni.prompt_builder import _adapt_visual_prompt

        assert _adapt_visual_prompt(self._BBOX_BOLD, "video") == self._BBOX_BOLD

    # query 路径逐字节钉住: 拼接方式从 "\n".join(parts + tail)" 展开成"改写前半段 +
    # 原样后半段", 段落间空行的数量属于这条 prompt 的一部分, 不能漂。
    _QUERY_BODY = (
        "本轮视频来自此设备。\n检测定位线索（不代表当前有人）：\n"
        "目标（不提供可沿用的身份标签）\n场景状态：static\n音频：silence（能量: 0.000）"
        "\n\n上一窗场景参考（仅历史，不证明本轮事实）：视频中一名男子起身\n\n用户问题：视频里那个人是谁"
    )

    def test_query_video_mode_body_is_byte_identical(self):
        """video 模式 query 文本逐字节不变。"""
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt

        payload = build_query_prompt(
            [_mock_edge_packet()], "视频里那个人是谁", "视频中一名男子起身",
        )
        assert payload["user_content"] == self._QUERY_BODY

    def test_query_image_mode_keeps_user_words_and_caption_verbatim(self):
        """图片模式只改模板段, 用户原话与上一窗 caption 原样进 prompt。

        改写整串会把用户问的"视频里那个人是谁"改成"按时间排列的画面图片里那个人
        是谁"再送模型 —— 用户原文被静默改动, 而 fused 实时路径对用户话语
        (pending_speech)是原样放进独立 user 消息的, 两条路要对齐。

        注: 不用「head == video 版」间接比对 —— 模板段(如"本轮视频来自此设备")在图片
        模式本就该被改写, 那不是回归; 直接钉用户原话 / caption 的原文未被波及才是本意。
        """
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt

        payload = build_query_prompt(
            [_mock_edge_packet()], "视频里那个人是谁", "视频中一名男子起身",
            visual_input_mode="image",
        )
        content = payload["user_content"]
        # 用户原话原样: "视频里"未被改写成"按时间排列的画面图片中"等
        assert "用户问题：视频里那个人是谁" in content, "图片模式不该动用户问题"
        # 上一窗 caption 原样: 引文里的"视频中一名男子起身"未被改写
        assert "视频中一名男子起身" in content, "图片模式不该动上一窗 caption 引文"
        # 模板段确实走了图片改写(证明改写在生效、只是没碰上面两处)
        assert "本轮按时间排列的画面图片来自此设备" in content, "模板段应按图片模式改写"
        # 末尾仍追加主画面序列说明
        assert "## 主画面图片序列" in content, "图片模式仍要在末尾追加主画面序列说明"

    def test_query_image_mode_drops_caption_when_absent(self):
        """没有 last_caption 时段落拼接不变形(少一个空行就会改变 prompt 结构)。"""
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt

        payload = build_query_prompt([_mock_edge_packet()], "现在怎么样")
        assert payload["user_content"].endswith(
            "音频：silence（能量: 0.000）\n\n\n用户问题：现在怎么样"
        )

    def test_query_image_encode_failure_degrades_to_text_only(self, monkeypatch, caplog):
        """图片模式 query 下视觉编码失败 → 端到端降级 text-only, 全链路不抛错。

        与 video 分支对称: 单房间编码失败若抛错, 会冒泡出 build_query_prompt →
        _run_room_query(无 except)→ _reraise_first, 把整个多房间查询拖成 answer=""。
        修复要求该函数把编码失败就地吞成 text-only, 查询隔离到房间级。

        **必须走到 _build_messages**: 它对「image 模式 + 无 image_frames + 无 audio」主动
        raise ValueError, 若 build_query_prompt 只是不抛、却仍把 payload 标成 image, 异常
        会在 _build_messages 处再次抛出 —— 降级形同虚设。故本测试把 payload 继续喂给
        _build_messages, 断言整条链路不抛、且产出 text-only 消息。
        """
        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.omni_client import _build_messages
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt
        from miloco.perception.engine.omni.provider import MiMoAdapter

        def _boom(*a, **k):
            raise ValueError("encoded omni image frame 0 is too short")

        monkeypatch.setattr(pb, "_encode_batch_images", _boom)
        with caplog.at_level("WARNING"):
            payload = build_query_prompt(
                [_mock_edge_packet()], "现在怎么样", visual_input_mode="image",
            )
        # text-only 降级: 无图片块; payload 的视觉模式落回 video, 使下游 _build_messages
        # 走 video 分支的优雅降级而非 image 分支的 raise。
        assert "image_frames" not in payload
        assert payload["visual_input_mode"] == "video"
        assert "用户问题：现在怎么样" in payload["user_content"]
        # 纯文字降级下不该出现 image 措辞(否则 prompt 自相矛盾)
        assert "画面图片" not in payload["user_content"]
        assert any("query_image_encode_failed" in r.getMessage() for r in caplog.records)
        # 端到端: 喂给 _build_messages 不抛, 且只有文本块(无 image_url / video_url)
        messages = _build_messages(payload, MiMoAdapter())
        types = [b["type"] for b in messages[1]["content"]]
        assert types == ["text"], types

    def test_query_image_empty_frames_degrades_to_text_only(self, monkeypatch, caplog):
        """图片模式 query 下一帧都没有 → 端到端降级 text-only, 不抛错(对称 video 的空帧路径)。"""
        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.omni_client import _build_messages
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt
        from miloco.perception.engine.omni.provider import MiMoAdapter

        monkeypatch.setattr(pb, "_encode_batch_images", lambda *a, **k: ([], None, None))
        with caplog.at_level("WARNING"):
            payload = build_query_prompt(
                [_mock_edge_packet()], "现在怎么样", visual_input_mode="image",
            )
        assert "image_frames" not in payload
        assert payload["visual_input_mode"] == "video"
        assert "用户问题：现在怎么样" in payload["user_content"]
        assert "画面图片" not in payload["user_content"]
        assert any("empty_image" in r.getMessage() for r in caplog.records)
        messages = _build_messages(payload, MiMoAdapter())
        types = [b["type"] for b in messages[1]["content"]]
        assert types == ["text"], types

    def _non_fused_context(self) -> OmniContext:
        """非 fused 内联路径的上下文: 用户规则原文 + 上一窗半句话。"""
        return OmniContext(
            room_name="study-room",
            pending_speech=[{"speaker": "小明", "content": "帮我放个视频"}],
            rule_conditions=[
                RuleCondition(rule_id="r1", rule_name="[r1] 看视频超时", query="看视频超过一小时提醒我"),
            ],
        )

    @staticmethod
    def _packet_with_bbox() -> IdentityPacket:
        """名册带 bbox 的 packet —— 只有名册含位置时才会附坐标系说明供改写。"""
        ep = _mock_edge_packet()
        ep.targets = [
            IdentityTarget(
                type=ObjectType.HUMAN_WITH_FACE,
                person_id="pid-uuid",
                track_id=1,
                needs_omni_verify=False,
                box_info=[],
                bbox_xyxy_norm=(120, 200, 480, 900),
            ),
        ]
        return ep

    def test_non_fused_image_mode_keeps_user_text_verbatim(self):
        """非 fused 内联路径: 改写只落模板段, 用户规则原文与 last_speech 引文原样。

        fused 靠"用户话语放独立只读消息"隔离改写; 非 fused 把规则/pending_speech 内联
        进同一条 user 文本, 整串改写会把用户说的"帮我放个视频"改成"帮我放个按时间排列
        的画面图片"——转写被静默篡改, 并顺着 speeches 流进 agent 与设备控制派发, 且无
        日志痕迹。模板段(名册 bbox 说明、末尾锚点)仍必须改写, 否则图片模式下这两处还
        在说"视频**最后一帧**"。
        """
        payload = build_prompt(
            self._packet_with_bbox(), self._non_fused_context(), visual_input_mode="image",
        )
        content = payload["user_content"]
        assert "看视频超过一小时提醒我" in content, "用户规则原文不得被改写"
        assert "帮我放个视频" in content, "last_speech 引文不得被改写"
        assert "**最后一张主画面图片**" in content, "名册 bbox 说明属模板, 必须改写"
        assert "视频**最后一帧**" not in content
        assert "只描述/判断本轮按时间排列的画面图片与音频" in content, "末尾锚点必须改写"

    def test_non_fused_video_mode_leaves_templates_untouched(self):
        """video 模式逐字节不变: 模板段仍是原措辞, 恒等改写不引入任何差异。"""
        payload = build_prompt(self._packet_with_bbox(), self._non_fused_context())
        content = payload["user_content"]
        assert "视频**最后一帧**" in content
        assert "只描述/判断本轮视频与音频的直接观察" in content
        assert "按时间排列的画面图片" not in content
        assert "看视频超过一小时提醒我" in content

    def test_system_prompt_user_written_sections_are_never_adapted(self):
        """机位说明与家庭档案是用户手写原文, 图片模式下也不得被机械替换。"""
        ep = _mock_edge_packet()
        camera_prompt = "忽略视频左上角的时间戳水印"
        with patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value="# 家庭档案\n客厅的电视平时放动画片, 别让孩子连看视频太久",
        ):
            payload = build_prompt(
                ep, OmniContext(camera_prompt=camera_prompt), visual_input_mode="image",
            )
        system = payload["system_prompt"]
        assert camera_prompt in system, "机位说明正文是用户手写, 不得改写"
        assert "别让孩子连看视频太久" in system, "家庭档案是用户手写, 不得改写"
        assert "本摄像头须知" in system
        # 模板段仍要改写(图片模式下 system 里不能再出现"视频"口径的角色/原则)
        assert "综合一个或多个设备的画面图片和音频" in system
        assert "综合一个或多个设备的视频和音频" not in system

    def test_query_system_prompt_home_profile_is_never_adapted(self):
        """query 的 system prompt 也内联家庭档案 —— 同款漏网, 用户手写段同样不得改写。

        与 ``build_system_prompt`` 的区别只是模板段少（_ROLE / _OUTPUT_MODE_FREE /
        _COMMONSENSE 三段）, 同样是"模板改写、用户手写原样"。
        """
        from miloco.perception.engine.omni.prompt_builder import build_query_prompt

        with patch(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            return_value="# 家庭档案\n客厅的电视平时放动画片, 别让孩子连看视频太久",
        ):
            payload = build_query_prompt(
                [_mock_edge_packet()], "现在怎么样", visual_input_mode="image",
            )
        system = payload["system_prompt"]
        assert "别让孩子连看视频太久" in system, "家庭档案不得被改写"
        assert "综合一个或多个设备的画面图片和音频" in system, "模板段仍要改写"

    def test_no_replacement_key_is_dead(self):
        """替换表的每个键都得在 prompt 正文里命中。

        死条目不报错, 只会静默漏改一处措辞, 灰度期从日志看不出来。语料取真正会被改写
        的那几个模块: system prompt 的模板常量在 constants.py / field_registry.py, user
        段在 prompt_builder.py(``build_system_prompt`` 与 ``_build_fused_user_content``
        两处都会过改写函数)。

        按 AST 取字面量, 再排掉注释与 docstring —— 注释本就不在 AST 里; docstring 在,
        但它是"描述 prompt 的散文", 在里面命中等于没命中(替换表最初那条 ``视频末帧``
        就是这么看着像活的)。替换表自身也排除, 免得自证。
        """
        import ast
        import pathlib

        import miloco.perception.engine.omni.prompt_builder as pb

        omni_dir = pathlib.Path(pb.__file__).parent
        keys = [k for k, _ in pb._IMAGE_PROMPT_REPLACEMENTS]
        table_strings = set(keys) | {v for _, v in pb._IMAGE_PROMPT_REPLACEMENTS}

        def _docstring_nodes(tree: ast.AST) -> set[int]:
            ids: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                body = getattr(node, "body", [])
                first = body[0] if body else None
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    ids.add(id(first.value))
            return ids

        literals: list[str] = []
        for name in ("constants.py", "field_registry.py", "prompt_builder.py"):
            tree = ast.parse((omni_dir / name).read_text())
            skip = _docstring_nodes(tree)
            literals.extend(
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and node.value not in table_strings
            )

        dead = sorted(k for k in keys if not any(k in lit for lit in literals))
        assert dead == [], f"替换表里这些键在 prompt 正文里不存在: {dead}"


class TestImageSequenceNote:
    """图片序列说明里的时间口径: 给"窗口时长 + 采样率", 不给"相邻间隔"。"""

    def _prepared(self, n: int, *, fps: int, duration_s: float):
        from miloco.perception.engine.omni.prompt_builder import PreparedVisualFrames

        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        return PreparedVisualFrames(
            frames=tuple(frame for _ in range(n)),
            width=8, height=8, fps=fps, duration_s=duration_s,
        )

    def test_note_states_duration_and_sampling_rate(self):
        """正常窗口: 4 帧 / 4 秒 → 时长 4 秒、采样率 1 fps。"""
        from miloco.perception.engine.omni.prompt_builder import _image_sequence_note

        note = _image_sequence_note(self._prepared(4, fps=1, duration_s=4.0))
        assert "4 张主画面" in note
        assert "窗口时长约 4 秒" in note
        assert "采样率约 1 fps" in note
        assert "间隔" not in note, "相邻间隔是反推出来的, 不再写进 prompt"

    def test_sampling_rate_comes_from_actual_frames_not_config_fps(self):
        """采样率按"实际帧数 / 窗口时长"算, 不按配置帧率。

        构造的是线上真实出现过的一类窗口: 相机按 ~18fps 投递、配置写 fps=3, 下采那一步
        用 config 的 3 当除数, 一个 4 秒窗口于是抽出 27 帧、``frame_info.fps`` 被写成
        round(3/3)=1。此时配置帧率说 1fps(即"相邻 1 秒"), 而送到模型的 27 帧其实挤在
        0.15 秒的间隔上 —— 按帧数算出来的 6.75fps 才是真的。
        """
        from miloco.perception.engine.omni.prompt_builder import _image_sequence_note

        note = _image_sequence_note(self._prepared(27, fps=1, duration_s=4.0))
        assert "窗口时长约 4 秒" in note
        assert "采样率约 6.75 fps" in note
        assert "采样率约 1 fps" not in note

    def test_single_frame_and_unknown_duration(self):
        """单帧只说明"仅一张图片"; 时长缺失时退回配置帧率。"""
        from miloco.perception.engine.omni.prompt_builder import _image_sequence_note

        assert "仅一张图片" in _image_sequence_note(
            self._prepared(1, fps=1, duration_s=4.0)
        )
        note = _image_sequence_note(self._prepared(4, fps=2, duration_s=0.0))
        assert "采样率约 2 fps" in note
        assert "窗口时长" not in note

    def test_fused_image_route_wires_window_duration(self):
        """集成: 时长要真的从 packet.frame_info 走到 prompt 里。

        单测直接构造 PreparedVisualFrames, 抓不住"调用点忘了传 duration_s"——
        那样 note 会静默退回配置帧率, 时间口径又变成反推值。
        """
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _video_route_packet()
        ep.all_frames = [np.zeros((100, 100, 3), dtype=np.uint8) for _ in range(4)]
        ep.frame_info = FrameInfo(start_timestamp=0, end_timestamp=4000, fps=1)
        fused = build_fused_payload(
            packets=[ep], context=OmniContext(), candidates=[],
            gallery_snapshot={}, visual_input_mode="image",
        )
        texts = [
            b["text"] for b in _multimodal_user_content(fused["messages"])
            if b["type"] == "text"
        ]
        note = next(t for t in texts if "主画面图片序列" in t)
        assert "4 张主画面" in note
        assert "窗口时长约 4 秒" in note
        assert "采样率约 1 fps" in note

    def test_window_duration_from_frame_info(self):
        """窗口时长由 packet.frame_info 的起止时刻(ms)换算; 倒挂/坏值一律当未知。"""
        from types import SimpleNamespace

        from miloco.perception.engine.omni.prompt_builder import _window_duration_s

        assert _window_duration_s(
            SimpleNamespace(start_timestamp=0, end_timestamp=4000)
        ) == 4.0
        assert _window_duration_s(
            SimpleNamespace(start_timestamp=5000, end_timestamp=4000)
        ) == 0.0
        assert _window_duration_s(SimpleNamespace()) == 0.0
        assert _window_duration_s(None) == 0.0


class TestFusedVideoBlockSkipped:
    """fused video route 拼出不可用 video 块(过短/为空)时必须留痕——与音频侧
    ``record_audio_encode_failure`` 同口径,图中不能出现 encode OK + request OK。"""

    def test_fused_video_b64_too_short_marks_block_skipped(self, monkeypatch, caplog):
        """编码产出过短 b64 → video 块被跳过、text-only。

        PyAV 编码异常可能返回非空但损坏的极短 base64 串(< _MIN_VIDEO_B64_LEN),
        入 payload 会让 omni 服务端 400,故走 skip。模型本窗没收到画面,排查
        「描述与实际画面不符」时图必须指向 encode ERROR 而非假全绿。
        """
        from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
        from miloco.perception.engine.omni import prompt_builder
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload
        from miloco.perception.flow_context import flow_diagnostics_scope

        diagnostics = PerDeviceFlowDiagnostics(
            device_id="camera-1",
            room_name="Living Room",
            trace_id="trace-1",
            device_trace_id="device-trace-1",
            observed_at=1,
        )
        monkeypatch.setattr(
            prompt_builder,
            "_encode_batch_video",
            lambda packets, short_edge=0: (b"c2hvcnQ=", None),  # 7 字符 < 1000
        )

        with flow_diagnostics_scope(diagnostics), caplog.at_level("WARNING"):
            fused = build_fused_payload(
                packets=[_video_route_packet()],
                context=OmniContext(room_name="厨房"),
                candidates=[],
                gallery_snapshot={},
            )

        assert diagnostics.media_encode_status.value == "error"
        assert diagnostics.omni_request_status.value == "skipped"
        types = [b["type"] for b in _multimodal_user_content(fused["messages"])]
        assert "video_url" not in types
        assert any(
            "fused_video_b64_too_short" in r.getMessage() for r in caplog.records
        )

    def test_fused_video_empty_marks_block_skipped(self, monkeypatch):
        """编码产出为空 → video 块整个消失,同样标记 encode ERROR + request SKIPPED。"""
        from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
        from miloco.perception.engine.omni import prompt_builder
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload
        from miloco.perception.flow_context import flow_diagnostics_scope

        diagnostics = PerDeviceFlowDiagnostics(
            device_id="camera-1",
            room_name="Living Room",
            trace_id="trace-1",
            device_trace_id="device-trace-1",
            observed_at=1,
        )
        monkeypatch.setattr(
            prompt_builder,
            "_encode_batch_video",
            lambda packets, short_edge=0: (None, None),
        )

        with flow_diagnostics_scope(diagnostics):
            fused = build_fused_payload(
                packets=[_video_route_packet()],
                context=OmniContext(room_name="厨房"),
                candidates=[],
                gallery_snapshot={},
            )

        assert diagnostics.media_encode_status.value == "error"
        assert diagnostics.omni_request_status.value == "skipped"
        types = [b["type"] for b in _multimodal_user_content(fused["messages"])]
        assert "video_url" not in types


class TestFusedPetRefs:
    """P2：has_pets 时 fused 主 user content 注入已登记宠物参考图块。"""

    # 真实可解码 JPEG（pet_refs 现会 imdecode 后 hstack 成 composite）
    _JPEG = cv2.imencode(
        ".jpg", np.random.default_rng(1).integers(0, 255, (48, 48, 3), dtype=np.uint8)
    )[1].tobytes()

    def test_pet_refs_injected_when_has_pets(self, tmp_path, monkeypatch):
        from miloco.perception.engine.identity.pet_library import PetLibrary
        from miloco.perception.engine.omni import pet_refs
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        lib = PetLibrary(root_dir=tmp_path)
        p = lib.create(name="小黑", species="猫")
        lib.set_reference_crops(p.id, [self._JPEG], scores=[1.0])
        monkeypatch.setattr(pet_refs, "_cache", {"sig": None, "content": []})
        monkeypatch.setattr(
            "miloco.perception.engine.identity.pet_library.get_pet_library", lambda: lib
        )
        monkeypatch.setattr(
            "miloco.perception.engine.omni.prompt_builder._has_pets_for_scene",
            lambda: True,
        )
        monkeypatch.setattr(
            "miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix",
            lambda: "",
        )
        fused = build_fused_payload(
            packets=[_video_route_packet()],
            context=OmniContext(),
            candidates=[],
            gallery_snapshot={},
        )
        blocks = _multimodal_user_content(fused["messages"])
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        assert any("<pets>" in t for t in texts)
        assert any("【小黑】" in t for t in texts)
        assert any(b["type"] == "image_url" for b in blocks)  # 宠物参考图注入

    def test_pet_refs_absent_when_no_pets_flag(self, monkeypatch):
        from miloco.perception.engine.omni import pet_refs
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        monkeypatch.setattr(pet_refs, "_cache", {"sig": None, "content": []})
        monkeypatch.setattr(
            "miloco.perception.engine.omni.prompt_builder._has_pets_for_scene",
            lambda: False,
        )
        fused = build_fused_payload(
            packets=[_video_route_packet()],
            context=OmniContext(),
            candidates=[],
            gallery_snapshot={},
        )
        texts = [
            b["text"]
            for b in _multimodal_user_content(fused["messages"])
            if b["type"] == "text"
        ]
        assert not any("<pets>" in t for t in texts)

    def test_pet_gate_requires_feature_on(self, monkeypatch):
        # 门控组合：档案有宠物段但 pet_recognition 关 → _has_pets_for_scene False → 不注入
        from types import SimpleNamespace

        monkeypatch.setattr(
            "miloco.perception.engine.omni.prompt_builder.home_profile_has_pets",
            lambda: True,
        )
        monkeypatch.setattr(
            "miloco.perception.engine.omni.prompt_builder.get_settings",
            lambda: SimpleNamespace(features=SimpleNamespace(pet_recognition=False)),
        )
        from miloco.perception.engine.omni.prompt_builder import _has_pets_for_scene

        assert _has_pets_for_scene() is False  # 有宠物但功能关 → 不注入


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


class TestAudioObservationDestinations:
    """音频观察的落点与本轮可用字段一致，人物在场仍由画面确认。"""

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize(
        ("has_audio", "has_speech"),
        [(True, True), (True, False), (False, True), (False, False)],
    )
    def test_video_audio_observations_follow_available_fields(self, has_audio, has_speech, stream):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(
            SceneDescriptor(route="video", has_audio=has_audio, has_speech=has_speech, stream=stream),
            include_home_profile=False,
        )
        speech_destination = "听到的话 / 声音只如实写进 speeches / env_sounds"
        environment_destination = "听到的声音只如实写进 env_sounds"
        assert (speech_destination in sp) is (has_audio and has_speech)
        assert (environment_destination in sp) is (has_audio and not has_speech)
        assert "不代表当前有人" in sp


class TestPersonNamingPolicy:
    """真实装配入口须保留名册命名条件、候选独立性与人物否证出口。"""

    @staticmethod
    def _assert_visual_policy(sp, has_candidates):
        assert "只有画面确认名册 bbox 对应真人" in sp
        assert "且该名册项不在本轮待识别 track 中，才可沿用其姓名" in sp
        assert "无法对应或未确认有人，不得沿用名册姓名" in sp
        assert "本轮有待识别 track 时，该目标的姓名只用本轮 identities 结果" in sp
        assert "unknown / 没给出 → 不得安成员名，画面确认有人时按对应字段要求使用泛称" in sp
        assert "no_person → 不据该框描述人物或动作" in sp
        assert "不得借名册覆盖该结果" in sp
        assert "规则点名的人未确认在场或姓名无依据则不成立" in sp
        assert "matched_rules 与 suggestions 的结论都不得与 caption 相矛盾" in sp
        assert "caption 写锅静置就不能判“在加热”" in sp
        assert "caption 认定是小明就不能当成小红" in sp
        assert ('"identities"' in sp) is has_candidates
        assert ("## identities\n" in sp) is has_candidates
        assert ("名册项不重判、不补进 identities" in sp) is has_candidates
        assert ("在其余输出字段中禁用该姓名及其人物结论" in sp) is has_candidates

        caption = sp.split("## caption\n", 1)[1].split("\n## ", 1)[0]
        rules = sp.split("## matched_rules\n", 1)[1].split("\n## ", 1)[0]
        suggestions = sp.split("## suggestions\n", 1)[1].split("\n## ", 1)[0]
        assert "画面确认后的非待识别名册姓名" in caption
        assert "该目标本轮 identities 判出的姓名" in caption
        assert "按总原则确认在场及姓名来源" in rules
        assert "人物称呼遵守总原则" in suggestions
        assert "按同一授权确认该成员姓名及在场" in suggestions
        assert "涉及人物只用本轮 identities" not in caption
        assert "指称画面里的人只用本轮 identities" not in suggestions
        assert "须 identities 已确认该成员在场" not in suggestions

    @pytest.mark.parametrize("has_audio,has_speech", [(True, True), (True, False), (False, False)])
    @pytest.mark.parametrize("identity_mode", ["none", "match", "no-match"])
    def test_fused_roster_and_candidate_naming_scopes(self, has_audio, has_speech, identity_mode):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _video_packet(audio_active=has_audio, speech_active=has_speech)
        ep.targets = [
            TestDeviceHeaderRoster._target(1, "stable", (100, 100, 400, 900)),
            TestDeviceHeaderRoster._target(2, "recheck", (500, 100, 900, 900)),
        ]
        has_candidates = identity_mode != "none"
        candidates = [IdentityQueryItem(track_id=2, bbox_xyxy_norm=(500, 100, 900, 900))] if has_candidates else []
        context = OmniContext(
            per_camera_crop_enabled=False,
            rule_conditions=[RuleCondition(rule_id="member_pc", rule_name="成员用电脑", query="稳定成员正在使用电脑")],
        )
        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""):
            fused = build_fused_payload(
                packets=[ep], context=context, candidates=candidates, gallery_snapshot={},
                label_lookup={"stable": "稳定成员", "recheck": "重核旧名"},
                matching_moot=identity_mode == "no-match",
            )
        self._assert_visual_policy(fused["messages"][0]["content"], has_candidates)
        main = _multimodal_user_content(fused["messages"])
        text = "\n".join(b.get("text", "") for b in main if b.get("type") == "text")
        assert "稳定成员[bbox=(100, 100, 400, 900)]" in text
        assert ("重核旧名" in text) is (not has_candidates)
        assert fused["candidate_track_ids"] == ([2] if has_candidates else [])

    @pytest.mark.parametrize("has_audio,has_speech", [(True, True), (True, False), (False, False)])
    def test_stream_roster_uses_same_naming_policy(self, has_audio, has_speech):
        ep = _video_packet(audio_active=has_audio, speech_active=has_speech)
        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""):
            payload = build_stream_prompt(ep, OmniContext())
        self._assert_visual_policy(payload["system_prompt"], has_candidates=False)

    def test_audio_only_does_not_authorize_visual_roster_names(self):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(SceneDescriptor(route="audio"), include_home_profile=False)
        assert "名册 bbox 对应真人" not in sp
        assert "名册项不在本轮待识别 track" not in sp
        assert '"caption"' not in sp


class TestSpeakerNamingPolicy:
    """speaker 姓名沿用同一授权，还须看到对应的人正在说话。"""

    @staticmethod
    def _video_prompt(entry):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        ep = _video_packet(audio_active=True, speech_active=True)
        ep.targets = [TestDeviceHeaderRoster._target(1, "stable", (100, 100, 400, 900))]
        context = OmniContext(per_camera_crop_enabled=False)
        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""):
            if entry == "stream":
                payload = build_stream_prompt(ep, context, label_lookup={"stable": "登记成员"})
                sp, text = payload["system_prompt"], payload["user_content"]
            else:
                candidates = [IdentityQueryItem(track_id=2, bbox_xyxy_norm=(500, 100, 900, 900))] if entry == "fused-candidate" else []
                payload = build_fused_payload(
                    packets=[ep], context=context, candidates=candidates, gallery_snapshot={},
                    label_lookup={"stable": "登记成员"},
                )
                sp = payload["messages"][0]["content"]
                text = "\n".join(b.get("text", "") for b in _multimodal_user_content(payload["messages"]) if b.get("type") == "text")
        assert "登记成员[bbox=(100, 100, 400, 900)]" in text
        return sp

    @pytest.mark.parametrize("entry", ["fused-roster", "fused-candidate", "stream"])
    def test_visible_video_speaker_uses_authorized_name(self, entry):
        sp = self._video_prompt(entry)
        speeches = sp.split("## speeches\n", 1)[1].split("\n## ", 1)[0]
        assert "且其姓名可按总原则确认" in speeches
        assert "本轮 identities 判出的姓名，或画面确认后的非待识别名册姓名" in speeches
        assert "画面里看到对应的人正在说话（嘴动/说话姿态）" in speeches
        assert '否则填"未知"' in speeches
        assert "不凭声音猜、不从 <gallery> 或家庭档案取名" in speeches
        assert "且该名册项不在本轮待识别 track 中，才可沿用其姓名" in sp
        assert "unknown / 没给出 → 不得安成员名" in sp
        assert "不得借名册覆盖该结果" in sp
        assert "unknown / 没给出 → 画面确认有人时写“某人 / 陌生人”" not in sp

    def test_identity_example_has_visual_speaker_evidence(self):
        sp = self._video_prompt("fused-candidate")
        assert "看到其嘴动说话，听到" in sp
        assert '"speaker":"小明"' in sp

    def test_audio_speaker_keeps_self_report_exception(self):
        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""):
            sp = build_prompt(_audio_only_packet(), OmniContext())["system_prompt"]
        speeches = sp.split("## speeches\n", 1)[1].split("\n## ", 1)[0]
        assert 'speaker 一律填"未知"（除非本人在话里自报姓名）' in speeches
        assert "不凭声音猜、不臆测姓名，也不从家庭档案取成员名" in speeches
        assert "非待识别名册姓名" not in speeches
        assert "画面里看到对应的人正在说话" not in speeches


class TestNoPersonCriterion:
    """完整与精简身份说明都须区分确认非人体、真人及无法辨别。"""

    @pytest.mark.parametrize("matching_moot", [False, True])
    @pytest.mark.parametrize("has_audio,has_speech", [(True, True), (True, False), (False, False)])
    def test_fused_criterion_preserves_human_recall(self, matching_moot, has_audio, has_speech):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with patch("miloco.perception.engine.omni.prompt_builder.get_home_profile_prefix", return_value=""):
            payload = build_fused_payload(
                packets=[_video_packet(audio_active=has_audio, speech_active=has_speech)],
                context=OmniContext(per_camera_crop_enabled=False),
                candidates=[IdentityQueryItem(track_id=7, bbox_xyxy_norm=(100, 100, 200, 400), face_visible=False)],
                gallery_snapshot={}, matching_moot=matching_moot,
            )
        sp = payload["messages"][0]["content"]
        assert payload["candidate_track_ids"] == [7]
        assert '"identities"' in sp
        identities = sp.split("## identities\n", 1)[1].split("\n## ", 1)[0]
        assert ("不做成员匹配" in identities) is matching_moot
        assert ("本人正向吻合" in identities) is (not matching_moot)
        criterion = identities.replace("name 填 ", "")
        assert '明确有人（哪怕背影/侧身/局部/遮挡/模糊）→ "unknown"' in criterion
        assert '能确认是非人体（包括椅子、椅背、头枕、扶手、衣物等，即使叫不出具体名称）→ "no_person"，纵使轮廓像人' in criterion
        assert '分不清是人还是物 → "unknown"（保护真人召回，别把真人误判成没人）' in criterion


class TestPrincipleHeading:
    """字段委托到总原则时，所有模态都须有包含正文的同名段。"""

    @pytest.mark.parametrize("stream", [False, True])
    @pytest.mark.parametrize("route,has_audio,has_speech", [
        ("audio", True, True), ("video", False, False),
        ("video", True, False), ("video", True, True),
    ])
    def test_all_variants_have_named_principle_section(self, route, has_audio, has_speech, stream):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(
            SceneDescriptor(route=route, has_audio=has_audio, has_speech=has_speech, stream=stream),
            include_home_profile=False,
        )
        heading = "\n\n# 总原则\n\n"
        assert sp.count(heading) == 1
        principle, task = sp.split(heading, 1)[1].split("\n\n# 任务\n", 1)
        assert principle.startswith("宁缺毋滥（贯穿所有字段的默认姿态）：")
        assert "拿不准 → 一律不输出该字段或该条" in principle
        assert "# 输出格式\n" in task
        assert "# 字段说明\n" in task


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
        # 带名版实例 B 自带名册授权依据，不依赖实例 A 的铺垫。
        assert "小明坐在电脑前" in out
        assert "非待识别名册项小明[bbox=" in out
        assert "画面确认该框对应真人" in out

    @pytest.mark.parametrize("has_speech", [False, True])
    def test_roster_example_authorization_without_identity_example(self, has_speech):
        from miloco.perception.engine.omni.field_registry import SceneDescriptor

        sp = build_system_prompt(SceneDescriptor(
            route="video", has_identity=False, has_audio=True, has_speech=has_speech,
            identity_match_disabled=False), include_home_profile=False)
        assert self._EXAMPLE_A_MARKER not in sp
        example = sp.split("## 实例 B", 1)[1]
        assert "非待识别名册项小明[bbox=" in example
        assert "画面确认该框对应真人" in example
        assert "小明坐在电脑前" in example

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


def test_encode_video_pins_libx264_thread_count(monkeypatch):
    """omni mp4 编码器必须钉死 ENCODE_THREADS(走真实 add_stream 路径,非桩)。

    删掉 prompt_builder 里那行 thread_count 赋值,本用例会失败;其余 _encode_video
    用例只验产物(有无音轨),抓不到线程数回退。
    """
    from miot.tuning import ENCODE_THREADS

    class _CapturingContainer:
        def __init__(self, real):
            self._real = real
            self._streams: list = []
            self.h264_thread_counts: list[int] = []

        def add_stream(self, codec, *a, **k):
            stream = self._real.add_stream(codec, *a, **k)
            self._streams.append((codec, stream))
            return stream

        def close(self):
            # 在底层释放前快照:close 后读 codec context 可能已失效。
            for codec, stream in self._streams:
                if codec == "h264":
                    self.h264_thread_counts.append(stream.thread_count)
            return self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    containers: list = []
    real_open = _pb_mod.av.open

    def _spy_open(*a, **k):
        cap = _CapturingContainer(real_open(*a, **k))
        containers.append(cap)
        return cap

    monkeypatch.setattr(_pb_mod.av, "open", _spy_open)

    b64, _info = _encode_video(_video_packet(audio_active=False))
    assert b64 is not None
    assert containers, "未捕获到 av.open"
    assert containers[0].h264_thread_counts == [ENCODE_THREADS]


def _adaptive_packet(
    *,
    person_id: str = "none",
    bbox_xyxy_norm: tuple[int, int, int, int] | None = None,
    frames: list[np.ndarray] | None = None,
    fps: int = 1,
    body_box: tuple[int, int, int, int] = (100, 100, 80, 120),
    main_det_boxes: list[tuple[int, int, int, int]] | None = None,
) -> IdentityPacket:
    """640x480 噪声帧(保证 crop 视频编码后过 size gate)+ 一个小 human_body 框(crop 面积达标)。

    默认 target 是 none(不进名册);传 confirmed pid + bbox_xyxy_norm 可让它以
    ``名[bbox=...]`` 进名册,用于验证 crop 生效时 bbox 坐标系说明的指向。
    frames / fps 可覆盖:验参考帧取末帧、crop 视频帧率跟随 frame_info.fps。
    body_box(像素 xywh)可覆盖:换大帧时默认框离画面角太近,_enforce_min_area 绕中心放大会
    被 clamp 截断而达不到 crop_min_area_ratio → 回退全景;此时需给一个居中的框。
    main_det_boxes 缺省由 body_box 换算(xywh→xyxy)同步给出 —— crop 区域读的是这个字段,
    不是 targets/box_info;显式传入可让两者分叉(验证 crop 只看前者)。
    """
    if frames is None:
        np.random.seed(1234)
        frames = [np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8) for _ in range(5)]
    if main_det_boxes is None:
        bx, by, bw, bh = body_box
        main_det_boxes = [(bx, by, bx + bw, by + bh)]
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
        main_det_boxes=main_det_boxes,
    )


class TestAdaptiveResolution:
    """Smart Crop(智能裁切增强)在 fused 路径的接线。

    激活看三闸相与:enabled=发版级开关 AND user_enabled=单机用户开关(两者都在 crop_enhance
    配置里) AND OmniContext.per_camera_crop_enabled=per-camera 开关(按机位存 KV、逐窗热读)。
    **与 video_short_edge 正交** —— 分辨率档只决定编码短边(全景 / crop / 参考帧),不决定裁不裁。
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

    def _content(self, packet=None, context=None, **kwargs):
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        fused = build_fused_payload(
            packets=[packet or _adaptive_packet()], context=context or OmniContext(),
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

    def _fixed_region(self, region, reason: str = "ok"):
        """钉住 crop 区域,便于硬编码 remap 期望值。

        patch 的是 ``compute_crop_region_detail``(区域 + 拒因),不是薄封装
        ``compute_crop_region`` —— 被测路径只调前者,patch 后者不会生效(测试静默失效)。
        """
        return patch(
            "miloco.perception.engine.omni.crop_enhance.compute_crop_region_detail",
            return_value=(region, reason),
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

    def test_ref_is_last_image_before_video_with_pet_refs(self):
        """宠物参考图同时注入时,全景参考帧必须仍是 video 前的**最后**一张图。

        引导语写的是「下方第一张图为全景场景参考,随后的视频是…」,中间再插图这话就不成立
        (模型会把某只宠物的参考图当成全景)。上面那条 img_idx < vid_idx 只看"有图在视频前",
        宠物图插在全景图之后也照样绿,所以这个顺序得单独钉。
        """
        from unittest.mock import patch as _patch

        pet_blocks = [
            {"type": "text", "text": "【豆豆】（cat）"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,UEVU"}},
        ]
        p1, p2 = self._patches()
        with p1, p2, _patch(
            "miloco.perception.engine.omni.prompt_builder._has_pets_for_scene",
            return_value=True,
        ), _patch(
            "miloco.perception.engine.omni.prompt_builder.build_pet_reference_content",
            return_value=pet_blocks,
        ):
            content = self._content(candidates=[])
        assert self._has_ref(content)
        vid_idx = next(i for i, b in enumerate(content) if b.get("type") == "video_url")
        # 筛**全部** image_url、不筛 mime：曾经这里只数 image/jpeg，于是走 PNG 的 gallery /
        # 单帧人像注入插到 ref 之后照样绿。格式无关才真钉得住。
        # ⚠️ 但只改 mime 还不够：本用例传的是 candidates=[]，注入分支根本进不来，所以它对
        # 人像注入始终是瞎的。真正覆盖那条路的是 TestPersonCropInject 里带候选的同款用例。
        img_idxs = [
            i for i, b in enumerate(content[:vid_idx]) if b.get("type") == "image_url"
        ]
        assert len(img_idxs) >= 2  # 宠物图 + 全景图都在
        guide_idx = next(
            i for i, b in enumerate(content)
            if b.get("type") == "text" and "全景场景参考" in b.get("text", "")
        )
        assert img_idxs[-1] == guide_idx + 1  # 引导语紧跟的那张,就是 video 前最后一张

    def test_crops_with_candidates_when_bbox_remaps(self):
        """有待识别候选时照样裁,候选行 bbox 换算进 crop 坐标系(与名册同一套换算)。

        v1 的身份门(有候选就整窗不裁)已去掉:候选侧真正的要求是**锚点不丢**,而不是不裁 ——
        换算成功时锚点仍在,同时吃到裁切的分辨率收益。此前恰恰是「新面孔待识别」这类窗口
        (每窗都派候选)被身份门全部挡掉,而它们最需要放大。
        """
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        cand = IdentityQueryItem(track_id=1, bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)):
            content = self._content(candidates=[cand])
        assert self._has_ref(content)  # 确实裁了
        roster = self._roster_text(content)
        assert "bbox=(166, 166, 433, 566)" in roster  # 换算后的 crop 坐标
        assert "bbox=(156, 208, 281, 458)" not in roster  # 全景原坐标不得残留

    def test_candidate_bbox_outside_region_falls_back_whole_window(self):
        """候选 bbox 换算不出来 → **整窗**回退全景(all-or-nothing)。

        与名册 bbox 的处置有意不同:名册只撤去坐标,人物称呼仍由总原则限制;
        候选 bbox 是模型把 track_id 对到画面里某个人的**唯一定位锚**,撤掉后多 track 场景
        只能凭猜分配姓名 —— 错认代价比分辨率收益高一个量级。故不允许"裁了但候选无锚"的
        中间态,宁可整窗不裁。
        """
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        cand = IdentityQueryItem(track_id=1, bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        # 区域与候选 bbox 的像素范围(x 100-180, y 100-220)无交集
        with p1, p2, self._fixed_region((400, 300, 640, 480)):
            content = self._content(candidates=[cand])
        assert not self._has_ref(content)  # 整窗回退全景,不是"裁了但撤 bbox"
        # 回退后视频即全景,坐标原样保留 —— 锚点没丢
        assert "bbox=(156, 208, 281, 458)" in self._roster_text(content)

    def test_one_unmappable_candidate_vetoes_the_window(self):
        # 部分候选换算失败也整窗回退:留一个无锚候选正是 all-or-nothing 要防的中间态
        # (模型看到 A 有坐标、B 没有,只能把 B 分配给"剩下那个人")。
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        ok = IdentityQueryItem(track_id=1, bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        # 像素 x 576-633 / y 432-475,落在 region 外
        bad = IdentityQueryItem(track_id=2, bbox_xyxy_norm=(900, 900, 990, 990))
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)):
            content = self._content(candidates=[ok, bad])
        assert not self._has_ref(content)

    def test_candidate_without_bbox_does_not_block_crop(self):
        # bbox 为 None 的候选在全景路径下本就没有锚点(coasting 已被上游剔除,剩下的是归一化
        # 失败等边缘情形),裁切不会让它更差 → 不参与 all-or-nothing 判定。
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)):
            content = self._content(candidates=[IdentityQueryItem(track_id=1)])
        assert self._has_ref(content)

    def test_veto_leaves_no_crop_artifacts(self):
        """否决必须发生在编码/落盘**之前**:盘上不能留下模型没看过的 crop 产物。

        region_ok 若挂在 _maybe_encode_adaptive 末尾(返回前),推理结果是对的(回退全景),
        但 ref.jpg / crop_meta 已经写出去了 —— badcase 复盘时会拿着一张 crop 参考图去解释
        一次全景推理,是纯误导。
        """
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        cand = IdentityQueryItem(track_id=1, bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((400, 300, 640, 480)), patch(
            "miloco.perception.snapshot_context.push_ref_frame"
        ) as push_ref, patch(
            "miloco.perception.snapshot_context.push_crop_meta"
        ) as push_meta:
            content = self._content(candidates=[cand])
        assert not self._has_ref(content)
        push_ref.assert_not_called()
        push_meta.assert_not_called()

    def test_multi_packet_with_candidates_falls_back(self):
        """多 packet + 有候选 → 回退全景。

        crop 区域只属于「首个有帧设备」,而候选来自哪个设备在 fused 这一层已无从区分:
        套上另一台设备的区域就是跨设备错坐标。名册侧同款情形是撤 bbox 退纯名
        (_build_device_header),候选侧按 all-or-nothing 整窗回退。当前 fused 恒单 packet,
        这是防御。
        """
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        cand = IdentityQueryItem(track_id=1, bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)):
            fused = build_fused_payload(
                packets=[_adaptive_packet(), _adaptive_packet()],
                context=OmniContext(), gallery_snapshot={}, candidates=[cand],
            )
        assert not self._has_ref(_multimodal_user_content(fused["messages"]))

    def test_off_when_user_switch_off(self):
        p1, p2 = self._patches(user_enabled=False)  # 发版级开关开、用户开关关
        with p1, p2:
            content = self._content(candidates=[])
        assert not self._has_ref(content)

    def test_off_when_release_gate_off(self):
        p1, p2 = self._patches(enabled=False)  # 用户开关开,但发版级开关关掉了整个能力
        with p1, p2:
            content = self._content(candidates=[])
        assert not self._has_ref(content)

    def test_off_when_per_camera_switch_off(self):
        """全局双闸都开,但该机位的 per-camera 闸关 → 回退全景(不裁)。"""
        p1, p2 = self._patches()  # 两个全局闸都开
        with p1, p2:
            content = self._content(
                context=OmniContext(per_camera_crop_enabled=False), candidates=[]
            )
        assert not self._has_ref(content)

    def test_on_when_per_camera_switch_on(self):
        """per-camera 闸显式为 True 时照旧裁切(与默认值同),证明第三道闸不是恒否。"""
        p1, p2 = self._patches()
        with p1, p2:
            content = self._content(
                context=OmniContext(per_camera_crop_enabled=True), candidates=[]
            )
        assert self._has_ref(content)

    def test_per_camera_switch_defaults_on(self):
        """OmniContext 不显式给该字段时默认开 —— 老调用点(未透传)零行为变化。"""
        assert OmniContext().per_camera_crop_enabled is True
        p1, p2 = self._patches()
        with p1, p2:
            content = self._content(candidates=[])
        assert self._has_ref(content)

    @pytest.mark.parametrize(
        ("enabled", "user_enabled", "per_camera"),
        [
            (False, True, True),
            (True, False, True),
            (True, True, False),
            (False, False, False),
        ],
    )
    def test_any_gate_off_falls_back(self, enabled, user_enabled, per_camera):
        """三闸是**相与**:任一为 false 就回退全景。"""
        p1, p2 = self._patches(enabled=enabled, user_enabled=user_enabled)
        with p1, p2:
            content = self._content(
                context=OmniContext(per_camera_crop_enabled=per_camera), candidates=[]
            )
        assert not self._has_ref(content)

    def test_per_camera_off_log_carries_device_id(self, caplog):
        """回退日志必须带 did：4 路交织在同一日志文件里，不带 did 的话行与行字节
        完全相同，这条日志自称的用途（确认哪台走到了这里）就无法完成。"""
        import logging

        from miloco.observability.context import (
            DeviceContext,
            reset_device_context,
            set_device_context,
        )

        p1, p2 = self._patches()
        token = set_device_context(DeviceContext(
            device_trace_id="t1", device_id="cam_x:ch1", room_name="客厅"
        ))
        try:
            with p1, p2, caplog.at_level(
                logging.DEBUG, logger="miloco.perception.engine.omni.prompt_builder"
            ):
                self._content(
                    context=OmniContext(per_camera_crop_enabled=False), candidates=[]
                )
        finally:
            reset_device_context(token)

        hit = [r for r in caplog.records if "reason=per_camera_off" in r.getMessage()]
        assert hit, "没打出 per_camera_off 回退日志"
        assert "device_id=cam_x:ch1" in hit[0].getMessage()

    def test_per_camera_off_log_falls_back_to_unknown_without_context(self, caplog):
        """DeviceContext 未设置时退 unknown，不抛异常（该行在推理主路径上）。"""
        import logging

        p1, p2 = self._patches()
        with p1, p2, caplog.at_level(
            logging.DEBUG, logger="miloco.perception.engine.omni.prompt_builder"
        ):
            self._content(
                context=OmniContext(per_camera_crop_enabled=False), candidates=[]
            )
        hit = [r for r in caplog.records if "reason=per_camera_off" in r.getMessage()]
        assert hit and "device_id=unknown" in hit[0].getMessage()

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
        # region=(50,50,350,350) → 300x300;帧 640x480。
        # x: 156*640/1000=99.84 →(-50)/300*1000 → 166;281*640/1000=179.84 → 433
        # y: 208*480/1000=99.84 →(-50)/300*1000 → 166;458*480/1000=219.84 → 566
        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)):
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
        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        # 与 bbox 像素范围(100-180, 100-220)无交集
        with p1, p2, self._fixed_region((400, 300, 640, 480)):
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
        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        for user_enabled, region_patch in (
            (True, self._fixed_region((50, 50, 350, 350))),
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
            assert "不替代本轮画面对" in note  # bbox 是定位线索,不是"有人"的证据
            assert "全景参考图" not in note  # 参考图不再充当 bbox 锚点

    def test_bbox_note_emitted_for_candidate_only_bbox(self):
        # 名册一个 bbox 都没有、但「待识别 track」行带 bbox 时,坐标系说明句仍必须发 ——
        # 否则模型拿到一串坐标却没有 [0,1000] / 末帧的口径说明。这条钉住 bbox_note_emitted
        # 里的 `or bool(candidates)` 项:名册扫的是 "[bbox="(带方括号),而 track 行由
        # _format_track_line 渲染成 "bbox="(无方括号)扫不到,少了该项就会漏发。
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        # 显式关掉 crop:本条验的是说明句发不发,坐标要留在全景口径。身份门去掉后有候选
        # 也可能裁切,不钉住开关的话这里的期望坐标会随 crop 区域换算而漂。
        p1, p2 = self._patches(user_enabled=False)
        with p1, p2:
            content = self._content(
                candidates=[IdentityQueryItem(track_id=1, bbox_xyxy_norm=(100, 200, 300, 400))]
            )
        roster = self._roster_text(content)
        assert "已识别人物：无" in roster
        assert "[bbox=" not in roster  # 名册侧确实一个 bbox 都没有
        assert "bbox=(100, 200, 300, 400)" in roster  # track 行带 bbox
        assert "最后一帧" in self._bbox_note(content)  # 说明句照发

    def test_roster_disclaimer_not_duplicated_on_fused_path(self):
        # 名册否证条件("名册是定位线索;bbox 若对应非人体…")与坐标系说明同门:fused 入口传
        # emit_bbox_note=False,该判据由组合说明句出,整条 user 消息里只该出现一次。门一松、
        # 或入口漏传该参数,同一条消息里就会出现两句近逐字相同的话 —— 白付 token,且同一
        # 判据存两份,改一处忘一处即漂移。
        # 断言走 build_fused_payload(真实入口):只调 _build_device_header 并显式传参的话,
        # 入口漏传那个参数照样绿,守的就不是要守的东西。
        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        # 显式关掉 crop:本条数的是措辞出现几次,坐标口径不参与。
        p1, p2 = self._patches(user_enabled=False)
        with p1, p2:
            content = self._content(packet=member, candidates=[])
        text = self._roster_text(content)
        assert "已识别人物：p-alice[bbox=" in text  # 前置:名册确实带了 bbox,条件句该发
        assert text.count("不得沿用姓名") == 1

    def test_ref_block_failure_drops_guidance_but_keeps_bbox(self):
        # 契约防御:_jpeg_block 抛错时,引导语与图块**同进同退**,不留悬空指代。
        # 但 bbox 说明**不受影响** —— 坐标已换算进 crop 坐标系、锚的是视频而非参考图,
        # 参考图在不在都不改变它的正确性。这正是换算方案相对"锚全景参考图"的净简化。
        # (调用方 _maybe_encode_adaptive 已用同一条件校验过 ref jpeg,故实际不可达。)
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        member = _adaptive_packet(person_id="p-alice", bbox_xyxy_norm=self._SELF_CONSISTENT_BBOX)
        p1, p2 = self._patches()
        with p1, p2, self._fixed_region((50, 50, 350, 350)), _patch.object(
            pb, "_jpeg_block", side_effect=ValueError("too small")
        ):
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
        """取全景参考帧的字节 —— 锚在引导语之后的第一张图,不是 content 里的第一张图。

        gallery / 宠物参考图同样是 image/jpeg 块且排在前面,按"第一张 image/jpeg"取会在这些
        特性同时开启时静默取到别人的图,断言仍绿(比较对象也是它自己)。
        """
        import base64

        start = next(
            i for i, b in enumerate(content)
            if b.get("type") == "text" and "全景场景参考" in b.get("text", "")
        )
        block = next(
            b for b in content[start + 1:]
            if b.get("type") == "image_url" and "image/jpeg" in b["image_url"]["url"]
        )
        return base64.b64decode(block["image_url"]["url"].split(",", 1)[1])

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

    def test_region_comes_from_main_det_boxes_not_box_info(self):
        """区域读 ``packet.main_det_boxes``,不读 ``targets[].box_info``。

        两者语义不同:box_info 是末帧快照(且宠物永远不在其中),main_det_boxes 是窗口内每一
        抽帧的 human/cat/dog 框 —— 区域必须包住后者。这条让两个字段指向画面两端,只有读对
        了才能得出对应的区域;读错了区域会落在另一侧,断言直接挂。
        """
        seen: dict = {}
        frames = [np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(3)]  # 静态帧 → 无运动块
        pkt = _adaptive_packet(
            frames=frames,
            body_box=(20, 20, 40, 40),            # box_info 在左上角
            main_det_boxes=[(400, 300, 480, 400)],  # 真正的依据在右下角
        )
        p1, p2 = self._patches()
        with p1, p2, patch(
            "miloco.perception.snapshot_context.push_crop_meta",
            side_effect=lambda **kw: seen.update(kw),
        ):
            self._content(packet=pkt, candidates=[])
        assert seen, "crop 应生效"
        x1, y1, x2, y2 = seen["region"]
        assert x1 >= 300 and y1 >= 200  # 落在 main_det_boxes 那侧;读 box_info 会得到左上角

    def test_no_det_boxes_falls_back_to_panorama(self):
        """``main_det_boxes`` 空 + 无运动 → 无裁切依据 → 回退全景。

        钉住"通路真的接上了":若 crop 侧偷偷回退去读 box_info(packet 里仍有一个 body box),
        这里会裁出区域、断言变红。
        """
        frames = [np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(3)]
        pkt = _adaptive_packet(frames=frames, main_det_boxes=[])
        p1, p2 = self._patches()
        with p1, p2:
            content = self._content(packet=pkt, candidates=[])
        assert not self._has_ref(content)

    def test_fallback_reason_distinguishes_the_two_area_paths(self, caplog):
        """面积回退有两条相反成因,日志必须分开 —— 处置完全不同。

        并集本身超上限 = 有东西把区域撑开(查误检/大幅走动);并集极小却仍被拒 = 目标贴边、
        绕中心放大被 clamp 截断后仍不足下限,是几何天花板、无需处置。合成一个 reason 时
        运维只能看到"面积不合格",没法分流,所以顺带把 union 框也打出来。
        """
        import logging

        frames = [np.zeros((480, 640, 3), dtype=np.uint8) for _ in range(3)]  # 静态 → 无运动块

        def _reason(main_det_boxes):
            caplog.clear()
            pkt = _adaptive_packet(frames=frames, main_det_boxes=main_det_boxes)
            p1, p2 = self._patches()
            with caplog.at_level(
                logging.INFO, logger="miloco.perception.engine.omni.prompt_builder"
            ), p1, p2:
                content = self._content(packet=pkt, candidates=[])
            assert not self._has_ref(content)  # 三种情形都必须回退全景
            line = next(m for m in caplog.messages if "event=adaptive_crop_fallback" in m)
            return line

        assert "reason=no_activity" in _reason([])
        # 几乎整帧的框 → 扩展后远超 crop_max_area_ratio
        big = _reason([(20, 20, 620, 460)])
        assert "reason=area_too_large" in big
        assert "union=(20, 20, 620, 460)" in big  # 并集框进日志,能看出是哪一侧被撑开
        # 紧贴画面角的极小框 → 绕中心放大被 clamp 截断,达不到 crop_min_area_ratio
        assert "reason=area_too_small" in _reason([(0, 0, 4, 4)])

    def test_encode_target_wh_matches_encoder_grid(self):
        # crop 的逐轴上限拿 _encode_target_wh 当「同档全景画面」的尺寸,所以它必须与
        # _encode_video_mp4 真正编出的网格逐像素一致(含 //2*2 取偶)。差 1px 就会让上限与画面
        # 错配,故这里用真编码回来的 media_info 对账,而不是把公式抄一遍自证。
        # 尺寸挑的是 int(w0*scale) 落在**奇数**上的组合(1280/768 档 → 1364.3、1270/512 档 →
        # 903.1):偶数组合下 1px 的口径漂移会被 //2*2 吸收掉,用例就抓不到两边算法分叉。
        import miloco.perception.engine.omni.prompt_builder as pb

        np.random.seed(7)
        for (h0, w0), se in (((720, 1280), 768), ((720, 1270), 512), ((576, 704), 512)):
            frames = [np.random.randint(0, 256, (h0, w0, 3), dtype=np.uint8) for _ in range(3)]
            _, mi = pb._encode_video_mp4(
                frames, np.empty(0, dtype=np.int16), 16000, fps=1, short_edge=se,
            )
            assert (mi.video_width, mi.video_height) == pb._encode_target_wh(w0, h0, se)

    def _crop_call(self, packet, *, short_edge: int):
        """跑一遍 fused 装配,取 crop 那次编码的几何:区域 wh / 目标短边 / 编出的网格 / 同档全景画面。

        断言一律对着「编出的网格」而不是 se/短边 反算的浮点值 —— 送模型的就是这张网格,
        //2*2 取偶也只体现在它上面。

        捕的是 ``_prepare_omni_visual_frames``(canonical 帧准备)而不是编码函数:图片/视频
        两路共用这一步, 视频的 mp4 编码只是它下游的一层, crop 已不再调 ``_encode_video_mp4``
        (那会把缩放跑第二遍)。网格直接读它的返回值, 不再拿公式反算一遍自证。
        """
        from types import SimpleNamespace
        from unittest.mock import patch as _patch

        import miloco.perception.engine.omni.prompt_builder as pb

        prepared_calls: list = []
        real_prepare = pb._prepare_omni_visual_frames

        def _capture(*args, **kwargs):
            result = real_prepare(*args, **kwargs)
            prepared_calls.append((args, kwargs, result))
            return result

        p1, p2 = self._patches(short_edge=short_edge)
        with p1, p2, _patch.object(
            pb, "_prepare_omni_visual_frames", side_effect=_capture
        ):
            content = self._content(packet=packet, candidates=[])
        assert self._has_ref(content)  # 确认确实走了 crop 分支
        # 裁切命中时全景不再预编, 全程只该有这一处准备帧 —— 多出来说明捕错了那一次
        assert len(prepared_calls) == 1, [c[1] for c in prepared_calls]
        args, kwargs, prepared = prepared_calls[-1]
        ch, cw = args[0][0].shape[:2]
        fh, fw = packet.all_frames[0].shape[:2]
        return SimpleNamespace(
            region=(cw, ch), se=kwargs["short_edge"],
            out=(prepared.width, prepared.height),
            pano=pb._encode_target_wh(fw, fh, short_edge),
        )

    def test_crop_video_fills_panorama_grid_no_budget(self):
        # 集成:crop 不再有独立短边预算(旧实现在 1080 档会被 759 压住),而是等比放到逐轴贴住
        # 同档全景画面。1080p 帧 + 居中 500x500 框 → 区域 900x800(占 34.7%,过面积双限):
        # 宽轴允许 800×1920//900 = 1706,高轴只允许 800×1080//800 = 1080 → 取 1080,编出
        # 1214x1080:高贴住画面、宽还剩富余。
        np.random.seed(13)
        frames = [np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames, body_box=(710, 290, 500, 500))
        r = self._crop_call(pkt, short_edge=1080)
        assert r.region == (900, 800)
        assert r.pano == (1920, 1080)
        assert r.se == 1080  # 旧实现:min(759, cap) = 759
        assert r.out == (1214, 1080)  # 高轴贴住画面

    def test_crop_video_upscaled_when_region_smaller_than_grid(self):
        # 区域比目标网格小(512 档默认小框 → 区域 152x204)→ **放大**,不再只缩不放。
        # 离线对照:720p 源下这类窗口占 57%,原生裁切 +0.6pp、放大后 +7.8pp。
        # 这里 152×512//204 = 381(高轴生效),编出 380x510 —— 高 510 已越过原生帧高 480,
        # 但仍在同档全景画面(682x512)之内,正是新口径:上限跟画面比、不跟原生帧比。
        r = self._crop_call(_adaptive_packet(), short_edge=512)
        assert r.region == (152, 204)
        assert r.pano == (682, 512)
        assert r.se == 381
        assert r.se > min(r.region)  # 确实放大了(区域原生短边 152)
        assert r.out == (380, 510)
        assert r.out[1] > 480  # 越过了原生帧高
        assert r.out[1] <= r.pano[1]  # 但没越过同档全景画面

    def test_crop_capped_by_panorama_grid_not_native_frame(self):
        # 上限锚「同档全景画面」而不是原生帧:档位高于源短边时全景本身就在放大(scale 未钳 1.0),
        # crop 同口径。640x360 帧 + 极扁区域 640x64、512 档:画面是 910x512,宽轴允许
        # 64×910//640 = 91、高轴允许 64×512//64 = 512 → 取 91,编出 910x90。
        # 宽 910 > 原生帧宽 640(旧实现会被回压到 64、等于不放大),但恰好贴住画面宽。
        np.random.seed(29)
        frames = [np.random.randint(0, 256, (360, 640, 3), dtype=np.uint8) for _ in range(5)]
        pkt = _adaptive_packet(frames=frames, body_box=(120, 160, 400, 40))
        r = self._crop_call(pkt, short_edge=512)
        assert r.region == (640, 64)
        assert r.pano == (910, 512)
        assert r.se == 91
        assert r.out == (910, 90)
        assert r.out[0] > 640  # 越过原生帧宽 —— 新口径下这是允许的
        assert r.out[0] == r.pano[0]  # 宽轴贴住画面
        assert r.out[1] <= r.pano[1]
        assert r.se >= min(r.region)  # 上限只限制放大,绝不反过来引入缩小

    def test_crop_cap_is_per_axis_not_single_axis(self):
        # 两轴必须各夹一次:只夹宽会让竖长区域的高冲出画面。1920x1080 帧 + 360x720 区域、
        # 1080 档(画面 1920x1080):
        #   只夹宽 → 宽拉到 1920、高跟着到 3840(画面高的 3.6 倍)
        #   逐轴   → min(360×1920//360, 360×1080//720) = 540 → 540x1080(高轴贴住画面)
        np.random.seed(31)
        frames = [np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8) for _ in range(5)]
        # 居中竖长框 → 扩展后区域 360x720(占 12.5%,过面积双限)
        pkt = _adaptive_packet(frames=frames, body_box=(900, 300, 200, 450))
        r = self._crop_call(pkt, short_edge=1080)
        assert r.region == (360, 720)
        assert r.se == 540  # 只夹宽会是 1920
        assert r.out == (540, 1080)  # 只夹宽会是 1920x3840

    def test_crop_never_coarser_or_costlier_than_panorama(self):
        # 三条不变量(与档位无关),用真实装配路径在四个档位 × 两种朝向上验。前两条精确成立:
        #   ① crop 编码网格**逐轴**不超过同档全景画面
        #   ② crop 编码像素 <= 同档全景画面像素(等号只在区域与帧等比时)
        # 第三条带取整余量:
        #   ③ 主体不比同档全景更糊 —— 区域在全景画面里占 (cw·pw/fw)x(ch·ph/fh) 像素,crop 编出
        #      的应不小于它。两处取整会啃掉一点:cse 那步整除亏 <1px、被长宽比放大到长轴上,
        #      _encode_target_wh 的 //2*2 再亏 <2px。极扁区域(输出轴只几十 px)相对亏损最大,
        #      四种源 × 四档 × 89 万种区域尺寸穷举下来最差 0.957,故门限取 0.95:收紧到 0.99
        #      会在极扁区域误红,放到 0.9 则挡不住真实回归。
        # 两种朝向都要跑:竖长区域高轴生效、扁长区域宽轴生效,只测一种会漏掉另一轴的钳位。
        np.random.seed(37)
        frames = [np.random.randint(0, 256, (720, 1280, 3), dtype=np.uint8) for _ in range(5)]
        bound = set()
        for box in ((500, 200, 260, 320), (300, 300, 700, 90)):  # 竖长 / 扁长
            pkt = _adaptive_packet(frames=frames, body_box=box)
            for tier in (360, 512, 768, 1080):
                r = self._crop_call(pkt, short_edge=tier)
                (cw, ch), (out_w, out_h), (pano_w, pano_h) = r.region, r.out, r.pano
                where = (box, tier)
                assert out_w <= pano_w and out_h <= pano_h, where  # ①
                assert out_w * out_h <= pano_w * pano_h, where  # ②
                ref_w, ref_h = cw * pano_w / 1280, ch * pano_h / 720  # 区域在全景画面里的像素
                assert min(out_w / ref_w, out_h / ref_h) >= 0.95, where  # ③
                bound.add("w" if 1280 / cw < 720 / ch else "h")
        # 确认两条钳位分支都被走到了,否则本用例等于只测了一种朝向
        assert bound == {"w", "h"}

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
        with p1, p2, _patch.object(
            pb, "_prepare_omni_visual_frames", wraps=pb._prepare_omni_visual_frames
        ) as spy:
            self._content(packet=pkt, candidates=[])
        # reorder 后裁切命中则只准备一次帧(全景不再预编);该次必须用 frame_info.fps=2
        assert spy.call_count >= 1
        assert all(c.kwargs.get("fps") == 2 for c in spy.call_args_list)

    def test_non_fused_never_crops(self):
        # 非 fused/legacy 路径不接 crop:即便 adaptive 全开,也恒走全景、crops 为空(零回归)。
        from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
        from miloco.perception.engine.omni.prompt_builder import build_batch_prompt
        from miloco.perception.flow_context import flow_diagnostics_scope

        diagnostics = PerDeviceFlowDiagnostics(
            device_id="camera-1",
            room_name="Living Room",
            trace_id="trace-1",
            device_trace_id="device-trace-1",
            observed_at=1,
        )
        p1, p2 = self._patches()
        with p1, p2, flow_diagnostics_scope(diagnostics):
            payload = build_batch_prompt([_adaptive_packet()], OmniContext())
        assert payload["crops"] == []
        assert payload.get("video_base64")
        assert diagnostics.smart_crop_enabled is False
        assert diagnostics.smart_crop_applied is False
        assert diagnostics.crop_region is None


def _request_text(messages: list[dict]) -> str:
    """把 messages 里的文本段按顺序摊平(媒体块丢弃)。

    fused 的 user content 是多模态 list: 文本段 + gallery image_url + 主 video_url。
    本函数只取文本, 视频字节不进指纹(编码器产物会随 PyAV 版本变, 不该混进 prompt 的
    逐字节断言)。
    """
    out: list[str] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.extend(b["text"] for b in content if b.get("type") == "text")
    return "\n".join(out)


class TestVideoModeGoldenPrompt:
    """video 模式(默认、存量路径)的 fused request 文本逐字节冻结。

    本 PR 的零回归承诺是"视频模式的请求与 prompt 逐字符不变", 而新加的
    ``visual_input_mode`` 渗透到三处: system prompt 装配(SceneDescriptor 多一维)、
    user content 改写(``_adapt_visual_prompt``)、媒体块拼装。任何一处漏判都会静默改变
    存量视频用户的请求 —— 断言零散字面量钉不住整串(4k+ 字符), 故用 golden 文件比对。

    golden 的来历: 在 PR 基线的 worktree 上跑**同一 fixture** 生成文本, 与本文件逐字节
    一致后才落库。即它证明的是"与改造前逐字节相同", 不只是"与今天的实现相同"。更新方式:
    确认 prompt 改动确属有意, 核对过基线差异后再重新生成该文件。

    环境无关性(不控住这几项, 换台机器 golden 就飘): 文本与 adapter 无关 —— mimo / qwen /
    gemini 三种 adapter 产出同一串(媒体块才分 provider); ``_has_pets_for_scene`` 为真会多出
    宠物段, ``get_home_profile_prefix`` 读环境里的 profile.md 会多出家庭档案段, 故两者都在
    测试里钉成空/False。
    """

    def test_fused_video_mode_request_text_is_byte_identical(self, monkeypatch):
        from pathlib import Path

        from miloco.perception.engine.omni import prompt_builder as pb
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        monkeypatch.setattr(pb, "_has_pets_for_scene", lambda: False)
        # 家庭档案是运行时数据($MILOCO_HOME/home-profile/profile.md),按环境注入 ——
        # CI 干净环境无此文件、注入空, 但有残留 profile.md 的机器(如部署机)会多出
        # 「# 家庭档案」段, 使 golden 随环境飘。它不属于 video 模式的 prompt 模板本身,
        # 故钉成空, 与 CI 口径一致(同 _has_pets_for_scene 的隔离)。
        monkeypatch.setattr(pb, "get_home_profile_prefix", lambda: "")
        fused = build_fused_payload(
            packets=[_mock_edge_packet()],
            context=OmniContext(room_name="study-room"),
            candidates=[],
            gallery_snapshot={},
            visual_input_mode="video",
        )
        # 落库的 golden 以换行收尾(文件惯例), 比对时补上这一个字节而不是 rstrip ——
        # rstrip 会把"末尾空行被吃掉"这种改动一起放过。
        golden = (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "omni_prompt_video_mode.txt"
        ).read_text(encoding="utf-8")
        assert _request_text(fused["messages"]) + "\n" == golden, (
            "video 模式(存量默认路径)的 request 文本变了 —— 本 PR 不该改到它"
        )


def test_transform_status_requires_every_frame_to_resize():
    # canonical 帧由 _prepare_omni_visual_frames 一次性产出(video/image 共用):
    # media_transform 遥测只在全部帧缩放成功后才记 OK，中途任一帧失败则整体抛错、
    # 诊断保持 unknown —— 不会留下「转换 OK 但帧不全」的假全绿。
    import miloco.perception.engine.omni.prompt_builder as pb
    from miloco.observability.perception_flow import PerDeviceFlowDiagnostics
    from miloco.perception.flow_context import flow_diagnostics_scope

    diagnostics = PerDeviceFlowDiagnostics(
        device_id="camera-1",
        room_name="Living Room",
        trace_id="trace-1",
        device_trace_id="device-trace-1",
        observed_at=1,
    )
    frames = [np.zeros((8, 8, 3), dtype=np.uint8) for _ in range(2)]

    with (
        flow_diagnostics_scope(diagnostics),
        patch.object(
            pb.cv2,
            "resize",
            side_effect=[frames[0], RuntimeError("resize failed")],
        ),
        pytest.raises(RuntimeError, match="resize failed"),
    ):
        pb._prepare_omni_visual_frames(frames, fps=2, short_edge=4)

    assert diagnostics.media_transform_status.value == "unknown"

    with flow_diagnostics_scope(diagnostics):
        prepared = pb._prepare_omni_visual_frames(frames, fps=2, short_edge=4)

    assert prepared is not None
    assert prepared.frame_count == 2
    assert diagnostics.media_transform_status.value == "ok"


# =============================================================================
# 单帧人像注入（4.4 段）在 fused 路径的接线
# =============================================================================


def _png_bytes(fill: int, h: int = 64, w: int = 32) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), fill, dtype=np.uint8))
    assert ok
    return buf.tobytes()


class TestPersonCropInject:
    """单帧人像注入的位置、门控与降级。

    位置是本段的重点：它必须落在 gallery **之后**（文案写的是「与上方 gallery 逐一比对」），
    又必须落在 4.6 全景参考帧引导语**之前**（那条不变式要求引导语紧跟的那张就是 video 前
    最后一张图）。两头都钉住，才不会在后续有人插新图块时静默漂走。
    """

    _NOTE = "【识别辅助】下方为每个待识别 track 的"

    def _gallery(self):
        from miloco.perception.engine.identity.library import GallerySamples

        return {
            "p-1": GallerySamples(
                person_id="p-1", name="张三", role=None,
                body_composite_jpeg=_png_bytes(200),
            ),
        }

    def _cands(self, *track_ids):
        from miloco.perception.engine.identity.dispatcher import IdentityQueryItem

        return [
            IdentityQueryItem(
                track_id=t,
                body_crop=np.full((120, 60, 3), 30 + t, dtype=np.uint8),
                bbox_xyxy_norm=(100, 100, 300, 600),
            )
            for t in track_ids
        ]

    def _content(self, *, candidates, gallery_snapshot, matching_moot=False, packet=None,
                 packets=None):
        """闸**由用例自己钉成开**，不读机器上的合并 settings。

        否则用户在自己 config.json 里合法地把它关掉，本类的阳性用例会集体变红；而阴性用例
        （test_not_injected_*）会变成"永远绿"的假保证 —— 它们要证明的是 prompt 侧的门控，
        不是配置默认值。随包默认值另有专门用例（test_person_crop_inject.py::TestShippedDefault）。
        """
        from miloco.perception.engine.config import PersonCropInjectConfig
        from miloco.perception.engine.omni.prompt_builder import build_fused_payload

        with patch(
            "miloco.perception.engine.omni.person_crop_inject"
            ".person_crop_inject_config_from_settings",
            return_value=PersonCropInjectConfig(enabled=True),
        ):
            fused = build_fused_payload(
                packets=packets or [packet or _adaptive_packet()], context=OmniContext(),
                candidates=candidates, gallery_snapshot=gallery_snapshot,
                matching_moot=matching_moot,
            )
        return _multimodal_user_content(fused["messages"])

    def _idx(self, content, pred):
        return next((i for i, b in enumerate(content) if pred(b)), None)

    def _note_idx(self, content):
        return self._idx(
            content, lambda b: b.get("type") == "text" and self._NOTE in b.get("text", "")
        )

    def test_injected_after_gallery_and_before_video(self):
        content = self._content(candidates=self._cands(1), gallery_snapshot=self._gallery())
        note = self._note_idx(content)
        assert note is not None
        gallery_close = self._idx(
            content, lambda b: b.get("type") == "text" and b.get("text") == "</gallery>"
        )
        vid = self._idx(content, lambda b: b.get("type") == "video_url")
        assert gallery_close < note < vid
        assert any("track_id=1 的外观单帧" in b.get("text", "") for b in content)

    def test_one_image_per_candidate(self):
        content = self._content(candidates=self._cands(1, 2, 5), gallery_snapshot=self._gallery())
        bind = [b for b in content if "的外观单帧：" in b.get("text", "")]
        assert [t["text"] for t in bind] == [
            "待识别 track_id=1 的外观单帧：",
            "待识别 track_id=2 的外观单帧：",
            "待识别 track_id=5 的外观单帧：",
        ]

    def test_injected_immediately_after_gallery(self):
        """紧贴 gallery：说明块必须是 </gallery> 的下一块。

        文案写的是「与**上方** gallery 成员参考图逐一比对」，中间垫东西这句就开始变虚；
        另一头（必须在全景引导语之前）由 test_ref_stays_last_image_before_video 钉。
        """
        content = self._content(candidates=self._cands(1), gallery_snapshot=self._gallery())
        close = self._idx(
            content, lambda b: b.get("type") == "text" and b.get("text") == "</gallery>"
        )
        assert self._note_idx(content) == close + 1

    def test_not_injected_when_gallery_gives_up(self):
        """gallery 走「全或无」放弃（成员参考图坏了）时不注入。

        这是运行时真会发生的那一支：库里存的 composite 损坏 / 半截，gallery 段整段放弃、
        退化为 unknown 以避免错认。此时注入一张人像没有可比对的对象，文案里的「上方 gallery」
        也成了悬空指代。
        """
        from miloco.perception.engine.identity.library import GallerySamples

        broken = {
            "p-1": GallerySamples(
                person_id="p-1", name="张三", role=None, body_composite_jpeg=b"xx",
            ),
        }
        content = self._content(candidates=self._cands(1), gallery_snapshot=broken)
        assert self._note_idx(content) is None
        # gallery 合成图与人像注入都是 PNG，全景参考帧是 JPEG —— 按 PNG 判定，才能同时证明
        # "gallery 段确已放弃"和"没注入人像"，又不会被 Smart Crop 的参考帧干扰。
        assert not any(
            b.get("type") == "image_url" and "image/png" in b["image_url"]["url"]
            for b in content
        )

    def test_not_injected_with_multiple_packets(self):
        """多设备并发时跳过：candidates 只属于其中一个设备，而 track 号在设备间会撞号，
        套错帧就是把另一个人的图绑到这个 track_id 上。"""
        content = self._content(
            candidates=self._cands(1), gallery_snapshot=self._gallery(),
            packets=[_adaptive_packet(), _adaptive_packet()],
        )
        assert self._note_idx(content) is None

    def test_not_injected_without_gallery(self):
        """gallery 段没渲染出参考图时不注入——否则「上方 gallery」是悬空指代，且比无可比。"""
        content = self._content(candidates=self._cands(1), gallery_snapshot={})
        assert self._note_idx(content) is None

    def test_not_injected_when_matching_moot(self):
        """身份库为空：identities 已被指示只判 unknown/no_person，注入人像帮不到任何事。"""
        content = self._content(
            candidates=self._cands(1), gallery_snapshot=self._gallery(), matching_moot=True,
        )
        assert self._note_idx(content) is None

    def test_not_injected_without_candidates(self):
        content = self._content(candidates=[], gallery_snapshot=self._gallery())
        assert self._note_idx(content) is None

    def test_picks_largest_frame_when_per_frame_boxes_present(self):
        """有逐帧框时走面积最大那帧，而不是候选自带的末帧 body_crop。"""
        import base64
        from dataclasses import replace

        frames = [np.full((480, 640, 3), v, dtype=np.uint8) for v in (20, 210, 60)]
        pkt = replace(
            _adaptive_packet(frames=frames),
            per_frame_track_boxes=[
                {1: (0, 0, 60, 60)},
                {1: (0, 0, 240, 330)},   # 面积最大 → fill=210
                {1: (0, 0, 90, 100)},
            ],
        )
        content = self._content(
            candidates=self._cands(1), gallery_snapshot=self._gallery(), packet=pkt,
        )
        note = self._note_idx(content)
        img = content[note + 2]                      # note, 绑定文本, 图
        assert img["type"] == "image_url"
        raw = base64.b64decode(img["image_url"]["url"].split(",", 1)[1])
        arr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        assert abs(int(arr.mean()) - 210) <= 2
        assert arr.shape[0] == 256                   # 归一高度

    def test_ref_stays_last_image_before_video(self):
        """开 Smart Crop 时，人像注入不能把全景参考帧从「video 前最后一张图」的位置挤走。

        这是 test_ref_is_last_image_before_video_with_pet_refs 的同款不变式，但走的是**注入
        真实开启**的路径 —— 那条守卫测试传 candidates=[]，根本进不了注入分支。
        """
        from miloco.perception.engine.config import CropEnhanceConfig

        # 区域必须**钉死**：不钉的话裁不裁取决于噪声帧上的运动块与面积闸能否过关，
        # 是涌现行为 —— 该用例会间歇性地在"全景参考帧根本没渲染"的前提下跑，断言随之漂移。
        # 同 TestAdaptiveResolution._fixed_region 的做法（patch 的是带拒因的那个函数，
        # 薄封装 compute_crop_region 不在被测路径上，patch 它不会生效）。
        with patch(
            "miloco.perception.engine.omni.prompt_builder._get_video_short_edge",
            return_value=512,
        ), patch(
            "miloco.perception.engine.omni.crop_enhance.crop_enhance_config_from_settings",
            return_value=CropEnhanceConfig(enabled=True, user_enabled=True),
        ), patch(
            "miloco.perception.engine.omni.crop_enhance.compute_crop_region_detail",
            return_value=((50, 50, 350, 350), "ok"),
        ):
            content = self._content(
                candidates=self._cands(1, 2), gallery_snapshot=self._gallery(),
            )
        guide = self._idx(
            content,
            lambda b: b.get("type") == "text" and "全景场景参考" in b.get("text", ""),
        )
        assert guide is not None, "本用例的前提是 Smart Crop 真生效、全景参考帧已渲染"
        vid = self._idx(content, lambda b: b.get("type") == "video_url")
        # vid 为 None 时 content[:None] 会静默变成"整个列表"，让下面的断言测的是别的东西。
        # 视频块被跳过（编码产物过短）是真实存在的降级路径，这里要它**响**而不是悄悄改语义。
        assert vid is not None, "本用例的前提是 video 块已渲染"
        imgs = [i for i, b in enumerate(content[:vid]) if b.get("type") == "image_url"]
        _dbg = (
            "layout=" + "".join(
                "V" if b.get("type") == "video_url"
                else ("I" if b.get("type") == "image_url" else "t") for b in content)
            + f" note={self._note_idx(content)} guide={guide} vid={vid} imgs={imgs}"
            + " texts=" + repr([b.get("text", "")[:22] for b in content
                                if b.get("type") == "text"])
        )
        assert imgs[-1] == guide + 1, _dbg     # 引导语紧跟的那张，仍是 video 前最后一张
        assert self._note_idx(content) < guide, _dbg  # 人像注入整段在引导语之前


def test_video_mp4_container_duration_uses_window_not_config_fps():
    """Regression for #560 (video path).

    The mp4 container duration must follow the real capture window
    (sampling_fps = frame_count / duration_s), not frame_count / config_fps.
    8 frames delivered over a 4-second window with config fps=1 must yield a
    ~4-second container, not 8 seconds.
    """
    import base64 as b64mod
    import io
    from types import SimpleNamespace

    import av
    import numpy as np

    from miloco.perception.engine.omni.prompt_builder import _encode_video

    n_frames = 8
    window_s = 4.0
    config_fps = 1  # misleading config-derived fps

    frames = [np.zeros((64, 64, 3), np.uint8) for _ in range(n_frames)]
    audio = np.zeros(16000 * int(window_s), np.int16)

    frame_info = SimpleNamespace(
        fps=config_fps,
        start_timestamp=0.0,
        end_timestamp=window_s * 1000.0,  # ms
    )
    packet = SimpleNamespace(
        all_frames=frames,
        audio_clip=audio,
        sample_rate=16000,
        frame_info=frame_info,
    )

    b64, info = _encode_video(packet)
    assert b64 is not None
    assert info is not None

    # Decode the mp4 and check its container duration.
    mp4_bytes = b64mod.b64decode(b64)
    with av.open(io.BytesIO(mp4_bytes)) as container:
        duration = float(container.duration / av.time_base)

    # Should be ~4s (the real capture window), not ~8s (frame_count / config_fps).
    assert abs(duration - window_s) < 0.5, (
        f"mp4 container duration {duration:.2f}s != expected {window_s:.2f}s "
        f"(would be {n_frames / config_fps:.2f}s if config fps were used)"
    )
