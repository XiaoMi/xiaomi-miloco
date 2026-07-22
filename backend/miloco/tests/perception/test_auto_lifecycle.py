# -*- coding: utf-8 -*-
"""Tests for PerceptionRunner auto lifecycle management.

Covers:
- _auto_manage_lifecycle: stop/resume decisions based on circuit breaker state
- _auto_stop_engine: pauses decoders, closes pipeline, does NOT shutdown collector
- _auto_restart_engine: resumes decoders, rebuilds engine
- _drive_recovery_probe: arms probe only in OPEN_RECOVERABLE
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from miloco.perception.engine.omni.circuit_breaker import (
    CircuitState,
    OmniCircuitBreaker,
)
from miloco.perception.runner import PerceptionRunner


@pytest.fixture
def mock_runner():
    """Create a PerceptionRunner with mocked dependencies."""
    collector = MagicMock()
    collector.pause_streams = MagicMock()
    collector.resume_streams = MagicMock()
    collector.shutdown = AsyncMock()
    collector.sync_all_devices = AsyncMock()
    collector.get_all_active_sources = MagicMock(return_value={})

    pipeline = MagicMock()
    pipeline.close = AsyncMock()
    pipeline.try_reinit_engine = MagicMock()
    pipeline.set_inference_worker = MagicMock()
    pipeline.drive_omni_probe = MagicMock()
    pipeline.process_realtime = AsyncMock(return_value=None)
    pipeline.last_latency = None
    pipeline.engine_ready = False
    pipeline.engine_status = "not_configured"
    pipeline.engine_status_message = ""

    log_repo = MagicMock()
    log_repo.get_today_inference_count = MagicMock(return_value=0)

    runner = PerceptionRunner.__new__(PerceptionRunner)
    runner._collector = collector
    runner._pipeline = pipeline
    runner._log_repo = log_repo
    runner._is_running = True
    runner._perception_task = None
    runner._sync_devices_task = None
    runner._recovery_probe_task = None
    runner._auto_stopped = False
    runner._auto_stopped_at = None
    runner._auto_restart_cooldown_sec = 120.0
    runner._cb_open_accumulated = 0.0
    runner._cb_last_open_tick = None
    runner._window_ready = None
    runner._inference_worker = MagicMock()
    runner._inference_worker.is_running = False
    runner._inference_worker.start = MagicMock()

    return runner


@pytest.fixture
def mock_settings(monkeypatch):
    """Mock settings with auto_stop enabled."""
    settings = MagicMock()
    settings.perception.collect.auto_stop_on_omni_failure = True
    settings.perception.collect.auto_stop_threshold_sec = 60.0
    settings.perception.collect.window_size = 4
    monkeypatch.setattr("miloco.perception.runner.get_settings", lambda: settings)
    return settings


@pytest.fixture
def cb(monkeypatch):
    """Create a real circuit breaker and patch it everywhere it's imported."""
    real_cb = OmniCircuitBreaker()
    # Patch at the source module so lazy imports resolve correctly
    monkeypatch.setattr(
        "miloco.perception.engine.omni.circuit_breaker._INSTANCE",
        real_cb,
    )
    return real_cb


# ---- _auto_manage_lifecycle tests ----


@pytest.mark.asyncio
async def test_auto_manage_disabled_by_config(mock_runner, cb, monkeypatch):
    """When auto_stop_on_omni_failure=False, lifecycle management is skipped."""
    settings = MagicMock()
    settings.perception.collect.auto_stop_on_omni_failure = False
    monkeypatch.setattr("miloco.perception.runner.get_settings", lambda: settings)

    await mock_runner._auto_manage_lifecycle()
    assert mock_runner._auto_stopped is False


@pytest.mark.asyncio
async def test_auto_manage_closed_stops_accumulating(mock_runner, cb, mock_settings):
    """CLOSED state stops accumulating but preserves existing time."""
    mock_runner._cb_open_accumulated = 30.0
    mock_runner._cb_last_open_tick = time.monotonic()
    mock_runner._auto_stopped = False

    # cb is CLOSED by default
    await mock_runner._auto_manage_lifecycle()

    # accumulated preserved, last_open_tick cleared
    assert mock_runner._cb_open_accumulated == 30.0
    assert mock_runner._cb_last_open_tick is None
    assert mock_runner._auto_stopped is False


@pytest.mark.asyncio
async def test_auto_manage_open_accumulates_time(mock_runner, cb, mock_settings):
    """OPEN_RECOVERABLE accumulates time."""
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "test", ErrorCategory.RECOVERABLE)
        )

    assert cb.current_state == CircuitState.OPEN_RECOVERABLE
    assert mock_runner._cb_open_accumulated == 0.0

    await mock_runner._auto_manage_lifecycle()

    # Should have started accumulating
    assert mock_runner._cb_last_open_tick is not None
    assert mock_runner._auto_stopped is False


