"""doctor 命令：环境诊断，判断主机能否 UDP 连上米家摄像头。

三段输出：
  1. 主机环境信息（OS/Kernel/运行时/网卡）
  2. Miloco 运行状态（backend/账号/家庭/摄像头）
  3. 检测状态（防火墙/容器/WSL/reachability）
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

import click
import httpx

from miloco_cli.config import load_config

# ─── Types ─────────────────────────────────────────────────────────────────────


class Status(Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


Section = Literal["host", "miloco", "checks"]


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    fix_hint: str | None = None
    section: Section = "checks"


@dataclass(frozen=True)
class Environment:
    platform: Literal["macos", "linux", "wsl", "unknown"]
    is_container: bool
    container_net: Literal["host", "bridge", "other"] | None
    distro: str | None
    kernel: str


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    ip: str
    prefix: int
    is_virtual: bool


@dataclass(frozen=True)
class NetworkState:
    interfaces: list[NetworkInterface] = field(default_factory=list)


@dataclass(frozen=True)
class UfwState:
    installed: bool
    enabled_via_conf: bool | None
    rules_readable: bool
    default_deny_incoming: bool
    has_udp_allow: bool


@dataclass(frozen=True)
class FirewalldState:
    installed: bool
    running: bool | None
    zone: str | None
    listing_readable: bool
    target: Literal["ACCEPT", "DROP", "REJECT", "default"] | None
    has_protocol_udp: bool
    has_port_udp_only: bool


@dataclass(frozen=True)
class IptablesState:
    installed: bool
    readable: bool
    policy_drop: bool
    has_udp_block: bool
    has_udp_accept: bool
    has_blanket_accept: bool
    udp_accept_all_port_limited: bool


@dataclass(frozen=True)
class ContainerState:
    is_container: bool
    net_mode: Literal["host", "bridge", "other"] | None


@dataclass(frozen=True)
class WslState:
    is_wsl: bool
    wslconfig_path: Path | None
    wslconfig_exists: bool
    mirrored_mode: bool
    hyperv_default_inbound: Literal["allow", "block", "unknown"] | None


@dataclass(frozen=True)
class CameraSummary:
    did: str
    name: str
    online: bool
    lan_online: bool | None
    local_ip: str | None


@dataclass(frozen=True)
class BackendState:
    url: str
    reachable: bool
    error: str | None
    account_bound: bool
    account_uid: str | None
    home_enabled: bool
    home_id: str | None
    home_name: str | None
    cameras: list[CameraSummary] = field(default_factory=list)


@dataclass(frozen=True)
class ReachabilityState:
    target_ip: str
    target_label: str
    same_subnet: bool
    same_subnet_iface: str | None
    route_iface: str | None
    route_src: str | None
    ping_ok: bool
    ping_rtt_ms: float | None
    neigh_state: Literal["REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE"] | None
    neigh_mac: str | None
    udp_send_ok: bool
    udp_error: str | None


# ─── Low-level helpers ─────────────────────────────────────────────────────────


@dataclass
class CmdResult:
    found: bool
    rc: int
    stdout: str
    stderr: str


_NOT_FOUND = CmdResult(found=False, rc=-1, stdout="", stderr="")


def _run_cmd(cmd: list[str], timeout: int = 5) -> CmdResult:
    if not shutil.which(cmd[0]):
        return _NOT_FOUND
    env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=timeout, env=env,
        )
        return CmdResult(found=True, rc=r.returncode, stdout=r.stdout, stderr=r.stderr)
    except (subprocess.TimeoutExpired, OSError):
        return CmdResult(found=True, rc=-1, stdout="", stderr="")


_VIRTUAL_PREFIXES = ("docker", "br-", "veth", "cni", "flannel", "cali", "kube", "cbr")


def _is_virtual_iface(name: str) -> bool:
    return name == "lo" or name.startswith(_VIRTUAL_PREFIXES)


def _in_same_subnet(
    interfaces: list[NetworkInterface], target_ip: str,
) -> tuple[bool, str | None]:
    try:
        target = ipaddress.IPv4Address(target_ip)
    except (ipaddress.AddressValueError, ValueError):
        return False, None
    for iface in interfaces:
        if iface.is_virtual:
            continue
        try:
            net = ipaddress.IPv4Network(f"{iface.ip}/{iface.prefix}", strict=False)
        except (ValueError, ipaddress.NetmaskValueError):
            continue
        if target in net:
            return True, iface.name
    return False, None


# ─── Environment probing ───────────────────────────────────────────────────────


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except (FileNotFoundError, PermissionError):
        return False


def _detect_platform() -> str:
    """遗留 API：新代码用 probe_environment().platform。"""
    if platform.system() == "Darwin":
        return "macos"
    if _is_wsl():
        return "wsl"
    if platform.system() == "Linux":
        return "linux"
    return "unknown"


_PHYSICAL_IFACE_PREFIXES = ("enp", "wlp", "eno", "ens", "wlan", "eth", "em", "en0")


def _detect_is_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    if os.environ.get("container"):
        return True
    try:
        cg = Path("/proc/1/cgroup").read_text(errors="ignore").lower()
        if any(kw in cg for kw in ("docker", "containerd", "kubepods", "libpod")):
            return True
    except (FileNotFoundError, PermissionError):
        pass
    return False


def _detect_container_net() -> Literal["host", "bridge", "other"] | None:
    try:
        for name in os.listdir("/sys/class/net"):
            if any(name.startswith(p) for p in _PHYSICAL_IFACE_PREFIXES):
                return "host"
    except OSError:
        pass

    gateway = _read_default_gateway()
    if gateway is None:
        return "other"
    try:
        gw_addr = ipaddress.IPv4Address(gateway)
    except (ipaddress.AddressValueError, ValueError):
        return "other"
    docker_bridge_net = ipaddress.IPv4Network("172.17.0.0/12")
    if gw_addr in docker_bridge_net:
        return "bridge"
    return "other"


def _read_default_gateway() -> str | None:
    try:
        lines = Path("/proc/net/route").read_text().splitlines()
    except (FileNotFoundError, PermissionError):
        return None
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[1] != "00000000":
            continue
        gw_hex = parts[2]
        try:
            gw_int = int(gw_hex, 16)
            return ".".join(str((gw_int >> (8 * i)) & 0xFF) for i in range(4))
        except ValueError:
            return None
    return None


def _read_distro() -> str | None:
    try:
        content = Path("/etc/os-release").read_text(errors="ignore")
        for line in content.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except (FileNotFoundError, PermissionError):
        pass
    try:
        first = Path("/etc/issue").read_text(errors="ignore").splitlines()[0]
        return first.strip() or None
    except (FileNotFoundError, PermissionError, IndexError):
        return None


def probe_environment() -> Environment:
    if platform.system() == "Darwin":
        plat: Literal["macos", "linux", "wsl", "unknown"] = "macos"
        mac_ver = platform.mac_ver()[0]
        distro = f"macOS {mac_ver}" if mac_ver else "macOS"
    else:
        if _is_wsl():
            plat = "wsl"
        elif platform.system() == "Linux":
            plat = "linux"
        else:
            plat = "unknown"
        distro = _read_distro()

    uname = platform.uname()
    kernel = f"{uname.system} {uname.release} {uname.machine}"

    is_container = _detect_is_container()
    container_net = _detect_container_net() if is_container else None

    return Environment(
        platform=plat,
        is_container=is_container,
        container_net=container_net,
        distro=distro,
        kernel=kernel,
    )


def _runtime_tags(env: Environment) -> list[str]:
    tags: list[str] = []
    if env.platform == "wsl":
        tags.append("WSL2")
    if env.is_container:
        tags.append("Docker container")
    if not tags:
        tags.append("裸机")
    return tags


# ─── Network ───────────────────────────────────────────────────────────────────


_IP_ADDR_RE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)")
_IFCONFIG_IFACE_RE = re.compile(r"^([a-zA-Z0-9_.-]+):\s")
_IFCONFIG_INET_RE = re.compile(
    r"inet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)"
)


def _prefix_from_netmask(mask: str) -> int:
    if mask.startswith("0x"):
        return bin(int(mask, 16)).count("1")
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except (ipaddress.NetmaskValueError, ValueError):
        return 0


def probe_network(env: Environment) -> NetworkState:
    if env.platform == "macos":
        return _probe_network_macos()
    return _probe_network_linux()


def _probe_network_linux() -> NetworkState:
    r = _run_cmd(["ip", "-4", "-o", "addr", "show"])
    if not r.found or r.rc != 0:
        return NetworkState()
    ifaces: list[NetworkInterface] = []
    for line in r.stdout.splitlines():
        m = _IP_ADDR_RE.match(line)
        if not m:
            continue
        name, ip, prefix = m.group(1), m.group(2), int(m.group(3))
        ifaces.append(NetworkInterface(
            name=name, ip=ip, prefix=prefix, is_virtual=_is_virtual_iface(name),
        ))
    return NetworkState(interfaces=ifaces)


def _probe_network_macos() -> NetworkState:
    r = _run_cmd(["ifconfig"])
    if not r.found or r.rc != 0:
        return NetworkState()
    ifaces: list[NetworkInterface] = []
    current: str | None = None
    for line in r.stdout.splitlines():
        m_iface = _IFCONFIG_IFACE_RE.match(line)
        if m_iface:
            current = m_iface.group(1)
            continue
        if current is None:
            continue
        m_inet = _IFCONFIG_INET_RE.search(line)
        if m_inet:
            ip = m_inet.group(1)
            prefix = _prefix_from_netmask(m_inet.group(2))
            ifaces.append(NetworkInterface(
                name=current, ip=ip, prefix=prefix, is_virtual=_is_virtual_iface(current),
            ))
    return NetworkState(interfaces=ifaces)


def assess_network_empty(state: NetworkState) -> list[CheckResult]:
    non_virtual = [i for i in state.interfaces if not i.is_virtual]
    if not non_virtual:
        return [CheckResult(
            section="host",
            name="IPv4 网卡",
            status=Status.FAIL,
            message="未检测到可用 IPv4 网卡, 网络未配置或断网",
        )]
    return []


# ─── Container ─────────────────────────────────────────────────────────────────


def check_container(env: Environment) -> list[CheckResult]:
    if not env.is_container:
        return []
    if env.container_net == "host":
        return [CheckResult(
            name="容器网络",
            status=Status.PASS,
            message="容器使用 host 网络, UDP 直通宿主机网卡",
        )]
    if env.container_net == "bridge":
        return [CheckResult(
            name="容器网络",
            status=Status.FAIL,
            message="容器运行在 bridge 网络, 无法接收局域网 UDP 打洞包",
            fix_hint=(
                "以 host 网络模式重启容器:\n"
                "  docker run --network=host <image>\n"
                "\n"
                "或 docker-compose:\n"
                "  network_mode: host"
            ),
        )]
    return [CheckResult(
        name="容器网络",
        status=Status.WARN,
        message="容器网络模式未知, 局域网 UDP 通路不确定",
        fix_hint=(
            "推荐改用 host 网络:\n"
            "  docker run --network=host <image>"
        ),
    )]


# ─── Firewall ──────────────────────────────────────────────────────────────────


def _read_ufw_conf_enabled() -> bool | None:
    try:
        content = Path("/etc/ufw/ufw.conf").read_text(errors="ignore")
    except (FileNotFoundError, PermissionError):
        return None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip().upper() == "ENABLED":
            return value.strip().lower() in ("yes", "true", "1")
    return None


def probe_ufw() -> UfwState:
    installed = shutil.which("ufw") is not None
    if not installed:
        return UfwState(
            installed=False, enabled_via_conf=None, rules_readable=False,
            default_deny_incoming=False, has_udp_allow=False,
        )
    enabled = _read_ufw_conf_enabled()
    if enabled is not True:
        return UfwState(
            installed=True, enabled_via_conf=enabled, rules_readable=False,
            default_deny_incoming=False, has_udp_allow=False,
        )
    verbose = _run_cmd(["ufw", "status", "verbose"])
    if not verbose.found or verbose.rc != 0:
        return UfwState(
            installed=True, enabled_via_conf=True, rules_readable=False,
            default_deny_incoming=False, has_udp_allow=False,
        )
    out = verbose.stdout.lower()
    default_deny = "deny (incoming)" in out or "reject (incoming)" in out
    has_udp_allow = any(
        "allow" in line and "udp" in line for line in out.splitlines()
    )
    return UfwState(
        installed=True, enabled_via_conf=True, rules_readable=True,
        default_deny_incoming=default_deny, has_udp_allow=has_udp_allow,
    )


def assess_ufw(state: UfwState) -> list[CheckResult]:
    if not state.installed:
        return []
    if state.enabled_via_conf is False:
        return [CheckResult(
            name="ufw 状态",
            status=Status.PASS,
            message="ufw 未启用 (/etc/ufw/ufw.conf ENABLED=no), 不阻断流量",
        )]
    if state.enabled_via_conf is None:
        return []
    if not state.rules_readable:
        return [CheckResult(
            name="ufw 状态",
            status=Status.WARN,
            message=(
                "ufw 已启用, 但无法读取规则详情。"
                "如需查看详情请以 sudo 运行: sudo ufw status verbose"
            ),
        )]
    if state.default_deny_incoming and not state.has_udp_allow:
        return [CheckResult(
            name="ufw UDP 入站",
            status=Status.FAIL,
            message="ufw 默认拒绝入站流量, PPCS UDP 包会被丢弃",
            fix_hint=(
                "允许局域网 UDP 入站 (推荐, 限定子网):\n"
                "  sudo ufw allow from 192.168.0.0/16 proto udp\n"
                "\n"
                "或允许所有 UDP 入站 (宽松):\n"
                "  sudo ufw allow proto udp from any"
            ),
        )]
    return [CheckResult(
        name="ufw UDP 入站",
        status=Status.PASS,
        message="ufw 已启用但允许 UDP 入站",
    )]


def probe_firewalld() -> FirewalldState:
    installed = shutil.which("firewall-cmd") is not None
    if not installed:
        return FirewalldState(
            installed=False, running=None, zone=None, listing_readable=False,
            target=None, has_protocol_udp=False, has_port_udp_only=False,
        )
    state = _run_cmd(["firewall-cmd", "--state"])
    combined = (state.stdout + state.stderr).lower()
    if "not running" in combined:
        return FirewalldState(
            installed=True, running=False, zone=None, listing_readable=False,
            target=None, has_protocol_udp=False, has_port_udp_only=False,
        )
    if state.rc != 0:
        return FirewalldState(
            installed=True, running=None, zone=None, listing_readable=False,
            target=None, has_protocol_udp=False, has_port_udp_only=False,
        )
    zone_cmd = _run_cmd(["firewall-cmd", "--get-default-zone"])
    if zone_cmd.rc != 0 or not zone_cmd.stdout.strip():
        return FirewalldState(
            installed=True, running=True, zone=None, listing_readable=False,
            target=None, has_protocol_udp=False, has_port_udp_only=False,
        )
    zone = zone_cmd.stdout.strip()
    list_cmd = _run_cmd(["firewall-cmd", f"--zone={zone}", "--list-all"])
    if list_cmd.rc != 0:
        return FirewalldState(
            installed=True, running=True, zone=zone, listing_readable=False,
            target=None, has_protocol_udp=False, has_port_udp_only=False,
        )
    lines = list_cmd.stdout.lower().splitlines()
    target_line = next((ln for ln in lines if "target:" in ln), "")
    if "accept" in target_line:
        target: Literal["ACCEPT", "DROP", "REJECT", "default"] | None = "ACCEPT"
    elif "drop" in target_line:
        target = "DROP"
    elif "reject" in target_line:
        target = "REJECT"
    elif "default" in target_line:
        target = "default"
    else:
        target = None
    protocols_line = next((ln for ln in lines if ln.strip().startswith("protocols:")), "")
    has_protocol_udp = "udp" in protocols_line
    ports_line = next((ln for ln in lines if ln.strip().startswith("ports:")), "")
    has_port_udp_only = "udp" in ports_line and not has_protocol_udp
    return FirewalldState(
        installed=True, running=True, zone=zone, listing_readable=True,
        target=target, has_protocol_udp=has_protocol_udp,
        has_port_udp_only=has_port_udp_only,
    )


def _firewalld_fix_hint(zone: str) -> str:
    return (
        f"允许局域网 UDP:\n"
        f"  sudo firewall-cmd --zone={zone} --add-rich-rule="
        f"'rule family=ipv4 source address=192.168.0.0/16 protocol value=udp accept' --permanent\n"
        f"  sudo firewall-cmd --reload"
    )


def assess_firewalld(state: FirewalldState) -> list[CheckResult]:
    if not state.installed or state.running is False:
        return []
    if state.running is None:
        return [CheckResult(
            name="firewalld 状态",
            status=Status.WARN,
            message=(
                "firewalld 已安装但无法读取状态。"
                "如需查看请以 sudo 运行: sudo firewall-cmd --state"
            ),
        )]
    if not state.listing_readable:
        return [CheckResult(
            name="firewalld 状态",
            status=Status.WARN,
            message=(
                "firewalld 运行中, 但无法读取 zone 规则详情。"
                "如需查看请以 sudo 运行: sudo firewall-cmd --list-all"
            ),
        )]
    zone = state.zone or "<unknown>"
    if state.target in ("DROP", "REJECT"):
        return [CheckResult(
            name="firewalld UDP 入站",
            status=Status.FAIL,
            message=f"firewalld zone '{zone}' 目标为 {state.target}, UDP 入站被丢弃",
            fix_hint=_firewalld_fix_hint(zone),
        )]
    if state.target == "ACCEPT" or state.has_protocol_udp:
        return [CheckResult(
            name="firewalld UDP 入站",
            status=Status.PASS,
            message=f"firewalld zone '{zone}' 允许 UDP 流量",
        )]
    if state.has_port_udp_only:
        return [CheckResult(
            name="firewalld UDP 入站",
            status=Status.WARN,
            message=(
                f"firewalld zone '{zone}' 仅放行特定端口的 UDP, "
                f"PPCS 使用随机高位端口可能被阻断"
            ),
            fix_hint=_firewalld_fix_hint(zone),
        )]
    return [CheckResult(
        name="firewalld UDP 入站",
        status=Status.WARN,
        message=(
            f"firewalld zone '{zone}' target 为 default, 未找到显式 UDP 放行规则, "
            f"可能阻断 PPCS UDP"
        ),
        fix_hint=_firewalld_fix_hint(zone),
    )]


def probe_iptables() -> IptablesState:
    installed = shutil.which("iptables") is not None
    if not installed:
        return IptablesState(
            installed=False, readable=False, policy_drop=False,
            has_udp_block=False, has_udp_accept=False,
            has_blanket_accept=False, udp_accept_all_port_limited=False,
        )
    r = _run_cmd(["iptables", "-L", "INPUT", "-n"])
    if r.rc != 0:
        return IptablesState(
            installed=True, readable=False, policy_drop=False,
            has_udp_block=False, has_udp_accept=False,
            has_blanket_accept=False, udp_accept_all_port_limited=False,
        )
    lines = r.stdout.splitlines()
    policy_drop = bool(
        lines and ("policy drop" in lines[0].lower() or "policy reject" in lines[0].lower())
    )
    has_udp_block = any(
        "udp" in line.lower() and ("drop" in line.lower() or "reject" in line.lower())
        for line in lines
    )
    udp_accept_lines = [
        line for line in lines
        if "udp" in line.lower() and "accept" in line.lower()
    ]
    has_blanket_accept = any(
        "accept" in line.lower()
        and "all" in line.lower().split()
        and "established" not in line.lower()
        for line in lines[1:]
    )
    has_udp_accept = bool(udp_accept_lines) or has_blanket_accept
    udp_accept_all_port_limited = (
        bool(udp_accept_lines)
        and not has_blanket_accept
        and all(
            "dpt:" in line.lower() or "dpts:" in line.lower()
            for line in udp_accept_lines
        )
    )
    return IptablesState(
        installed=True, readable=True, policy_drop=policy_drop,
        has_udp_block=has_udp_block, has_udp_accept=has_udp_accept,
        has_blanket_accept=has_blanket_accept,
        udp_accept_all_port_limited=udp_accept_all_port_limited,
    )


_IPTABLES_FIX_HINT = (
    "允许局域网 UDP 入站:\n"
    "  sudo iptables -I INPUT -p udp -s 192.168.0.0/16 -j ACCEPT\n"
    "\n"
    "持久化 (Ubuntu/Debian):\n"
    "  sudo apt install iptables-persistent && sudo netfilter-persistent save"
)


def assess_iptables(state: IptablesState) -> list[CheckResult]:
    if not state.installed:
        return []
    if not state.readable:
        return [CheckResult(
            name="iptables 状态",
            status=Status.WARN,
            message=(
                "iptables 已安装但无法读取规则。"
                "如需查看请以 sudo 运行: sudo iptables -L INPUT -n"
            ),
        )]
    if state.has_udp_block and state.has_udp_accept:
        return [CheckResult(
            name="iptables UDP 入站",
            status=Status.WARN,
            message=(
                "iptables INPUT 链同时存在 UDP ACCEPT 与 UDP DROP/REJECT 规则, "
                "实际行为取决于规则顺序, 请人工核对"
            ),
            fix_hint=(
                "查看带行号的完整规则:\n"
                "  sudo iptables -L INPUT -nv --line-numbers"
            ),
        )]
    if state.has_udp_block or (state.policy_drop and not state.has_udp_accept):
        msg = (
            "iptables INPUT 链默认策略为 DROP 且无 UDP ACCEPT 规则"
            if state.policy_drop and not state.has_udp_block
            else "iptables INPUT 链存在 DROP/REJECT UDP 规则"
        )
        return [CheckResult(
            name="iptables UDP 入站",
            status=Status.FAIL,
            message=f"{msg}, PPCS UDP 包会被丢弃",
            fix_hint=_IPTABLES_FIX_HINT,
        )]
    if state.policy_drop and state.udp_accept_all_port_limited:
        return [CheckResult(
            name="iptables UDP 入站",
            status=Status.WARN,
            message="iptables 仅放行特定端口的 UDP, PPCS 使用随机高位端口可能被阻断",
            fix_hint=_IPTABLES_FIX_HINT,
        )]
    return [CheckResult(
        name="iptables UDP 入站",
        status=Status.PASS,
        message="iptables INPUT 链未阻断 UDP 入站",
    )]


def check_firewall(env: Environment) -> list[CheckResult]:
    if env.platform == "macos":
        return [CheckResult(
            name="防火墙 (macOS)",
            status=Status.PASS,
            message="macOS 默认不阻断 UDP 入站回包, 通常无需配置",
        )]

    ufw_state = probe_ufw()
    ufw_results = assess_ufw(ufw_state)
    if ufw_results:
        return ufw_results

    fwd_state = probe_firewalld()
    fwd_results = assess_firewalld(fwd_state)
    if fwd_results:
        return fwd_results

    ipt_state = probe_iptables()
    ipt_results = assess_iptables(ipt_state)
    if ipt_results:
        return ipt_results

    return [CheckResult(
        name="防火墙",
        status=Status.PASS,
        message="未检测到 ufw、firewalld 或 iptables, UDP 入站不受防火墙限制",
    )]


# ─── WSL ───────────────────────────────────────────────────────────────────────


def probe_wsl(env: Environment) -> WslState:
    if env.platform != "wsl":
        return WslState(
            is_wsl=False, wslconfig_path=None, wslconfig_exists=False,
            mirrored_mode=False, hyperv_default_inbound=None,
        )
    path = _get_wslconfig_path()
    exists = bool(path and path.exists())
    mirrored = False
    if exists and path is not None:
        content = path.read_text(errors="ignore")
        in_wsl2 = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_wsl2 = stripped.lower() == "[wsl2]"
                continue
            if stripped.startswith("#") or stripped.startswith(";"):
                continue
            if in_wsl2 and "networkingmode" in stripped.replace(" ", "").lower():
                if "=mirrored" in stripped.replace(" ", "").lower():
                    mirrored = True
                    break

    hv = _run_cmd([
        "powershell.exe", "-NoProfile", "-Command",
        "(Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore "
        "-Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}').DefaultInboundAction",
    ], timeout=15)
    hv_action: Literal["allow", "block", "unknown"] | None
    if hv.found and hv.rc == 0:
        action = hv.stdout.strip().lower()
        if action == "allow":
            hv_action = "allow"
        elif action:
            hv_action = "block"
        else:
            hv_action = "unknown"
    else:
        hv_action = "unknown"

    return WslState(
        is_wsl=True, wslconfig_path=path, wslconfig_exists=exists,
        mirrored_mode=mirrored, hyperv_default_inbound=hv_action,
    )


def assess_wsl(state: WslState) -> list[CheckResult]:
    if not state.is_wsl:
        return []
    results: list[CheckResult] = []
    if state.wslconfig_path is None:
        results.append(CheckResult(
            name="WSL 网络模式",
            status=Status.WARN,
            message=(
                "无法定位 .wslconfig (Windows 用户目录检测失败)。"
                "请手动确认 %USERPROFILE%\\.wslconfig 含 [wsl2] networkingMode=mirrored"
            ),
        ))
    elif not state.wslconfig_exists:
        results.append(CheckResult(
            name="WSL 网络模式",
            status=Status.FAIL,
            message=f".wslconfig 不存在 ({state.wslconfig_path}), 默认 NAT 模式无法接收局域网 UDP",
            fix_hint=(
                "创建 %USERPROFILE%\\.wslconfig:\n"
                "  [wsl2]\n"
                "  networkingMode=mirrored\n"
                "\n"
                "保存后执行: wsl --shutdown && wsl"
            ),
        ))
    elif state.mirrored_mode:
        results.append(CheckResult(
            name="WSL 网络模式",
            status=Status.PASS,
            message="已启用镜像网络模式 (networkingMode=mirrored)",
        ))
    else:
        results.append(CheckResult(
            name="WSL 网络模式",
            status=Status.FAIL,
            message="未启用镜像网络模式, WSL 无法接收宿主机局域网 UDP 包",
            fix_hint=(
                "在 Windows 侧编辑 %USERPROFILE%\\.wslconfig:\n"
                "  [wsl2]\n"
                "  networkingMode=mirrored\n"
                "\n"
                "保存后执行: wsl --shutdown && wsl"
            ),
        ))

    if state.hyperv_default_inbound == "allow":
        results.append(CheckResult(
            name="Hyper-V 防火墙",
            status=Status.PASS,
            message="Hyper-V 防火墙 DefaultInboundAction=Allow, UDP 入站已放行",
        ))
    elif state.hyperv_default_inbound == "block":
        results.append(CheckResult(
            name="Hyper-V 防火墙",
            status=Status.FAIL,
            message="Hyper-V 防火墙 DefaultInboundAction=Block, UDP 入站被阻断",
            fix_hint=(
                "在 Windows PowerShell (管理员) 执行:\n"
                "  Set-NetFirewallHyperVVMSetting -Name "
                "'{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' "
                "-DefaultInboundAction Allow"
            ),
        ))
    else:
        results.append(CheckResult(
            name="Hyper-V 防火墙",
            status=Status.WARN,
            message=(
                "无法检测 Hyper-V 防火墙 (powershell.exe 不可用/无权限/超时)。"
                "如首次运行较慢可重试; 或在 Windows PowerShell (管理员) 手动检查 "
                "Get-NetFirewallHyperVVMSetting 的 DefaultInboundAction"
            ),
        ))

    return results


def check_wsl(env: Environment) -> list[CheckResult]:
    return assess_wsl(probe_wsl(env))


def _get_wslconfig_path() -> Path | None:
    ps_result = _run_cmd(
        [
            "powershell.exe", "-NoProfile", "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:USERPROFILE",
        ],
        timeout=15,
    )
    if ps_result.found and ps_result.rc == 0:
        profile = ps_result.stdout.strip().lstrip("﻿")
        if profile:
            wsl_result = _run_cmd(["wslpath", "-u", profile])
            if wsl_result.found and wsl_result.rc == 0 and wsl_result.stdout.strip():
                return Path(wsl_result.stdout.strip()) / ".wslconfig"

    users_dir = Path("/mnt/c/Users")
    skip = {"Public", "Default", "Default User", "All Users"}
    try:
        if not users_dir.exists():
            return None

        def _safe_mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0

        dirs = sorted(
            (d for d in users_dir.iterdir() if d.is_dir() and d.name not in skip),
            key=_safe_mtime,
            reverse=True,
        )
        for d in dirs:
            p = d / ".wslconfig"
            if p.exists():
                return p
        if dirs:
            return dirs[0] / ".wslconfig"
    except OSError:
        pass
    return None


# ─── Backend ───────────────────────────────────────────────────────────────────


def _backend_state(url: str, *, reachable: bool, error: str | None) -> BackendState:
    return BackendState(
        url=url, reachable=reachable, error=error,
        account_bound=False, account_uid=None,
        home_enabled=False, home_id=None, home_name=None,
        cameras=[],
    )


def probe_backend() -> BackendState:
    cfg = load_config()
    server = cfg.get("server", {})
    base_url = server.get("url", "http://127.0.0.1:1810")
    token = server.get("token", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        with httpx.Client(
            base_url=base_url, headers=headers, timeout=3.0, verify=False,
        ) as client:
            r_status = client.get("/api/miot/status")
            if not r_status.is_success:
                return _backend_state(
                    base_url, reachable=True,
                    error=f"HTTP {r_status.status_code}",
                )
            status_body = r_status.json()
            if status_body.get("code", 0) != 0:
                return _backend_state(
                    base_url, reachable=True,
                    error=f"业务错误 code={status_body.get('code')}",
                )
            status_data = status_body.get("data") or {}
            is_bound = bool(status_data.get("is_bound"))
            uid = (status_data.get("user_info") or {}).get("uid")
            if not is_bound:
                return BackendState(
                    url=base_url, reachable=True, error=None,
                    account_bound=False, account_uid=None,
                    home_enabled=False, home_id=None, home_name=None,
                    cameras=[],
                )

            r_homes = client.get("/api/miot/scope/homes")
            homes: list[dict] = []
            if r_homes.is_success:
                hb = r_homes.json()
                if hb.get("code", 0) == 0:
                    homes = hb.get("data") or []
            enabled_home = next((h for h in homes if h.get("in_use")), None)
            if enabled_home is None:
                return BackendState(
                    url=base_url, reachable=True, error=None,
                    account_bound=True, account_uid=uid,
                    home_enabled=False, home_id=None, home_name=None,
                    cameras=[],
                )

            r_cams = client.get("/api/miot/camera_list")
            cameras: list[CameraSummary] = []
            if r_cams.is_success:
                cb = r_cams.json()
                if cb.get("code", 0) == 0:
                    for c in cb.get("data") or []:
                        cameras.append(CameraSummary(
                            did=c.get("did", ""),
                            name=c.get("name", c.get("did", "")),
                            online=bool(c.get("online")),
                            lan_online=c.get("lan_online"),
                            local_ip=c.get("local_ip"),
                        ))

            return BackendState(
                url=base_url, reachable=True, error=None,
                account_bound=True, account_uid=uid,
                home_enabled=True,
                home_id=enabled_home.get("home_id"),
                home_name=enabled_home.get("home_name"),
                cameras=cameras,
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as e:
        return _backend_state(
            base_url, reachable=False, error=str(e) or type(e).__name__,
        )


def assess_backend(state: BackendState) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not state.reachable:
        results.append(CheckResult(
            section="miloco",
            name="backend 运行状态",
            status=Status.WARN,
            message=f"无法连接 backend ({state.url}): {state.error}",
            fix_hint=(
                "启动 backend:\n"
                "  miloco-cli service start\n"
                "\n"
                "若已启动仍无法连接, 检查 server.url 配置:\n"
                "  miloco-cli config get server.url"
            ),
        ))
        return results

    if state.error:
        results.append(CheckResult(
            section="miloco",
            name="backend 运行状态",
            status=Status.WARN,
            message=f"backend 可达但接口异常 ({state.url}): {state.error}",
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name="backend 运行状态",
        status=Status.PASS,
        message=f"backend HTTP 服务运行中 ({state.url})",
    ))

    if not state.account_bound:
        results.append(CheckResult(
            section="miloco",
            name="小米账号绑定",
            status=Status.WARN,
            message="尚未绑定 Xiaomi 账号",
            fix_hint="miloco-cli account login",
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name="小米账号绑定",
        status=Status.PASS,
        message=f"已绑定 Xiaomi 账号 (uid: {state.account_uid or 'unknown'})",
    ))

    if not state.home_enabled:
        results.append(CheckResult(
            section="miloco",
            name="家庭配置",
            status=Status.WARN,
            message="账号下无启用的家庭",
            fix_hint=(
                "列出并切换家庭:\n"
                "  miloco-cli scope home list\n"
                "  miloco-cli scope home switch <home_id>"
            ),
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name="家庭配置",
        status=Status.PASS,
        message=f"已启用家庭: {state.home_name or state.home_id}",
    ))

    if not state.cameras:
        results.append(CheckResult(
            section="miloco",
            name="摄像头列表",
            status=Status.WARN,
            message="当前家庭未发现摄像头设备",
        ))
        return results

    lines = [f'  - "{c.name}" (did={c.did}): {c.local_ip or "未发现 LAN IP"}'
             for c in state.cameras]
    all_have_ip = all(c.local_ip for c in state.cameras)
    all_missing_ip = all(not c.local_ip for c in state.cameras)
    if all_have_ip:
        results.append(CheckResult(
            section="miloco",
            name="摄像头列表",
            status=Status.PASS,
            message=f"检测到 {len(state.cameras)} 台摄像头:\n" + "\n".join(lines),
        ))
    elif all_missing_ip:
        results.append(CheckResult(
            section="miloco",
            name="摄像头列表",
            status=Status.WARN,
            message=(
                f"发现 {len(state.cameras)} 台摄像头但均未获得 LAN IP:\n"
                + "\n".join(lines)
            ),
            fix_hint=(
                "确认摄像头与本机在同一局域网; "
                "重启 backend 触发 LAN 发现: miloco-cli service restart"
            ),
        ))
    else:
        results.append(CheckResult(
            section="miloco",
            name="摄像头列表",
            status=Status.WARN,
            message=(
                f"发现 {len(state.cameras)} 台摄像头, 部分未获得 LAN IP:\n"
                + "\n".join(lines)
            ),
        ))
    return results


# ─── Reachability ──────────────────────────────────────────────────────────────


_PING_RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_IP_ROUTE_DEV_RE = re.compile(r"\bdev\s+(\S+)")
_IP_ROUTE_SRC_RE = re.compile(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)")
_MAC_RE = re.compile(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")
_NEIGH_STATES = ("REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE")


def _probe_udp_send(target_ip: str, port: int = 32100) -> tuple[bool, str | None]:
    """验证本机能否将 UDP 包递交给内核发出, 并捕获 connected socket 的 ICMP error。

    仅能验证:
      1. 本地路由表有到目标的路径 (connect() 不失败)
      2. 本地防火墙不拦截 UDP 出站 (send 不失败)
      3. 短窗口内内核未收到 ICMP Destination/Port Unreachable

    UDP 无 delivery confirmation, 不能确认对端收到; 综合 ping/neigh 才能判达。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.connect((target_ip, port))
        sock.send(b"miloco-doctor-probe")
        sock.settimeout(0.3)
        try:
            sock.recv(1024)
        except socket.timeout:
            return True, None
        except ConnectionRefusedError:
            return True, "ICMP Port Unreachable"
    except OSError as e:
        return False, f"{e.strerror or type(e).__name__} (errno={e.errno})"
    finally:
        sock.close()
    return True, None


