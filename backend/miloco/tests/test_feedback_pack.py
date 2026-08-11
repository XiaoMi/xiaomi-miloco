"""feedback_pack 核心逻辑单测."""

import gzip
import json
import tarfile
from unittest.mock import MagicMock, patch

from miloco.admin.feedback_pack import (
    _sanitize_pii,
    build_feedback_pack,
    build_on_demand_feedback_pack,
)


def test_sanitize_pii_masks_phone():
    assert _sanitize_pii("手机号13800138000结束") == "手机号***结束"


def test_sanitize_pii_masks_ip():
    assert _sanitize_pii("地址192.168.1.1端口") == "地址***端口"


def test_sanitize_pii_masks_idcard():
    assert _sanitize_pii("身份证11010519491231002X号") == "身份证***号"


def test_sanitize_pii_masks_all_three():
    text = "13800138000 192.168.1.1 11010519491231002X"
    assert _sanitize_pii(text) == "*** *** ***"


def test_sanitize_pii_preserves_short_numbers():
    assert _sanitize_pii("token数1350万") == "token数1350万"


def test_sanitize_pii_preserves_model_version():
    assert _sanitize_pii("mimo-v2.5.1.0模型") == "mimo-v2.5.1.0模型"


# ─── _ERROR_TYPE_LABELS 映射 ──────────────────────────────────────────


def test_error_type_labels_maps_known_keys():
    from miloco.admin.feedback_pack import _ERROR_TYPE_LABELS
    assert _ERROR_TYPE_LABELS["person"] == "人物识别错误"
    assert _ERROR_TYPE_LABELS["pet"] == "宠物识别错误"
    assert _ERROR_TYPE_LABELS["other"] == "其他"


def test_error_type_labels_fallback_unknown_key():
    from miloco.admin.feedback_pack import _ERROR_TYPE_LABELS
    assert _ERROR_TYPE_LABELS.get("bogus", "bogus") == "bogus"


# ─── build_feedback_pack 集成测试 ─────────────────────────────────────