@pytest.mark.asyncio
async def test_auto_manage_open_exceeds_threshold_stops_engine(
    mock_runner, cb, mock_settings
):
    """OPEN_RECOVERABLE persisting beyond threshold triggers auto-stop."""
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "test", ErrorCategory.RECOVERABLE)
        )

    # Simulate accumulated OPEN time exceeding threshold
    mock_runner._cb_open_accumulated = 100.0  # > 60s threshold
    mock_runner._cb_last_open_tick = time.monotonic()

    await mock_runner._auto_manage_lifecycle()

    assert mock_runner._auto_stopped is True
    mock_runner._collector.pause_streams.assert_called_once()
    mock_runner._pipeline.close.assert_called_once()
    # collector.shutdown should NOT be called (fix for sync loop race)
    mock_runner._collector.shutdown.assert_not_called()


@pytest.mark.asyncio
async def test_auto_manage_auto_stopped_and_closed_restarts(
    mock_runner, cb, mock_settings
):
    """When auto-stopped, circuit breaker CLOSED, and cooldown expired, engine restarts."""
    mock_runner._auto_stopped = True
    mock_runner._auto_stopped_at = time.monotonic() - 200  # cooldown (120s) expired

    # cb is CLOSED by default
    await mock_runner._auto_manage_lifecycle()

    assert mock_runner._auto_stopped is False
    mock_runner._collector.resume_streams.assert_called_once()
    mock_runner._pipeline.try_reinit_engine.assert_called_once()


@pytest.mark.asyncio
async def test_auto_manage_auto_stopped_and_open_drives_probe(
    mock_runner, cb, mock_settings, monkeypatch
):
    """When auto-stopped and circuit breaker is OPEN, drives recovery probe."""
    from miloco.perception.engine.omni.error_classifier import (
        ClassifiedError,
        ErrorCategory,
    )

    for _ in range(3):
        await cb.record_failure(
            ClassifiedError("unreachable", "test", ErrorCategory.RECOVERABLE)
        )

    mock_runner._auto_stopped = True

    # Force backoff expired so try_arm_probe returns True
    cb._next_probe_at_monotonic = time.monotonic() - 1

    # Mock _run_omni_probe to avoid actual HTTP call
    mock_probe = AsyncMock()
    monkeypatch.setattr("miloco.perception.processor._run_omni_probe", mock_probe)

    await mock_runner._auto_manage_lifecycle()

    # probe task should have been created
    assert mock_runner._recovery_probe_task is not None
    assert mock_runner._auto_stopped is True  # still stopped until probe succeeds


# ---- _auto_stop_engine tests ----


@pytest.mark.asyncio
async def test_auto_stop_does_not_shutdown_collector(mock_runner):
    """Auto-stop pauses decoders but does NOT shutdown collector."""
    await mock_runner._auto_stop_engine()

    mock_runner._collector.pause_streams.assert_called_once()
    mock_runner._pipeline.close.assert_called_once()
    # collector.shutdown should NOT be called
    mock_runner._collector.shutdown.assert_not_called()
    assert mock_runner._auto_stopped is True
    assert mock_runner._auto_stopped_at is not None


@pytest.mark.asyncio
async def test_cooldown_prevents_immediate_restart(mock_runner, cb, mock_settings):
    """CLOSED within cooldown period should NOT restart."""
    mock_runner._auto_stopped = True
    mock_runner._auto_stopped_at = time.monotonic() - 10  # only10s, cooldown is120s

    # cb is CLOSED
    await mock_runner._auto_manage_lifecycle()

    # Should still be stopped (cooldown not expired)
    assert mock_runner._auto_stopped is True
    mock_runner._collector.resume_streams.assert_not_called()


# ---- _auto_restart_engine tests ----


@pytest.mark.asyncio
async def test_auto_restart_resumes_streams(mock_runner):
    """Auto-restart resumes decoders and rebuilds engine."""
    mock_runner._auto_stopped = True

    await mock_runner._auto_restart_engine()

    mock_runner._collector.resume_streams.assert_called_once()
    mock_runner._collector.sync_all_devices.assert_called_once()
    mock_runner._pipeline.try_reinit_engine.assert_called_once_with(
        include_failed=True
    )
    assert mock_runner._auto_stopped is False


@pytest.mark.asyncio
async def test_auto_restart_failure_keeps_stopped(mock_runner):
    """If restart fails, _auto_stopped stays True."""
    mock_runner._auto_stopped = True
    mock_runner._collector.sync_all_devices = AsyncMock(
        side_effect=Exception("sync failed")
    )

    await mock_runner._auto_restart_engine()

    assert mock_runner._auto_stopped is True
