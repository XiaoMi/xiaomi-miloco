"""注册路径把真名写进 meta.json 的回归护栏（单图端点 + commit 兜底）。

背景：感知层 list_persons() / omni gallery 的姓名来自 meta.json。若注册只落 body 图、
不写 meta name，gallery 标签退化成 UUID（表现为"认不出已注册成员"）。本测试守两条缺口：
- 单图端点 register_sample：过去漏传 name → 断言它现在从 SQL 取真名写进 meta。
- commit 按 member_id 绑定且未带 member_name 时不写 meta name → _ensure_meta_name_from_sql
  从 SQL 兜底；断言"缺失即补、已有不覆盖、SQL 无对应行则安全不动"。

沿用 samples_batch 测试的做法：不引 TestClient，直接 await 路由协程 / 调 helper，
用真实 IdentityLibrary(tmp_path) + 桩掉模块级 manager。
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity.library import IdentityLibrary
from miloco.person import router as prouter
from miloco.person.router import _ensure_meta_name_from_sql, register_sample

_PID = "22222222-2222-4222-8222-222222222222"
_NAME = "朱小朱"


def _jpeg_bytes(seed: int = 0) -> bytes:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class _Upload:
    """最小 UploadFile 桩：register_sample 只用到 async read() 与 filename。"""

    def __init__(self, data: bytes, filename: str = "body.jpg"):
        self._data = data
        self.filename = filename

    async def read(self) -> bytes:
        return self._data


def _patch_manager(monkeypatch, *, persons: list) -> None:
    """把模块级 manager 换成桩：person_service.{exists,list_persons,get_person} +
    perception_service.get_reid_extractor。persons 里每项需带 id / name（可带 role）。"""
    by_id = {p.id: p for p in persons}
    monkeypatch.setattr(
        prouter, "manager",
        SimpleNamespace(
            person_service=SimpleNamespace(
                exists=lambda person_id: person_id in by_id,
                list_persons=lambda: list(persons),
                get_person=lambda person_id: by_id.get(person_id),
            ),
            perception_service=SimpleNamespace(get_reid_extractor=lambda: None),
        ),
    )


@pytest.fixture
def lib(tmp_path, monkeypatch) -> IdentityLibrary:
    library = IdentityLibrary(tmp_path / "identity_lib")
    monkeypatch.setattr(prouter, "_get_identity_library", lambda: library)
    return library


# ─── 单图端点：注册后 meta.json 必须有真名 ───────────────────────────────────


async def test_register_sample_writes_meta_name(lib: IdentityLibrary, monkeypatch):
    _patch_manager(monkeypatch, persons=[SimpleNamespace(id=_PID, name=_NAME, role=None)])
    # 直接调协程绕过 FastAPI，File/Form 默认值仍是 marker 对象——face_image/source 必须显式传，
    # 否则 File(None) 是 truthy、会命中 face_image.filename 崩溃。
    res = await register_sample(
        _PID, body_image=_Upload(_jpeg_bytes()), face_image=None,
        source="user_upload", current_user="t",
    )
    assert res.code == 0
    # 关键护栏：单图登记后 meta.json 能读到真名，否则 omni gallery 退化成 UUID。
    assert lib.get_name(_PID) == _NAME


# ─── commit 兜底：_ensure_meta_name_from_sql ────────────────────────────────


def _seed_nameless_person(lib: IdentityLibrary) -> None:
    """模拟"有 body 样本目录、但 meta.json 无 name"的坏状态（member_id 绑定漏写 name）。"""
    body = np.zeros((64, 64, 3), dtype=np.uint8)
    assert lib.add_tier_a_sample(_PID, body_crop=body)  # 不传 name → meta 无 name
    assert lib.get_name(_PID) is None


def test_ensure_meta_name_backfills_when_missing(lib: IdentityLibrary, monkeypatch):
    _seed_nameless_person(lib)
    _patch_manager(monkeypatch, persons=[SimpleNamespace(id=_PID, name=_NAME, role=None)])
    _ensure_meta_name_from_sql(_PID)
    assert lib.get_name(_PID) == _NAME


def test_ensure_meta_name_no_overwrite_when_present(lib: IdentityLibrary, monkeypatch):
    body = np.zeros((64, 64, 3), dtype=np.uint8)
    assert lib.add_tier_a_sample(_PID, body_crop=body, name="原名")
    # SQL 里是另一个名字，但 meta 已有 name → 不覆盖（漂移交给 meta_sync / update_person）。
    _patch_manager(monkeypatch, persons=[SimpleNamespace(id=_PID, name=_NAME, role=None)])
    _ensure_meta_name_from_sql(_PID)
    assert lib.get_name(_PID) == "原名"


def test_ensure_meta_name_noop_when_sql_missing(lib: IdentityLibrary, monkeypatch):
    _seed_nameless_person(lib)
    _patch_manager(monkeypatch, persons=[])  # SQL 无对应行（orphan）→ 无从补，安全不动。
    _ensure_meta_name_from_sql(_PID)
    assert lib.get_name(_PID) is None
