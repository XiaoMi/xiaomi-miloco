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
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

import click
import httpx

from miloco_cli.commands.doctor_i18n import SUPPORTED_LANGS, Translator, make_translator
from miloco_cli.config import load_config

_ZH_T: Translator = make_translator("zh")

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
    version_data: dict | None = None


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
    udp_local_ip: str | None = None
    udp_local_iface: str | None = None


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
    return name in ("lo", "lo0") or name.startswith(_VIRTUAL_PREFIXES)


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


_DOCKER_BRIDGE_NET = "172.16.0.0/12"


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
        # /proc/1/cgroup 不存在或不可读, 视为无容器信号
        pass
    return False


def _detect_container_net() -> Literal["host", "bridge", "other"] | None:
    """区分 host / bridge / other 三种容器网络模式。

    判据: 主 = 是否存在非 virtual 网卡 (host 模式共享宿主机物理设备);
    辅 = 默认网关是否落在 docker 私网段 (bridge 兜底判定)。
    不依赖网卡名前缀 —— Docker bridge 也叫 eth0, 名字启发式会误判。
    """
    net_dir = "/sys/class/net"
    try:
        names = os.listdir(net_dir)
    except OSError:
        return None

    for name in names:
        if name == "lo":
            continue
        try:
            target = os.readlink(f"{net_dir}/{name}")
        except OSError:
            continue
        if "/virtual/" not in target:
            return "host"

    gateway = _read_default_gateway()
    if gateway is None:
        return "other"
    try:
        gw_addr = ipaddress.IPv4Address(gateway)
        docker_bridge_net = ipaddress.IPv4Network(_DOCKER_BRIDGE_NET)
    except (ipaddress.AddressValueError, ValueError):
        return "other"
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
        # /etc/os-release 缺失或不可读, 降级到 /etc/issue
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


def _runtime_tags(env: Environment, t: Translator = _ZH_T) -> list[str]:
    tags: list[str] = []
    if env.platform == "wsl":
        tags.append("WSL2")
    if env.is_container:
        tags.append("Docker container")
    if not tags:
        tags.append(t("runtime.native"))
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


def assess_network_empty(state: NetworkState, t: Translator = _ZH_T) -> list[CheckResult]:
    non_virtual = [i for i in state.interfaces if not i.is_virtual]
    if not non_virtual:
        return [CheckResult(
            section="host",
            name=t("nic_empty.name"),
            status=Status.FAIL,
            message=t("nic_empty.message"),
        )]
    return []


# ─── Container ─────────────────────────────────────────────────────────────────


