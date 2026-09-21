"""Tests for deterministic per-camera perception window staggering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from miloco.perception.collect.camera_adapter import (
    CameraDeviceAdapter,
    _CameraDeviceState,
    _window_phase_offsets,
)
from miloco.perception.collect.stream_buffer import MultiTrackSyncBuffer


def _state(did: str, window_ms: int = 10_000) -> _CameraDeviceState:
    return _CameraDeviceState(
        did=did,
        sync_buffer=MultiTrackSyncBuffer(["video"], window_ms=window_ms),
    )


def test_stream_buffer_default_phase_preserves_existing_boundaries():
    buf = MultiTrackSyncBuffer(["video"], window_ms=100, window_settle_ms=0)
    buf.put("video", b"partial", 10, 10)
    buf.put("video", b"wanted", 110, 110)
    buf.put("video", b"trigger", 220, 220)

    ready = buf.drain_ready()

    assert ready is not None
    assert ready.start_ms == 100
    assert ready.end_ms == 200


def test_stream_buffer_phase_shifts_boundaries_without_changing_period():
    buf = MultiTrackSyncBuffer(
        ["video"],
        window_ms=100,
        phase_offset_ms=25,
        window_settle_ms=0,
    )
    buf.put("video", b"partial", 30, 30)
    buf.put("video", b"wanted", 130, 130)
    buf.put("video", b"trigger", 240, 240)

    ready = buf.drain_ready()

    assert ready is not None
    assert ready.start_ms == 125
    assert ready.end_ms == 225


@pytest.mark.parametrize("phase", [-1, 100])
def test_stream_buffer_rejects_phase_outside_window(phase: int):
    with pytest.raises(ValueError, match="phase_offset_ms"):
        MultiTrackSyncBuffer(["video"], window_ms=100, phase_offset_ms=phase)


def test_phase_change_clears_old_alignment_only_when_value_changes():
    buf = MultiTrackSyncBuffer(["video"], window_ms=100)
    buf.put("video", b"partial", 10, 10)
    assert buf.window_count == 1

    assert buf.set_phase_offset_ms(25) is True
    assert buf.phase_offset_ms == 25
    assert buf.window_count == 0

    buf.put("video", b"new", 30, 30)
    assert buf.set_phase_offset_ms(25) is False
    assert buf.window_count == 1


def test_phase_change_reports_discarded_windows():
    buf = MultiTrackSyncBuffer(["video"], window_ms=100)
    buf.put("video", b"partial", 10, 10)

    assert buf.set_phase_offset_ms(25) is True
    assert buf.consume_drop_stats() == (1, 0, 0, "phase_realign")


def test_four_camera_offsets_are_sorted_and_evenly_spaced():
    offsets = _window_phase_offsets(
        ["cam-d", "cam-b", "cam-a", "cam-c"],
        window_ms=10_000,
        enabled=True,
    )

    assert offsets == {
        "cam-a": 0,
        "cam-b": 2_500,
        "cam-c": 5_000,
        "cam-d": 7_500,
    }


def test_disabled_offsets_preserve_legacy_alignment():
    offsets = _window_phase_offsets(
        ["cam-b", "cam-a"],
        window_ms=10_000,
        enabled=False,
    )

    assert offsets == {"cam-a": 0, "cam-b": 0}


def test_adapter_rebalances_connected_devices_from_live_settings(monkeypatch):
    collect = SimpleNamespace(window_size=10, stagger_devices=True)
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter.get_settings",
        lambda: SimpleNamespace(perception=SimpleNamespace(collect=collect)),
    )
    adapter = CameraDeviceAdapter(miot_proxy=object())  # type: ignore[arg-type]
    adapter._devices = {
        "cam-c": _state("cam-c"),
        "cam-a": _state("cam-a"),
        "cam-b": _state("cam-b"),
        "cam-d": _state("cam-d"),
    }

    changed = adapter._rebalance_window_phases()

    assert changed == 3
    assert {
        did: state.sync_buffer.phase_offset_ms
        for did, state in adapter._devices.items()
    } == {
        "cam-a": 0,
        "cam-b": 2_500,
        "cam-c": 5_000,
        "cam-d": 7_500,
    }

    assert adapter._rebalance_window_phases() == 0

    adapter._devices.pop("cam-d")
    assert adapter._rebalance_window_phases() == 2
    assert {
        did: state.sync_buffer.phase_offset_ms
        for did, state in adapter._devices.items()
    } == {
        "cam-a": 0,
        "cam-b": 3_333,
        "cam-c": 6_666,
    }