def _parse_ping(output: str) -> tuple[bool, float | None]:
    m = _PING_RTT_RE.search(output)
    if m:
        try:
            return True, float(m.group(1))
        except ValueError:
            return True, None
    ok = "1 received" in output or "1 packets received" in output
    return ok, None


def _parse_neigh_linux(
    output: str,
) -> tuple[Literal["REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE"] | None, str | None]:
    for line in output.splitlines():
        tokens = line.split()
        state = next((t for t in tokens if t.upper() in _NEIGH_STATES), None)
        mac_m = _MAC_RE.search(line)
        if state or mac_m:
            return (state.upper() if state else None), (mac_m.group(1) if mac_m else None)
    return None, None


def _parse_arp_macos(
    output: str,
) -> tuple[Literal["REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE"] | None, str | None]:
    if "no entry" in output.lower() or not output.strip():
        return None, None
    mac_m = _MAC_RE.search(output)
    return ("REACHABLE" if mac_m else None), (mac_m.group(1) if mac_m else None)


def probe_reachability(
    env: Environment, target_ip: str, target_label: str,
    interfaces: list[NetworkInterface],
) -> ReachabilityState:
    same_subnet, same_subnet_iface = _in_same_subnet(interfaces, target_ip)

    if env.platform == "macos":
        route = _run_cmd(["route", "-n", "get", target_ip])
        route_iface = None
        route_src = None
        if route.found and route.rc == 0:
            for line in route.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("interface:"):
                    route_iface = stripped.split(":", 1)[1].strip()
                elif stripped.startswith("src:") or stripped.startswith("source:"):
                    route_src = stripped.split(":", 1)[1].strip()
        ping = _run_cmd(["ping", "-c", "1", "-W", "1000", target_ip], timeout=3)
        arp = _run_cmd(["arp", "-n", target_ip])
        neigh_state, neigh_mac = _parse_arp_macos(arp.stdout if arp.found else "")
    else:
        route = _run_cmd(["ip", "-o", "route", "get", target_ip])
        route_iface = None
        route_src = None
        if route.found and route.rc == 0:
            dev_m = _IP_ROUTE_DEV_RE.search(route.stdout)
            src_m = _IP_ROUTE_SRC_RE.search(route.stdout)
            route_iface = dev_m.group(1) if dev_m else None
            route_src = src_m.group(1) if src_m else None
        ping = _run_cmd(["ping", "-c", "1", "-W", "1", target_ip], timeout=3)
        neigh = _run_cmd(["ip", "-o", "neigh", "show", target_ip])
        neigh_state, neigh_mac = _parse_neigh_linux(neigh.stdout if neigh.found else "")

    ping_ok, rtt = (_parse_ping(ping.stdout) if ping.found and ping.rc == 0 else (False, None))
    udp_ok, udp_err = _probe_udp_send(target_ip)

    return ReachabilityState(
        target_ip=target_ip, target_label=target_label,
        same_subnet=same_subnet, same_subnet_iface=same_subnet_iface,
        route_iface=route_iface, route_src=route_src,
        ping_ok=ping_ok, ping_rtt_ms=rtt,
        neigh_state=neigh_state, neigh_mac=neigh_mac,
        udp_send_ok=udp_ok, udp_error=udp_err,
    )


