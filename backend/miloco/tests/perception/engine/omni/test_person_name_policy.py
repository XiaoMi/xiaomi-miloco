"""Shared names must reach actual fused payloads and identity parsing."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import cv2
import numpy as np
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.identity.dispatcher import IdentityQueryItem
from miloco.perception.engine.identity.library import GallerySamples, PersonRef
from miloco.perception.engine.identity.names import (
    build_name_lookup,
    usable_person_name,
)
from miloco.perception.engine.omni import omni as orchestrator
from miloco.perception.engine.omni import prompt_builder as builder
from miloco.perception.engine.omni.provider import MiMoAdapter
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    FrameInfo,
    IdentityPacket,
    IdentityTarget,
    MotionState,
    ObjectType,
    OmniContext,
)

PID = "11111111-1111-4111-8111-111111111111"
OTHER_PID = "22222222-2222-4222-8222-222222222222"


def _person(name, pid=PID, role="家长"):
    return PersonRef(pid, name, role, True, 0)


def _packet():
    return IdentityPacket(
        packet_id="name-policy",
        room_name="study",
        timestamp=3000,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=3000, fps=1),
        targets=[
            IdentityTarget(
                type=ObjectType.HUMAN_WITH_FACE,
                person_id=PID,
                track_id=7,
                needs_omni_verify=False,
                box_info=[],
                bbox_xyxy_norm=(100, 100, 400, 900),
            )
        ],
        scene_motion=MotionState.STATIC,
        frames=[],
        all_frames=[np.full((100, 100, 3), 60, dtype=np.uint8)],
        audio_clip=np.array([], dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE, is_urgent=False, energy_level=0
        ),
    )


def _gallery(persons):
    crop = np.random.default_rng(31).integers(0, 256, (64, 64, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".png", crop)
    assert ok
    return {
        p.person_id: GallerySamples(
            p.person_id, p.name, p.role, body_composite_jpeg=encoded.tobytes()
        )
        for p in persons
    }


def _user_blocks(messages):
    blocks = [
        block
        for message in messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
    ]
    assert blocks
    return blocks


def _user_text(messages):
    return "\n".join(b["text"] for b in _user_blocks(messages) if b["type"] == "text")


@pytest.fixture
def fused_entry(monkeypatch):
    monkeypatch.setattr(builder, "get_home_profile_prefix", lambda: "")
    monkeypatch.setattr(builder, "_effective_panorama_short_edge", lambda: 128)
    monkeypatch.setattr(builder, "_has_pets_for_scene", lambda: False)

    async def run(persons, *, library_error=None, answer=PID, pending=True):
        library = Mock()
        library.list_persons.return_value = persons
        library.list_persons.side_effect = library_error
        gallery = _gallery(persons)
        candidates = [
            IdentityQueryItem(track_id=19, bbox_xyxy_norm=(100, 100, 400, 900))
        ]
        engine = SimpleNamespace(
            library=library,
            take_fused_pending=Mock(
                return_value=SimpleNamespace(
                    candidates=candidates, gallery_snapshot=gallery
                )
                if pending
                else None
            ),
            config=SimpleNamespace(
                stranger=SimpleNamespace(distinguish=True), confidence_cutoff=0.5
            ),
            deliver_fused_response=AsyncMock(),
            deliver_fused_failure=AsyncMock(),
        )
        raw = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "caption": "验证场景",
                                "identity_assignments": [
                                    {"track_id": 19, "name": answer, "confidence": 0.9}
                                ],
                            }
                        )
                    }
                }
            ]
        }
        call = AsyncMock(return_value=raw)
        build = Mock(wraps=builder.build_fused_payload)
        parse = Mock(wraps=orchestrator.parse_identity_assignments)
        monkeypatch.setattr(orchestrator, "_call_omni_messages", call)
        monkeypatch.setattr(orchestrator, "build_fused_payload", build)
        monkeypatch.setattr(orchestrator, "parse_identity_assignments", parse)
        output = await orchestrator.run_omni_fused(
            [_packet()], OmniContext(), OmniConfig(), engine
        )
        library.list_persons.assert_called_once_with()
        call.assert_awaited_once()
        build.assert_called_once()
        assert output.caption and output.caption[0].description == "验证场景"
        if pending:
            parse.assert_called_once()
            engine.deliver_fused_response.assert_awaited_once()
        return SimpleNamespace(
            messages=call.await_args.args[0],
            build=build,
            parse=parse,
            engine=engine,
        )

    return run


class TestSharedPersonNames:
    @pytest.mark.parametrize(
        "name,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("\t\r\n", None),
            ("\u3000", None),
            ("登记成员", "登记成员"),
            (" 登记成员 ", " 登记成员 "),
        ],
    )
    def test_name_availability_and_original_spelling(self, name, expected):
        ref = _person(name)
        assert usable_person_name(name) == expected
        assert build_name_lookup(iter([ref])) == (
            {} if expected is None else {PID: expected}
        )
        assert ref.name == name

    def test_lookup_retains_only_available_names(self):
        people = [_person("登记成员"), _person("\u3000", OTHER_PID)]
        assert build_name_lookup(people) == {PID: "登记成员"}
        assert build_name_lookup([]) == {}


class TestFusedPersonNameEntry:
    @pytest.mark.parametrize("pending", [False, True])
    @pytest.mark.parametrize(
        "name,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("\t\r\n", None),
            ("\u3000", None),
            ("登记成员", "登记成员"),
            (" 登记成员 ", " 登记成员 "),
        ],
    )
    async def test_library_names_reach_payload_and_parser(
        self, fused_entry, name, expected, pending
    ):
        result = await fused_entry([_person(name)], pending=pending)
        assert result.build.call_args.kwargs["label_lookup"] == (
            {} if expected is None else {PID: expected}
        )
        assert result.build.call_args.kwargs["matching_moot"] is False
        text = _user_text(result.messages)
        label = expected if expected is not None else PID
        if pending:
            assert f"【{label}】" in text
        assert f"{label}[bbox=(100, 100, 400, 900)]" in text
        assert sum(
            b["type"] == "image_url" for b in _user_blocks(result.messages)
        ) == int(pending)
        if not pending:
            result.parse.assert_not_called()
            result.engine.deliver_fused_response.assert_not_awaited()
            return
        aliases = result.parse.call_args.kwargs["name_to_pid"]
        assert not any(k in aliases for k in ["", "   ", "\t\r\n", "\u3000"])
        assert aliases[PID] == PID
        assert (
            result.engine.deliver_fused_response.await_args.args[0][0]["person_id"]
            == PID
        )

    @pytest.mark.parametrize(
        "name,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("\t\r\n", None),
            ("\u3000", None),
            ("登记成员", "登记成员"),
            (" 登记成员 ", " 登记成员 "),
        ],
    )
    async def test_gallery_fallback_obeys_same_name_policy(
        self, fused_entry, name, expected
    ):
        result = await fused_entry(
            [_person(name)], library_error=PermissionError("private detail")
        )
        assert result.build.call_args.kwargs["label_lookup"] == {}
        assert result.build.call_args.kwargs["matching_moot"] is False
        text = _user_text(result.messages)
        assert f"【{expected if expected is not None else PID}】" in text
        assert sum(b["type"] == "image_url" for b in _user_blocks(result.messages)) == 1
        aliases = result.parse.call_args.kwargs["name_to_pid"]
        assert not any(k in aliases for k in ["", "   ", "\t\r\n", "\u3000"])
        assert (expected in aliases) is (expected is not None)
        assert "家长" not in aliases  # 读库失败时没有全库角色唯一性依据。
        assert aliases[PID] == PID
        assert (
            result.engine.deliver_fused_response.await_args.args[0][0]["person_id"]
            == PID
        )

    @pytest.mark.parametrize(
        "answer,duplicate_role,expected",
        [
            ("登记成员", False, PID),
            ("登记成员(角色:家长)", False, PID),
            ("家长", False, PID),
            (PID, False, PID),
            ("家长", True, "unknown"),
            ("登记成员(角色:家长)", True, PID),
        ],
    )
    async def test_name_role_and_uuid_reverse_resolution(
        self, fused_entry, answer, duplicate_role, expected
    ):
        people = [_person("登记成员")]
        if duplicate_role:
            people.append(_person("其他成员", OTHER_PID))
        result = await fused_entry(people, answer=answer)
        aliases = result.parse.call_args.kwargs["name_to_pid"]
        assert ("家长" in aliases) is (not duplicate_role)
        assert aliases["登记成员"] == PID
        assert aliases["登记成员(角色:家长)"] == PID
        assert aliases[PID] == PID
        assert (
            result.engine.deliver_fused_response.await_args.args[0][0]["person_id"]
            == expected
        )

    @pytest.mark.parametrize("pending", [False, True])
    async def test_all_unnamed_members_are_not_an_empty_library(
        self, fused_entry, pending
    ):
        result = await fused_entry([_person("\u3000")], pending=pending)
        assert result.build.call_args.kwargs["label_lookup"] == {}
        assert result.build.call_args.kwargs["matching_moot"] is False
        assert f"{PID}[bbox=" in _user_text(result.messages)
        assert "【\u3000】" not in _user_text(result.messages)
        if not pending:
            result.parse.assert_not_called()
            result.engine.deliver_fused_response.assert_not_awaited()

    async def test_unnamed_member_still_makes_role_ambiguous(self, fused_entry):
        result = await fused_entry(
            [_person("登记成员"), _person("\u3000", OTHER_PID)], answer="家长"
        )
        aliases = result.parse.call_args.kwargs["name_to_pid"]
        assert "家长" not in aliases
        assert aliases[PID] == PID
        assert aliases[OTHER_PID] == OTHER_PID
        assert sum(b["type"] == "image_url" for b in _user_blocks(result.messages)) == 2
        assert (
            result.engine.deliver_fused_response.await_args.args[0][0]["person_id"]
            == "unknown"
        )

    async def test_genuinely_empty_library_disables_member_matching(self, fused_entry):
        result = await fused_entry([])
        assert result.build.call_args.kwargs["label_lookup"] == {}
        assert result.build.call_args.kwargs["matching_moot"] is True
        assert sum(b["type"] == "image_url" for b in _user_blocks(result.messages)) == 0


class TestStandaloneFusedPersonNames:
    @pytest.mark.parametrize(
        "name,expected",
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("\t\r\n", None),
            ("\u3000", None),
            ("登记成员", "登记成员"),
            (" 登记成员 ", " 登记成员 "),
        ],
    )
    def test_default_lookup_and_gallery_labels(self, monkeypatch, name, expected):
        monkeypatch.setattr(builder, "get_home_profile_prefix", lambda: "")
        monkeypatch.setattr(builder, "_effective_panorama_short_edge", lambda: 128)
        monkeypatch.setattr(builder, "_has_pets_for_scene", lambda: False)
        payload = builder.build_fused_payload(
            [_packet()],
            OmniContext(),
            [IdentityQueryItem(track_id=19)],
            _gallery([_person(name)]),
            adapter=MiMoAdapter(),
        )
        text = _user_text(payload["messages"])
        assert f"【{expected if expected is not None else PID}】" in text
        roster_label = f"{expected}(角色:家长)" if expected is not None else PID
        assert f"{roster_label}[bbox=" in text
        assert (
            sum(b["type"] == "image_url" for b in _user_blocks(payload["messages"]))
            == 1
        )
        assert builder.format_person_label(name, "家长") == (
            None if expected is None else f"{expected}(角色:家长)"
        )