def check_container(env: Environment, t: Translator = _ZH_T) -> list[CheckResult]:
    if not env.is_container:
        return []
    if env.container_net == "host":
        return [CheckResult(
            name=t("container.host.name"),
            status=Status.PASS,
            message=t("container.host.message"),
        )]
    if env.container_net == "bridge":
        return [CheckResult(
            name=t("container.bridge.name"),
            status=Status.FAIL,
            message=t("container.bridge.message"),
            fix_hint=t("container.bridge.fix"),
        )]
    return [CheckResult(
        name=t("container.other.name"),
        status=Status.WARN,
        message=t("container.other.message"),
        fix_hint=t("container.other.fix"),
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


def assess_ufw(state: UfwState, t: Translator = _ZH_T) -> list[CheckResult]:
    if not state.installed:
        return []
    if state.enabled_via_conf is False:
        return [CheckResult(
            name=t("ufw.disabled.name"),
            status=Status.PASS,
            message=t("ufw.disabled.message"),
        )]
    if state.enabled_via_conf is None:
        return []
    if not state.rules_readable:
        return [CheckResult(
            name=t("ufw.unreadable.name"),
            status=Status.WARN,
            message=t("ufw.unreadable.message"),
        )]
    if state.default_deny_incoming and not state.has_udp_allow:
        return [CheckResult(
            name=t("ufw.deny.name"),
            status=Status.FAIL,
            message=t("ufw.deny.message"),
            fix_hint=t("ufw.deny.fix"),
        )]
    return [CheckResult(
        name=t("ufw.allow.name"),
        status=Status.PASS,
        message=t("ufw.allow.message"),
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


def _firewalld_fix_hint(zone: str, t: Translator = _ZH_T) -> str:
    return t("firewalld.fix", zone=zone)


def assess_firewalld(state: FirewalldState, t: Translator = _ZH_T) -> list[CheckResult]:
    if not state.installed or state.running is False:
        return []
    if state.running is None:
        return [CheckResult(
            name=t("firewalld.state_unreadable.name"),
            status=Status.WARN,
            message=t("firewalld.state_unreadable.message"),
        )]
    if not state.listing_readable:
        return [CheckResult(
            name=t("firewalld.zone_unreadable.name"),
            status=Status.WARN,
            message=t("firewalld.zone_unreadable.message"),
        )]
    zone = state.zone or "<unknown>"
    if state.target in ("DROP", "REJECT"):
        return [CheckResult(
            name=t("firewalld.blocked.name"),
            status=Status.FAIL,
            message=t("firewalld.blocked.message", zone=zone, target=state.target),
            fix_hint=_firewalld_fix_hint(zone, t),
        )]
    if state.target == "ACCEPT" or state.has_protocol_udp:
        return [CheckResult(
            name=t("firewalld.accept.name"),
            status=Status.PASS,
            message=t("firewalld.accept.message", zone=zone),
        )]
    if state.has_port_udp_only:
        return [CheckResult(
            name=t("firewalld.port_udp_only.name"),
            status=Status.WARN,
            message=t("firewalld.port_udp_only.message", zone=zone),
            fix_hint=_firewalld_fix_hint(zone, t),
        )]
    return [CheckResult(
        name=t("firewalld.unclear.name"),
        status=Status.WARN,
        message=t("firewalld.unclear.message", zone=zone),
        fix_hint=_firewalld_fix_hint(zone, t),
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


def assess_iptables(state: IptablesState, t: Translator = _ZH_T) -> list[CheckResult]:
    if not state.installed:
        return []
    if not state.readable:
        return [CheckResult(
            name=t("iptables.unreadable.name"),
            status=Status.WARN,
            message=t("iptables.unreadable.message"),
        )]
    if state.has_udp_block and state.has_udp_accept:
        return [CheckResult(
            name=t("iptables.conflict.name"),
            status=Status.WARN,
            message=t("iptables.conflict.message"),
            fix_hint=t("iptables.conflict.fix"),
        )]
    if state.has_udp_block or (state.policy_drop and not state.has_udp_accept):
        reason = (
            t("iptables.policy_drop_reason")
            if state.policy_drop and not state.has_udp_block
            else t("iptables.explicit_drop_reason")
        )
        return [CheckResult(
            name=t("iptables.blocked.name"),
            status=Status.FAIL,
            message=t("iptables.blocked.message", reason=reason),
            fix_hint=t("iptables.blocked.fix"),
        )]
    if state.policy_drop and state.udp_accept_all_port_limited:
        return [CheckResult(
            name=t("iptables.port_limited.name"),
            status=Status.WARN,
            message=t("iptables.port_limited.message"),
            fix_hint=t("iptables.blocked.fix"),
        )]
    return [CheckResult(
        name=t("iptables.pass.name"),
        status=Status.PASS,
        message=t("iptables.pass.message"),
    )]


def check_firewall(env: Environment, t: Translator = _ZH_T) -> list[CheckResult]:
    if env.platform == "macos":
        return [CheckResult(
            name=t("firewall.macos.name"),
            status=Status.PASS,
            message=t("firewall.macos.message"),
        )]

    ufw_state = probe_ufw()
    ufw_results = assess_ufw(ufw_state, t)
    if ufw_results:
        return ufw_results

    fwd_state = probe_firewalld()
    fwd_results = assess_firewalld(fwd_state, t)
    if fwd_results:
        return fwd_results

    ipt_state = probe_iptables()
    ipt_results = assess_iptables(ipt_state, t)
    if ipt_results:
        return ipt_results

    return [CheckResult(
        name=t("firewall.none.name"),
        status=Status.PASS,
        message=t("firewall.none.message"),
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
        (
            "(Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore "
            "-Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}').DefaultInboundAction"
        ),
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


def assess_wsl(state: WslState, t: Translator = _ZH_T) -> list[CheckResult]:
    if not state.is_wsl:
        return []
    results: list[CheckResult] = []
    if state.wslconfig_path is None:
        results.append(CheckResult(
            name=t("wsl.no_path.name"),
            status=Status.WARN,
            message=t("wsl.no_path.message"),
        ))
    elif not state.wslconfig_exists:
        results.append(CheckResult(
            name=t("wsl.no_config.name"),
            status=Status.FAIL,
            message=t("wsl.no_config.message", path=state.wslconfig_path),
            fix_hint=t("wsl.no_config.fix"),
        ))
    elif state.mirrored_mode:
        results.append(CheckResult(
            name=t("wsl.mirrored.name"),
            status=Status.PASS,
            message=t("wsl.mirrored.message"),
        ))
    else:
        results.append(CheckResult(
            name=t("wsl.nat.name"),
            status=Status.FAIL,
            message=t("wsl.nat.message"),
            fix_hint=t("wsl.nat.fix"),
        ))

    if state.hyperv_default_inbound == "allow":
        results.append(CheckResult(
            name=t("hyperv.allow.name"),
            status=Status.PASS,
            message=t("hyperv.allow.message"),
        ))
    elif state.hyperv_default_inbound == "block":
        results.append(CheckResult(
            name=t("hyperv.block.name"),
            status=Status.FAIL,
            message=t("hyperv.block.message"),
            fix_hint=t("hyperv.block.fix"),
        ))
    else:
        results.append(CheckResult(
            name=t("hyperv.unknown.name"),
            status=Status.WARN,
            message=t("hyperv.unknown.message"),
        ))

    return results


def check_wsl(env: Environment, t: Translator = _ZH_T) -> list[CheckResult]:
    return assess_wsl(probe_wsl(env), t)


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


def _fetch_backend_version(client: httpx.Client) -> dict | None:
    """拉 /api/admin/version。旧 backend 无此端点返 404, 或 token 无效返 401 → None。"""
    try:
        r = client.get("/api/admin/version")
    except (httpx.HTTPError, httpx.TimeoutException):
        return None
    if not r.is_success:
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if body.get("code", 0) != 0:
        return None
    data = body.get("data")
    return data if isinstance(data, dict) else None


def probe_backend() -> BackendState:
    cfg = load_config()
    server = cfg.get("server", {})
    base_url = server.get("url", "http://127.0.0.1:1810")
    token = server.get("token", "")
    verify = bool(server.get("tls_verify", False))
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        with httpx.Client(
            base_url=base_url, headers=headers, timeout=3.0, verify=verify,
        ) as client:
            r_status = client.get("/api/miot/status")
            if not r_status.is_success:
                return _backend_state(
                    base_url, reachable=True,
                    error=f"HTTP {r_status.status_code}",
                )
            try:
                status_body = r_status.json()
            except ValueError:
                return _backend_state(
                    base_url, reachable=True,
                    error="non-JSON response (server.url may point to non-backend)",
                )
            if status_body.get("code", 0) != 0:
                return _backend_state(
                    base_url, reachable=True,
                    error=f"api code={status_body.get('code')}",
                )
            status_data = status_body.get("data") or {}
            is_bound = bool(status_data.get("is_bound"))
            uid = (status_data.get("user_info") or {}).get("uid")

            version_data = _fetch_backend_version(client)

            if not is_bound:
                return BackendState(
                    url=base_url, reachable=True, error=None,
                    account_bound=False, account_uid=None,
                    home_enabled=False, home_id=None, home_name=None,
                    cameras=[],
                    version_data=version_data,
                )

            r_homes = client.get("/api/miot/scope/homes")
            homes: list[dict] = []
            if r_homes.is_success:
                try:
                    hb = r_homes.json()
                except ValueError:
                    hb = {}
                if hb.get("code", 0) == 0:
                    homes = hb.get("data") or []
            enabled_home = next((h for h in homes if h.get("in_use")), None)
            if enabled_home is None:
                return BackendState(
                    url=base_url, reachable=True, error=None,
                    account_bound=True, account_uid=uid,
                    home_enabled=False, home_id=None, home_name=None,
                    cameras=[],
                    version_data=version_data,
                )

            r_cams = client.get("/api/miot/camera_list")
            cameras: list[CameraSummary] = []
            if r_cams.is_success:
                try:
                    cb = r_cams.json()
                except ValueError:
                    cb = {}
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
                version_data=version_data,
            )
    except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPError) as e:
        return _backend_state(
            base_url, reachable=False, error=str(e) or type(e).__name__,
        )


def _build_version_result(
    version_data: dict | None, t: Translator = _ZH_T,
) -> CheckResult | None:
    """把 /api/admin/version 的 payload 渲染成一条 CheckResult; 数据缺失返 None。"""
    if not isinstance(version_data, dict):
        return None
    pkg = version_data.get("version")
    if not pkg:
        return None
    lines = [t("version.pkg_line", version=pkg)]

    git = version_data.get("git")
    if isinstance(git, dict):
        short = git.get("commit_short")
        if not short:
            commit = git.get("commit")
            short = commit[:7] if isinstance(commit, str) and commit else None
        if short:
            parts = [short]
            branch = git.get("branch")
            if isinstance(branch, str) and branch:
                parts.append(branch)
            dirty = git.get("dirty")
            if dirty is True:
                parts.append(t("version.dirty"))
            elif dirty is False:
                parts.append(t("version.clean"))
            lines.append(t("version.git_prefix") + " · ".join(parts))
            commit_time = git.get("commit_time")
            if isinstance(commit_time, str) and commit_time:
                lines.append(t("version.commit_time_line", time=commit_time))

    return CheckResult(
        section="miloco",
        name=t("version.name"),
        status=Status.PASS,
        message="\n".join(lines),
    )


def assess_backend(state: BackendState, t: Translator = _ZH_T) -> list[CheckResult]:
    results: list[CheckResult] = []
    if not state.reachable:
        results.append(CheckResult(
            section="miloco",
            name=t("backend.unreachable.name"),
            status=Status.WARN,
            message=t("backend.unreachable.message", url=state.url, error=state.error),
            fix_hint=t("backend.unreachable.fix"),
        ))
        return results

    if state.error:
        results.append(CheckResult(
            section="miloco",
            name=t("backend.error.name"),
            status=Status.WARN,
            message=t("backend.error.message", url=state.url, error=state.error),
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name=t("backend.ok.name"),
        status=Status.PASS,
        message=t("backend.ok.message", url=state.url),
    ))

    version_result = _build_version_result(state.version_data, t)
    if version_result is not None:
        results.append(version_result)

    if not state.account_bound:
        results.append(CheckResult(
            section="miloco",
            name=t("account.unbound.name"),
            status=Status.WARN,
            message=t("account.unbound.message"),
            fix_hint=t("account.unbound.fix"),
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name=t("account.bound.name"),
        status=Status.PASS,
        message=t("account.bound.message", uid=state.account_uid or "unknown"),
    ))

    if not state.home_enabled:
        results.append(CheckResult(
            section="miloco",
            name=t("home.none.name"),
            status=Status.WARN,
            message=t("home.none.message"),
            fix_hint=t("home.none.fix"),
        ))
        return results

    results.append(CheckResult(
        section="miloco",
        name=t("home.ok.name"),
        status=Status.PASS,
        message=t("home.ok.message", home=state.home_name or state.home_id),
    ))

    if not state.cameras:
        results.append(CheckResult(
            section="miloco",
            name=t("cameras.none.name"),
            status=Status.WARN,
            message=t("cameras.none.message"),
        ))
        return results

    no_ip_text = t("cameras.no_lan_ip")
    lines = [f'  - "{c.name}" (did={c.did}): {c.local_ip or no_ip_text}'
             for c in state.cameras]
    joined = "\n".join(lines)
    count = len(state.cameras)
    all_have_ip = all(c.local_ip for c in state.cameras)
    all_missing_ip = all(not c.local_ip for c in state.cameras)
    if all_have_ip:
        results.append(CheckResult(
            section="miloco",
            name=t("cameras.all_ip.name"),
            status=Status.PASS,
            message=t("cameras.all_ip.message", count=count, lines=joined),
        ))
    elif all_missing_ip:
        results.append(CheckResult(
            section="miloco",
            name=t("cameras.all_missing.name"),
            status=Status.WARN,
            message=t("cameras.all_missing.message", count=count, lines=joined),
            fix_hint=t("cameras.all_missing.fix"),
        ))
    else:
        results.append(CheckResult(
            section="miloco",
            name=t("cameras.partial.name"),
            status=Status.WARN,
            message=t("cameras.partial.message", count=count, lines=joined),
        ))
    return results


# ─── Reachability ──────────────────────────────────────────────────────────────


_PING_RTT_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_IP_ROUTE_DEV_RE = re.compile(r"\bdev\s+(\S+)")
_IP_ROUTE_SRC_RE = re.compile(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)")
_MAC_RE = re.compile(r"((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})")
_NEIGH_STATES = ("REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE")


def _probe_udp_send(
    target_ip: str, port: int = 32100,
) -> tuple[bool, str | None, str | None]:
    """验证本机能否将 UDP 包递交给内核发出, 并捕获 connected socket 的 ICMP error。

    仅能验证:
      1. 本地路由表有到目标的路径 (connect() 不失败)
      2. 本地防火墙不拦截 UDP 出站 (send 不失败)
      3. 短窗口内内核未收到 ICMP Destination/Port Unreachable

    UDP 无 delivery confirmation, 不能确认对端收到; 综合 ping/neigh 才能判达。

    返回: (ok, error, local_ip); local_ip 是内核 connect() 后自动选定的 src IP,
    可用于反查实际出接口。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local_ip: str | None = None
    try:
        sock.settimeout(1.0)
        sock.connect((target_ip, port))
        try:
            local_ip = sock.getsockname()[0] or None
        except OSError:
            local_ip = None
        sock.send(b"miloco-doctor-probe")
        sock.settimeout(0.3)
        try:
            sock.recv(1024)
        except socket.timeout:
            return True, None, local_ip
        except ConnectionRefusedError:
            return True, "ICMP Port Unreachable", local_ip
    except OSError as e:
        return False, f"{e.strerror or type(e).__name__} (errno={e.errno})", local_ip
    finally:
        sock.close()
    return True, None, local_ip


_PING_RECV_RE = re.compile(r"(\d+)\s+(?:packets\s+)?received", re.IGNORECASE)


def _parse_ping(output: str) -> tuple[bool, float | None]:
    m = _PING_RTT_RE.search(output)
    if m:
        try:
            return True, float(m.group(1))
        except ValueError:
            return True, None
    m_recv = _PING_RECV_RE.search(output)
    if m_recv:
        try:
            return int(m_recv.group(1)) > 0, None
        except ValueError:
            return False, None
    return False, None


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
        ping = _run_cmd(["ping", "-c", "3", "-W", "1000", target_ip], timeout=5)
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
        ping = _run_cmd(["ping", "-c", "3", "-W", "1", target_ip], timeout=5)
        neigh = _run_cmd(["ip", "-o", "neigh", "show", target_ip])
        neigh_state, neigh_mac = _parse_neigh_linux(neigh.stdout if neigh.found else "")

    ping_ok, rtt = (_parse_ping(ping.stdout) if ping.found and ping.rc == 0 else (False, None))
    udp_ok, udp_err, udp_local = _probe_udp_send(target_ip)
    udp_local_iface = next(
        (i.name for i in interfaces if udp_local and i.ip == udp_local), None,
    )

    return ReachabilityState(
        target_ip=target_ip, target_label=target_label,
        same_subnet=same_subnet, same_subnet_iface=same_subnet_iface,
        route_iface=route_iface, route_src=route_src,
        ping_ok=ping_ok, ping_rtt_ms=rtt,
        neigh_state=neigh_state, neigh_mac=neigh_mac,
        udp_send_ok=udp_ok, udp_error=udp_err,
        udp_local_ip=udp_local, udp_local_iface=udp_local_iface,
    )


def _udp_iface_suffix(state: ReachabilityState, t: Translator = _ZH_T) -> str:
    """基于 UDP socket connect() 后 getsockname() 拿到的实际出接口 src, 拼输出后缀。"""
    if state.udp_local_iface and state.udp_local_ip:
        return t(
            "reach.udp.iface_suffix_full",
            iface=state.udp_local_iface, src=state.udp_local_ip,
        )
    if state.udp_local_ip:
        return t("reach.udp.iface_suffix_ip_only", src=state.udp_local_ip)
    return ""


def assess_reachability(state: ReachabilityState, t: Translator = _ZH_T) -> list[CheckResult]:
    results: list[CheckResult] = []
    prefix = t("reach.name_prefix", label=state.target_label)

    if state.same_subnet:
        results.append(CheckResult(
            name=prefix + t("reach.subnet.match.name"),
            status=Status.PASS,
            message=t(
                "reach.subnet.match.message",
                ip=state.target_ip, iface=state.same_subnet_iface,
            ),
        ))
    else:
        results.append(CheckResult(
            name=prefix + t("reach.subnet.mismatch.name"),
            status=Status.WARN,
            message=t("reach.subnet.mismatch.message", ip=state.target_ip),
            fix_hint=t("reach.subnet.mismatch.fix"),
        ))

    if state.route_iface is None:
        results.append(CheckResult(
            name=prefix + t("reach.route.none.name"),
            status=Status.WARN,
            message=t("reach.route.none.message"),
        ))
    elif state.same_subnet and state.route_iface != state.same_subnet_iface:
        results.append(CheckResult(
            name=prefix + t("reach.route.mismatch.name"),
            status=Status.WARN,
            message=t(
                "reach.route.mismatch.message",
                route_iface=state.route_iface,
                subnet_iface=state.same_subnet_iface,
            ),
        ))
    else:
        src_suffix = (
            t("reach.route.src_suffix", src=state.route_src) if state.route_src else ""
        )
        results.append(CheckResult(
            name=prefix + t("reach.route.ok.name"),
            status=Status.PASS,
            message=t(
                "reach.route.ok.message",
                route_iface=state.route_iface, src_suffix=src_suffix,
            ),
        ))

    if state.ping_ok:
        rtt_suffix = (
            t("reach.l3.ping_ok.rtt_suffix", rtt=f"{state.ping_rtt_ms:.1f}")
            if state.ping_rtt_ms is not None
            else ""
        )
        results.append(CheckResult(
            name=prefix + t("reach.l3.ping_ok.name"),
            status=Status.PASS,
            message=t("reach.l3.ping_ok.message", rtt_suffix=rtt_suffix),
        ))
    elif state.neigh_state in ("REACHABLE", "STALE", "DELAY"):
        results.append(CheckResult(
            name=prefix + t("reach.l3.arp_ok.name"),
            status=Status.WARN,
            message=t("reach.l3.arp_ok.message", state=state.neigh_state),
        ))
    else:
        neigh_desc = state.neigh_state or t("reach.l3.fail.unknown")
        results.append(CheckResult(
            name=prefix + t("reach.l3.fail.name"),
            status=Status.FAIL,
            message=t("reach.l3.fail.message", state=neigh_desc),
        ))

    if not state.udp_send_ok:
        results.append(CheckResult(
            name=prefix + t("reach.udp.blocked.name"),
            status=Status.FAIL,
            message=t("reach.udp.blocked.message", error=state.udp_error),
            fix_hint=t("reach.udp.blocked.fix"),
        ))
    else:
        suffix = _udp_iface_suffix(state, t)
        if state.udp_error and "ICMP Port Unreachable" in state.udp_error:
            results.append(CheckResult(
                name=prefix + t("reach.udp.port_unreach.name"),
                status=Status.PASS,
                message=t("reach.udp.port_unreach.message") + suffix,
            ))
        elif state.ping_ok and state.neigh_state in ("REACHABLE", "STALE", "DELAY"):
            results.append(CheckResult(
                name=prefix + t("reach.udp.pass.name"),
                status=Status.PASS,
                message=t("reach.udp.pass.message") + suffix,
            ))
        else:
            results.append(CheckResult(
                name=prefix + t("reach.udp.warn.name"),
                status=Status.WARN,
                message=t("reach.udp.warn.message") + suffix,
            ))
    return results


def check_reachability(
    env: Environment, target_ip: str, target_label: str,
    interfaces: list[NetworkInterface], t: Translator = _ZH_T,
) -> list[CheckResult]:
    state = probe_reachability(env, target_ip, target_label, interfaces)
    return assess_reachability(state, t)


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


_HOST_LABEL_WIDTH = 10


def _pad_display(s: str, width: int) -> str:
    return s + " " * max(0, width - _display_width(s))


def _render_host(env: Environment, network_state: NetworkState, t: Translator = _ZH_T) -> None:
    def _label(s: str) -> str:
        return _pad_display(s, _HOST_LABEL_WIDTH)

    click.echo()
    click.echo(_section_header(t("render.host.title")))
    click.echo(f"    {_label('OS:')}{env.distro or 'unknown'}")
    click.echo(f"    {_label('Kernel:')}{env.kernel}")
    click.echo(f"    {_label(t('render.host.runtime'))}{' · '.join(_runtime_tags(env, t))}")
    non_virtual = [i for i in network_state.interfaces if not i.is_virtual]
    if not non_virtual:
        click.echo(f"    {_label(t('render.host.nic'))}{t('render.host.nic_empty')}")
    else:
        nic_label = t("render.host.nic")
        for idx, iface in enumerate(non_virtual):
            label = nic_label if idx == 0 else ""
            click.echo(f"    {_label(label)}{iface.name:<8}{iface.ip}/{iface.prefix}")
    click.echo()


def _render_result(r: CheckResult, t: Translator = _ZH_T) -> None:
    icon = _STATUS_ICON[r.status]
    click.echo(f"  {icon} {r.name}")
    for line in r.message.splitlines():
        click.echo(f"     {line}")
    if r.fix_hint:
        click.echo()
        click.echo(f"     {t('render.fix_hint')}")
        for line in r.fix_hint.split("\n"):
            click.echo(f"        {line}")
    click.echo()


def _render_section_empty(t: Translator = _ZH_T) -> None:
    click.echo(f"  {t('render.section.empty')}")
    click.echo()


def _render_summary(results: list[CheckResult], t: Translator = _ZH_T) -> None:
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
    click.echo(f"  {' / '.join(parts) if parts else t('render.summary.empty')}")
    click.echo()


# ─── Rendering (JSON) ──────────────────────────────────────────────────────────


def _to_json(
    env: Environment,
    network_state: NetworkState,
    backend_state: BackendState,
    all_results: list[CheckResult],
    t: Translator = _ZH_T,
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
            "runtime_tags": _runtime_tags(env, t),
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
                "version": backend_state.version_data,
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
    help=(
        "指定摄像头/设备 IP, 触发主动连通性探测。不指定时自动对已发现的摄像头逐台探测。 / "
        "Target camera/device IP for active reachability probe; "
        "when omitted, discovered cameras are probed one by one."
    ),
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help=(
        "输出结构化 JSON 到 stdout, 无文本渲染。 / "
        "Emit structured JSON to stdout instead of text output."
    ),
)
@click.option(
    "--lang", "lang", type=click.Choice(list(SUPPORTED_LANGS)), default="zh",
    show_default=True,
    help="输出语言。 / Output language.",
)
def doctor_cmd(device_ip: str | None, json_output: bool, lang: str):
    """环境诊断: 判断本机能否 UDP 连上米家摄像头。 / Diagnose whether this host can reach Mi cameras via UDP."""
    t = make_translator(lang)

    if device_ip:
        try:
            ipaddress.IPv4Address(device_ip)
        except (ipaddress.AddressValueError, ValueError):
            raise click.BadParameter(
                t("entry.invalid_ip", ip=device_ip), param_hint="--device-ip",
            )

    if json_output:
        env = probe_environment()
        network_state = probe_network(env)
        backend_state = probe_backend()

        all_results: list[CheckResult] = []
        all_results.extend(assess_network_empty(network_state, t))
        all_results.extend(assess_backend(backend_state, t))
        all_results.extend(check_firewall(env, t))
        all_results.extend(check_container(env, t))
        all_results.extend(check_wsl(env, t))

        if device_ip:
            all_results.extend(check_reachability(
                env, device_ip, "--device-ip", network_state.interfaces, t,
            ))
        else:
            for cam in backend_state.cameras:
                if cam.local_ip:
                    all_results.extend(check_reachability(
                        env, cam.local_ip, t("camera.label", name=cam.name),
                        network_state.interfaces, t,
                    ))

        click.echo(json.dumps(
            _to_json(env, network_state, backend_state, all_results, t),
            ensure_ascii=False,
        ))
        if any(r.status == Status.FAIL for r in all_results):
            raise SystemExit(1)
        return

    def _flush() -> None:
        sys.stdout.flush()

    click.echo()
    click.echo(t("render.banner"))
    _flush()

    env = probe_environment()
    network_state = probe_network(env)
    _render_host(env, network_state, t)
    _flush()

    click.echo(_section_header(t("render.miloco.title")))
    _flush()
    backend_state = probe_backend()
    miloco_results = assess_backend(backend_state, t)
    if miloco_results:
        for r in miloco_results:
            _render_result(r, t)
            _flush()
    else:
        _render_section_empty(t)

    click.echo(_section_header(t("render.checks.title")))
    _flush()
    checks_results: list[CheckResult] = []

    def _emit(rs: list[CheckResult]) -> None:
        for r in rs:
            _render_result(r, t)
            checks_results.append(r)
        _flush()

    _emit(assess_network_empty(network_state, t))
    _emit(check_firewall(env, t))
    _emit(check_container(env, t))
    _emit(check_wsl(env, t))

    if device_ip:
        _emit(check_reachability(
            env, device_ip, "--device-ip", network_state.interfaces, t,
        ))
    else:
        for cam in backend_state.cameras:
            if cam.local_ip:
                _emit(check_reachability(
                    env, cam.local_ip, t("camera.label", name=cam.name),
                    network_state.interfaces, t,
                ))

    if not checks_results:
        _render_section_empty(t)

    all_results = miloco_results + checks_results
    _render_summary(all_results, t)

    if any(r.status == Status.FAIL for r in all_results):
        raise SystemExit(1)
