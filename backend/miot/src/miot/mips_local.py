# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT local MQTT (mips_local) client for the central hub gateway (中枢网关).

The central hub gateway exposes an mTLS MQTT broker on the LAN. On top of the
MQTT payload the gateway speaks MIPS: a binary TLV envelope carrying a message
id, a reply topic and a JSON payload — effectively a lightweight RPC over MQTT.

This client:
  - connects to the gateway broker over mTLS (client cert from `cert.py`),
  - subscribes ``{did}/#`` (replies + all pushes for devices under the gateway)
    and ``master/appMsg/devListChange``,
  - pulls the device list via ``master/proxy/getDevList``,
  - controls devices via ``master/proxy/rpcReq`` and ``master/proxy/get``,
  - receives property / event pushes and device-list-change notifications.

MIPS protocol and topic layout are ported from the Xiaomi Home integration
(`miot_mips.py::MipsLocalClient`). The MQTT/asyncio plumbing (paho ``loop_start``
network thread + ``call_soon_threadsafe`` dispatch to the main loop) mirrors this
project's ``mips_cloud.py`` rather than HA's separate-internal-loop model.

Threading:
    - paho runs its own network thread. All paho callbacks fire on that thread.
    - request replies, broadcasts and dev-list-change are hopped to ``loop``
      via ``call_soon_threadsafe``. Request timeout timers live on ``loop``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import ssl
import struct
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from paho.mqtt.client import (
    Client,
    MQTTMessage,
    MQTTv5,
    topic_matches_sub,
)
from paho.mqtt.enums import CallbackAPIVersion, MQTTErrorCode

from .const import (
    MIHOME_MQTT_KEEPALIVE,
    MIPS_LOCAL_PORT_DEFAULT,
    MIPS_LOCAL_RECONNECT_MAX_SEC,
    MIPS_LOCAL_RECONNECT_MIN_SEC,
    MIPS_LOCAL_RPC_TIMEOUT,
)
from .error import MIoTErrorCode, MIoTMipsError
from .types import MipsConnectionError

_LOGGER = logging.getLogger(__name__)

_UINT32_MAX: int = 0xFFFFFFFF
_MIPS_QOS: int = 2
_CONNECT_TIMEOUT: float = 15.0
# MQTT v5 CONNACK reason code 0x87 "Not authorized".
_CONNACK_NOT_AUTHORIZED: int = 0x87


class _MipsMsgType(Enum):
    """MIPS TLV field type."""

    ID = 0
    RET_TOPIC = 1
    PAYLOAD = 2
    FROM = 3
    MAX = 4


class _MipsMessage:
    """A MIPS message: id + optional from/ret_topic + JSON payload.

    Wire format is a concatenation of TLV fields, each ``len(4B LE) + type(1B)
    + data``. For string fields, ``len`` includes a trailing NUL, i.e.
    ``len = len(str) + 1``.
    """

    def __init__(
        self,
        mid: int = 0,
        msg_from: Optional[str] = None,
        ret_topic: Optional[str] = None,
        payload: Optional[str] = None,
    ) -> None:
        self.mid = mid
        self.msg_from = msg_from
        self.ret_topic = ret_topic
        self.payload = payload

    @staticmethod
    def unpack(data: bytes) -> "_MipsMessage":
        msg = _MipsMessage()
        data_len = len(data)
        data_start = 0
        while data_start < data_len:
            data_end = data_start + 5
            unpack_len, unpack_type = struct.unpack("<IB", data[data_start:data_end])
            unpack_data = data[data_end : data_end + unpack_len]
            if unpack_type == _MipsMsgType.ID.value:
                msg.mid = int.from_bytes(unpack_data, byteorder="little")
            elif unpack_type == _MipsMsgType.RET_TOPIC.value:
                msg.ret_topic = str(unpack_data.rstrip(b"\x00"), "utf-8")
            elif unpack_type == _MipsMsgType.PAYLOAD.value:
                msg.payload = str(unpack_data.rstrip(b"\x00"), "utf-8")
            elif unpack_type == _MipsMsgType.FROM.value:
                msg.msg_from = str(unpack_data.rstrip(b"\x00"), "utf-8")
            data_start = data_end + unpack_len
        return msg

    @staticmethod
    def pack(
        mid: int,
        payload: str,
        msg_from: Optional[str] = None,
        ret_topic: Optional[str] = None,
    ) -> bytes:
        if mid is None or payload is None:
            raise MIoTMipsError("invalid mid or payload")
        pack_msg: bytes = b""
        # mid: fixed 4-byte body
        pack_msg += struct.pack("<IBI", 4, _MipsMsgType.ID.value, mid)
        # String fields: the declared length is the UTF-8 *byte* length + 1 for
        # the trailing NUL. Must encode first and size on the encoded bytes —
        # sizing on len(str) truncates any multibyte (e.g. Chinese) content.
        if msg_from:
            data = msg_from.encode("utf-8")
            n = len(data)
            pack_msg += struct.pack(f"<IB{n}sx", n + 1, _MipsMsgType.FROM.value, data)
        if ret_topic:
            data = ret_topic.encode("utf-8")
            n = len(data)
            pack_msg += struct.pack(
                f"<IB{n}sx", n + 1, _MipsMsgType.RET_TOPIC.value, data
            )
        data = payload.encode("utf-8")
        n = len(data)
        pack_msg += struct.pack(f"<IB{n}sx", n + 1, _MipsMsgType.PAYLOAD.value, data)
        return pack_msg

    def __str__(self) -> str:
        return f"{self.mid}, {self.msg_from}, {self.ret_topic}, {self.payload}"


