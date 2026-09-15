# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unit tests for snapshot_writer(D3-T4).

覆盖 region_slug / get_snapshot_root / check_disk_space / save_event_artifacts /
cleanup_snapshots.
"""

import gzip
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from miloco.perception import snapshot_writer
from miloco.perception.snapshot_context import OmniEventArtifacts
from miloco.perception.snapshot_writer import (
    check_disk_space,
    cleanup_snapshots,
    count_frames,
    frame_filename,
    get_snapshot_root,
    list_artifact_files,
    locate_frame_file,
    region_slug,
    save_event_artifacts,
)


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """每个用例独立 $MILOCO_HOME,避免读到用户真实 config."""
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    from miloco.config import reset_settings

    reset_settings()
    yield tmp_path
    reset_settings()


# ─── region_slug ────────────────────────────────────────────────────────────


class TestRegionSlug:
    def test_ascii_passthrough(self):
        assert region_slug("cam_living_01") == "cam_living_01"
        assert region_slug("camera-1") == "camera-1"
        assert region_slug("file.ext") == "file.ext"

    def test_unsafe_chars_replaced(self):
        """`/` `#` `?` 空格等会被替换成 `_`."""
        assert region_slug("cam/living/01") == "cam_living_01"
        assert region_slug("cam #1") == "cam__1"
        assert region_slug("cam?id=x") == "cam_id_x"

    def test_chinese_replaced(self):
        """中文按 ASCII-safe 规则被替换(避免 fs encoding 不一致问题)."""
        assert region_slug("客厅") == "__"  # 2 中文字符 → 2 _

    def test_empty_string(self):
        assert region_slug("") == "_"

    def test_dot_dot_traversal_blocked(self):
        """M4 路径逃逸:'..' / '.' / 以 '.' 开头的字串必须改写为 '_' 前缀,
        防 event_dir / slug 解析到父目录."""
        assert region_slug("..") == "_"
        assert region_slug(".") == "_"
        assert region_slug("../etc/passwd") == "__etc_passwd"
        # 隐藏目录 '.hidden' 也按 '_' 前缀
        assert region_slug(".hidden") == "_hidden"
        # 中间的 '.' 不影响(仍允许 file.ext / cam.living.01)
        assert region_slug("a..b") == "a..b"


# ─── get_snapshot_root ──────────────────────────────────────────────────────


class TestGetSnapshotRoot:
    def test_default_from_directories(self, isolated_settings):
        """settings.perception.snapshot_root=None 时,使用 directories.snapshot_dir."""
        root = get_snapshot_root()
        assert root == isolated_settings / "snapshots"

    def test_override_from_perception(self, isolated_settings, tmp_path):
        """settings.perception.snapshot_root 非 None 时优先生效."""
        custom = tmp_path / "custom_snaps"
        monkeypatch_env = {"MILOCO_PERCEPTION__SNAPSHOT_ROOT": str(custom)}
        with patch.dict(os.environ, monkeypatch_env):
            from miloco.config import reset_settings

            reset_settings()
            try:
                root = get_snapshot_root()
                assert root == custom
            finally:
                reset_settings()


# ─── check_disk_space ───────────────────────────────────────────────────────


class TestCheckDiskSpace:
    def test_sufficient_space(self, tmp_path):
        """tmp 通常有 > 1MB 空间."""
        assert check_disk_space(tmp_path, min_free_mb=1) is True

    def test_insufficient_space(self, tmp_path):
        """要求 1 EB 空间 → False."""
        # 1 EB = 1024^6 MB,远超任何机器
        assert check_disk_space(tmp_path, min_free_mb=10**12) is False

    def test_nonexistent_dir_uses_parent(self, tmp_path):
        """root 还不存在时,用 parent."""
        nonexistent = tmp_path / "not_yet_created" / "deeper"
        # parent 存在(tmp_path),应能查到磁盘统计
        assert check_disk_space(nonexistent, min_free_mb=1) is True

    def test_oserror_returns_true_failsafe(self, tmp_path):
        """OSError 时按"可用"处理,避免误杀."""
        with patch("shutil.disk_usage", side_effect=OSError("io error")):
            assert check_disk_space(tmp_path, min_free_mb=1) is True


# ─── save_event_artifacts ───────────────────────────────────────────────────


class TestSaveEventArtifacts:
    @pytest.fixture(autouse=True)
    def _patch_root(self, tmp_path, monkeypatch):
        """每个测试独立 snapshot_root,避免污染."""
        monkeypatch.setattr(snapshot_writer, "get_snapshot_root", lambda: tmp_path)
        self.root = tmp_path

    def test_empty_artifacts_returns_empty_list(self):
        """clips / frames / trace / gallery / ref_frames 全空 → 不创建任何文件,返空列表."""
        assert save_event_artifacts("event-1", OmniEventArtifacts()) == []
        assert not (self.root / "event-1").exists()

    def test_single_device_one_clip(self):
        """喂 1 个 device 的 mp4 字节 → 落 1 个 clip.mp4 文件."""
        clip_bytes = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100
        artifacts = OmniEventArtifacts(clips={"cam_living_01": (clip_bytes, "mp4")})
        clip_dids = save_event_artifacts("event-1", artifacts)
        assert clip_dids == ["cam_living_01"]
        path = self.root / "event-1" / "cam_living_01" / "clip.mp4"
        assert path.read_bytes() == clip_bytes

    def test_multi_device(self):
        """两个 device 各 1 个 clip.mp4."""
        artifacts = OmniEventArtifacts(
            clips={
                "cam_living_01": (b"video-bytes-A", "mp4"),
                "cam_kitchen_01": (b"video-bytes-B", "mp4"),
            }
        )
        clip_dids = save_event_artifacts("event-multi", artifacts)
        assert set(clip_dids) == {"cam_living_01", "cam_kitchen_01"}
        assert (self.root / "event-multi" / "cam_living_01" / "clip.mp4").exists()
        assert (self.root / "event-multi" / "cam_kitchen_01" / "clip.mp4").exists()

    def test_empty_bytes_skipped(self):
        """某 device 字节为空 → 跳过."""
        artifacts = OmniEventArtifacts(clips={"cam_a": (b"", "mp4")})
        assert save_event_artifacts("event-empty", artifacts) == []

    def test_device_id_slug_applied(self):
        """device_id 含 '/' → slug 化为 '_',目录路径合法."""
        artifacts = OmniEventArtifacts(clips={"cam/living/01": (b"x" * 100, "mp4")})
        clip_dids = save_event_artifacts("event-slug", artifacts)
        assert clip_dids == ["cam/living/01"]
        assert (self.root / "event-slug" / "cam_living_01" / "clip.mp4").exists()

    def test_kind_decides_extension(self):
        """kind='m4a' 落 clip.m4a,kind='mp4' 落 clip.mp4."""
        artifacts = OmniEventArtifacts(
            clips={
                "cam_audio": (b"audio-bytes", "m4a"),
                "cam_video": (b"video-bytes", "mp4"),
            }
        )
        clip_dids = save_event_artifacts("event-tuple", artifacts)
        assert set(clip_dids) == {"cam_audio", "cam_video"}
        assert (self.root / "event-tuple" / "cam_audio" / "clip.m4a").read_bytes() == b"audio-bytes"
        assert (self.root / "event-tuple" / "cam_video" / "clip.mp4").read_bytes() == b"video-bytes"

    def test_unknown_kind_skipped(self):
        """非法 kind(非 mp4/m4a)→ 该 device 跳过不落盘,避免污染目录."""
        artifacts = OmniEventArtifacts(
            clips={"cam_a": (b"x", "webm")},  # type: ignore[dict-item]
        )
        assert save_event_artifacts("event-bad-kind", artifacts) == []
        assert not (self.root / "event-bad-kind" / "cam_a").exists()

    def test_trace_only_writes_gz(self):
        """只有 trace → 落 omni_trace.json.gz,不创建 device 子目录,返空列表."""
        trace = {"schema_version": 1, "calls": [{"model": "mimo"}]}
        artifacts = OmniEventArtifacts(trace=trace)
        clip_dids = save_event_artifacts("event-trace", artifacts)
        assert clip_dids == []
        gz_path = self.root / "event-trace" / "omni_trace.json.gz"
        assert gz_path.exists()
        # device 子目录不存在
        device_dirs = [p for p in (self.root / "event-trace").iterdir() if p.is_dir()]
        assert device_dirs == []
        # gzip 解压后 schema 对齐
        decoded = json.loads(gzip.decompress(gz_path.read_bytes()))
        assert decoded == trace

    def test_clips_and_trace_both(self):
        """同时 clips + trace → 两类文件都落,clip_dids 只含 clip 设备."""
        artifacts = OmniEventArtifacts(
            clips={"cam_a": (b"v", "mp4"), "cam_b": (b"a", "m4a")},
            trace={"schema_version": 1, "calls": []},
        )
        clip_dids = save_event_artifacts("event-both", artifacts)
        assert set(clip_dids) == {"cam_a", "cam_b"}
        assert (self.root / "event-both" / "cam_a" / "clip.mp4").exists()
        assert (self.root / "event-both" / "cam_b" / "clip.m4a").exists()
        assert (self.root / "event-both" / "omni_trace.json.gz").exists()

    def test_ref_frame_saved_alongside_clip(self):
        """Smart Crop:ref_frames → 落 {device}/ref.jpg,与 clip 同目录;返回值只计 clip."""
        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        artifacts = OmniEventArtifacts(
            clips={"cam_a": (b"crop-video", "mp4")},
            ref_frames={"cam_a": jpg},
        )
        clip_dids = save_event_artifacts("event-ref", artifacts)
        assert clip_dids == ["cam_a"]  # ref 不进 clip_dids
        assert (self.root / "event-ref" / "cam_a" / "clip.mp4").exists()
        assert (self.root / "event-ref" / "cam_a" / "ref.jpg").read_bytes() == jpg

    def test_ref_frame_only(self):
        """只有 ref_frames(无 clip/trace/gallery)→ 仍落 ref.jpg,不因空 guard 早退."""
        jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        artifacts = OmniEventArtifacts(ref_frames={"cam_a": jpg})
        clip_dids = save_event_artifacts("event-ref-only", artifacts)
        assert clip_dids == []
        assert (self.root / "event-ref-only" / "cam_a" / "ref.jpg").read_bytes() == jpg

    def test_ref_frame_empty_bytes_skipped(self):
        """ref 字节为空 → 跳过,不落空文件."""
        artifacts = OmniEventArtifacts(ref_frames={"cam_a": b""})
        save_event_artifacts("event-ref-empty", artifacts)
        assert not (self.root / "event-ref-empty" / "cam_a" / "ref.jpg").exists()

    # ── 图像推理路径:整组帧 ──────────────────────────────────────────────

    def test_frames_saved_as_group(self):
        """frames → 逐张落 frame_000/001/002.jpg,返回值含该 device."""
        jpegs = [b"\xff\xd8\xff\xe0" + bytes([i]) * 40 for i in range(3)]
        artifacts = OmniEventArtifacts(frames={"cam_a": jpegs})
        assert save_event_artifacts("event-frames", artifacts) == ["cam_a"]
        device_dir = self.root / "event-frames" / "cam_a"
        assert sorted(p.name for p in device_dir.iterdir()) == [
            "frame_000.jpg",
            "frame_001.jpg",
            "frame_002.jpg",
        ]
        assert (device_dir / "frame_000.jpg").read_bytes() == jpegs[0]
        assert (device_dir / "frame_002.jpg").read_bytes() == jpegs[2]

    def test_frames_device_id_slug_applied(self):
        """device_id 含 '/' → 与 clip 同款 slug 化,目录路径合法."""
        artifacts = OmniEventArtifacts(frames={"cam/living/01": [b"j" * 40]})
        assert save_event_artifacts("event-frames-slug", artifacts) == ["cam/living/01"]
        assert (self.root / "event-frames-slug" / "cam_living_01" / "frame_000.jpg").exists()

    def test_frames_empty_list_skipped(self):
        """某 device 帧列表为空 → 跳过,不建目录,不进返回值(与 clip 空字节同处置)."""
        artifacts = OmniEventArtifacts(frames={"cam_a": []})
        assert save_event_artifacts("event-frames-empty", artifacts) == []
        assert not (self.root / "event-frames-empty" / "cam_a").exists()

    def test_frames_partial_write_rolls_back_group(self):
        """整组全成才计入:第 3 张写失败 → 前两张也删掉,device 不进返回值.

        半组留在盘上比一张不留更坏:读侧按连号数帧(count_frames),半组会被数成一个
        "看似完整"的 N,而模型手里那组并不是这个 N.
        """
        jpegs = [b"j" * 40] * 3
        artifacts = OmniEventArtifacts(frames={"cam_a": jpegs})
        real_write = Path.write_bytes
        failed_name = frame_filename(2)

        def flaky_write(path, data):
            if path.name == failed_name:
                raise OSError("disk full")
            return real_write(path, data)

        with patch.object(Path, "write_bytes", flaky_write):
            assert save_event_artifacts("event-frames-rollback", artifacts) == []
        device_dir = self.root / "event-frames-rollback" / "cam_a"
        assert list(device_dir.iterdir()) == []

    def test_frames_partial_write_keeps_other_device(self):
        """一台整组回滚不牵连另一台:好设备照常落盘并出现在返回值里."""
        artifacts = OmniEventArtifacts(
            frames={"cam_bad": [b"j" * 40] * 2, "cam_ok": [b"k" * 40]},
        )
        real_write = Path.write_bytes

        def flaky_write(path, data):
            if path.parent.name == "cam_bad" and path.name == frame_filename(1):
                raise OSError("disk full")
            return real_write(path, data)

        with patch.object(Path, "write_bytes", flaky_write):
            assert save_event_artifacts("event-frames-mixed", artifacts) == ["cam_ok"]
        assert list((self.root / "event-frames-mixed" / "cam_bad").iterdir()) == []
        assert (self.root / "event-frames-mixed" / "cam_ok" / "frame_000.jpg").exists()

    def test_clips_and_frames_same_device_deduped(self):
        """clips 与 frames 正常互斥,真并存时同一 device 也不能被数两次.

        save_event_artifacts 的返回值就是事件的 snapshot_count,重复会把"落了 1 台"
        报成 2 台.
        """
        artifacts = OmniEventArtifacts(
            clips={"cam_a": (b"v" * 40, "mp4")},
            frames={"cam_a": [b"j" * 40]},
        )
        assert save_event_artifacts("event-both-media", artifacts) == ["cam_a"]
        device_dir = self.root / "event-both-media" / "cam_a"
        assert (device_dir / "clip.mp4").exists()
        assert (device_dir / "frame_000.jpg").exists()


# ─── 帧产物读取侧辅助函数 ───────────────────────────────────────────────────


class TestFrameHelpers:
    """frame_filename / locate_frame_file / count_frames / list_artifact_files.

    落盘侧写下的帧名、读取侧按连号数出来的帧数、反馈打包列出的产物名,三处必须同源
    ——任何一处单独改都会让"盘上有几张"和"它们叫什么"对不上.
    """

    def test_frame_filename_zero_pads_three_digits(self):
        """补零保证字典序 == 帧序(落盘顺序即时间序)."""
        assert frame_filename(0) == "frame_000.jpg"
        assert frame_filename(7) == "frame_007.jpg"
        assert frame_filename(15) == "frame_015.jpg"

    def test_frame_filename_beyond_999_keeps_four_digits(self):
        """超 999 帧补零溢出成 4 位:该区间字典序不再等于帧序,但 16 帧上限下不可达.

        这里只钉住"不崩、不截断",不宣称 4 位区间仍有序.
        """
        assert frame_filename(1000) == "frame_1000.jpg"

    def test_locate_frame_file_hit_and_miss(self, tmp_path):
        """命中返路径,缺号返 None(端点据此分 404 / 410)."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        assert locate_frame_file(device_dir, 0) is None
        (device_dir / frame_filename(0)).write_bytes(b"jpg")
        assert locate_frame_file(device_dir, 0) == device_dir / frame_filename(0)
        assert locate_frame_file(device_dir, 1) is None

    def test_count_frames_stops_at_first_gap(self, tmp_path):
        """连号才计数:0/1 在、2 缺、3 在 → 2(_save_frames 已保证不会留洞)."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        assert count_frames(device_dir) == 0
        for i in (0, 1, 3):
            (device_dir / frame_filename(i)).write_bytes(b"jpg")
        assert count_frames(device_dir) == 2

    def test_count_frames_ignores_ref_jpg(self, tmp_path):
        """同目录的 ref.jpg(Smart Crop 参考帧)不能被数成帧."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        (device_dir / "ref.jpg").write_bytes(b"jpg")
        assert count_frames(device_dir) == 0

    def test_count_frames_missing_dir_is_zero(self, tmp_path):
        """目录不存在(事件已被 cleanup 清掉)→ 0,不抛."""
        assert count_frames(tmp_path / "nope") == 0

    def test_list_artifact_files_prefers_clip(self, tmp_path):
        """有 clip 就先返 clip(与 locate_clip_file 同序:先 mp4 后 m4a)."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        (device_dir / "clip.m4a").write_bytes(b"a")
        assert list_artifact_files(device_dir) == ["clip.m4a"]
        (device_dir / "clip.mp4").write_bytes(b"v")
        assert list_artifact_files(device_dir) == ["clip.mp4"]

    def test_list_artifact_files_returns_frame_group(self, tmp_path):
        """无 clip 时返整组帧文件名 —— 旧口径只认 CLIP_CANDIDATES,图像模式下会把整组判 missing."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        for i in range(3):
            (device_dir / frame_filename(i)).write_bytes(b"jpg")
        assert list_artifact_files(device_dir) == [
            "frame_000.jpg",
            "frame_001.jpg",
            "frame_002.jpg",
        ]

    def test_list_artifact_files_empty_dir(self, tmp_path):
        """既无 clip 也无帧 → 空列表(调用方据此记 missing)."""
        device_dir = tmp_path / "cam_a"
        device_dir.mkdir()
        assert list_artifact_files(device_dir) == []


