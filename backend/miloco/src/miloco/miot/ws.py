"""MIoT WebSocket stream managers — Video and Audio."""

import asyncio
import io
import json
import logging
import struct
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import av
from fastapi import WebSocket
from fastapi.websockets import WebSocketState
from miot.types import MIoTCameraCodec

from miloco.manager import get_manager
from miloco.miot.transcoder import H264LiveEncoder

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class NalClipRecorder:
    """One-shot in-memory BGR → mp4 recorder (class name kept for API stability).

    Despite the legacy "Nal" prefix, this implementation receives **BGR
    ndarrays** straight from the SDK decoder and encodes a fresh H.264 mp4
    inline. The previous design (collect Annex-B NAL bytes from the live
    encoder, then re-mux to mp4) hit a wall on PyAV 17.0.1: the raw h264
    demuxer yielded zero packets regardless of input source (BytesIO or
    temp file), producing 0-byte mp4 outputs every time. BGR-in / mp4-out
    avoids that entire demuxer code path — we go straight through PyAV's
    standard ``stream.encode(frame) → container.mux(packet)`` API that is
    documented, well-tested, and used by virtually every PyAV tutorial.

    State machine::

        WAITING_FIRST ─ idle until first BGR frame (sets shape, opens encoder)
                  └─→ RECORDING ─ encode + mux each frame in a worker thread
                              └─→ DONE ─ flush encoder, resolve future

    Design notes:
      * Recording always starts at the very first BGR frame fed (no
        keyframe wait — H.264 encoder emits its own IDR at frame 0).
      * BGR frames are NOT buffered. Each ``feed_bgr`` immediately
        schedules encode+mux on a single-worker executor (serialised so
        the libx264 context is never accessed concurrently). Memory stays
        bounded by ``Queue``-style backpressure inside the executor.
      * libx264 ``ultrafast/zerolatency`` keeps per-frame encode ~3-8 ms
        on Apple Silicon — well below the 33 ms frame interval at 30fps.
      * On the duration boundary, ``feed_bgr`` enqueues a final flush
        task and trips ``DONE``. The flush task closes the mp4 container,
        resolves the future with the mp4 bytes.
      * The recorder is *passive* w.r.t. the SDK subscription — the
        manager handles start/stop lifecycle around register/unregister.
    """

    def __init__(self, duration_ms: int = 15000):
        self._duration_ms = duration_ms
        self._state = "WAITING_FIRST"
        self._start_ts: int | None = None
        self._frame_count = 0
        # PyAV objects created lazily on first frame (we don't know
        # width/height until then).
        self._out_buf: io.BytesIO | None = None
        self._container: "av.container.OutputContainer | None" = None
        self._stream: "av.video.stream.VideoStream | None" = None
        # Serialise all libx264 work on one thread — the codec context is
        # not thread-safe and PyAV will deadlock on concurrent encode().
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="clip-enc"
        )
        # ``__init__`` 由 route handler 在 async 上下文里调,这里能拿到
        # running loop;``get_event_loop()`` 在 3.10+ 标 deprecated。
        loop = asyncio.get_running_loop()
        self._result_future: asyncio.Future[bytes] = loop.create_future()

    async def feed_bgr(self, bgr: "NDArray[np.uint8]", ts_ms: int) -> None:
        """Async — invoked by the manager's video callback fan-out.

        Lazy-init the encoder on the first frame, then encode-and-mux each
        BGR frame on the recorder's executor. Returns when this frame's
        encode finishes; the caller awaits us so we naturally apply
        backpressure if encoding falls behind the live stream.
        """
        if self._state == "DONE":
            return
        loop = asyncio.get_running_loop()
        if self._state == "WAITING_FIRST":
            h, w = int(bgr.shape[0]), int(bgr.shape[1])
            await loop.run_in_executor(self._executor, self._init_encoder, w, h)
            self._start_ts = ts_ms
            self._state = "RECORDING"
            logger.info(
                "clip recorder init: %dx%d, target duration %dms",
                w, h, self._duration_ms,
            )
        assert self._start_ts is not None
        elapsed = ts_ms - self._start_ts
        # Encode this frame BEFORE the boundary check: at ``elapsed ==
        # duration`` the frame sits exactly on the edge (ts resolution ~33ms
        # @30fps makes this reachable). Finalizing before encoding would drop
        # it, so a "15s" clip would top out at ~14.967s. Encode-then-finalize
        # keeps the edge frame and still finalizes immediately — no dependence
        # on a subsequent frame arriving to trigger it.
        await loop.run_in_executor(self._executor, self._encode_frame_sync, bgr)
        self._frame_count += 1
        if elapsed >= self._duration_ms:
            self._state = "DONE"
            # Fire-and-forget the finalize; resolves _result_future when done.
            asyncio.ensure_future(self._finalize_async())

    def _init_encoder(self, width: int, height: int) -> None:
        """Build the mp4 container + h264 encoder stream. Runs in executor."""
        self._out_buf = io.BytesIO()
        # 不能用 ``movflags=+faststart``:faststart 在 av_write_trailer 时要把
        # moov 搬到文件头,需按文件名重开输出做第二趟;而我们 mux 到 in-memory
        # BytesIO 没有文件名,libav 会拿 '<none>' 去 open → FileNotFoundError,
        # finalize 直接炸(此前挂在 stream.options 上 libx264 不认、被静默吞掉,
        # 所以没暴露)。当前用法是 finalize 后把完整 mp4 bytes 返给前端喂
        # /extract,解码器整文件可 seek,moov 在尾部不影响,无需 faststart。
        self._container = av.open(self._out_buf, mode="w", format="mp4")
        self._stream = self._container.add_stream("h264", rate=30)
        self._stream.width = width
        self._stream.height = height
        self._stream.pix_fmt = "yuv420p"
        # ultrafast keeps encode at ~3-8ms/frame on Apple Silicon.
        self._stream.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
        }

    def _encode_frame_sync(self, bgr: "NDArray[np.uint8]") -> None:
        """Encode one BGR frame and mux any emitted packets. Runs in executor."""
        assert self._container is not None and self._stream is not None
        frame = av.VideoFrame.from_ndarray(bgr, format="bgr24")
        # PTS in stream time_base; stream.encode handles assigning DTS.
        frame.pts = self._frame_count
        for packet in self._stream.encode(frame):
            self._container.mux(packet)

    async def _finalize_async(self) -> None:
        """Flush + close the mp4, resolve the result future."""
        loop = asyncio.get_running_loop()
        try:
            mp4_bytes = await loop.run_in_executor(
                self._executor, self._finalize_sync
            )
            if not self._result_future.done():
                self._result_future.set_result(mp4_bytes)
        except Exception as e:  # noqa: BLE001
            logger.error("clip finalize error: %s", e, exc_info=True)
            if not self._result_future.done():
                self._result_future.set_exception(e)
        finally:
            self._executor.shutdown(wait=False)

    def _finalize_sync(self) -> bytes:
        """Drain the encoder, close the mp4 container. Runs in executor."""
        if self._container is None or self._stream is None or self._out_buf is None:
            logger.warning("clip finalize: no frames received")
            return b""
        # Drain encoder of any buffered frames (B-frame reorder etc.; in
        # zerolatency mode this is usually empty, but cheap and required
        # by the API).
        for packet in self._stream.encode():
            self._container.mux(packet)
        self._container.close()
        out = self._out_buf.getvalue()
        logger.info(
            "clip finalize: %d frames encoded → %d bytes mp4",
            self._frame_count, len(out),
        )
        return out

    async def wait(self, timeout: float) -> bytes:
        """Block until the clip is muxed and ready.

        Raises ``asyncio.TimeoutError`` if no keyframe arrives within the
        timeout window (camera offline, GOP > timeout, etc.).
        """
        return await asyncio.wait_for(self._result_future, timeout=timeout)

    def cancel(self) -> None:
        """Mark recorder as DONE to short-circuit further feed() calls."""
        self._state = "DONE"
        if not self._result_future.done():
            self._result_future.cancel()
        # 失败路径(超时 / 客户端断开 / register 失败)不会经过 _finalize_async
        # 的 finally,executor 在这里显式回收;ThreadPoolExecutor 的 worker
        # thread 是 non-daemon 的,不主动 shutdown 会泄漏(每次失败 +1 个常驻
        # idle 线程),长跑 backend 会慢慢吃 fd / 内存。
        # shutdown(wait=False) 幂等,与成功路径的 _finalize_async finally
        # 双重 shutdown 也无害。
        self._executor.shutdown(wait=False)

