"""
Base device adapter — abstract interface for device type capability modules.

Each device type (camera, speaker, etc.) implements this interface to provide:
1. Device discovery — find/filter devices of this type
2. Stream management — subscribe/unsubscribe raw multimodal streams
3. Data collection — produce DeviceData from stream buffers
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from miloco.node_monitor import Lifecycle, NodeName, get_monitor
from miloco.perception.schema import DeviceData
from miloco.perception.types import PerceptionDevice

logger = logging.getLogger(__name__)


class BaseDeviceAdapter(ABC):
    """Device type capability module base class."""

    device_type: str  # Subclass must define: "camera", "speaker", etc.
    _node_name: NodeName | None = None  # Subclass sets if it owns a node_monitor node.

    @abstractmethod
    async def discover_devices(
        self, all_devices: dict | None = None, online_only: bool = True, cap: bool = True
    ) -> dict[str, PerceptionDevice]:
        """Discover devices of this type.

        Args:
            all_devices: If provided, filter this type from the full device dict.
                         If None, query MIoT directly for this device type.
            online_only: If True (default), only return online devices.
                         If False, return all discovered devices regardless of
                         online status.
            cap: If True (default), truncate to the device type's feed limit
                 (camera: MAX_ENABLED_CAMERAS). Pass False for "list full set"
                 callers (e.g. rule target validation). Adapters without a feed
                 limit ignore it.

        Returns:
            {did: PerceptionDevice} for devices of this type.
        """

    @abstractmethod
    async def connect_device(
        self, did: str, source: PerceptionDevice | None = None
    ) -> None:
        """Connect to a device and subscribe to all supported modality streams.

        Args:
            did: Device ID to connect.
            source: Pre-resolved device metadata. If provided, the adapter can
                skip a redundant discover_devices() call.
        """

    @abstractmethod
    async def disconnect_device(self, did: str) -> None:
        """Disconnect from a device, unsubscribe all streams, clear buffers."""

    @abstractmethod
    def collect(self, did: str, *, drain: bool = True) -> DeviceData | None:
        """Collect multimodal data from the device's stream buffers.

        Args:
            did: Device ID to collect from.
            drain: If True, consume data from the buffer (realtime pipeline).
                   If False, peek a copy without consuming (active queries).
        """

    @abstractmethod
    def get_connected_devices(self) -> dict[str, PerceptionDevice]:
        """Get currently connected devices of this type."""

    def clear_buffers(self) -> None:
        """Clear all stream buffers for connected devices.

        Override in subclasses that maintain stream buffers.
        """

    async def sync_devices(self, all_devices: dict | None = None) -> None:
        """Sync connected devices with current online state (hot-plug).

        Discovers current devices, connects new ones, disconnects removed ones.

        Lifecycle 语义 (self._node_name 不为 None 时):
        - 进入时 set_lifecycle(STARTING)。已在运行中的节点不会被打断。
        - discover/connect 全程完成后标 READY。discover 抛异常则标 FAILED。
        """
        mon = get_monitor()
        node = self._node_name
        if node is not None:
            mon.set_lifecycle(node, Lifecycle.STARTING)

        try:
            discovered = await self.discover_devices(all_devices)
        except Exception as e:
            logger.error("[%s] Failed to discover devices: %s", self.device_type, e)
            # 若本次 init 阶段把节点引入了 STARTING,降级为 FAILED
            if node is not None:
                state = mon.get_state(node)
                if state and state.lifecycle == Lifecycle.STARTING:
                    mon.set_lifecycle(node, Lifecycle.FAILED, error=repr(e))
            return

        connected = self.get_connected_devices()
        discovered_dids = set(discovered.keys())
        connected_dids = set(connected.keys())

        # Connect newly discovered devices
        for did in discovered_dids - connected_dids:
            try:
                await self.connect_device(did, source=discovered[did])
                logger.info(
                    "[%s] Connected device: %s (%s)",
                    self.device_type,
                    did,
                    discovered[did].name,
                )
            except Exception as e:
                logger.error(
                    "[%s] Failed to connect device %s: %s",
                    self.device_type,
                    did,
                    e,
                )

        # Disconnect removed devices
        for did in connected_dids - discovered_dids:
            try:
                await self.disconnect_device(did)
                logger.info("[%s] Disconnected device: %s", self.device_type, did)
            except Exception as e:
                logger.error(
                    "[%s] Failed to disconnect device %s: %s",
                    self.device_type,
                    did,
                    e,
                )

        # init 完成,把 STARTING 标 READY (跳过已经 RUNNING_*/STALLED 的)
        if node is not None:
            state = mon.get_state(node)
            if state and state.lifecycle == Lifecycle.STARTING:
                mon.set_lifecycle(node, Lifecycle.READY)

    async def shutdown(self) -> None:
        """Disconnect all devices."""
        if self._node_name is not None:
            get_monitor().set_lifecycle(self._node_name, Lifecycle.STOPPED)
        for did in list(self.get_connected_devices().keys()):
            try:
                await self.disconnect_device(did)
            except Exception as e:
                logger.error(
                    "[%s] Failed to disconnect device %s during shutdown: %s",
                    self.device_type,
                    did,
                    e,
                )

    def pause_streams(self) -> None:
        """暂停设备数据流（解码器等）。默认 no-op，子类按需覆写。"""

    def resume_streams(self) -> None:
        """恢复设备数据流。默认 no-op，子类按需覆写。"""