def test_build_pack_metadata_and_trace(tmp_path, monkeypatch):
    """打包主流程：metadata 组装 + trace 脱敏入包 + tar 结构."""
    event_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    # 准备 snapshots 目录
    snapshot_root = tmp_path / "snapshots"
    event_dir = snapshot_root / event_id
    event_dir.mkdir(parents=True)

    # 写一份含手机号的 trace（验证脱敏生效）
    trace_data = {"schema_version": 1, "calls": [{"phone": "13800138000"}]}
    trace_bytes = gzip.compress(json.dumps(trace_data).encode())
    (event_dir / "omni_trace.json.gz").write_bytes(trace_bytes)

    # 写一个 clip
    clip_dir = event_dir / "cam1"
    clip_dir.mkdir()
    (clip_dir / "clip.mp4").write_bytes(b"fake-mp4")

    # mock DAO
    mock_dao = MagicMock()
    mock_dao.get_by_id.return_value = {
        "id": event_id,
        "timestamp": 1234567890000,
        "text": "test event",
        "device_ids": ["cam1"],
    }

    mock_mgr = MagicMock()
    mock_mgr.meaningful_events_dao = mock_dao

    monkeypatch.setattr("miloco.admin.feedback_pack.get_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    with patch("miloco.manager.get_manager", return_value=mock_mgr):
        result = build_feedback_pack(
            event_id=event_id,
            error_types=["person", "other"],
            feedback_text="test feedback",
        )

    assert result["size_bytes"] > 0
    assert result["components"]["omni_trace_found"] is True
    assert "cam1/clip.mp4" in result["components"]["clips_found"]

    with tarfile.open(result["path"], "r:gz") as tar:
        names = tar.getnames()
        assert "metadata.json" in names
        assert "omni_trace.json.gz" in names
        assert "clips/cam1/clip.mp4" in names

        # metadata 检查
        meta = json.loads(tar.extractfile("metadata.json").read())
        assert meta["event_id"] == event_id
        assert meta["error_types"] == ["人物识别错误", "其他"]  # key → 中文映射
        assert meta["user_feedback"] == "test feedback"
        assert meta["miloco_version"] != "unknown"

        # trace 脱敏检查
        trace_raw = gzip.decompress(tar.extractfile("omni_trace.json.gz").read())
        trace_text = trace_raw.decode()
        assert "13800138000" not in trace_text
        assert "***" in trace_text


def test_build_pack_includes_ref_frame(tmp_path, monkeypatch):
    """Smart Crop 事件:ref.jpg 与 clip 同附进 clips/{slug}/ref.jpg,并记入 components/metadata."""
    from miloco.perception.snapshot_writer import region_slug

    event_id = "22222222-3333-4444-5555-666666666666"

    snapshot_root = tmp_path / "snapshots"
    event_dir = snapshot_root / event_id
    event_dir.mkdir(parents=True)

    slug = region_slug("cam1")
    clip_dir = event_dir / slug
    clip_dir.mkdir()
    (clip_dir / "clip.mp4").write_bytes(b"fake-crop-mp4")
    (clip_dir / "ref.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-ref")

    mock_dao = MagicMock()
    mock_dao.get_by_id.return_value = {
        "id": event_id,
        "timestamp": 1000,
        "text": "t",
        "device_ids": ["cam1"],
    }
    mock_mgr = MagicMock()
    mock_mgr.meaningful_events_dao = mock_dao

    monkeypatch.setattr("miloco.admin.feedback_pack.get_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    with patch("miloco.manager.get_manager", return_value=mock_mgr):
        result = build_feedback_pack(
            event_id=event_id, error_types=[], feedback_text="",
        )

    assert f"{slug}/ref.jpg" in result["components"]["refs_found"]

    with tarfile.open(result["path"], "r:gz") as tar:
        names = tar.getnames()
        assert f"clips/{slug}/ref.jpg" in names
        meta = json.loads(tar.extractfile("metadata.json").read())
        assert f"{slug}/ref.jpg" in meta["refs_found"]


def test_build_pack_no_ref_frame(tmp_path, monkeypatch):
    """非 crop 事件(无 ref.jpg)→ refs_found 空,包内无 ref."""
    from miloco.perception.snapshot_writer import region_slug

    event_id = "33333333-4444-5555-6666-777777777777"

    snapshot_root = tmp_path / "snapshots"
    event_dir = snapshot_root / event_id
    event_dir.mkdir(parents=True)

    slug = region_slug("cam1")
    clip_dir = event_dir / slug
    clip_dir.mkdir()
    (clip_dir / "clip.mp4").write_bytes(b"fake-mp4")

    mock_dao = MagicMock()
    mock_dao.get_by_id.return_value = {
        "id": event_id, "timestamp": 1000, "text": "t", "device_ids": ["cam1"],
    }
    mock_mgr = MagicMock()
    mock_mgr.meaningful_events_dao = mock_dao

    monkeypatch.setattr("miloco.admin.feedback_pack.get_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    with patch("miloco.manager.get_manager", return_value=mock_mgr):
        result = build_feedback_pack(
            event_id=event_id, error_types=[], feedback_text="",
        )

    assert result["components"]["refs_found"] == []
    with tarfile.open(result["path"], "r:gz") as tar:
        assert not any(n.endswith("ref.jpg") for n in tar.getnames())


def test_build_pack_event_not_found(tmp_path, monkeypatch):
    """event 不存在时抛 EventNotFoundError."""
    import pytest
    from miloco.admin.feedback_pack import EventNotFoundError

    mock_dao = MagicMock()
    mock_dao.get_by_id.return_value = None
    mock_mgr = MagicMock()
    mock_mgr.meaningful_events_dao = mock_dao

    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    with patch("miloco.manager.get_manager", return_value=mock_mgr):
        with pytest.raises(EventNotFoundError):
            build_feedback_pack(event_id="nonexistent", error_types=[], feedback_text="")


def test_build_pack_with_gallery(tmp_path, monkeypatch):
    """include_gallery=True 时 gallery 文件入包."""
    event_id = "11111111-2222-3333-4444-555555555555"

    snapshot_root = tmp_path / "snapshots"
    event_dir = snapshot_root / event_id
    event_dir.mkdir(parents=True)

    # trace
    trace_data = {"schema_version": 1, "calls": []}
    (event_dir / "omni_trace.json.gz").write_bytes(gzip.compress(json.dumps(trace_data).encode()))

    # gallery
    gallery_dir = event_dir / "gallery"
    gallery_dir.mkdir()
    (gallery_dir / "person1_body.png").write_bytes(b"\x89PNG fake")
    (gallery_dir / "person1_face.jpg").write_bytes(b"\xff\xd8\xff fake")

    mock_dao = MagicMock()
    mock_dao.get_by_id.return_value = {
        "id": event_id, "timestamp": 1000, "text": "t", "device_ids": [],
    }
    mock_mgr = MagicMock()
    mock_mgr.meaningful_events_dao = mock_dao

    monkeypatch.setattr("miloco.admin.feedback_pack.get_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    with patch("miloco.manager.get_manager", return_value=mock_mgr):
        result = build_feedback_pack(
            event_id=event_id, error_types=[], feedback_text="",
            include_gallery=True,
        )

    assert result["components"]["gallery_included"] is True

    with tarfile.open(result["path"], "r:gz") as tar:
        names = tar.getnames()
        assert any("gallery/" in n for n in names)


# ─── build_on_demand_feedback_pack 集成测试 ──────────────────────────


def test_on_demand_pack_roundtrip(tmp_path, monkeypatch):
    """on-demand 反馈包：metadata.type == 'on_demand' + clips_missing_basis + 包名含完整 UUID."""
    log_id = "aaaaaaaa-bbbb-cccc-dddd-111111111111"

    snapshot_root = tmp_path / "snapshots"
    event_dir = snapshot_root / log_id
    event_dir.mkdir(parents=True)

    trace_data = {"schema_version": 1, "calls": [{"note": "13800138000"}]}
    (event_dir / "omni_trace.json.gz").write_bytes(
        gzip.compress(json.dumps(trace_data).encode())
    )

    clip_dir = event_dir / "cam1"
    clip_dir.mkdir()
    (clip_dir / "clip.mp4").write_bytes(b"fake-mp4")

    monkeypatch.setattr("miloco.admin.feedback_pack.get_snapshot_root", lambda: snapshot_root)
    monkeypatch.setattr("miloco.admin.feedback_pack.miloco_home", lambda: tmp_path)

    result = build_on_demand_feedback_pack(
        log_id=log_id,
        row={
            "timestamp": 1234567890000,
            "query": "谁在客厅?",
            "answer": "没人",
            "sources": ["cam1"],
            "latency_ms": 150,
            "clip_dids": ["cam1"],
        },
        error_types=["person"],
        feedback_text="认错人了",
        uid="test_user",
    )

    assert result["size_bytes"] > 0
    assert result["components"]["omni_trace_found"] is True
    assert "cam1/clip.mp4" in result["components"]["clips_found"]
    assert result["components"]["clips_missing"] == []

    assert f"-od-{log_id}-" in result["path"]

    with tarfile.open(result["path"], "r:gz") as tar:
        names = tar.getnames()
        assert "metadata.json" in names
        assert "omni_trace.json.gz" in names
        assert "clips/cam1/clip.mp4" in names
        assert not any("gallery/" in n for n in names)

        meta = json.loads(tar.extractfile("metadata.json").read())
        assert meta["type"] == "on_demand"
        assert meta["log_id"] == log_id
        assert meta["clips_missing_basis"] == "clip_dids"
        assert meta["error_types"] == ["人物识别错误"]

        trace_raw = gzip.decompress(tar.extractfile("omni_trace.json.gz").read())
        assert "13800138000" not in trace_raw.decode()
        assert "***" in trace_raw.decode()