def assess_reachability(state: ReachabilityState) -> list[CheckResult]:
    results: list[CheckResult] = []
    prefix = f"{state.target_label} · "

    if state.same_subnet:
        results.append(CheckResult(
            name=prefix + "网段匹配",
            status=Status.PASS,
            message=(
                f"目标 IP {state.target_ip} 与本机 {state.same_subnet_iface} 同网段"
            ),
        ))
    else:
        results.append(CheckResult(
            name=prefix + "网段匹配",
            status=Status.WARN,
            message=f"目标 IP {state.target_ip} 与本机任一网卡均不同网段",
            fix_hint=(
                "PPCS 打洞跨网段成功率低。若确需跨网段, 请确认:\n"
                "  1. 两个网段之间存在三层可达\n"
                "  2. 路由器/网关允许 UDP 双向转发\n"
                "  3. 摄像头/主机均无静态 ACL 拦截"
            ),
        ))

    if state.route_iface is None:
        results.append(CheckResult(
            name=prefix + "路由出接口",
            status=Status.WARN,
            message="路由表无法给出到目标的出接口",
        ))
    elif state.same_subnet and state.route_iface != state.same_subnet_iface:
        results.append(CheckResult(
            name=prefix + "路由出接口",
            status=Status.WARN,
            message=(
                f"路由走 {state.route_iface} 但目标与 {state.same_subnet_iface} 同网段, "
                "多网卡场景请核对"
            ),
        ))
    else:
        src = f" (src {state.route_src})" if state.route_src else ""
        results.append(CheckResult(
            name=prefix + "路由出接口",
            status=Status.PASS,
            message=f"路由走接口 {state.route_iface}{src}",
        ))

    if state.ping_ok:
        rtt_str = f", RTT {state.ping_rtt_ms:.1f} ms" if state.ping_rtt_ms is not None else ""
        results.append(CheckResult(
            name=prefix + "L3 可达",
            status=Status.PASS,
            message=f"ping 成功{rtt_str}",
        ))
    elif state.neigh_state in ("REACHABLE", "STALE", "DELAY"):
        results.append(CheckResult(
            name=prefix + "L3 可达",
            status=Status.WARN,
            message=(
                f"ping 未收到回包, 但 ARP 表状态为 {state.neigh_state}, "
                "对端可能仅拦 ICMP"
            ),
        ))
    else:
        neigh_desc = state.neigh_state or "未知"
        results.append(CheckResult(
            name=prefix + "L3 可达",
            status=Status.FAIL,
            message=f"ping 失败, ARP 表状态: {neigh_desc}",
        ))

    if not state.udp_send_ok:
        results.append(CheckResult(
            name=prefix + "UDP 探测",
            status=Status.FAIL,
            message=f"UDP 无法发出: {state.udp_error}",
            fix_hint=(
                "UDP 出站被本机策略拦截, 请检查:\n"
                "  1. iptables OUTPUT 链: sudo iptables -L OUTPUT -n\n"
                "  2. 容器 seccomp / AppArmor 策略\n"
                "  3. 若 errno=101 (Network unreachable), 说明无路由到目标网段"
            ),
        ))
    elif state.udp_error and "ICMP Port Unreachable" in state.udp_error:
        results.append(CheckResult(
            name=prefix + "UDP 探测",
            status=Status.PASS,
            message="UDP 到达目标主机 (收到 ICMP Port Unreachable, 端口无监听属正常)",
        ))
    elif state.ping_ok and state.neigh_state in ("REACHABLE", "STALE", "DELAY"):
        results.append(CheckResult(
            name=prefix + "UDP 探测",
            status=Status.PASS,
            message="UDP 出站正常, L3/L2 综合可达 (UDP 无 ACK, 无法 100% 确认送达)",
        ))
    else:
        results.append(CheckResult(
            name=prefix + "UDP 探测",
            status=Status.WARN,
            message=(
                "UDP 出站正常, 但 L3/L2 证据不足, 无法确认对端收到 "
                "(UDP 协议限制, 无 delivery confirmation)"
            ),
        ))
    return results


