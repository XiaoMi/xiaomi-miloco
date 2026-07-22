"""
Multimodal collector — manages device adapters and provides unified collection.

Holds adapters for each device type, delegates device sync and data collection.
Both realtime and active perception use collect_batch() as the unified entry point.
"""

from __future__ import annotations

import logging

from miloco.node_monitor import Lifecycle, NodeName, get_monitor
from miloco.perception.collect.adapter_base import BaseDeviceAdapter
from miloco.perception.schema import DeviceData, PerceptionBatch
from miloco.perception.types import PerceptionDevice

logger = logging.getLogger(__name__)


def _pack_batch_latency_aggregates(batch: PerceptionBatch) -> None:
    """Fill batch-level decode averages from each DeviceData.

    Each DeviceData already carries per-window weighted means (packed in
    its adapter). This pass does one more reduction across devices using
    the same frame-count-weighted rule. The result lives on the batch so
    the processor can copy it into PerceptionLatency verbatim.
    """
    v_count = a_count = total = 0
    v_dec_sum = a_dec_sum = 0.0

    for dd in batch.devices.values():
        v = len(dd.video)
        a = len(dd.audio)
        v_count += v
        a_count += a
        total += v + a
        v_dec_sum += dd.decode_video_avg_ms * v
        a_dec_sum += dd.decode_audio_avg_ms * a

    def _avg(s: float, c: int) -> float:
        return (s / c) if c else 0.0

    batch.video_frame_count = v_count
    batch.audio_frame_count = a_count
    batch.decode_video_avg_ms = _avg(v_dec_sum, v_count)
    batch.decode_audio_avg_ms = _avg(a_dec_sum, a_count)
    batch.decode_avg_ms = _avg(v_dec_sum + a_dec_sum, total)

    recv_values: list[int] = []
    for dd in batch.devices.values():
        for vf in dd.video:
            if vf.recv_unix_ms > 0:
                recv_values.append(vf.recv_unix_ms)
        for af in dd.audio:
            if af.recv_unix_ms > 0:
                recv_values.append(af.recv_unix_ms)
    batch.window_first_frame_recv_ms = min(recv_values) if recv_values else None


class MultimodalCollector:
    """Unified multimodal data collection across all device types."""

    def __init__(self, adapters: list[BaseDeviceAdapter]):
        self._adapters: dict[str, BaseDeviceAdapter] = {
            adapter.device_type: adapter for adapter in adapters
        }

    async def sync_all_devices(self, all_devices: dict | None = None) -> None:
        """Notify all adapters to sync their devices (hot-plug).

        Lifecycle: STARTING → READY (success) / FAILED (异常)。
        已活跃的不动(_sync_devices_loop 周期触发时不重置)。

        Args:
            all_devices: Optional full device dict. Each adapter filters its type.
                         If None, each adapter queries its own device type.
        """
        mon = get_monitor()
        mon.set_lifecycle(NodeName.COLLECTOR, Lifecycle.STARTING)

        try:
            for adapter in self._adapters.values():
                await adapter.sync_devices(all_devices)
        except Exception as e:
            state = mon.get_state(NodeName.COLLECTOR)
            if state and state.lifecycle == Lifecycle.STARTING:
                mon.set_lifecycle(NodeName.COLLECTOR, Lifecycle.FAILED, error=repr(e))
            raise

        state = mon.get_state(NodeName.COLLECTOR)
        if state and state.lifecycle == Lifecycle.STARTING:
            mon.set_lifecycle(NodeName.COLLECTOR, Lifecycle.READY)

    def collect(self, did: str, *, drain: bool = True) -> DeviceData | None:
        """Collect multimodal data from a specific device.

        Args:
            did: Device ID.
            drain: If True, consume data from buffer. If False, peek a copy.
        """
        for adapter in self._adapters.values():
            if did in adapter.get_connected_devices():
                return adapter.collect(did, drain=drain)
        return None

    def peek_latest_frame(self, did: str):
        """非破坏性取该 did 最近一帧解码图(numpy BGR);无则 None。

        供 tier_c 闲时定期清的 live 检测(gate 关停时正常 pipeline 不取帧)。
        仅 camera 类 adapter 实现 ``peek_latest_frame``,其它类型返 None。
        """
        for adapter in self._adapters.values():
            if did in adapter.get_connected_devices():
                fn = getattr(adapter, "peek_latest_frame", None)
                return fn(did) if fn is not None else None
        return None

    def collect_batch(
        self, dids: list[str] | None = None, *, drain: bool = True
    ) -> PerceptionBatch:
        """Collect and assemble a PerceptionBatch from multiple devices.

        Args:
            dids: Specific device IDs to collect. If None, collects from all
                  active sources.
            drain: If True (realtime), consume data from buffers — each
                   fragment is processed exactly once.
                   If False (active query), peek a copy without consuming
                   so the realtime pipeline can still drain it later.

        Returns:
            PerceptionBatch with devices grouped by did, empty devices filtered out.
        """
        with get_monitor().track(NodeName.COLLECTOR, "batch") as h:
            batch = PerceptionBatch()

            target_dids = dids if dids else list(self.get_all_active_sources())

            for did in target_dids:
                device_data = self.collect(did, drain=drain)
                if device_data and device_data.has_data:
                    batch.devices[did] = device_data

            _pack_batch_latency_aggregates(batch)
            if batch.empty:
                h.skip_rolling()
            elif batch.end_timestamp and batch.start_timestamp:
                h.add_window_ms(batch.end_timestamp - batch.start_timestamp)
            return batch

    def get_all_active_sources(self) -> dict[str, PerceptionDevice]:
        """Get all currently connected devices across all adapters."""
        result: dict[str, PerceptionDevice] = {}
        for adapter in self._adapters.values():
            result.update(adapter.get_connected_devices())
        return result

    def clear_all_buffers(self) -> None:
        """Clear all stream buffers across all adapters.

        Devices remain connected — only buffered data is discarded.
        New data arriving after this call will start fresh.
        """
        for adapter in self._adapters.values():
            adapter.clear_buffers()
        logger.info("All device buffers cleared")

    def get_adapter(self, device_type: str) -> BaseDeviceAdapter | None:
        """Get adapter for a specific device type."""
        return self._adapters.get(device_type)

    def pause_streams(self) -> None:
        """暂停所有设备的数据流（解码器等）。设备保持连接，数据直接丢弃。"""
        for adapter in self._adapters.values():
            adapter.pause_streams()

    def resume_streams(self) -> None:
        """恢复所有设备的数据流。"""
        for adapter in self._adapters.values():
            adapter.resume_streams()

    async def shutdown(self) -> None:
        """Shutdown all adapters — disconnect all devices."""
        for adapter in self._adapters.values():
            await adapter.shutdown()
        logger.info("All device adapters shut down")
        get_monitor().set_lifecycle(NodeName.COLLECTOR, Lifecycle.STOPPED)
