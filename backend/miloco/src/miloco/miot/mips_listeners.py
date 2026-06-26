# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""MIPS push-event listeners (bind / device-meta / scene).

All three share one shape: a trailing-edge debounce that, once it settles,
refreshes the authoritative cloud state and acts on it. They differ only in:

  * the debounce KEY — bind debounces per-did (independent timers); meta and
    scene use a single global timer (any event refreshes the whole list);
  * the settled ACTION — bind decides bind-vs-unbind and delegates the
    greeting; meta refreshes devices+cameras+scenes (and greets move-ins);
    scene refreshes the scene list.

The shared debounce skeleton lives in ``_TrailingDebounce``; each listener is
a thin subclass. The welcome ACTION itself lives in
``miloco.miot.welcome_service.DeviceWelcomeService`` (an action, not a
listener) and is injected here.

All listeners are intentionally decoupled from MiotProxy — they depend only
on injected callables, so the debounce logic is testable without the full
proxy / MIoTClient / camera stack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from miot.types import (
    MIoTDeviceBindEvent,
    MIoTDeviceInfo,
    MIoTDeviceStateEvent,
    MIoTSceneChangedEvent,
)

logger = logging.getLogger(__name__)


# Trailing-edge debounce windows. Repeated events within the window collapse
# into a single settled action, fired this many seconds after the last event.
BIND_DEBOUNCE_SEC: float = 5.0
META_DEBOUNCE_SEC: float = 5.0
SCENE_DEBOUNCE_SEC: float = 5.0
# Cloud online/offline state events settle quickly under normal flapping, but
# the trailing full reconciliation (refresh_camera_online_status) is what makes
# the event-driven path trustworthy: each event updates the cached `online`
# field directly, and once the burst settles we re-fetch the authoritative
# cloud state once. 60s matches the maintainer's spec (re-set on every event
# via _schedule).
CAMERA_STATE_DEBOUNCE_SEC: float = 60.0

# Single key used by the global (non-per-did) debouncers (meta / scene).
_GLOBAL_KEY = "_global"


# Injected-callable type aliases. The proxy passes its own bound methods.
RefreshDevices = Callable[[], Awaitable[Any]]
RefreshCameras = Callable[[], Awaitable[Any]]
RefreshScenes = Callable[[], Awaitable[Any]]
RefreshScenesOnly = Callable[[], Awaitable[Any]]
# Lightweight cloud-status re-fetch (refresh_camera_online_status): updates
# _camera_info_dict metadata only, does NOT touch stream connections.
RefreshCameraOnlineStatus = Callable[[], Awaitable[Any]]
GetDevice = Callable[[str], MIoTDeviceInfo | None]
# Greets a device by did (owned by DeviceWelcomeService); returns whether a
# welcome was sent.
Welcome = Callable[[str], Awaitable[bool]]


async def _refresh_all(
    refreshers: list[tuple[str, Callable[[], Awaitable[Any]] | None]],
) -> dict[str, bool]:
    """Run each (label, refresher) best-effort. Per-item failures only log."""
    status: dict[str, bool] = {}
    for label, fn in refreshers:
        if fn is None:
            continue
        try:
            await fn()
            status[label] = True
        except Exception as e:
            logger.error("refresh %s failed: %s", label, e)
            status[label] = False
    return status