def check_reachability(
    env: Environment, target_ip: str, target_label: str,
    interfaces: list[NetworkInterface],
) -> list[CheckResult]:
    state = probe_reachability(env, target_ip, target_label, interfaces)
    return assess_reachability(state)


# ─── Rendering (text) ──────────────────────────────────────────────────────────


_STATUS_ICON = {
    Status.PASS: "✅",
    Status.WARN: "⚠️ ",
    Status.FAIL: "❌",
}

_SECTION_WIDTH = 60


def _display_width(s: str) -> int:
    w = 0
    for ch in s:
        code = ord(ch)
        if (
            0x1100 <= code <= 0x115F
            or 0x2E80 <= code <= 0x9FFF
            or 0xA960 <= code <= 0xA97F
            or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE4F
            or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6
        ):
            w += 2
        else:
            w += 1
    return w


def _section_header(title: str) -> str:
    prefix = f"━━━ {title} "
    return prefix + "━" * max(0, _SECTION_WIDTH - _display_width(prefix))


def _render_host(env: Environment, network_state: NetworkState) -> None:
    click.echo()
    click.echo(_section_header("主机环境信息"))
    click.echo(f"    OS:       {env.distro or 'unknown'}")
    click.echo(f"    Kernel:   {env.kernel}")
    click.echo(f"    运行时:   {' · '.join(_runtime_tags(env))}")
    non_virtual = [i for i in network_state.interfaces if not i.is_virtual]
    if not non_virtual:
        click.echo("    网卡:     (无可用 IPv4 网卡)")
    else:
        for idx, iface in enumerate(non_virtual):
            label = "网卡:" if idx == 0 else "     "
            click.echo(f"    {label:<10}{iface.name:<8}{iface.ip}/{iface.prefix}")
    click.echo()