@dataclass
class _MipsRequest:
    """A pending RPC request awaiting its reply (matched by mid)."""

    mid: int
    future: asyncio.Future
    timer: Optional[asyncio.TimerHandle] = None


@dataclass
class _MipsBroadcast:
    """A registered broadcast subscription (property/event push)."""

    # Matcher pattern, e.g. "{did}/appMsg/notify/iot/{did2}/property/#".
    topic: str
    handler: Callable[[str, str, Any], None]
    handler_ctx: Any = None


def _resolve_future(fut: "asyncio.Future", value: str) -> None:
    """Atomically complete *fut* on the event loop.

    Both the check and the set_result must run in the same callback —
    the timeout timer also fires on the event loop and can slip between
    a paho-thread ``done()`` check and a separately-queued ``set_result``.
    """
    if not fut.done():
        fut.set_result(value)


def _discard_stale_connect(mqtt: Client) -> None:
    """Close a client whose ``connect()`` outlived our TCP/TLS timeout.

    Runs in an executor thread once the abandoned ``connect()`` finally returns
    (or raises); best-effort by design — the client is already being thrown
    away, only the fd matters.
    """
    try:
        mqtt.disconnect()
    except Exception as e:  # noqa: BLE001 - fd cleanup only, nothing to recover
        _LOGGER.debug("mips_local discard stale connect raised: %s", e)


def _complete_future(fut: "asyncio.Future", value: object = None) -> None:
    """Atomically complete *fut* on the event loop, tolerating both result
    and exception values. ``_resolve_future`` handles the result-only case
    (RPC replies); this is the general form used by the connect future and
    deinit-time request cleanup, where the value may be an exception.
    """
    if fut.done():
        return
    if isinstance(value, BaseException):
        fut.set_exception(value)
    else:
        fut.set_result(value)


