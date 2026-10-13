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
        self,
        all_devices: dict | None = None,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        """Discover devices of this type.

        Args:
            all_devices: If provided, filter this type from the full device dict.
                         If None, query MIoT directly for this device type.
            online_only: If True (default), only return online devices.
                         If False, return all discovered devices regardless of
                         online status.
            require_lan: If True (default), a device must be LAN-reachable to be
                 returned. Pass False for "cloud-online is enough" callers — the
                 retained-set recompute in ``sync_devices`` is the only one.
                 Notably NOT the camera on-demand-rebuild probe: that one asks
                 "would ``refresh_cameras`` actually build a manager for it?" and
                 must use the strict gate (see ``camera_adapter.sync_devices``).
                 Adapters without a LAN reachability notion ignore it.
                 ``sync_devices`` calls this by keyword, so every adapter must
                 accept it.
            cap: If True (default), truncate to the device type's feed limit
                 (camera: MAX_ENABLED_CAMERAS). Pass False for "list full set"
                 callers (e.g. rule target validation, retained-set recompute).
                 Adapters without a feed limit ignore it.

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

    async def sync_devices(
        self,
        all_devices: dict | None = None,
        disconnect_require_lan: bool = True,
    ) -> None:
        """Sync connected devices with current online state (hot-plug).

        Discovers current devices, connects new ones, disconnects removed ones.

        ``disconnect_require_lan``: 断开判断用的 require_lan 口径。默认与 discover
        一致 (True)。设为 False 时,断开只以「云端在线」为准,保留 lan_online 暂时
        为 False 的已连设备 —— 防止 LAN 探测偶发失败 (lan_online 假 False) 把正在
        拉流的设备断开。camera 的感知同步用 False (见 camera_adapter.sync_devices)。

        ⚠️ 这与 camera 侧「按需补建」用的严格门 (require_lan=True) **故意不同口径**,
        不是待清理的漂移:补建问的是「refresh_cameras 会不会为它建 manager」(它自己就用
        严格门,宽松门下判据永真、每轮空转打云端接口),断开问的是「会不会误杀已经连上
        的」。改本参数前先读下面「保留集 ⊇ 发现集」那段不变量。

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

        # 断开判断的「应保留」集合:默认就是 discovered (require_lan 与 discover 一致)。
        # disconnect_require_lan=False 时改用 require_lan=False 重算 —— 只按云端在线
        # 判定,lan_online 偶发假 False 的设备仍保留,避免把拉流中的设备误断。
        #
        # 这条路径成立的唯一前提是「保留集 ⊇ 发现集」:放宽了一道门,候选只应变多。
        # 所以必须 cap=False —— 投喂上限的截断发生在过滤之后、按合成 did 字典序取前
        # N 个,而 sorted(超集)[:N] 并不包含 sorted(子集)[:N]。带截断重算时,超过上限的
        # 家庭里「唯一真正 LAN 可达那台」会落在保留集之外,于是每轮同步连上、下一轮
        # 又被断开,画面反复中断且永不自愈。再与 discovered_dids 取并集兜底:将来
        # cap / 排序口径若再变,也不会让「放宽一道门」反而丢掉已通过严格门的设备。
        if disconnect_require_lan:
            retained_dids = discovered_dids
        else:
            try:
                retained = await self.discover_devices(
                    all_devices, require_lan=False, cap=False
                )
                retained_dids = set(retained.keys()) | discovered_dids
            except Exception as e:
                logger.warning(
                    "[%s] Retained-device rediscover failed (%s); "
                    "falling back to lan-gated set",
                    self.device_type,
                    e,
                )
                retained_dids = discovered_dids

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
        for did in connected_dids - retained_dids:
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