def _render_result(r: CheckResult) -> None:
    icon = _STATUS_ICON[r.status]
    click.echo(f"  {icon} {r.name}")
    for line in r.message.splitlines():
        click.echo(f"     {line}")
    if r.fix_hint:
        click.echo()
        click.echo("     \U0001f4a1 修复建议:")
        for line in r.fix_hint.split("\n"):
            click.echo(f"        {line}")
    click.echo()


def _render_miloco(results: list[CheckResult]) -> None:
    click.echo(_section_header("Miloco 运行状态"))
    if not results:
        click.echo("  (无输出)")
        click.echo()
        return
    for r in results:
        _render_result(r)


def _render_checks(results: list[CheckResult]) -> None:
    click.echo(_section_header("检测状态"))
    if not results:
        click.echo("  (无输出)")
        click.echo()
        return
    for r in results:
        _render_result(r)


def _render_summary(results: list[CheckResult]) -> None:
    click.echo("─" * _SECTION_WIDTH)
    counts = {s: 0 for s in Status}
    for r in results:
        counts[r.status] += 1
    parts = []
    if counts[Status.PASS]:
        parts.append(f"✅ {counts[Status.PASS]} pass")
    if counts[Status.WARN]:
        parts.append(f"⚠️  {counts[Status.WARN]} warn")
    if counts[Status.FAIL]:
        parts.append(f"❌ {counts[Status.FAIL]} fail")
    click.echo(f"  {' / '.join(parts) if parts else '(无检测项)'}")
    click.echo()