manager = get_manager()


class MIoTVideoStreamManager:
    """MIoT Video WS Manager.

    Live transcode design:
        SDK PyAV decoder (one per camera, shared with perception via multi_reg)
        → BGR ndarray
        → H264LiveEncoder (per-camera libx264 ultrafast + zerolatency, GOP=30)
        → H.264 Annex-B NAL bytes
        → fan-out to all subscribers

    Browser receives a fixed "h264" stream regardless of native camera codec
    (H.264 or H.265) — works on any browser that supports H.264 (= all of them).

    Wire protocol pushed to the browser:
      1. First text message:  {"type":"init","codec":"h264","container":"annexb"}
      2. Then each binary message = 16-byte header + Annex-B NAL bytes:
           offset 0 : uint8  frame_type   (1 = I/IDR, 0 = P)
           offset 1 : 7 bytes padding
           offset 8 : uint64 timestamp (big-endian, ms — camera-side ts)
      3. Frames before the first IDR (in the encoded stream) are dropped per
         camera_tag so the manager hands the browser a clean GOP boundary.
         Late joiners get a cached init JSON immediately and wait at most one
         GOP (~1.2s) for the next IDR.
    """

    _CAMERA_CONNECT_COUNT_MAX: int = 4
    # Encoded output is always H.264 — keep the dict for future flexibility.
    _CODEC_NAME = {
        MIoTCameraCodec.VIDEO_H264: "h264",
        MIoTCameraCodec.VIDEO_H265: "h265",
    }
    # GOP for the transcoded stream. 30 frames ≈ 1.2s at 25fps; balances
    # bandwidth (~1.5 Mbps for 1080p) against late-joiner first-frame wait.
    _TRANSCODE_GOP: int = 30

    _camera_connect_map: dict[str, dict[str, OrderedDict[str, WebSocket]]]
    _camera_connect_id: int
    # camera_tag → MIoTCameraCodec we're currently emitting (always VIDEO_H264
    # in transcode mode, but kept as cache for late-joiner init handshake).
    _camera_codec: dict[str, MIoTCameraCodec]
    _camera_seen_keyframe: set[str]
    # Active per-camera resources (only present while subscribers exist).
    _camera_encoder: dict[str, H264LiveEncoder]
    _camera_reg_id: dict[str, int]      # SDK register_decode_video reg_id
    # Click-triggered NAL clip recorders. Counted as subscribers alongside WS
    # clients for the SDK start/stop lifecycle — adding the first recorder
    # while no WS is connected triggers ``start_video_stream``; removing the
    # last subscriber (WS or recorder) triggers ``stop_video_stream``. The
    # SDK uses ``multi_reg`` internally so this never creates a second PPCS
    # stream against the camera; it just adds another callback consumer.
    _camera_recorders: dict[str, list["NalClipRecorder"]]
    # Per camera_tag asyncio.Lock that serialises new_connection and
    # close_connection. Without this, two simultaneous new_connection calls
    # could both enter the is_first_subscriber branch (corrupting each other's
    # reg_id/encoder), or a close_connection could yank the connect_map slot
    # while a peer new_connection was awaiting start_video_stream. Locks are
    # never garbage-collected — bounded memory cost (≤ a few hundred bytes
    # per unique camera_tag, and the camera_tag set is small).
    _camera_locks: dict[str, asyncio.Lock]

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_connect_id = 0
        self._camera_codec = {}
        self._camera_seen_keyframe = set()
        self._camera_encoder = {}
        self._camera_reg_id = {}
        self._camera_recorders = {}
        self._camera_locks = {}
        logger.info("Init MIoT Video WS Manager (transcode mode, gop=%d)",
                    self._TRANSCODE_GOP)

    def _has_subscribers(self, camera_tag: str) -> bool:
        """True iff any WS client OR recorder is currently attached."""
        has_ws = bool(self._camera_connect_map.get(camera_tag))
        has_rec = bool(self._camera_recorders.get(camera_tag))
        return has_ws or has_rec

    def has_emitted_frame(self, camera_id: str, channel: int) -> bool:
        """True once at least one keyframe has been broadcast for this camera.

        ``__video_stream_callback`` adds the camera_tag to ``_camera_seen_keyframe``
        the moment it forwards the first IDR. The first-frame watchdog in the
        router polls this to distinguish "registered with SDK but camera never
        produced a frame" (cross-LAN / offline / PPCS relay never established →
        reg_id≥0 but no frames ever arrive) from a healthy stream. Cause-agnostic
        on purpose: lan_online=False does NOT imply unreachable (miot can relay
        over the cloud), so the only trustworthy signal is whether frames are
        actually flowing.
        """
        return f"{camera_id}.{channel}" in self._camera_seen_keyframe

    async def _ensure_sdk_subscription(
        self, camera_id: str, channel: int, camera_tag: str
    ) -> None:
        """Idempotent: start SDK stream + allocate per-camera encoder.

        Called by both :meth:`new_connection` and :meth:`register_recorder`
        on the first subscriber of any kind. Holding ``_lock_for(camera_tag)``
        is the caller's responsibility — this method does not re-acquire it.
        """
        self._camera_seen_keyframe.discard(camera_tag)
        self._camera_codec.pop(camera_tag, None)
        self._camera_connect_map.setdefault(camera_tag, {})
        try:
            reg_id = await manager.miot_service.start_video_stream(
                camera_id=camera_id,
                channel=channel,
                callback=self.__video_stream_callback,
            )
        except Exception:
            self._camera_connect_map.pop(camera_tag, None)
            raise
        if reg_id < 0:
            self._camera_connect_map.pop(camera_tag, None)
            raise RuntimeError(
                f"Camera {camera_id} not registered with SDK "
                "(likely PPCS not handshaken). "
                "Try `miloco-cli account unbind && account bind`."
            )
        self._camera_reg_id[camera_tag] = reg_id
        self._camera_encoder[camera_tag] = H264LiveEncoder(gop=self._TRANSCODE_GOP)
        logger.info(
            "Start video stream (transcode), %s.%d reg_id=%d",
            camera_id, channel, reg_id,
        )

    async def _teardown_if_idle(
        self, camera_id: str, channel: int, camera_tag: str
    ) -> None:
        """If no subscribers remain, stop SDK stream and free encoder.

        Caller must hold ``_lock_for(camera_tag)``.
        """
        if self._has_subscribers(camera_tag):
            return
        reg_id = self._camera_reg_id.pop(camera_tag, -1)
        if reg_id >= 0:
            await manager.miot_service.stop_video_stream(
                camera_id, channel, reg_id
            )
        encoder = self._camera_encoder.pop(camera_tag, None)
        if encoder is not None:
            await encoder.close()
        self._camera_connect_map.pop(camera_tag, None)
        self._camera_codec.pop(camera_tag, None)
        self._camera_seen_keyframe.discard(camera_tag)
        logger.info(
            "No connection, stop video stream, %s.%d",
            camera_id, channel,
        )

    def _lock_for(self, camera_tag: str) -> asyncio.Lock:
        """Get-or-create the asyncio.Lock for this camera_tag.

        ``dict.setdefault`` is atomic under the asyncio single-thread model
        so no separate guard is needed for the lookup itself.
        """
        return self._camera_locks.setdefault(camera_tag, asyncio.Lock())

    def _build_init_msg(self, codec_id: MIoTCameraCodec) -> str:
        return json.dumps({
            "type": "init",
            "codec": self._CODEC_NAME.get(codec_id, "h264"),
            "container": "annexb",
        })

    async def new_connection(
        self,
        websocket: WebSocket,
        user_name: str,
        token_hash: str,
        camera_id: str,
        channel: int,
    ) -> str:
        """New video stream connection.

        Body is serialised by ``self._lock_for(camera_tag)`` so the
        is_first_subscriber check, SDK registration, encoder allocation, and
        ws/user_tag map mutation all see a consistent view of state — no
        racing first-subscribers, no peer's close_connection yanking the
        connect_map slot while we await ``start_video_stream``.
        """
        camera_tag = f"{camera_id}.{channel}"
        async with self._lock_for(camera_tag):
            # First subscriber of *any* type triggers the SDK stream. We
            # check both WS and recorder maps so a recorder already attached
            # before any browser tab opens doesn't cause us to start a
            # second SDK callback.
            sdk_just_started = not self._has_subscribers(camera_tag)
            if sdk_just_started:
                await self._ensure_sdk_subscription(camera_id, channel, camera_tag)
            user_tag = f"{user_name}.{token_hash}"
            self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
            connection_id = str(self._camera_connect_id)
            self._camera_connect_id += 1
            self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
            logger.info(
                "New video stream connection, %s, %s, %s",
                camera_tag,
                user_tag,
                connection_id,
            )
            if (
                len(self._camera_connect_map[camera_tag][user_tag])
                > self._CAMERA_CONNECT_COUNT_MAX
            ):
                logger.warning(
                    "Too many connections, %s.%d, %s, remove first connect",
                    camera_id,
                    channel,
                    user_tag,
                )
                _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(
                    last=False
                )
                try:
                    if ws.client_state == WebSocketState.CONNECTED:
                        await ws.close()
                except Exception as err:
                    logger.error("WebSocket close error: %s", err)

            # Late joiners: if codec is already known (a frame has been
            # observed since the stream started), send init handshake to
            # *this* WS now so it doesn't have to wait for a fresh
            # first-frame event (which never fires again until the
            # camera_tag fully tears down).
            cached_codec = self._camera_codec.get(camera_tag)
            if cached_codec is not None and not sdk_just_started:
                try:
                    await websocket.send_text(self._build_init_msg(cached_codec))
                except Exception as err:
                    logger.error("WebSocket send init error: %s", err)

        return connection_id

    async def close_connection(
        self, user_name: str, token_hash: str, camera_id: str, channel: int, cid: str
    ):
        """Close video stream connection.

        Held under the same per-camera_tag lock as new_connection so a
        concurrent peer's new_connection cannot read the connect_map mid
        teardown.
        """
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        async with self._lock_for(camera_tag):
            if (
                camera_tag not in self._camera_connect_map
                or user_tag not in self._camera_connect_map[camera_tag]
                or cid not in self._camera_connect_map[camera_tag][user_tag]
            ):
                return
            logger.info(
                "Close video stream connection, %s, %s, %s",
                camera_tag, user_tag, cid,
            )

            try:
                ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close()
            except Exception as err:
                logger.error("WebSocket close error: %s", err)
            if len(self._camera_connect_map[camera_tag][user_tag]) == 0:
                self._camera_connect_map[camera_tag].pop(user_tag, None)
            # Teardown only when *both* WS clients and recorders are gone;
            # otherwise an active recorder would lose its NAL feed mid-clip.
            await self._teardown_if_idle(camera_id, channel, camera_tag)

    async def register_recorder(
        self,
        camera_id: str,
        channel: int,
        recorder: "NalClipRecorder",
    ) -> None:
        """Attach a click-triggered NAL recorder to this camera.

        If no other subscriber (WS or recorder) is active, this triggers
        ``start_video_stream`` — same SDK call path that ``new_connection``
        uses. The SDK's multi_reg ensures we never spin up a second PPCS
        stream against the camera; we just add another callback consumer
        alongside perception's existing decoder consumer.

        Serialised by the per-camera_tag lock so concurrent register /
        new_connection / close_connection calls observe consistent state.
        """
        camera_tag = f"{camera_id}.{channel}"
        async with self._lock_for(camera_tag):
            if not self._has_subscribers(camera_tag):
                await self._ensure_sdk_subscription(camera_id, channel, camera_tag)
            self._camera_recorders.setdefault(camera_tag, []).append(recorder)
            logger.info(
                "Recorder attached, %s, active_count=%d",
                camera_tag, len(self._camera_recorders[camera_tag]),
            )

    async def unregister_recorder(
        self,
        camera_id: str,
        channel: int,
        recorder: "NalClipRecorder",
    ) -> None:
        """Detach a recorder. May trigger SDK teardown if it was last subscriber."""
        camera_tag = f"{camera_id}.{channel}"
        async with self._lock_for(camera_tag):
            lst = self._camera_recorders.get(camera_tag)
            if lst is not None:
                try:
                    lst.remove(recorder)
                except ValueError:
                    pass
                if not lst:
                    self._camera_recorders.pop(camera_tag, None)
            await self._teardown_if_idle(camera_id, channel, camera_tag)

    def _all_websockets(self, camera_tag: str) -> list[WebSocket]:
        out: list[WebSocket] = []
        for conn in self._camera_connect_map.get(camera_tag, {}).values():
            out.extend(conn.values())
        return out

    async def _broadcast(self, camera_tag: str, *, text: str | None = None,
                         payload: bytes | None = None) -> None:
        """Fan out to every subscriber of camera_tag concurrently.

        Failed websockets are logged; their cleanup happens on close_connection
        when the WSDisconnect handler runs in the route, so we don't mutate the
        connection map here.
        """
        targets = self._all_websockets(camera_tag)
        if not targets:
            return

        async def _send(ws: WebSocket) -> None:
            try:
                if text is not None:
                    await ws.send_text(text)
                else:
                    await ws.send_bytes(payload)  # type: ignore[arg-type]
            except Exception as err:
                logger.error("WebSocket send error: %s", err)

        await asyncio.gather(*(_send(ws) for ws in targets), return_exceptions=False)

    async def __video_stream_callback(
        self,
        did: str,
        bgr: "NDArray[np.uint8]",
        ts: int,
        channel: int,
        recv_unix_ms: int,
        decoded_unix_ms: int,
    ) -> None:
        """Decoded video callback — encodes BGR → H.264 then fans out.

        Receives BGR ndarrays from the SDK's PyAV decoder (shared with
        perception via multi_reg). Encodes each frame through the per-camera
        :class:`H264LiveEncoder` and broadcasts the resulting Annex-B packets.
        """
        camera_tag = f"{did}.{channel}"
        # ``_camera_connect_map`` may be empty when only NAL recorders are
        # attached (user clicked record without any open watch tab) — that's
        # fine, we still feed the recorder below; the WS encode path then
        # short-circuits since there are no clients to broadcast to.
        if not self._has_subscribers(camera_tag):
            logger.error("No subscribers, %s.%d", did, channel)
            return

        # Fan-out the raw BGR frame to any attached clip recorders BEFORE
        # the H.264 encode path. Recorders run an independent libx264 in
        # their own executor — feeding BGR (not Annex-B NAL) sidesteps the
        # PyAV 17 raw-h264 demuxer entirely. ``feed_bgr`` awaits the
        # recorder's own encode so this naturally backpressures if a
        # recorder falls behind.
        for rec in list(self._camera_recorders.get(camera_tag, ())):
            try:
                await rec.feed_bgr(bgr, ts)
            except Exception as e:
                logger.error("recorder feed_bgr error %s: %s", camera_tag, e)

        # Announce the h264 init handshake once per camera_tag, BEFORE the
        # encode path and independent of it. Keeping _camera_codec populated
        # even during a recorder-only window means a WS client joining later
        # still gets its init via new_connection()'s cached-codec replay. The
        # codec is statically H.264 from our own encoder, so no packet is
        # needed to confirm it (SPS/PPS rides inline with the first IDR NAL).
        if camera_tag not in self._camera_codec:
            self._camera_codec[camera_tag] = MIoTCameraCodec.VIDEO_H264
            await self._broadcast(
                camera_tag,
                text=self._build_init_msg(MIoTCameraCodec.VIDEO_H264),
            )

        # Recorder-only fast path: with no WS client attached, the H.264
        # encode + broadcast below fans out to zero subscribers — libx264
        # would burn ~3-8ms/frame for nobody, competing for CPU with the
        # recorder's own encoder. Recorders already got their BGR above, so
        # bail before the wasted transcode.
        if not self._all_websockets(camera_tag):
            return

        encoder = self._camera_encoder.get(camera_tag)
        if encoder is None:
            # Race: subscriber teardown happened between scheduling this
            # callback and now. Drop silently.
            return

        # Encode in dedicated thread (libx264 is sync C). PTS in ms.
        try:
            packets = await encoder.encode(bgr, pts_ms=ts)
        except Exception as e:
            logger.error("transcode encode error %s: %s", camera_tag, e)
            return

        # 净化要打进 wire 帧头的 ts。摄像头 PTS 未知时发哨兵 0xFFFFFFFFFFFFFFFF(同
        # transcoder __init__ 注释,典型在 PPCS 重连后头几帧)。编码器侧已改走本地计数器
        # 不再 OverflowError——但这恰好"打开"了这条新路:哨兵帧现在能正常编码并广播,ts
        # 第一次原样流到前端。前端 WebCodecs 走 `new EncodedVideoChunk({timestamp: ts*1000})`,
        # timestamp 是 WebIDL [EnforceRange] long long(±2^63),哨兵 ×1000 ≈ 1.8e22 远超 →
        # 抛 TypeError → watch.html 弹红"解码失败",且那批帧全丢,直到 ts 恢复正常。
        # 服务端单点兜:ts 超出安全区时,用服务端解码 wall-clock(decoded_unix_ms,host
        # unix ms)替代——它永远合法、量级正常(~1.7e12)。一处覆盖所有客户端(WebCodecs + MSE)。
        # 安全上界取 9e15:① 严格小于 2^63/1000 ≈ 9.22e15(保证前端 ts*1000 不溢出 int64);
        # ② 跟前端 watch.html 的净化阈值用同一个字面值,前后端口径完全一致;③ 正常相机 ts
        # (uptime/unix ms ~1e7–1e12)远在其下,真实流不受影响。
        _TS_SAFE_MAX = 9_000_000_000_000_000  # 9e15,与 watch.html 的前端兜底阈值一致
        wire_ts = ts if 0 <= ts < _TS_SAFE_MAX else decoded_unix_ms

        for nal_bytes, is_keyframe in packets:
            # Until we've seen the first IDR in the encoded stream, drop
            # frames so subscribers don't try to decode garbage. After that,
            # forward everything; new browser tabs joining mid-GOP wait for
            # the next IDR (≤ ~1.2s at GOP=30) to start their own decoder.
            if camera_tag not in self._camera_seen_keyframe:
                if not is_keyframe:
                    continue
                self._camera_seen_keyframe.add(camera_tag)

            header = struct.pack(
                ">B7xQ",
                1 if is_keyframe else 0,
                wire_ts & 0xFFFFFFFFFFFFFFFF,
            )
            await self._broadcast(camera_tag, payload=header + nal_bytes)