class MipsLocalClient:
    """Local MQTT client for one central hub gateway (one group_id)."""

    def __init__(
        self,
        did: str,
        host: str,
        group_id: str,
        ca_file: str,
        cert_file: str,
        key_file: str,
        port: int = MIPS_LOCAL_PORT_DEFAULT,
        home_name: str = "",
        loop: Optional[asyncio.AbstractEventLoop] = None,
        client_factory: Optional[Callable[..., Client]] = None,
    ) -> None:
        if not did:
            raise ValueError("did is required")
        if not host:
            raise ValueError("host is required")
        if not group_id:
            raise ValueError("group_id is required")

        self._did = did
        self._host = host
        self._group_id = group_id
        self._ca_file = ca_file
        self._cert_file = cert_file
        self._key_file = key_file
        self._port = port
        self._home_name = home_name
        self._main_loop = loop or asyncio.get_running_loop()
        self._client_factory = client_factory or self._default_client_factory

        self._reply_topic = f"{did}/reply"
        self._dev_list_change_topic = f"{did}/appMsg/devListChange"

        self._mqtt: Optional[Client] = None
        self._state_lock = threading.Lock()
        self._connected: bool = False
        self._connect_future: Optional[asyncio.Future[None]] = None

        self._mips_seed_id: int = random.randint(0, _UINT32_MAX)

        # mid(str) -> pending request. Accessed from paho thread + main loop.
        self._request_map: dict[str, _MipsRequest] = {}
        self._request_lock = threading.Lock()

        # matcher pattern -> broadcast registration.
        self._broadcasts: dict[str, _MipsBroadcast] = {}
        self._broadcasts_lock = threading.Lock()

        self._on_dev_list_changed: Optional[
            Callable[["MipsLocalClient", list], Coroutine]
        ] = None

    # ------------------------------------------------------------------ props

    @property
    def did(self) -> str:
        return self._did

    @property
    def group_id(self) -> str:
        return self._group_id

    @property
    def host(self) -> str:
        return self._host

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def on_dev_list_changed(
        self,
    ) -> Optional[Callable[["MipsLocalClient", list], Coroutine]]:
        return self._on_dev_list_changed

    @on_dev_list_changed.setter
    def on_dev_list_changed(
        self, func: Optional[Callable[["MipsLocalClient", list], Coroutine]]
    ) -> None:
        self._on_dev_list_changed = func

    # ----------------------------------------------------------- factory hook

    @staticmethod
    def _default_client_factory(client_id: str) -> Client:
        return Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTv5,
        )

    # --------------------------------------------------------------- lifecycle

    async def init_async(self) -> None:
        """Connect to the gateway broker over mTLS and wait for CONNACK."""
        if self._mqtt is not None:
            _LOGGER.warning("mips_local(%s) already initialized", self._group_id)
            return

        mqtt = self._client_factory(self._did)
        # mTLS: present our Ed25519 client cert. Hostname verification is off
        # because we connect to the gateway by LAN IP (from mDNS), whose cert
        # CN does not match the IP.
        mqtt.tls_set(
            ca_certs=self._ca_file,
            certfile=self._cert_file,
            keyfile=self._key_file,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        mqtt.tls_insecure_set(True)
        mqtt.reconnect_delay_set(
            min_delay=int(MIPS_LOCAL_RECONNECT_MIN_SEC),
            max_delay=int(MIPS_LOCAL_RECONNECT_MAX_SEC),
        )
        mqtt.on_connect = self._on_connect
        mqtt.on_disconnect = self._on_disconnect
        mqtt.on_message = self._on_message

        self._mqtt = mqtt
        self._connect_future = self._main_loop.create_future()

        try:
            # 必须套超时:网关 IP 静默丢包(真黑洞,交换机连 ARP 都不代答)时
            # mqtt.connect 会阻塞到内核放弃 SYN 重试(Linux 默认 ~2 分钟),而
            # __ensure_desired_connections 是逐网关串行 await —— 一台挂住就把其它
            # 网关的重连和白名单补刷一起压住两分钟。
            connect_fut = self._main_loop.run_in_executor(
                None,
                lambda: mqtt.connect(
                    host=self._host,
                    port=self._port,
                    keepalive=MIHOME_MQTT_KEEPALIVE,
                    clean_start=True,
                ),
            )
            await asyncio.wait_for(connect_fut, timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError as e:
            # executor 里的线程取消不掉,connect 迟早会返回或抛错。挂个回调在那一刻
            # 收尾,否则黑洞网关每轮重连都漏一个已建立的 socket(此处没有
            # loop_start,不存在网络线程,只是 fd)。
            connect_fut.add_done_callback(
                lambda _f: self._main_loop.run_in_executor(
                    None, _discard_stale_connect, mqtt
                )
            )
            self._connect_future = None
            self._mqtt = None
            raise MipsConnectionError(
                f"mips_local({self._group_id}) TCP/TLS connect timeout"
            ) from e
        except Exception as e:
            self._connect_future = None
            self._mqtt = None
            raise MipsConnectionError(
                f"mips_local({self._group_id}) TCP/TLS connect failed: {e}"
            ) from e

        mqtt.loop_start()

        try:
            await asyncio.wait_for(self._connect_future, timeout=_CONNECT_TIMEOUT)
        except (asyncio.TimeoutError, MipsConnectionError) as e:
            self._connect_future = None
            try:
                mqtt.loop_stop()
                mqtt.disconnect()
            except Exception:
                pass  # best-effort teardown of a half-open client; errors here are irrelevant
            self._mqtt = None
            if isinstance(e, asyncio.TimeoutError):
                raise MipsConnectionError(
                    f"mips_local({self._group_id}) CONNACK timeout"
                ) from e
            raise

        _LOGGER.info(
            "mips_local connected, group_id=%s host=%s did=%s",
            self._group_id,
            self._host,
            self._did,
        )

    async def deinit_async(self) -> None:
        """Disconnect and stop the paho network thread."""
        mqtt = self._mqtt
        if mqtt is None:
            return
        self._mqtt = None
        try:
            await self._main_loop.run_in_executor(None, mqtt.disconnect)
        except Exception as e:
            _LOGGER.warning("mips_local disconnect raised: %s", e)
        try:
            await self._main_loop.run_in_executor(None, mqtt.loop_stop)
        except Exception as e:
            _LOGGER.warning("mips_local loop_stop raised: %s", e)

        with self._state_lock:
            self._connected = False
        with self._request_lock:
            for req in self._request_map.values():
                if req.timer:
                    req.timer.cancel()
                # 收尾成**与应答超时同形**的结果,而不是抛 MipsConnectionError。
                # 理由:future 只在 __mips_publish 成功之后才进 _request_map(见
                # __request_async 的 except 分支会把它摘掉),所以"在途"必然意味着
                # 报文已经发给网关 —— 结果未知,不是"肯定没执行"。路由层把异常一律
                # 当"明确未执行"转云端重发,若这里抛异常,连接被替换/拆除(切家庭、
                # 切号、20s 重连 sweep 撞上 ≤5s 应答窗口)就会让非幂等动作走两遍。
                # -10006 落进既有的"结果未知"分支:set/action 不重发 + 冷却,
                # get 幂等仍走云端兜底。
                # done() 检查 + 完成动作原子(与 _fire_connect_future 同型竞态):
                # 超时回调可能已先完成 future,这里排队的完成会落到已完成的
                # future 上抛 InvalidStateError。
                self._main_loop.call_soon_threadsafe(
                    _complete_future,
                    req.future,
                    {
                        "error": {
                            "code": MIoTErrorCode.CODE_TIMEOUT.value,
                            "message": "mips_local deinit during request",
                        }
                    },
                )
            self._request_map.clear()
        with self._broadcasts_lock:
            self._broadcasts.clear()

    # -------------------------------------------------------------- device API

    async def get_dev_list_async(
        self, payload: Optional[str] = None, timeout_ms: Optional[int] = None
    ) -> dict[str, dict]:
        """Pull the gateway device list (master/proxy/getDevList).

        Returns ``{did: {did, online, specv2_access, push_available}}``.
        """
        result_obj = await self.__request_async(
            topic="proxy/getDevList", payload=payload or "{}", timeout_ms=timeout_ms
        )
        if not result_obj or "devList" not in result_obj:
            raise MIoTMipsError("invalid getDevList result")
        device_list: dict[str, dict] = {}
        for did, info in result_obj["devList"].items():
            device_list[did] = {
                "did": did,
                "online": info.get("online", False),
                "specv2_access": info.get("specV2Access", False),
                "push_available": info.get("pushAvailable", False),
            }
        return device_list

    async def set_prop_async(
        self, did: str, siid: int, piid: int, value: Any, timeout_ms: Optional[int] = None
    ) -> dict:
        """Set one property via master/proxy/rpcReq (method=set_properties)."""
        payload_obj: dict = {
            "did": did,
            "rpc": {
                "id": self.__gen_mips_id(),
                "method": "set_properties",
                "params": [{"did": did, "siid": siid, "piid": piid, "value": value}],
            },
        }
        result_obj = await self.__request_async(
            topic="proxy/rpcReq", payload=json.dumps(payload_obj), timeout_ms=timeout_ms
        )
        # 回包可能是任何合法 JSON(int / str / list / 结构不同的 dict)。守卫必须
        # 逐层验型:任何一层不符就落到最后的 -10041,绝不允许抛异常或返回非 dict
        # ——client.py 把异常和"非 dict / 无 code"都判成"肯定没执行"并云端重发,
        # 而网关既然回了包,请求一定送到过。之前只挡了"是 dict 但没有 result/error
        # 键"这一种畸形回包:result 是对象而非单元素数组会在下标访问处抛 KeyError,
        # error 是字符串而非对象会原样返回字符串(client.py 里 isinstance 判 code
        # 为 None),两条路都终结于云端重发——同一条非幂等指令在设备上执行两次。
        if isinstance(result_obj, dict):
            result = result_obj.get("result")
            if (
                isinstance(result, list)
                and len(result) == 1
                and isinstance(result[0], dict)
                and result[0].get("did") == did
                and "code" in result[0]
            ):
                return result[0]
            error = result_obj.get("error")
            if isinstance(error, dict) and "code" in error:
                return error
            # __request_async 自产的码(如 -10040 应答非合法 JSON)原样上抛,不要在
            # 这里重新贴成 -10004:那会把"网关已回包"的事实抹掉,让上层误判成
            # "肯定没执行"而重发云端。落到最后一行的是"回了包但结构不认识",同样
            # 是结果未知。
            if "code" in result_obj:
                return result_obj
        return {
            "code": MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value,
            "message": "Invalid result",
        }

    async def get_prop_async(
        self, did: str, siid: int, piid: int, timeout_ms: Optional[int] = None
    ) -> dict:
        """Read one property via master/proxy/get.

        Returns a dict shaped like ``set_prop_async``/``action_async`` so the
        caller can distinguish a *timeout* from a *fast rejection* — important
        because only timeouts should trigger the did's cooldown window.
        """
        result_obj = await self.__request_async(
            topic="proxy/get",
            payload=json.dumps({"did": did, "siid": siid, "piid": piid}),
            timeout_ms=timeout_ms,
        )
        if isinstance(result_obj, dict) and "value" in result_obj:
            return {"value": result_obj.get("value"), "code": result_obj.get("code", 0)}
        if isinstance(result_obj, dict) and "error" in result_obj:
            return result_obj["error"]
        # __request_async 自产的码(如 -10040 应答非合法 JSON)原样上抛,不要在这里
        # 重新贴成 -10004:那会把"网关已回包"的事实抹掉,让上层误判成"肯定没执行"
        # 而重发云端。落到最后一行的是"回了包但结构不认识",同样是结果未知。
        if isinstance(result_obj, dict) and "code" in result_obj:
            return result_obj
        return {
            "code": MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value,
            "message": "Invalid result",
        }

    async def action_async(
        self,
        did: str,
        siid: int,
        aiid: int,
        in_list: list,
        timeout_ms: Optional[int] = None,
    ) -> dict:
        """Call an action via master/proxy/rpcReq (method=action)."""
        payload_obj: dict = {
            "did": did,
            "rpc": {
                "id": self.__gen_mips_id(),
                "method": "action",
                "params": {"did": did, "siid": siid, "aiid": aiid, "in": in_list},
            },
        }
        result_obj = await self.__request_async(
            topic="proxy/rpcReq", payload=json.dumps(payload_obj), timeout_ms=timeout_ms
        )
        # 同 set_prop_async 的逐层验型理由:result 是非 dict(如整数)会在
        # "code" in result_obj["result"] 处抛 TypeError,error 是字符串而非对象
        # 会原样返回字符串,两条路都被上层判成"肯定没执行"而云端重发——累加型
        # 动作(窗帘 +10% / 音量 +1)正是本模块反复强调最不能容忍双发的场景。
        if isinstance(result_obj, dict):
            result = result_obj.get("result")
            if isinstance(result, dict) and "code" in result:
                return result
            error = result_obj.get("error")
            if isinstance(error, dict) and "code" in error:
                return error
            # __request_async 自产的码(如 -10040 应答非合法 JSON)原样上抛,不要在
            # 这里重新贴成 -10004:那会把"网关已回包"的事实抹掉,让上层误判成
            # "肯定没执行"而重发云端。落到最后一行的是"回了包但结构不认识",同样
            # 是结果未知。
            if "code" in result_obj:
                return result_obj
        return {
            "code": MIoTErrorCode.CODE_MIPS_RESULT_UNKNOWN.value,
            "message": "Invalid result",
        }

    # ------------------------------------------------------- push subscription

    def sub_prop(
        self,
        did: str,
        handler: Callable[[dict, Any], None],
        siid: Optional[int] = None,
        piid: Optional[int] = None,
        handler_ctx: Any = None,
    ) -> bool:
        """Subscribe property-change pushes for a device (all props if siid/piid None)."""
        topic = (
            f"appMsg/notify/iot/{did}/property/"
            f'{"#" if siid is None or piid is None else f"{siid}.{piid}"}'
        )

        def on_prop_msg(sub_topic: str, payload: str, ctx: Any) -> None:
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                _LOGGER.info("mips_local unknown prop msg, %s", payload)
                return
            if not isinstance(msg, dict) or not {"did", "siid", "piid", "value"} <= set(
                msg
            ):
                _LOGGER.info("mips_local unknown prop msg, %s", payload)
                return
            if handler:
                handler(msg, ctx)

        return self.__reg_broadcast(topic, on_prop_msg, handler_ctx)

    def unsub_prop(
        self, did: str, siid: Optional[int] = None, piid: Optional[int] = None
    ) -> bool:
        topic = (
            f"appMsg/notify/iot/{did}/property/"
            f'{"#" if siid is None or piid is None else f"{siid}.{piid}"}'
        )
        return self.__unreg_broadcast(topic)

    def sub_event(
        self,
        did: str,
        handler: Callable[[dict, Any], None],
        siid: Optional[int] = None,
        eiid: Optional[int] = None,
        handler_ctx: Any = None,
    ) -> bool:
        """Subscribe event pushes for a device (all events if siid/eiid None)."""
        topic = (
            f"appMsg/notify/iot/{did}/event/"
            f'{"#" if siid is None or eiid is None else f"{siid}.{eiid}"}'
        )

        def on_event_msg(sub_topic: str, payload: str, ctx: Any) -> None:
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                _LOGGER.info("mips_local unknown event msg, %s", payload)
                return
            if not isinstance(msg, dict) or not {"did", "siid", "eiid"} <= set(msg):
                _LOGGER.info("mips_local unknown event msg, %s", payload)
                return
            msg.setdefault("arguments", [])
            if handler:
                handler(msg, ctx)

        return self.__reg_broadcast(topic, on_event_msg, handler_ctx)

    def unsub_event(
        self, did: str, siid: Optional[int] = None, eiid: Optional[int] = None
    ) -> bool:
        topic = (
            f"appMsg/notify/iot/{did}/event/"
            f'{"#" if siid is None or eiid is None else f"{siid}.{eiid}"}'
        )
        return self.__unreg_broadcast(topic)

    # -------------------------------------------------------------- internals

    def __gen_mips_id(self) -> int:
        mips_id = self._mips_seed_id
        self._mips_seed_id = (self._mips_seed_id + 1) % (_UINT32_MAX + 1)
        return mips_id

    def __mips_subscribe(self, topic: str) -> None:
        mqtt = self._mqtt
        if mqtt is None:
            return
        result, mid = mqtt.subscribe(topic, qos=_MIPS_QOS)
        if result != MQTTErrorCode.MQTT_ERR_SUCCESS:
            _LOGGER.warning("mips_local subscribe(%s) failed: %s", topic, result)
        else:
            _LOGGER.debug("mips_local subscribed topic=%s", topic)

    def __mips_unsubscribe(self, topic: str) -> None:
        mqtt = self._mqtt
        if mqtt is None:
            return
        try:
            mqtt.unsubscribe(topic)
        except Exception as e:
            _LOGGER.warning("mips_local unsubscribe(%s) raised: %s", topic, e)

    def __mips_publish(self, topic: str, payload: str, mid: int) -> None:
        mqtt = self._mqtt
        if mqtt is None:
            raise MipsConnectionError("mips_local not connected; cannot publish")
        packed = _MipsMessage.pack(
            mid=mid, payload=payload, msg_from="local", ret_topic=self._reply_topic
        )
        info = mqtt.publish(topic.strip(), packed, qos=_MIPS_QOS)
        # paho 2.x 对"未连接"不抛异常,只把 rc 置成 MQTT_ERR_NO_CONN 并丢弃报文。
        # 不查 rc 的话:连接恰在 __request_async 的 _connected 检查与这里之间断掉时,
        # 请求已入在途表且定时器已武装,调用方干等满 5s 拿 -10006,而路由层把 -10006
        # 按"可能已送达"处置(set/action 不回落云端 + 冷却)—— 一条根本没出本机的
        # 指令被当成结果未知,用户操作无声丢失。抛异常才是准确的"明确未发出"。
        if info.rc != MQTTErrorCode.MQTT_ERR_SUCCESS:
            raise MipsConnectionError(
                f"mips_local({self._group_id}) publish failed, rc={info.rc}"
            )

    async def __request_async(
        self, topic: str, payload: str, timeout_ms: Optional[int] = None
    ) -> dict:
        """Publish an RPC to master/{topic} and await the reply (matched by mid)."""
        if not self._connected or self._mqtt is None:
            raise MipsConnectionError("mips_local not connected; cannot request")
        timeout_s = (timeout_ms / 1000) if timeout_ms else MIPS_LOCAL_RPC_TIMEOUT
        mid = self.__gen_mips_id()
        future: asyncio.Future = self._main_loop.create_future()

        def on_timeout() -> None:
            with self._request_lock:
                self._request_map.pop(str(mid), None)
            if not future.done():
                future.set_result(
                    {
                        "error": {
                            "code": MIoTErrorCode.CODE_TIMEOUT.value,
                            "message": "timeout",
                        }
                    }
                )

        timer = self._main_loop.call_later(timeout_s, on_timeout)
        with self._request_lock:
            self._request_map[str(mid)] = _MipsRequest(mid=mid, future=future, timer=timer)

        pub_topic = f"master/{topic}"
        try:
            self.__mips_publish(pub_topic, payload, mid)
        except Exception:
            timer.cancel()
            with self._request_lock:
                self._request_map.pop(str(mid), None)
            raise
        _LOGGER.debug("mips_local request mid=%s topic=%s", mid, pub_topic)

        result = await future
        if isinstance(result, dict):
            return result
        try:
            return json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return {
                "code": MIoTErrorCode.CODE_MIPS_INVALID_RESULT.value,
                "message": f"Error: {result}",
            }

    def __reg_broadcast(
        self, topic: str, handler: Callable[[str, str, Any], None], handler_ctx: Any
    ) -> bool:
        # Matcher key is prefixed with our did (broadcasts are delivered under
        # {did}/...); registration is issued to the gateway as master/{topic}.
        sub_topic = f"{self._did}/{topic}"
        with self._broadcasts_lock:
            if sub_topic in self._broadcasts:
                _LOGGER.debug("mips_local re-reg broadcast, %s", sub_topic)
                return True
            self._broadcasts[sub_topic] = _MipsBroadcast(
                topic=sub_topic, handler=handler, handler_ctx=handler_ctx
            )
        self.__mips_subscribe(f"master/{topic}")
        return True

    def __unreg_broadcast(self, topic: str) -> bool:
        sub_topic = f"{self._did}/{topic}"
        with self._broadcasts_lock:
            if sub_topic not in self._broadcasts:
                return False
            del self._broadcasts[sub_topic]
        self.__mips_unsubscribe(f"master/{topic}")
        return True

    # ----------------------------------------------------------- paho hooks

    def _on_connect(
        self, client: Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        rc_value = getattr(reason_code, "value", reason_code)
        if rc_value != 0:
            _LOGGER.error(
                "mips_local CONNACK rejected, group_id=%s reason_code=%s",
                self._group_id,
                reason_code,
            )
            # 0x87 Not authorized: the gateway authorizes a single central-hub
            # client per Mi account, so this almost always means another
            # instance (another Miloco / the Xiaomi Home HA integration) is
            # signed in with the same account and already holds the slot.
            if rc_value == _CONNACK_NOT_AUTHORIZED:
                self._fire_connect_future(
                    MipsConnectionError(
                        "CONNACK 0x87 Not authorized: another central-hub client "
                        "on this Mi account is likely holding the connection "
                        "(only one per account is allowed)"
                    )
                )
            else:
                self._fire_connect_future(
                    MipsConnectionError(f"CONNACK reason_code={reason_code}")
                )
            return
        with self._state_lock:
            self._connected = True
        _LOGGER.info("mips_local CONNACK success, group_id=%s", self._group_id)
        # Everything addressed to us (replies + pushes) + device-list change.
        self.__mips_subscribe(f"{self._did}/#")
        self.__mips_subscribe("master/appMsg/devListChange")
        # Re-register broadcasts after a reconnect.
        with self._broadcasts_lock:
            patterns = list(self._broadcasts.keys())
        for sub_topic in patterns:
            # sub_topic == "{did}/{topic}"; register as "master/{topic}".
            self.__mips_subscribe("master/" + sub_topic[len(self._did) + 1 :])
        self._fire_connect_future(None)

    def _on_disconnect(
        self, client: Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        rc_value = getattr(reason_code, "value", reason_code)
        log = _LOGGER.info if rc_value == 0 else _LOGGER.warning
        log("mips_local disconnected, group_id=%s reason_code=%s", self._group_id, reason_code)
        with self._state_lock:
            self._connected = False

    def _on_message(self, client: Client, userdata: Any, msg: MQTTMessage) -> None:
        topic = msg.topic
        try:
            mips_msg = _MipsMessage.unpack(msg.payload)
        except Exception as e:
            _LOGGER.warning("mips_local failed to unpack, topic=%s: %s", topic, e)
            return

        # 1) RPC reply.
        if topic == self._reply_topic:
            with self._request_lock:
                req = self._request_map.pop(str(mips_msg.mid), None)
            if req:
                if req.timer:
                    self._main_loop.call_soon_threadsafe(req.timer.cancel)
                # queue the resolve on the event loop so done() +
                # set_result happen as one atomic operation.  If we check
                # done() on the paho thread and queue the set_result
                # separately, the timeout callback (which also runs on the
                # event loop) can slip between them and complete the future
                # first — the trailing set_result hits an already-done
                # future and raises InvalidStateError.
                self._main_loop.call_soon_threadsafe(
                    _resolve_future, req.future, mips_msg.payload or "{}"
                )
            return

        # 2) Broadcast (property / event push).
        with self._broadcasts_lock:
            broadcasts = list(self._broadcasts.values())
        matched = False
        for bc in broadcasts:
            if not topic_matches_sub(bc.topic, topic):
                continue
            matched = True
            # Strip the leading "{did}/" so handlers see "appMsg/notify/...".
            stripped = topic[topic.find("/") + 1 :]
            self._main_loop.call_soon_threadsafe(
                bc.handler, stripped, mips_msg.payload or "{}", bc.handler_ctx
            )
        if matched:
            return

        # 3) Device-list change.
        if topic in (self._dev_list_change_topic, "master/appMsg/devListChange"):
            if mips_msg.payload is None:
                return
            try:
                payload_obj = json.loads(mips_msg.payload)
            except json.JSONDecodeError:
                _LOGGER.error("mips_local bad devListChange, %s", mips_msg.payload)
                return
            dev_list = payload_obj.get("devList")
            if not isinstance(dev_list, list):
                return
            if self._on_dev_list_changed:
                self._main_loop.call_soon_threadsafe(
                    self._main_loop.create_task,
                    self._on_dev_list_changed(self, dev_list),
                )
            return

        _LOGGER.debug("mips_local recv unhandled msg, topic=%s", topic)

    # --------------------------------------------------------------- helpers

    def _fire_connect_future(self, error: Optional[Exception]) -> None:
        fut = self._connect_future
        if fut is None:
            return
        # done() 检查 + set_result/set_exception 必须原子:wait_for 超时 cancel
        # 与这里的 set_result 之间若分离,排队的 set_result 会落到已取消 future
        # 上抛 InvalidStateError。塞进同一个事件循环回调(_complete_future)。
        self._main_loop.call_soon_threadsafe(
            _complete_future, fut, error if error is not None else None
        )