# ─── Rendering (JSON) ──────────────────────────────────────────────────────────


def _to_json(
    env: Environment,
    network_state: NetworkState,
    backend_state: BackendState,
    all_results: list[CheckResult],
) -> dict:
    counts = {s: 0 for s in Status}
    for r in all_results:
        counts[r.status] += 1
    return {
        "schema_version": 1,
        "host": {
            "platform": env.platform,
            "distro": env.distro,
            "kernel": env.kernel,
            "runtime_tags": _runtime_tags(env),
            "is_container": env.is_container,
            "container_net": env.container_net,
            "network_interfaces": [
                {
                    "name": i.name, "ip": i.ip, "prefix": i.prefix,
                    "is_virtual": i.is_virtual,
                }
                for i in network_state.interfaces
            ],
        },
        "miloco": {
            "backend": {
                "url": backend_state.url,
                "reachable": backend_state.reachable,
                "error": backend_state.error,
            },
            "account": {
                "bound": backend_state.account_bound,
                "uid": backend_state.account_uid,
            },
            "home": {
                "enabled": backend_state.home_enabled,
                "id": backend_state.home_id,
                "name": backend_state.home_name,
            },
            "cameras": [
                {
                    "did": c.did, "name": c.name, "online": c.online,
                    "lan_online": c.lan_online, "local_ip": c.local_ip,
                }
                for c in backend_state.cameras
            ],
        },
        "checks": [
            {
                "section": r.section, "name": r.name, "status": r.status.value,
                "message": r.message, "fix_hint": r.fix_hint,
            }
            for r in all_results
        ],
        "summary": {
            "pass": counts[Status.PASS],
            "warn": counts[Status.WARN],
            "fail": counts[Status.FAIL],
        },
        "exit_code": 1 if counts[Status.FAIL] else 0,
    }