miot_video_stream_manager = MIoTVideoStreamManager()


class MIoTAudioStreamManager:
    """MIoT Audio WS Manager."""

    _CAMERA_CONNECT_COUNT_MAX: int = 4
    _SAMPLERATE_MAP: dict[str, int] = {"opus": 48000, "g711a": 8000, "g711u": 8000}
    _camera_connect_map: dict[str, dict[str, OrderedDict[str, WebSocket]]]
    _camera_connect_id: int
    _camera_init_done: set

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_connect_id = 0
        self._camera_init_done = set()
        logger.info("Init MIoT Audio WS Manager")

    async def new_connection(
        self,
        websocket: WebSocket,
        user_name: str,
        token_hash: str,
        camera_id: str,
        channel: int,
    ) -> str:
        """New audio stream connection."""
        camera_tag = f"{camera_id}.{channel}"
        if (
            camera_tag not in self._camera_connect_map
            or not self._camera_connect_map[camera_tag]
        ):
            self._camera_connect_map[camera_tag] = {}
            await manager.miot_service.start_audio_stream(
                camera_id=camera_id,
                channel=channel,
                callback=self.__audio_stream_callback,
            )
            logger.info("Start audio stream, %s.%d", camera_id, channel)
        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(self._camera_connect_id)
        self._camera_connect_id += 1
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        if (
            len(self._camera_connect_map[camera_tag][user_tag])
            > self._CAMERA_CONNECT_COUNT_MAX
        ):
            logger.warning(
                "Too many audio connections, %s.%d, %s, remove first",
                camera_id,
                channel,
                user_tag,
            )
            _, ws = self._camera_connect_map[camera_tag][user_tag].popitem(last=False)
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close()
            except Exception as err:
                logger.error("WebSocket close error: %s", err)
        # Send init only if codec is already known (first frame already arrived)
        if camera_tag in self._camera_init_done:
            codec = manager.miot_service.get_audio_codec(camera_id, channel)
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "init",
                        "codec": codec,
                        "sampleRate": self._SAMPLERATE_MAP.get(codec, 48000),
                        "numberOfChannels": 1,
                    }
                )
            )
        logger.info(
            "New audio stream connection, %s, %s, %s",
            camera_tag,
            user_tag,
            connection_id,
        )
        return connection_id

    async def close_connection(
        self, user_name: str, token_hash: str, camera_id: str, channel: int, cid: str
    ):
        """Close audio stream connection."""
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        if (
            camera_tag not in self._camera_connect_map
            or user_tag not in self._camera_connect_map[camera_tag]
            or cid not in self._camera_connect_map[camera_tag][user_tag]
        ):
            return
        logger.info(
            "Close audio stream connection, %s, %s, %s", camera_tag, user_tag, cid
        )
        try:
            ws = self._camera_connect_map[camera_tag][user_tag].pop(cid)
            if ws.client_state == WebSocketState.CONNECTED:
                await ws.close()
        except Exception as err:
            logger.error("WebSocket close error: %s", err)
        if len(self._camera_connect_map[camera_tag][user_tag]) == 0:
            self._camera_connect_map[camera_tag].pop(user_tag, None)
        if len(self._camera_connect_map[camera_tag]) == 0:
            await manager.miot_service.stop_audio_stream(camera_id, channel)
            self._camera_connect_map.pop(camera_tag)
            self._camera_init_done.discard(camera_tag)
            logger.info("No connection, stop audio stream, %s.%d", camera_id, channel)

    async def __audio_stream_callback(
        self, did: str, data: bytes, ts: int, seq: int, channel: int
    ) -> None:
        """Audio stream callback."""

        camera_tag = f"{did}.{channel}"
        if camera_tag not in self._camera_connect_map:
            logger.error("No connection, %s.%d", did, channel)
            await manager.miot_service.stop_audio_stream(did, channel)
            return
        # On first frame: codec is now known, send init to all connected websockets
        if camera_tag not in self._camera_init_done:
            codec = manager.miot_service.get_audio_codec(did, channel)
            if codec:
                self._camera_init_done.add(camera_tag)
                init_msg = json.dumps(
                    {
                        "type": "init",
                        "codec": codec,
                        "sampleRate": self._SAMPLERATE_MAP.get(codec, 48000),
                        "numberOfChannels": 1,
                    }
                )
                logger.info(
                    "Audio codec detected, sending init to all connections, %s codec=%s",
                    camera_tag,
                    codec,
                )
                for conn in self._camera_connect_map[camera_tag].values():
                    for ws in conn.values():
                        try:
                            await ws.send_text(init_msg)
                        except Exception as err:
                            logger.error("Audio init send error: %s", err)
        for conn in self._camera_connect_map[camera_tag].values():
            for ws in conn.values():
                try:
                    await ws.send_bytes(data)
                except Exception as err:
                    logger.error("Audio WebSocket send error: %s", err)