class _TrailingDebounce:
    """Keyed trailing-edge debounce skeleton.

    Subclasses implement ``_window()`` (debounce seconds, read fresh so tests
    can monkeypatch the module constant) and ``_fire(key)`` (the settled
    action). ``_schedule(key)`` (re)starts the per-key timer; ``deinit()``
    cancels all timers and fences in-flight ``_fire`` tasks via ``_closed``.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop  # resolved lazily — see _get_loop
        self._timers: dict[Any, asyncio.TimerHandle] = {}
        # Set by deinit(); checked before and after the refresh in _fire so a
        # task spawned before deinit but firing after it bails out early.
        self._closed: bool = False

    # ------------------------------------------------------------- subclass API

    def _window(self) -> float:
        raise NotImplementedError

    async def _fire(self, key: Any) -> None:
        raise NotImplementedError

    # ------------------------------------------------------------------ shared

    def _schedule(self, key: Any) -> None:
        """(Re)start the trailing-edge timer for ``key``."""
        if self._closed:
            return
        pending = self._timers.pop(key, None)
        if pending is not None:
            pending.cancel()
        loop = self._get_loop()
        self._timers[key] = loop.call_later(
            self._window(),
            lambda k=key: asyncio.create_task(self._run_fire(k)),
        )

    async def _run_fire(self, key: Any) -> None:
        if self._closed:
            logger.debug("debounce fire skipped: listener closed (key=%s)", key)
            return
        # Clear the record first so a push arriving during _fire starts a fresh
        # debounce cycle instead of mutating this one.
        self._timers.pop(key, None)
        await self._fire(key)

    def deinit(self) -> None:
        """Cancel pending timers and fence in-flight fires. Idempotent."""
        self._closed = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def _get_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.get_event_loop()
        return self._loop


class BindEventListener(_TrailingDebounce):
    """Per-did debounce for `user/{uid}/g_op/{bind,unbind}`.

    After the burst settles it refreshes the authoritative device list:
    present → bind → delegate the greeting to ``welcome``; absent → unbind →
    log and drop. Cameras + scenes are refreshed regardless (a deletion
    invalidates those lists too).
    """

    def __init__(
        self,
        refresh_devices: RefreshDevices,
        get_device: GetDevice,
        welcome: Welcome,
        refresh_cameras: RefreshCameras | None = None,
        refresh_scenes: RefreshScenes | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(loop)
        self._refresh = refresh_devices
        self._get_device = get_device
        self._welcome = welcome
        self._refresh_cameras = refresh_cameras
        self._refresh_scenes = refresh_scenes

    def _window(self) -> float:
        return BIND_DEBOUNCE_SEC

    async def on_event(self, msg: MIoTDeviceBindEvent) -> None:
        if self._closed:
            logger.debug("bind event ignored: listener closed (did=%s)", msg.did)
            return
        if not msg.did:
            return
        logger.info(
            "mips user-bind event received: uid=%s event=%s did=%s raw=%r; "
            "scheduling %ss debounce",
            msg.uid,
            msg.event,
            msg.did,
            msg.raw,
            BIND_DEBOUNCE_SEC,
        )
        self._schedule(msg.did)

    async def _fire(self, key: Any) -> None:
        did = key
        try:
            await self._refresh()
        except Exception as e:
            logger.error(
                "bind debounce settled but refresh_devices failed: did=%s err=%s",
                did,
                e,
            )
            return
        if self._closed:
            logger.debug("bind debounce aborted post-refresh: closed (did=%s)", did)
            return

        status = await _refresh_all(
            [("cameras", self._refresh_cameras), ("scenes", self._refresh_scenes)]
        )
        logger.info(
            "bind debounce settled: did=%s cameras_ok=%s scenes_ok=%s",
            did,
            status.get("cameras", "N/A"),
            status.get("scenes", "N/A"),
        )

        if self._get_device(did) is None:
            logger.info("bind debounce settled: did=%s final-state=unbind", did)
            return
        # Present → bind. The welcome service applies the scope gate, formats
        # the message, sends it, and dedups against a concurrent move-in.
        await self._welcome(did)


class DeviceMetaEventListener(_TrailingDebounce):
    """Global debounce for `device/{did}/g_op/{rename,hr_change}`.

    Refreshes devices (+ cameras + scenes, since a rename / home-room move
    affects those lists). A move INTO a managed home is additionally a welcome
    trigger: the caller flags such events via ``on_event(.., welcome=True)`` and
    each flagged did is greeted (via ``welcome``) once the refresh settles. The
    move-into-scope DECISION stays with the caller (it owns the whitelist).
    """

    def __init__(
        self,
        refresh_devices: RefreshDevices,
        refresh_cameras: RefreshCameras | None = None,
        refresh_scenes: RefreshScenes | None = None,
        welcome: Welcome | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(loop)
        self._refresh = refresh_devices
        self._refresh_cameras = refresh_cameras
        self._refresh_scenes = refresh_scenes
        self._welcome = welcome
        # Dids flagged welcome=True during the current window; greeted after
        # the refresh settles. Accumulated across the burst.
        self._pending_welcome_dids: set[str] = set()

    def _window(self) -> float:
        return META_DEBOUNCE_SEC

    async def on_event(self, msg: MIoTDeviceBindEvent, welcome: bool = False) -> None:
        if self._closed:
            logger.debug("meta event ignored: listener closed (did=%s)", msg.did)
            return
        if welcome and msg.did:
            self._pending_welcome_dids.add(msg.did)
        logger.info(
            "mips device-meta event received: uid=%s event=%s did=%s raw=%r; "
            "scheduling %ss debounce",
            msg.uid,
            msg.event,
            msg.did,
            msg.raw,
            META_DEBOUNCE_SEC,
        )
        self._schedule(_GLOBAL_KEY)

    def deinit(self) -> None:
        super().deinit()
        self._pending_welcome_dids.clear()

    async def _fire(self, key: Any) -> None:
        # Drain BEFORE the refresh: the dids seen up to now settled this window,
        # so the refresh we are about to run will reflect their new home/room. A
        # push arriving DURING the (multi-second, three-call) refresh stays in
        # the now-fresh set and is greeted by the timer it just (re)scheduled —
        # greeting it here would run against a device list that predates it
        # (→ out-of-scope skip) and then drop it, losing the welcome entirely.
        # Same trailing-edge contract as _run_fire popping the timer up front.
        pending = self._pending_welcome_dids
        self._pending_welcome_dids = set()
        try:
            await self._refresh()
        except Exception as e:
            logger.error("meta debounce settled but refresh_devices failed: %s", e)
            return
        if self._closed:
            logger.debug("meta debounce aborted post-refresh: closed")
            return

        status = await _refresh_all(
            [("cameras", self._refresh_cameras), ("scenes", self._refresh_scenes)]
        )
        logger.info(
            "meta debounce settled: devices refreshed cameras_ok=%s scenes_ok=%s",
            status.get("cameras", "N/A"),
            status.get("scenes", "N/A"),
        )

        # Greet devices that moved INTO a managed home — the refresh above now
        # reflects their new home/room.
        if pending and self._welcome is not None:
            for did in pending:
                try:
                    await self._welcome(did)
                except Exception as e:
                    logger.error("meta debounce: welcome failed did=%s: %s", did, e)


class SceneEventListener(_TrailingDebounce):
    """Global debounce for `home/{home_id}/scene/{rename,delete,edit}`.

    Any scene change refreshes the whole scene list once the window settles.
    """

    def __init__(
        self,
        refresh_scenes: RefreshScenesOnly,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(loop)
        self._refresh = refresh_scenes

    def _window(self) -> float:
        return SCENE_DEBOUNCE_SEC

    async def on_event(self, msg: MIoTSceneChangedEvent) -> None:
        if self._closed:
            logger.debug("scene event ignored: listener closed (home=%s)", msg.home_id)
            return
        logger.info(
            "mips scene event received: home=%s event=%s scene_id=%s raw=%r; "
            "scheduling %ss debounce",
            msg.home_id,
            msg.event,
            msg.scene_id,
            msg.raw,
            SCENE_DEBOUNCE_SEC,
        )
        self._schedule(_GLOBAL_KEY)

    async def _fire(self, key: Any) -> None:
        try:
            await self._refresh()
        except Exception as e:
            logger.error("scene debounce settled but refresh_scenes failed: %s", e)
            return
        logger.info("scene debounce settled: scene list refreshed")


class CameraStateEventListener(_TrailingDebounce):
    """Global debounce for `device/{did}/state/{online,offline}`.

    Each state event already updated the cached `online` field directly (in
    MiotProxy._on_camera_state_changed_event) — this listener is the trailing
    reconciliation: once the burst settles it re-fetches the authoritative
    cloud camera status once, so a missed or stale event can't strand a
    camera. refresh_camera_online_status is lightweight (metadata only, no
    stream disturbance). Any state event (any did) re-arms the single global
    timer.
    """

    def __init__(
        self,
        refresh_camera_online_status: RefreshCameraOnlineStatus,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__(loop)
        self._refresh = refresh_camera_online_status

    def _window(self) -> float:
        return CAMERA_STATE_DEBOUNCE_SEC

    async def on_event(self, msg: MIoTDeviceStateEvent) -> None:
        if self._closed:
            logger.debug(
                "camera-state event ignored: listener closed (did=%s)", msg.did
            )
            return
        logger.info(
            "mips device-state event received: did=%s event=%s raw=%r; "
            "scheduling %ss reconciliation",
            msg.did,
            msg.event,
            msg.raw,
            CAMERA_STATE_DEBOUNCE_SEC,
        )
        self._schedule(_GLOBAL_KEY)

    async def _fire(self, key: Any) -> None:
        try:
            await self._refresh()
        except Exception as e:
            logger.error(
                "camera-state debounce settled but "
                "refresh_camera_online_status failed: %s",
                e,
            )
            return
        logger.info("camera-state debounce settled: cloud online status reconciled")