# ─── Command entry ─────────────────────────────────────────────────────────────


@click.command("doctor")
@click.option(
    "--device-ip", default=None, metavar="IPv4",
    help="指定摄像头/设备 IP, 触发主动连通性探测。不指定时自动对已发现的摄像头逐台探测。",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="输出结构化 JSON 到 stdout, 无文本渲染。",
)
def doctor_cmd(device_ip: str | None, json_output: bool):
    """环境诊断: 判断本机能否 UDP 连上米家摄像头。"""
    if device_ip:
        try:
            ipaddress.IPv4Address(device_ip)
        except (ipaddress.AddressValueError, ValueError):
            raise click.BadParameter(
                f"'{device_ip}' 不是合法的 IPv4 地址", param_hint="--device-ip",
            )

    env = probe_environment()
    network_state = probe_network(env)
    backend_state = probe_backend()

    all_results: list[CheckResult] = []
    all_results.extend(assess_network_empty(network_state))
    all_results.extend(assess_backend(backend_state))
    all_results.extend(check_firewall(env))
    all_results.extend(check_container(env))
    all_results.extend(check_wsl(env))

    if device_ip:
        all_results.extend(check_reachability(
            env, device_ip, "--device-ip", network_state.interfaces,
        ))
    else:
        for cam in backend_state.cameras:
            if cam.local_ip:
                all_results.extend(check_reachability(
                    env, cam.local_ip, f'摄像头 "{cam.name}"',
                    network_state.interfaces,
                ))

    if json_output:
        click.echo(json.dumps(
            _to_json(env, network_state, backend_state, all_results),
            ensure_ascii=False,
        ))
    else:
        click.echo()
        click.echo("\U0001fa7a Miloco 环境诊断")
        _render_host(env, network_state)
        _render_miloco([r for r in all_results if r.section == "miloco"])
        _render_checks([r for r in all_results if r.section in ("host", "checks")])
        _render_summary(all_results)

    if any(r.status == Status.FAIL for r in all_results):
        raise SystemExit(1)