# ─── _save_gallery ─────────────────────────────────────────────────────────


class TestSaveGallery:
    def test_png_gets_png_extension(self, tmp_path):
        from miloco.perception.snapshot_writer import _save_gallery
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        _save_gallery(tmp_path, {"person1": {"body": png_bytes}})
        names = [p.name for p in (tmp_path / "gallery").iterdir()]
        assert any(n.endswith("_body.png") for n in names)

    def test_jpeg_gets_jpg_extension(self, tmp_path):
        from miloco.perception.snapshot_writer import _save_gallery
        jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        _save_gallery(tmp_path, {"person1": {"face": jpg_bytes}})
        names = [p.name for p in (tmp_path / "gallery").iterdir()]
        assert any(n.endswith("_face.jpg") for n in names)

    def test_mixed_formats(self, tmp_path):
        from miloco.perception.snapshot_writer import _save_gallery
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        jpg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        _save_gallery(tmp_path, {"p1": {"body": png_bytes, "face": jpg_bytes}})
        names = {p.name for p in (tmp_path / "gallery").iterdir()}
        assert "p1_body.png" in names
        assert "p1_face.jpg" in names

    def test_empty_bytes_skipped(self, tmp_path):
        from miloco.perception.snapshot_writer import _save_gallery
        _save_gallery(tmp_path, {"p1": {"body": b""}})
        assert not (tmp_path / "gallery").exists() or not any((tmp_path / "gallery").iterdir())


