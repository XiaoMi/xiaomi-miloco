"""``/samples/batch`` 端点超容量契约单测。

前端兜底(EnrollFlow + register.ts::saveSamplesBatch)依赖一个后端约定:即便部分
item 因每类容量上限(``tier_a_max // 2`` = 5)写入失败,端点仍返回 ``code=0``
(HTTP 200)、把失败项列进 ``data.failed``、``written_*`` 计数如实。前端据此在
``failed`` 非空时提示"有 N 张未保存"。

本测试就是这层契约的护栏:后端若改成超容量抛错、或不再返回 ``failed`` 字段,
前端的"部分失败提示"会静默失效——届时这里先红。

不引入 TestClient(现 codebase 不 mock 重链路):直接 await 路由协程,用真实
``IdentityLibrary(tmp_path)`` + 桩掉 person 存在性检查。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from miloco.perception.engine.identity.library import IdentityLibrary
from miloco.person import router as prouter
from miloco.person.router import SampleBatchPayload, register_sample_batch

_PID = "22222222-2222-4222-8222-222222222222"


def _jpeg_b64(seed: int = 0) -> str:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture
def lib(tmp_path, monkeypatch) -> IdentityLibrary:
    library = IdentityLibrary(tmp_path / "identity_lib")
    monkeypatch.setattr(prouter, "_get_identity_library", lambda: library)
    # 端点用到 manager.person_service.{exists,get_person} + perception_service.
    # get_reid_extractor;person_service 是只读 property、不能直接 setattr,故整体把
    # 模块级 manager 替换成桩。get_person 按 id 返带 name 的 person 供 batch 查 name;
    # get_reid_extractor 返 None(本测试只验容量契约,不验 ReID/name 写入)。
    monkeypatch.setattr(
        prouter, "manager",
        SimpleNamespace(
            person_service=SimpleNamespace(
                exists=lambda person_id: True,
                get_person=lambda person_id: (
                    SimpleNamespace(id=_PID, name="测试") if person_id == _PID else None
                ),
            ),
            perception_service=SimpleNamespace(get_reid_extractor=lambda: None),
        ),
    )
    return library


def _payload(types: list[str]) -> SampleBatchPayload:
    return SampleBatchPayload(
        items=[{"type": t, "image_b64": _jpeg_b64(i)} for i, t in enumerate(types)]
    )


async def test_over_cap_body_reports_failed_but_code_0(lib: IdentityLibrary):
    cap = lib.tier_a_max // 2  # 5
    res = await register_sample_batch(_PID, _payload(["body"] * (cap + 2)), current_user="t")
    assert res.code == 0                               # HTTP 层仍 200
    assert res.data["written_body"] == cap             # 只写满 cap
    assert len(res.data["failed"]) == 2                # 超出的 2 张计入 failed
    assert all("index" in f and "reason" in f for f in res.data["failed"])


async def test_under_cap_all_written_no_failed(lib: IdentityLibrary):
    res = await register_sample_batch(_PID, _payload(["body", "body", "face", "face"]), current_user="t")
    assert res.code == 0
    assert res.data["written_body"] == 2
    assert res.data["written_face"] == 2
    assert res.data["failed"] == []


async def test_body_and_face_capped_independently(lib: IdentityLibrary):
    cap = lib.tier_a_max // 2
    res = await register_sample_batch(
        _PID, _payload(["body"] * (cap + 1) + ["face"] * (cap + 1)), current_user="t",
    )
    assert res.code == 0
    assert res.data["written_body"] == cap
    assert res.data["written_face"] == cap
    assert len(res.data["failed"]) == 2  # body 超 1 + face 超 1


async def test_decode_failure_counted_in_failed(lib: IdentityLibrary):
    # 合法 base64 但不是图片 → cv2.imdecode 返 None → 计入 failed,不阻断其余。
    bad = base64.b64encode(b"not an image").decode()
    payload = SampleBatchPayload(items=[
        {"type": "body", "image_b64": _jpeg_b64(0)},
        {"type": "body", "image_b64": bad},
    ])
    res = await register_sample_batch(_PID, payload, current_user="t")
    assert res.code == 0
    assert res.data["written_body"] == 1
    assert len(res.data["failed"]) == 1
    assert res.data["failed"][0]["index"] == 1
