from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

from miloco.perception.engine.api import PerceptionEngine
from miloco.perception.engine.identity.model_resources import TrackingModelResources
from miloco.perception.engine.identity.tracking_service import (
    DeepSortTrackingService,
)


def _session_for(model_path: str):
    session = MagicMock(name=model_path)
    if model_path.endswith("det_4C.onnx"):
        session.get_inputs.return_value = [
            SimpleNamespace(name="images", shape=[1, 3, 640, 640])
        ]
        session.get_outputs.return_value = [SimpleNamespace(name="output0")]
    else:
        session.get_inputs.return_value = [
            SimpleNamespace(name="input", shape=[1, 3, 192, 96])
        ]
        session.get_outputs.return_value = [
            SimpleNamespace(name="head/out_emb:0")
        ]
    return session


def test_session_created_once_under_concurrent_first_access(monkeypatch, tmp_path):
    created = []

    def fake_make_session(model_path, **kwargs):
        created.append((model_path, kwargs))
        return _session_for(model_path)

    monkeypatch.setattr(
        "miloco.perception.inference.ort_utils.make_session",
        fake_make_session,
    )
    resources = TrackingModelResources(use_gpu=True, num_threads=2)
    model_path = str(tmp_path / "det_4C.onnx")

    with ThreadPoolExecutor(max_workers=8) as executor:
        sessions = list(executor.map(resources.get_session, [model_path] * 32))

    assert all(session is sessions[0] for session in sessions)
    assert len(created) == 1
    assert created[0][1] == {"use_gpu": True, "num_threads": 2}
    assert resources.session_count == 1

    resources.release()
    assert resources.session_count == 0


def test_two_cameras_share_sessions_but_not_trackers(monkeypatch, tmp_path):
    created = []

    def fake_make_session(model_path, **kwargs):
        created.append(model_path)
        return _session_for(model_path)

    monkeypatch.setattr(
        "miloco.perception.inference.ort_utils.make_session",
        fake_make_session,
    )
    resources = TrackingModelResources()

    first = DeepSortTrackingService(
        model_dir=str(tmp_path),
        model_resources=resources,
    )
    second = DeepSortTrackingService(
        model_dir=str(tmp_path),
        model_resources=resources,
    )

    assert first._detector.session is second._detector.session
    assert first.tracker.human_reid.session is second.tracker.human_reid.session
    assert first.tracker is not second.tracker
    assert first.tracker._mot is not second.tracker._mot
    assert len(created) == 2
    assert resources.session_count == 2

    first.release()
    assert resources.session_count == 2
    second.release()
    resources.release()


async def test_engine_close_releases_wrappers_before_shared_sessions():
    events = []

    class _IdentityEngine:
        async def close(self):
            events.append("identity")

    class _TrackingService:
        def release(self):
            events.append("tracking")

    class _Fallback:
        def release(self):
            events.append("fallback")

    class _Resources:
        def release(self):
            events.append("resources")

    engine = SimpleNamespace(
        _tierc_clear_task=None,
        _identity_engines={"cam": _IdentityEngine()},
        _tracking_services={"cam": _TrackingService()},
        _deep_sort_trackers={"cam": object()},
        _fallback_human_reid=_Fallback(),
        _tracking_model_resources=_Resources(),
    )

    await PerceptionEngine.close(engine)

    assert events == ["identity", "tracking", "fallback", "resources"]
    assert engine._tracking_services == {}
    assert engine._deep_sort_trackers == {}
    assert engine._fallback_human_reid is None