miot_audio_stream_manager = MIoTAudioStreamManager()


class MIoTDecodedAudioStreamManager:
    """Manage decoded audio stream WebSocket connections.

    Serves 16kHz mono s16 PCM audio (post-decode, post-AGC).
    """

    _camera_connect_map: dict[str, dict[str, dict[str, WebSocket]]]
    _camera_reg_ids: dict[str, int]  # camera_tag -> reg_id

    def __init__(self):
        self._camera_connect_map = {}
        self._camera_reg_ids = {}

    async def new_connection(
        self,
        websocket: WebSocket,
        user_name: str,
        token_hash: str,
        camera_id: str,
        channel: int,
    ) -> str:
        camera_tag = f"{camera_id}.{channel}"
        if (
            camera_tag not in self._camera_connect_map
            or not self._camera_connect_map[camera_tag]
        ):
            self._camera_connect_map[camera_tag] = {}
            reg_id = await manager.miot_service.start_decoded_audio_stream(
                camera_id=camera_id,
                channel=channel,
                callback=self.__decoded_audio_callback,
            )
            self._camera_reg_ids[camera_tag] = reg_id
            logger.info("Start decoded audio stream, %s.%d, reg_id=%d", camera_id, channel, reg_id)
        user_tag = f"{user_name}.{token_hash}"
        self._camera_connect_map[camera_tag].setdefault(user_tag, OrderedDict())
        connection_id = str(id(websocket))
        self._camera_connect_map[camera_tag][user_tag][connection_id] = websocket
        return connection_id

    async def close_connection(
        self, user_name: str, token_hash: str, camera_id: str, channel: int, connection_id: str
    ):
        camera_tag = f"{camera_id}.{channel}"
        user_tag = f"{user_name}.{token_hash}"
        if camera_tag in self._camera_connect_map:
            if user_tag in self._camera_connect_map[camera_tag]:
                self._camera_connect_map[camera_tag][user_tag].pop(connection_id, None)
                if not self._camera_connect_map[camera_tag][user_tag]:
                    del self._camera_connect_map[camera_tag][user_tag]
            if not self._camera_connect_map[camera_tag]:
                del self._camera_connect_map[camera_tag]
                reg_id = self._camera_reg_ids.pop(camera_tag, None)
                if reg_id is not None:
                    await manager.miot_service.stop_decoded_audio_stream(camera_id, channel, reg_id)
                logger.info("Stop decoded audio stream, %s.%d", camera_id, channel)

    async def __decoded_audio_callback(
        self, did: str, pcm_ndarray, ts: int, channel: int, recv_unix_ms: int = 0, decoded_unix_ms: int = 0
    ) -> None:
        """Callback for decoded audio frames from decoder.py.

        pcm_ndarray is already a numpy int16 array (16kHz mono, post-AGC).
        """
        camera_tag = f"{did}.{channel}"
        if camera_tag not in self._camera_connect_map:
            return

        # Convert numpy array to raw PCM bytes
        try:
            pcm_bytes = pcm_ndarray.tobytes()
        except Exception as err:
            logger.error("Decoded audio convert error: %s", err)
            return

        for conn in self._camera_connect_map[camera_tag].values():
            for ws in conn.values():
                try:
                    await ws.send_bytes(pcm_bytes)
                except Exception as err:
                    logger.error("Decoded audio WebSocket send error: %s", err)


miot_decoded_audio_stream_manager = MIoTDecodedAudioStreamManager()
