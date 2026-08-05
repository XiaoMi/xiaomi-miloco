#!/usr/bin/env python3
"""
UDP LAN broadcast relay daemon for Docker bridge networks.

Optional component — only needed when Miloco runs in a Docker container
without --net=host.  Intercepts Xiaomi miot.lan broadcast probes on the
Docker bridge and relays them to the real LAN, then injects device
responses back so that the miot.lan discovery can find local cameras.

Usage
-----
    python3 udp_lan_relay.py [camera_ip ...]

    Camera IPs default to the ``MILOCO_RELAY_CAMERAS`` env var (comma-
    separated) or the value baked into ``CAMERA_IPS_DEFAULT`` below.

Environment
-----------
``MILOCO_RELAY_BROADCAST``   LAN broadcast address  (default 192.168.31.255)
``MILOCO_RELAY_PORT``        miot UDP port          (default 54321)
``MILOCO_RELAY_CAMERAS``     comma-separated IPs    (default from code)
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import socket
import struct
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("udp_lan_relay")

PROBE_MAGIC = b"\x21\x31"
CAMERA_IPS_DEFAULT = ["192.168.31.38"]
MIOT_PORT = 54321
DEDUP_SECONDS = 10
CLEANUP_INTERVAL = 60  # seconds between TTL sweeps
RESPONSE_TIMEOUT = 1.5  # seconds to wait for device responses


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_camera_ips(args: list[str]) -> list[str]:
    """Resolve camera IPs from CLI args, env, or default."""
    if args:
        return args
    env_val = os.environ.get("MILOCO_RELAY_CAMERAS", "")
    if env_val:
        return [ip.strip() for ip in env_val.split(",") if ip.strip()]
    return list(CAMERA_IPS_DEFAULT)


# ---------------------------------------------------------------------------
# relay
# ---------------------------------------------------------------------------

class UDPRelay:
    """Capture miot.lan probes → forward to LAN → inject responses back."""

    def __init__(self, camera_ips: list[str], broadcast_ip: str) -> None:
        self.camera_ips = camera_ips
        self.broadcast_ip = broadcast_ip
        self._relay_count = 0
        self._seen: dict[str, float] = {}
        self._running = True

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        raw = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003)
        )
        raw.settimeout(1.0)

        tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx_sock.settimeout(RESPONSE_TIMEOUT)

        inject_sock = socket.socket(
            socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW
        )
        inject_sock.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)

        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_running", False))

        logger.info(
            "UDP relay started — cameras=%s  broadcast=%s:%d",
            self.camera_ips,
            self.broadcast_ip,
            MIOT_PORT,
        )

        last_cleanup = time.time()
        try:
            while self._running:
                try:
                    packet, _ = raw.recvfrom(65535)
                    self._handle(packet, tx_sock, inject_sock)
                except socket.timeout:
                    if time.time() - last_cleanup > CLEANUP_INTERVAL:
                        self._cleanup()
                        last_cleanup = time.time()
                except OSError:
                    break  # socket closed by signal
        finally:
            raw.close()
            tx_sock.close()
            inject_sock.close()
            logger.info("Stopped — %d probe(s) relayed", self._relay_count)

    # -- internals ----------------------------------------------------------

    def _cleanup(self) -> None:
        cutoff = time.time() - DEDUP_SECONDS
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

    def _handle(
        self,
        packet: bytes,
        tx_sock: socket.socket,
        inject_sock: socket.socket,
    ) -> None:
        # --- parse Ethernet -------------------------------------------------
        if len(packet) < 14:
            return
        if struct.unpack("!H", packet[12:14])[0] != 0x0800:  # not IPv4
            return

        ip_hdr = packet[14:34]
        src_ip = socket.inet_ntoa(ip_hdr[12:16])

        if src_ip in self.camera_ips:  # skip our own injected packets
            return

        if ip_hdr[9] != 17:  # not UDP
            return

        # --- parse UDP -----------------------------------------------------
        udp_off = 14 + (ip_hdr[0] & 0x0F) * 4
        if len(packet) < udp_off + 8:
            return

        dst_port = struct.unpack("!H", packet[udp_off + 2 : udp_off + 4])[0]
        if dst_port != MIOT_PORT:
            return

        udp_len = struct.unpack("!H", packet[udp_off + 4 : udp_off + 6])[0]
        payload_start = udp_off + 8
        payload_end = payload_start + max(0, udp_len - 8)
        payload = packet[payload_start:payload_end]

        if len(payload) < 2 or payload[:2] != PROBE_MAGIC:
            return

        src_port = struct.unpack("!H", packet[udp_off : udp_off + 2])[0]

        # --- dedup ---------------------------------------------------------
        payload_hash = hashlib.md5(payload).hexdigest()
        now = time.time()
        if self._seen.get(payload_hash, 0) > now - DEDUP_SECONDS:
            return
        self._seen[payload_hash] = now

        # --- forward to LAN ------------------------------------------------
        targets = [self.broadcast_ip] + self.camera_ips
        for target in targets:
            try:
                tx_sock.sendto(payload, (target, MIOT_PORT))
            except OSError as exc:
                logger.warning("sendto %s:%d failed: %s", target, MIOT_PORT, exc)

        # --- collect & inject responses ------------------------------------
        deadline = now + RESPONSE_TIMEOUT
        while time.time() < deadline:
            try:
                tx_sock.settimeout(max(0.05, deadline - time.time()))
                data, addr = tx_sock.recvfrom(1400)
            except socket.timeout:
                break

            if addr[0] == src_ip:
                continue  # don't echo back to miloco

            logger.info(
                "relay  %s:%d  %dB  →  %s:%d",
                addr[0],
                addr[1],
                len(data),
                src_ip,
                src_port,
            )
            self._inject(inject_sock, addr[0], addr[1], src_ip, src_port, data)
            self._relay_count += 1

    @staticmethod
    def _inject(
        sock: socket.socket,
        src_ip: str,
        src_port: int,
        dst_ip: str,
        dst_port: int,
        payload: bytes,
    ) -> None:
        """Inject a raw IP/UDP packet with a spoofed source address."""
        ip_src = socket.inet_aton(src_ip)
        ip_dst = socket.inet_aton(dst_ip)

        ip_header = struct.pack(
            "!BBHHHBBH4s4s",
            (4 << 4) + 5,           # version + IHL
            0,                       # TOS
            20 + 8 + len(payload),  # total length
            0,                       # ID
            0,                       # flags / fragment offset
            64,                      # TTL
            socket.IPPROTO_UDP,
            0,                       # checksum (kernel fills)
            ip_src,
            ip_dst,
        )

        udp_header = struct.pack(
            "!HHHH",
            src_port,
            dst_port,
            8 + len(payload),
            0,  # checksum (optional for IPv4)
        )

        sock.sendto(ip_header + udp_header + payload, (dst_ip, 0))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    camera_ips = _parse_camera_ips(sys.argv[1:])
    broadcast_ip = os.environ.get("MILOCO_RELAY_BROADCAST", "192.168.31.255")
    UDPRelay(camera_ips, broadcast_ip).start()


if __name__ == "__main__":
    main()