# ─── cleanup_snapshots ──────────────────────────────────────────────────────


class TestCleanupSnapshots:
    @pytest.fixture(autouse=True)
    def _patch_root(self, tmp_path, monkeypatch):
        monkeypatch.setattr(snapshot_writer, "get_snapshot_root", lambda: tmp_path)
        self.root = tmp_path

    def _make_event(self, event_id: str, age_days: float, size_bytes: int = 1024) -> Path:
        """造一个 event 目录,mtime 设为 age_days 天前."""
        event_dir = self.root / event_id / "cam_a"
        event_dir.mkdir(parents=True)
        f = event_dir / "0.jpg"
        f.write_bytes(b"x" * size_bytes)
        # 设 mtime
        mtime = time.time() - age_days * 86400
        os.utime(f, (mtime, mtime))
        # 同时 mtime event 顶级目录(cleanup_snapshots 用顶级 mtime)
        os.utime(self.root / event_id, (mtime, mtime))
        return event_dir

    def test_empty_root_no_op(self):
        stats = cleanup_snapshots(ttl_days=7, max_disk_mb=5000)
        assert stats == {"deleted_by_ttl": 0, "deleted_by_lru": 0, "remaining_mb": 0}

    def test_ttl_deletes_old_events(self):
        self._make_event("old-event", age_days=10)  # > 7d
        self._make_event("fresh-event", age_days=1)  # < 7d
        stats = cleanup_snapshots(ttl_days=7, max_disk_mb=5000)
        assert stats["deleted_by_ttl"] == 1
        assert not (self.root / "old-event").exists()
        assert (self.root / "fresh-event").exists()

    def test_lru_evicts_oldest(self):
        """5 个事件每个 ~2MB,max=5MB → LRU 删最旧 ~3 个."""
        mb = 1024 * 1024
        for i in range(5):
            self._make_event(f"e{i}", age_days=i, size_bytes=2 * mb)
        stats = cleanup_snapshots(ttl_days=30, max_disk_mb=5)
        # 总共 ~10MB,留下 ≤ 5MB,删了至少 3 个
        assert stats["deleted_by_lru"] >= 3
        # 最新(age=0)应保留
        assert (self.root / "e0").exists()
        # 最旧(age=4)应被删
        assert not (self.root / "e4").exists()

    def test_ttl_runs_before_lru(self):
        """TTL 已删的不算入 LRU."""
        mb = 1024 * 1024
        # 5 个都 10 天前 → 全被 TTL 删,LRU 阶段没事干
        for i in range(5):
            self._make_event(f"e{i}", age_days=10, size_bytes=2 * mb)
        stats = cleanup_snapshots(ttl_days=7, max_disk_mb=5)
        assert stats["deleted_by_ttl"] == 5
        assert stats["deleted_by_lru"] == 0
