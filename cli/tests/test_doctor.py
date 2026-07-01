"""doctor 命令测试：mock 系统调用/网络/文件系统, 覆盖 probe/assess 分层。"""

import json
import socket
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from miloco_cli.commands.doctor import (
    BackendState,
    CameraSummary,
    CheckResult,
    CmdResult,
    Environment,
    FirewalldState,
    IptablesState,
    NetworkInterface,
    NetworkState,
    ReachabilityState,
    Status,
    UfwState,
    WslState,
    _in_same_subnet,
    _is_virtual_iface,
    _parse_neigh_linux,
    _parse_ping,
    _probe_udp_send,
    _build_version_result,
    _to_json,
    assess_backend,
    assess_firewalld,
    assess_iptables,
    assess_network_empty,
    assess_reachability,
    assess_ufw,
    assess_wsl,
    check_container,
    check_firewall,
    check_wsl,
    probe_backend,
    probe_environment,
    probe_network,
)
from miloco_cli.main import cli

# ─── Helpers ───────────────────────────────────────────────────────────────────


def _ok(stdout: str) -> CmdResult:
    return CmdResult(found=True, rc=0, stdout=stdout, stderr="")


def _fail(rc: int = 1, stderr: str = "") -> CmdResult:
    return CmdResult(found=True, rc=rc, stdout="", stderr=stderr)


def _env(
    platform: str = "linux",
    is_container: bool = False,
    container_net=None,
    distro: str = "Ubuntu 22.04 LTS",
    kernel: str = "Linux 6.5.0 x86_64",
) -> Environment:
    return Environment(
        platform=platform, is_container=is_container, container_net=container_net,
        distro=distro, kernel=kernel,
    )


def _iface(name: str, ip: str, prefix: int, virtual: bool | None = None) -> NetworkInterface:
    return NetworkInterface(
        name=name, ip=ip, prefix=prefix,
        is_virtual=_is_virtual_iface(name) if virtual is None else virtual,
    )


class _HTTPXBody:
    """轻量 mock httpx.Response, 支持 is_success + json()。"""

    def __init__(self, body: dict, status_code: int = 200):
        self.status_code = status_code
        self._body = body

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


def _mock_httpx(responses: dict):
    """构造一个可作为 httpx.Client 上下文的 MagicMock, GET 按 path 分发。

    未显式列出的 path 默认返 404, 用于让 doctor 的容错路径 (如可选的
    /api/admin/version 缺失) 走 None 而不是断言失败。
    """
    client = MagicMock()
    default_404 = _HTTPXBody({"code": 404}, status_code=404)

    def fake_get(path, *args, **kwargs):
        return responses.get(path, default_404)

    client.get.side_effect = fake_get
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx


@pytest.fixture
def runner():
    return CliRunner()


# ─── _is_virtual_iface / _in_same_subnet ───────────────────────────────────────


class TestIsVirtualIface:
    @pytest.mark.parametrize("name", ["lo", "docker0", "br-abc123", "veth1", "cni0", "flannel1", "cali_x", "kube-ipvs0"])
    def test_virtual(self, name):
        assert _is_virtual_iface(name) is True

    @pytest.mark.parametrize("name", ["eth0", "eno1", "enp3s0", "wlan0", "wlp2s0", "ens160"])
    def test_physical(self, name):
        assert _is_virtual_iface(name) is False


class TestInSameSubnet:
    def test_match(self):
        ifs = [_iface("eth0", "192.168.1.100", 24)]
        assert _in_same_subnet(ifs, "192.168.1.55") == (True, "eth0")

    def test_no_match(self):
        ifs = [_iface("eth0", "192.168.1.100", 24)]
        assert _in_same_subnet(ifs, "10.0.0.1") == (False, None)

    def test_ignores_virtual(self):
        ifs = [_iface("docker0", "172.17.0.1", 16), _iface("eth0", "192.168.1.100", 24)]
        assert _in_same_subnet(ifs, "172.17.0.5") == (False, None)

    def test_invalid_target(self):
        ifs = [_iface("eth0", "192.168.1.100", 24)]
        assert _in_same_subnet(ifs, "not-an-ip") == (False, None)

    def test_multi_iface_first_match(self):
        ifs = [
            _iface("eth0", "192.168.1.100", 24),
            _iface("eth1", "10.0.0.1", 24),
        ]
        assert _in_same_subnet(ifs, "10.0.0.5") == (True, "eth1")


# ─── probe_environment ─────────────────────────────────────────────────────────


class TestProbeEnvironment:
    def test_linux_bare_metal(self):
        with (
            patch("miloco_cli.commands.doctor.platform.system", return_value="Linux"),
            patch("miloco_cli.commands.doctor._is_wsl", return_value=False),
            patch("miloco_cli.commands.doctor._detect_is_container", return_value=False),
            patch("miloco_cli.commands.doctor._read_distro", return_value="Ubuntu 22.04"),
            patch("miloco_cli.commands.doctor.platform.uname") as mock_uname,
        ):
            mock_uname.return_value = MagicMock(system="Linux", release="6.5.0", machine="x86_64")
            env = probe_environment()
        assert env.platform == "linux"
        assert env.is_container is False
        assert env.container_net is None
        assert env.distro == "Ubuntu 22.04"
        assert "Linux" in env.kernel

    def test_wsl(self):
        with (
            patch("miloco_cli.commands.doctor.platform.system", return_value="Linux"),
            patch("miloco_cli.commands.doctor._is_wsl", return_value=True),
            patch("miloco_cli.commands.doctor._detect_is_container", return_value=False),
            patch("miloco_cli.commands.doctor._read_distro", return_value="Ubuntu 22.04"),
        ):
            env = probe_environment()
        assert env.platform == "wsl"

    def test_docker_host_net(self):
        with (
            patch("miloco_cli.commands.doctor.platform.system", return_value="Linux"),
            patch("miloco_cli.commands.doctor._is_wsl", return_value=False),
            patch("miloco_cli.commands.doctor._detect_is_container", return_value=True),
            patch("miloco_cli.commands.doctor._detect_container_net", return_value="host"),
            patch("miloco_cli.commands.doctor._read_distro", return_value="Ubuntu 22.04"),
        ):
            env = probe_environment()
        assert env.is_container is True
        assert env.container_net == "host"

    def test_docker_bridge_net(self):
        with (
            patch("miloco_cli.commands.doctor.platform.system", return_value="Linux"),
            patch("miloco_cli.commands.doctor._is_wsl", return_value=False),
            patch("miloco_cli.commands.doctor._detect_is_container", return_value=True),
            patch("miloco_cli.commands.doctor._detect_container_net", return_value="bridge"),
            patch("miloco_cli.commands.doctor._read_distro", return_value="Alpine"),
        ):
            env = probe_environment()
        assert env.container_net == "bridge"

    def test_macos(self):
        with (
            patch("miloco_cli.commands.doctor.platform.system", return_value="Darwin"),
            patch("miloco_cli.commands.doctor.platform.mac_ver", return_value=("14.5", "", "")),
            patch("miloco_cli.commands.doctor._detect_is_container", return_value=False),
        ):
            env = probe_environment()
        assert env.platform == "macos"
        assert env.distro == "macOS 14.5"


# ─── _detect_container_net ─────────────────────────────────────────────────────


class TestDetectContainerNet:
    """symlink 判据 (has virtual/) + 网关兜底, 覆盖 host / bridge / other / 兜底崩溃场景。"""

    @staticmethod
    def _patch_net(names, readlinks, gateway=None):
        from miloco_cli.commands import doctor as m

        def fake_readlink(path):
            key = path.split("/")[-1]
            if key in readlinks:
                return readlinks[key]
            raise OSError(2, "not a symlink")

        return (
            patch.object(m.os, "listdir", return_value=names),
            patch.object(m.os, "readlink", side_effect=fake_readlink),
            patch.object(m, "_read_default_gateway", return_value=gateway),
        )

    def test_host_mode_pci_iface(self):
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "enp0s31f6"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "enp0s31f6": "../../devices/pci0000:00/0000:00:1f.6/net/enp0s31f6",
            },
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "host"

    def test_bridge_default_docker_eth0(self):
        """Docker 默认 bridge: 容器内 eth0 是 veth (virtual), 网关 172.17.0.1。"""
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "eth0"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "eth0": "../../devices/virtual/net/eth0",
            },
            gateway="172.17.0.1",
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "bridge"

    def test_bridge_custom_subnet_172_20(self):
        """docker custom bridge (172.20.0.0/16) 也应被识别为 bridge。"""
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "eth0"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "eth0": "../../devices/virtual/net/eth0",
            },
            gateway="172.20.0.1",
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "bridge"

    def test_other_podman_slirp4netns(self):
        """podman rootless slirp4netns: 网关 10.0.2.2 不在 docker 私网段。"""
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "tap0"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "tap0": "../../devices/virtual/net/tap0",
            },
            gateway="10.0.2.2",
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "other"

    def test_other_no_gateway(self):
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "eth0"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "eth0": "../../devices/virtual/net/eth0",
            },
            gateway=None,
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "other"

    def test_host_mode_with_docker_gateway_still_host(self):
        """有物理网卡 → host 直接返回, 不会走到网关判定 (即使网关看起来像 docker 段)。"""
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "eno1"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "eno1": "../../devices/pci0000:00/0000:00:19.0/net/eno1",
            },
            gateway="172.17.0.1",
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "host"

    def test_listdir_permission_error_returns_none(self):
        from miloco_cli.commands import doctor as m
        with patch.object(m.os, "listdir", side_effect=OSError("perm denied")):
            assert m._detect_container_net() is None

    def test_gateway_invalid_ipv4_returns_other(self):
        """网关字符串非法 IPv4 (如 IPv6 fe80::), IPv4Address 抛错时应兜住返回 other。"""
        from miloco_cli.commands.doctor import _detect_container_net
        patches = self._patch_net(
            names=["lo", "eth0"],
            readlinks={
                "lo": "../../devices/virtual/net/lo",
                "eth0": "../../devices/virtual/net/eth0",
            },
            gateway="fe80::1",
        )
        with patches[0], patches[1], patches[2]:
            assert _detect_container_net() == "other"


# ─── probe_network ─────────────────────────────────────────────────────────────


class TestProbeNetwork:
    def test_linux_parses_ip_addr(self):
        fake_out = (
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: eth0    inet 192.168.1.100/24 brd 192.168.1.255 scope global eth0\n"
            "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
        )
        with patch("miloco_cli.commands.doctor._run_cmd", return_value=_ok(fake_out)):
            state = probe_network(_env(platform="linux"))
        names = [i.name for i in state.interfaces]
        assert names == ["lo", "eth0", "docker0"]
        eth0 = next(i for i in state.interfaces if i.name == "eth0")
        assert eth0.ip == "192.168.1.100"
        assert eth0.prefix == 24
        assert eth0.is_virtual is False
        docker = next(i for i in state.interfaces if i.name == "docker0")
        assert docker.is_virtual is True

    def test_macos_parses_ifconfig(self):
        fake_out = (
            "lo0: flags=8049<UP,LOOPBACK> mtu 16384\n"
            "\tinet 127.0.0.1 netmask 0xff000000\n"
            "en0: flags=8863<UP,BROADCAST> mtu 1500\n"
            "\tinet 192.168.1.100 netmask 0xffffff00 broadcast 192.168.1.255\n"
        )
        with patch("miloco_cli.commands.doctor._run_cmd", return_value=_ok(fake_out)):
            state = probe_network(_env(platform="macos"))
        assert len(state.interfaces) == 2
        en0 = next(i for i in state.interfaces if i.name == "en0")
        assert en0.ip == "192.168.1.100"
        assert en0.prefix == 24


class TestAssessNetworkEmpty:
    def test_only_virtual_fails(self):
        state = NetworkState(interfaces=[_iface("lo", "127.0.0.1", 8), _iface("docker0", "172.17.0.1", 16)])
        results = assess_network_empty(state)
        assert len(results) == 1
        assert results[0].status == Status.FAIL
        assert results[0].section == "host"

    def test_has_physical_no_output(self):
        state = NetworkState(interfaces=[_iface("eth0", "192.168.1.100", 24)])
        assert assess_network_empty(state) == []


# ─── check_container ───────────────────────────────────────────────────────────


class TestCheckContainer:
    def test_not_container_empty(self):
        assert check_container(_env(is_container=False)) == []

    def test_host_pass(self):
        results = check_container(_env(is_container=True, container_net="host"))
        assert len(results) == 1
        assert results[0].status == Status.PASS

    def test_bridge_fail(self):
        results = check_container(_env(is_container=True, container_net="bridge"))
        assert results[0].status == Status.FAIL
        assert "--network=host" in results[0].fix_hint

    def test_other_warn(self):
        results = check_container(_env(is_container=True, container_net="other"))
        assert results[0].status == Status.WARN


# ─── assess_ufw ────────────────────────────────────────────────────────────────


class TestAssessUfw:
    def test_not_installed_empty(self):
        state = UfwState(installed=False, enabled_via_conf=None, rules_readable=False,
                         default_deny_incoming=False, has_udp_allow=False)
        assert assess_ufw(state) == []

    def test_disabled_via_conf_pass(self):
        state = UfwState(installed=True, enabled_via_conf=False, rules_readable=False,
                         default_deny_incoming=False, has_udp_allow=False)
        results = assess_ufw(state)
        assert len(results) == 1
        assert results[0].status == Status.PASS

    def test_conf_unreadable_falls_through(self):
        state = UfwState(installed=True, enabled_via_conf=None, rules_readable=False,
                         default_deny_incoming=False, has_udp_allow=False)
        assert assess_ufw(state) == []

    def test_enabled_rules_unreadable_warn_no_fix(self):
        state = UfwState(installed=True, enabled_via_conf=True, rules_readable=False,
                         default_deny_incoming=False, has_udp_allow=False)
        results = assess_ufw(state)
        assert results[0].status == Status.WARN
        assert results[0].fix_hint is None
        assert "sudo" in results[0].message

    def test_enabled_deny_no_udp_fail(self):
        state = UfwState(installed=True, enabled_via_conf=True, rules_readable=True,
                         default_deny_incoming=True, has_udp_allow=False)
        results = assess_ufw(state)
        assert results[0].status == Status.FAIL
        assert "ufw allow" in results[0].fix_hint

    def test_enabled_allow_incoming_pass(self):
        state = UfwState(installed=True, enabled_via_conf=True, rules_readable=True,
                         default_deny_incoming=False, has_udp_allow=False)
        results = assess_ufw(state)
        assert results[0].status == Status.PASS

    def test_enabled_deny_but_udp_allowed_pass(self):
        state = UfwState(installed=True, enabled_via_conf=True, rules_readable=True,
                         default_deny_incoming=True, has_udp_allow=True)
        results = assess_ufw(state)
        assert results[0].status == Status.PASS


# ─── assess_firewalld ──────────────────────────────────────────────────────────


class TestAssessFirewalld:
    def test_not_installed_empty(self):
        state = FirewalldState(installed=False, running=None, zone=None,
                               listing_readable=False, target=None,
                               has_protocol_udp=False, has_port_udp_only=False)
        assert assess_firewalld(state) == []

    def test_not_running_empty(self):
        state = FirewalldState(installed=True, running=False, zone=None,
                               listing_readable=False, target=None,
                               has_protocol_udp=False, has_port_udp_only=False)
        assert assess_firewalld(state) == []

    def test_state_unknown_warn(self):
        state = FirewalldState(installed=True, running=None, zone=None,
                               listing_readable=False, target=None,
                               has_protocol_udp=False, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.WARN

    def test_listing_unreadable_warn(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=False, target=None,
                               has_protocol_udp=False, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.WARN

    def test_target_drop_fail(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=True, target="DROP",
                               has_protocol_udp=False, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.FAIL

    def test_target_accept_pass(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=True, target="ACCEPT",
                               has_protocol_udp=False, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.PASS

    def test_protocol_udp_pass(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=True, target="default",
                               has_protocol_udp=True, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.PASS

    def test_port_udp_only_warn(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=True, target="default",
                               has_protocol_udp=False, has_port_udp_only=True)
        results = assess_firewalld(state)
        assert results[0].status == Status.WARN
        assert "特定端口" in results[0].message

    def test_default_warn(self):
        state = FirewalldState(installed=True, running=True, zone="public",
                               listing_readable=True, target="default",
                               has_protocol_udp=False, has_port_udp_only=False)
        results = assess_firewalld(state)
        assert results[0].status == Status.WARN


# ─── assess_iptables ───────────────────────────────────────────────────────────


class TestAssessIptables:
    def test_not_installed_empty(self):
        state = IptablesState(installed=False, readable=False, policy_drop=False,
                              has_udp_block=False, has_udp_accept=False,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        assert assess_iptables(state) == []

    def test_unreadable_warn(self):
        state = IptablesState(installed=True, readable=False, policy_drop=False,
                              has_udp_block=False, has_udp_accept=False,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        results = assess_iptables(state)
        assert results[0].status == Status.WARN
        assert results[0].fix_hint is None

    def test_udp_block_fail(self):
        state = IptablesState(installed=True, readable=True, policy_drop=False,
                              has_udp_block=True, has_udp_accept=False,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        results = assess_iptables(state)
        assert results[0].status == Status.FAIL

    def test_policy_drop_no_accept_fail(self):
        state = IptablesState(installed=True, readable=True, policy_drop=True,
                              has_udp_block=False, has_udp_accept=False,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        results = assess_iptables(state)
        assert results[0].status == Status.FAIL

    def test_block_and_accept_warn(self):
        state = IptablesState(installed=True, readable=True, policy_drop=True,
                              has_udp_block=True, has_udp_accept=True,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        results = assess_iptables(state)
        assert results[0].status == Status.WARN

    def test_policy_drop_port_limited_warn(self):
        state = IptablesState(installed=True, readable=True, policy_drop=True,
                              has_udp_block=False, has_udp_accept=True,
                              has_blanket_accept=False, udp_accept_all_port_limited=True)
        results = assess_iptables(state)
        assert results[0].status == Status.WARN
        assert "特定端口" in results[0].message

    def test_pass_default(self):
        state = IptablesState(installed=True, readable=True, policy_drop=False,
                              has_udp_block=False, has_udp_accept=False,
                              has_blanket_accept=False, udp_accept_all_port_limited=False)
        results = assess_iptables(state)
        assert results[0].status == Status.PASS


# ─── check_firewall 整体调度 ──────────────────────────────────────────────────


class TestCheckFirewall:
    def test_macos_pass(self):
        results = check_firewall(_env(platform="macos"))
        assert len(results) == 1
        assert results[0].status == Status.PASS
        assert "macOS" in results[0].name

    def test_ufw_short_circuit(self):
        ufw_state = UfwState(installed=True, enabled_via_conf=False, rules_readable=False,
                             default_deny_incoming=False, has_udp_allow=False)
        fwd_state = FirewalldState(installed=False, running=None, zone=None,
                                   listing_readable=False, target=None,
                                   has_protocol_udp=False, has_port_udp_only=False)
        with (
            patch("miloco_cli.commands.doctor.probe_ufw", return_value=ufw_state),
            patch("miloco_cli.commands.doctor.probe_firewalld", return_value=fwd_state),
        ):
            results = check_firewall(_env(platform="linux"))
        assert len(results) == 1
        assert "ufw" in results[0].name

    def test_no_firewall_fallback_pass(self):
        no_ufw = UfwState(installed=False, enabled_via_conf=None, rules_readable=False,
                          default_deny_incoming=False, has_udp_allow=False)
        no_fwd = FirewalldState(installed=False, running=None, zone=None,
                                listing_readable=False, target=None,
                                has_protocol_udp=False, has_port_udp_only=False)
        no_ipt = IptablesState(installed=False, readable=False, policy_drop=False,
                               has_udp_block=False, has_udp_accept=False,
                               has_blanket_accept=False, udp_accept_all_port_limited=False)
        with (
            patch("miloco_cli.commands.doctor.probe_ufw", return_value=no_ufw),
            patch("miloco_cli.commands.doctor.probe_firewalld", return_value=no_fwd),
            patch("miloco_cli.commands.doctor.probe_iptables", return_value=no_ipt),
        ):
            results = check_firewall(_env(platform="linux"))
        assert len(results) == 1
        assert results[0].status == Status.PASS
        assert "未检测到" in results[0].message


# ─── assess_wsl / check_wsl ────────────────────────────────────────────────────


class TestAssessWsl:
    def test_not_wsl_empty(self):
        state = WslState(is_wsl=False, wslconfig_path=None, wslconfig_exists=False,
                         mirrored_mode=False, hyperv_default_inbound=None)
        assert assess_wsl(state) == []

    def test_mirrored_and_hyperv_allow(self, tmp_path):
        cfg = tmp_path / ".wslconfig"
        cfg.touch()
        state = WslState(is_wsl=True, wslconfig_path=cfg, wslconfig_exists=True,
                         mirrored_mode=True, hyperv_default_inbound="allow")
        results = assess_wsl(state)
        assert len(results) == 2
        assert all(r.status == Status.PASS for r in results)

    def test_no_mirrored_fails(self, tmp_path):
        cfg = tmp_path / ".wslconfig"
        cfg.touch()
        state = WslState(is_wsl=True, wslconfig_path=cfg, wslconfig_exists=True,
                         mirrored_mode=False, hyperv_default_inbound="unknown")
        results = assess_wsl(state)
        assert results[0].status == Status.FAIL
        assert results[1].status == Status.WARN

    def test_wslconfig_missing_fails(self, tmp_path):
        cfg = tmp_path / "nonexistent.wslconfig"
        state = WslState(is_wsl=True, wslconfig_path=cfg, wslconfig_exists=False,
                         mirrored_mode=False, hyperv_default_inbound="allow")
        results = assess_wsl(state)
        assert results[0].status == Status.FAIL
        assert ".wslconfig 不存在" in results[0].message

    def test_wslconfig_path_none_warn(self):
        state = WslState(is_wsl=True, wslconfig_path=None, wslconfig_exists=False,
                         mirrored_mode=False, hyperv_default_inbound="allow")
        results = assess_wsl(state)
        assert results[0].status == Status.WARN
        assert "无法定位" in results[0].message

    def test_hyperv_block_fail(self, tmp_path):
        cfg = tmp_path / ".wslconfig"
        cfg.touch()
        state = WslState(is_wsl=True, wslconfig_path=cfg, wslconfig_exists=True,
                         mirrored_mode=True, hyperv_default_inbound="block")
        results = assess_wsl(state)
        assert results[1].status == Status.FAIL


class TestCheckWslIntegration:
    def test_non_wsl_empty(self):
        assert check_wsl(_env(platform="linux")) == []


# ─── probe_backend ─────────────────────────────────────────────────────────────


_DEFAULT_CFG = {"server": {"url": "http://127.0.0.1:1810", "token": "t"}}


class TestProbeBackend:
    def test_connect_refused(self):
        import httpx
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client",
                  side_effect=httpx.ConnectError("Connection refused")),
        ):
            state = probe_backend()
        assert state.reachable is False
        assert "refused" in state.error.lower()
        assert state.cameras == []

    def test_business_error_code_non_zero(self):
        responses = {"/api/miot/status": _HTTPXBody({"code": 3001, "message": "err", "data": None})}
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client", return_value=_mock_httpx(responses)),
        ):
            state = probe_backend()
        assert state.reachable is True
        assert "3001" in state.error

    def test_not_bound(self):
        responses = {"/api/miot/status": _HTTPXBody({
            "code": 0, "data": {"is_bound": False, "max_enabled_cameras": 4},
        })}
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client", return_value=_mock_httpx(responses)),
        ):
            state = probe_backend()
        assert state.reachable is True
        assert state.account_bound is False
        assert state.account_uid is None
        assert state.home_enabled is False

    def test_bound_no_home(self):
        responses = {
            "/api/miot/status": _HTTPXBody({
                "code": 0, "data": {
                    "is_bound": True, "max_enabled_cameras": 4,
                    "user_info": {"uid": "12345"},
                },
            }),
            "/api/miot/scope/homes": _HTTPXBody({
                "code": 0, "data": [
                    {"home_id": "h1", "home_name": "客厅", "in_use": False},
                ],
            }),
        }
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client", return_value=_mock_httpx(responses)),
        ):
            state = probe_backend()
        assert state.account_bound is True
        assert state.account_uid == "12345"
        assert state.home_enabled is False

    def test_bound_home_no_cameras(self):
        responses = {
            "/api/miot/status": _HTTPXBody({
                "code": 0, "data": {
                    "is_bound": True, "user_info": {"uid": "12345"},
                },
            }),
            "/api/miot/scope/homes": _HTTPXBody({
                "code": 0, "data": [{"home_id": "h1", "home_name": "客厅", "in_use": True}],
            }),
            "/api/miot/camera_list": _HTTPXBody({"code": 0, "data": []}),
        }
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client", return_value=_mock_httpx(responses)),
        ):
            state = probe_backend()
        assert state.home_enabled is True
        assert state.home_id == "h1"
        assert state.cameras == []

    def test_bound_home_with_cameras(self):
        responses = {
            "/api/miot/status": _HTTPXBody({
                "code": 0, "data": {
                    "is_bound": True, "user_info": {"uid": "12345"},
                },
            }),
            "/api/miot/scope/homes": _HTTPXBody({
                "code": 0, "data": [{"home_id": "h1", "home_name": "客厅", "in_use": True}],
            }),
            "/api/miot/camera_list": _HTTPXBody({"code": 0, "data": [
                {"did": "d1", "name": "客厅摄像头", "online": True,
                 "lan_online": True, "local_ip": "192.168.1.55"},
                {"did": "d2", "name": "卧室摄像头", "online": True,
                 "lan_online": False, "local_ip": None},
            ]}),
        }
        with (
            patch("miloco_cli.commands.doctor.load_config", return_value=_DEFAULT_CFG),
            patch("miloco_cli.commands.doctor.httpx.Client", return_value=_mock_httpx(responses)),
        ):
            state = probe_backend()
        assert len(state.cameras) == 2
        assert state.cameras[0].local_ip == "192.168.1.55"
        assert state.cameras[1].local_ip is None


# ─── assess_backend ────────────────────────────────────────────────────────────


def _bs(**kw) -> BackendState:
    defaults = dict(
        url="http://127.0.0.1:1810", reachable=True, error=None,
        account_bound=True, account_uid="12345",
        home_enabled=True, home_id="h1", home_name="客厅",
        cameras=[CameraSummary(
            did="d1", name="客厅摄像头", online=True,
            lan_online=True, local_ip="192.168.1.55",
        )],
    )
    defaults.update(kw)
    return BackendState(**defaults)


class TestAssessBackend:
    def test_backend_unreachable(self):
        results = assess_backend(_bs(reachable=False, error="Connection refused"))
        assert len(results) == 1
        assert results[0].status == Status.WARN
        assert results[0].section == "miloco"
        assert "miloco-cli service start" in results[0].fix_hint

    def test_not_bound(self):
        results = assess_backend(_bs(account_bound=False, account_uid=None,
                                     home_enabled=False, home_id=None,
                                     home_name=None, cameras=[]))
        assert len(results) == 2
        assert results[0].status == Status.PASS
        assert results[1].status == Status.WARN
        assert "account login" in results[1].fix_hint

    def test_bound_no_home(self):
        results = assess_backend(_bs(home_enabled=False, home_id=None,
                                     home_name=None, cameras=[]))
        assert len(results) == 3
        assert results[2].status == Status.WARN

    def test_bound_home_no_cams(self):
        results = assess_backend(_bs(cameras=[]))
        assert len(results) == 4
        assert results[3].status == Status.WARN
        assert "未发现" in results[3].message

    def test_all_cameras_have_ip_pass(self):
        results = assess_backend(_bs())
        assert results[3].status == Status.PASS
        assert "192.168.1.55" in results[3].message

    def test_all_cams_missing_ip_warn(self):
        results = assess_backend(_bs(cameras=[
            CameraSummary(did="d1", name="cam1", online=True, lan_online=False, local_ip=None),
        ]))
        assert results[3].status == Status.WARN
        assert "均未获得" in results[3].message

    def test_uid_plaintext(self):
        results = assess_backend(_bs(account_uid="1234567890"))
        assert "1234567890" in results[1].message

    def test_version_shown_between_backend_and_account(self):
        version_data = {
            "version": "0.1.0",
            "git": {
                "commit": "4a2b3c1d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
                "commit_short": "4a2b3c1",
                "branch": "main",
                "dirty": False,
                "commit_time": "2026-07-01T10:16:07+08:00",
            },
        }
        results = assess_backend(_bs(version_data=version_data))
        assert results[0].name == "backend 运行状态"
        assert results[1].name == "版本"
        assert "v0.1.0" in results[1].message
        assert "4a2b3c1" in results[1].message
        assert "main" in results[1].message

    def test_version_absent_no_extra_result(self):
        results = assess_backend(_bs(version_data=None))
        names = [r.name for r in results]
        assert "版本" not in names


# ─── _build_version_result ─────────────────────────────────────────────────────


class TestBuildVersionResult:
    def test_none_input(self):
        assert _build_version_result(None) is None

    def test_missing_version_key(self):
        assert _build_version_result({"git": {"commit_short": "abc"}}) is None

    def test_pkg_only(self):
        r = _build_version_result({"version": "1.2.3", "git": None})
        assert r is not None
        assert r.status == Status.PASS
        assert r.message == "v1.2.3"

    def test_pkg_plus_git_dirty(self):
        r = _build_version_result({
            "version": "1.2.3",
            "git": {"commit_short": "abcdef1", "branch": "main", "dirty": True,
                    "commit_time": "2026-01-01T00:00:00+00:00"},
        })
        assert r is not None
        assert "v1.2.3" in r.message
        assert "abcdef1" in r.message
        assert "main" in r.message
        assert "有未提交修改" in r.message
        assert "2026-01-01" in r.message

    def test_pkg_plus_git_clean(self):
        r = _build_version_result({
            "version": "1.2.3",
            "git": {"commit_short": "abcdef1", "branch": "main", "dirty": False},
        })
        assert r is not None
        assert "干净" in r.message

    def test_pkg_plus_git_no_branch(self):
        r = _build_version_result({
            "version": "1.2.3",
            "git": {"commit_short": "abcdef1", "branch": None, "dirty": None},
        })
        assert r is not None
        # 无 branch, 无 dirty 判断 → 只显示 short
        lines = r.message.split("\n")
        assert lines[0] == "v1.2.3"
        assert lines[1].endswith("abcdef1")

    def test_falls_back_to_commit_when_short_missing(self):
        r = _build_version_result({
            "version": "1.2.3",
            "git": {"commit": "abcdef1234567890" + "0" * 24, "commit_short": None},
        })
        assert r is not None
        assert "abcdef1" in r.message  # 取前 7 位

    def test_en_locale(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        r = _build_version_result(
            {"version": "1.2.3",
             "git": {"commit_short": "abcdef1", "branch": "main", "dirty": True}},
            make_translator("en"),
        )
        assert r is not None
        assert r.name == "Version"
        assert "dirty" in r.message


# ─── _probe_udp_send ───────────────────────────────────────────────────────────


class TestProbeUdpSend:
    def test_connect_oserror(self):
        fake_sock = MagicMock()
        fake_sock.connect.side_effect = OSError(101, "Network is unreachable")
        with patch("miloco_cli.commands.doctor.socket.socket", return_value=fake_sock):
            ok, err = _probe_udp_send("10.99.99.99")
        assert ok is False
        assert "101" in err

    def test_send_recv_timeout_pass(self):
        fake_sock = MagicMock()
        fake_sock.recv.side_effect = socket.timeout()
        with patch("miloco_cli.commands.doctor.socket.socket", return_value=fake_sock):
            ok, err = _probe_udp_send("192.168.1.55")
        assert ok is True
        assert err is None

    def test_icmp_port_unreachable_returned(self):
        fake_sock = MagicMock()
        fake_sock.recv.side_effect = ConnectionRefusedError()
        with patch("miloco_cli.commands.doctor.socket.socket", return_value=fake_sock):
            ok, err = _probe_udp_send("192.168.1.55")
        assert ok is True
        assert "ICMP Port Unreachable" in err


# ─── assess_reachability ───────────────────────────────────────────────────────


def _rs(**kw) -> ReachabilityState:
    defaults = dict(
        target_ip="192.168.1.55", target_label="cam1",
        same_subnet=True, same_subnet_iface="eth0",
        route_iface="eth0", route_src="192.168.1.100",
        ping_ok=True, ping_rtt_ms=2.3,
        neigh_state="REACHABLE", neigh_mac="aa:bb:cc:dd:ee:ff",
        udp_send_ok=True, udp_error=None,
    )
    defaults.update(kw)
    return ReachabilityState(**defaults)


class TestAssessReachability:
    def test_all_pass(self):
        results = assess_reachability(_rs())
        assert len(results) == 4
        assert all(r.status == Status.PASS for r in results)
        assert all("cam1 · " in r.name for r in results)

    def test_different_subnet_warn(self):
        results = assess_reachability(_rs(same_subnet=False, same_subnet_iface=None))
        assert results[0].status == Status.WARN

    def test_no_route_warn(self):
        results = assess_reachability(_rs(route_iface=None, route_src=None))
        assert results[1].status == Status.WARN

    def test_route_iface_mismatch_warn(self):
        results = assess_reachability(_rs(route_iface="eth1"))
        assert results[1].status == Status.WARN

    def test_ping_fail_neigh_reachable_warn(self):
        results = assess_reachability(_rs(ping_ok=False, ping_rtt_ms=None,
                                          neigh_state="REACHABLE"))
        assert results[2].status == Status.WARN

    def test_ping_fail_neigh_failed_fail(self):
        results = assess_reachability(_rs(ping_ok=False, ping_rtt_ms=None,
                                          neigh_state="FAILED"))
        assert results[2].status == Status.FAIL

    def test_udp_send_fail_result_fail(self):
        results = assess_reachability(_rs(udp_send_ok=False,
                                          udp_error="Network unreachable (errno=101)"))
        assert results[3].status == Status.FAIL
        assert results[3].fix_hint is not None

    def test_udp_icmp_port_unreachable_pass(self):
        results = assess_reachability(_rs(udp_error="ICMP Port Unreachable"))
        assert results[3].status == Status.PASS

    def test_udp_no_icmp_l3_ok_pass(self):
        results = assess_reachability(_rs(udp_error=None, ping_ok=True,
                                          neigh_state="STALE"))
        assert results[3].status == Status.PASS

    def test_udp_no_icmp_no_l3_warn(self):
        results = assess_reachability(_rs(udp_error=None, ping_ok=False,
                                          neigh_state="FAILED", ping_rtt_ms=None))
        assert results[3].status == Status.WARN


# ─── ping / neigh 解析 ─────────────────────────────────────────────────────────


class TestParseHelpers:
    def test_parse_ping_c_locale(self):
        output = (
            "PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.\n"
            "64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=2.34 ms\n"
        )
        ok, rtt = _parse_ping(output)
        assert ok is True
        assert rtt == 2.34

    def test_parse_ping_fail(self):
        assert _parse_ping("PING failed\n") == (False, None)

    def test_parse_neigh_reachable(self):
        state, mac = _parse_neigh_linux(
            "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
        )
        assert state == "REACHABLE"
        assert mac == "aa:bb:cc:dd:ee:ff"

    def test_parse_neigh_failed(self):
        state, mac = _parse_neigh_linux("192.168.1.99 dev eth0  FAILED\n")
        assert state == "FAILED"
        assert mac is None

    def test_parse_neigh_empty(self):
        assert _parse_neigh_linux("") == (None, None)


# ─── _to_json ──────────────────────────────────────────────────────────────────


class TestToJson:
    def test_schema_complete(self):
        env = _env(platform="linux", is_container=False)
        net = NetworkState(interfaces=[
            _iface("lo", "127.0.0.1", 8),
            _iface("eth0", "192.168.1.100", 24),
            _iface("docker0", "172.17.0.1", 16),
        ])
        backend = _bs()
        results = [
            CheckResult(section="miloco", name="backend 运行状态",
                        status=Status.PASS, message="ok"),
            CheckResult(section="checks", name="ufw 状态",
                        status=Status.PASS, message="ok"),
        ]
        payload = _to_json(env, net, backend, results)
        assert payload["schema_version"] == 1
        assert len(payload["host"]["network_interfaces"]) == 3
        assert payload["host"]["network_interfaces"][0]["is_virtual"] is True
        assert payload["miloco"]["account"]["uid"] == "12345"
        assert payload["miloco"]["home"]["id"] == "h1"
        sections = {c["section"] for c in payload["checks"]}
        assert sections == {"miloco", "checks"}
        assert payload["summary"] == {"pass": 2, "warn": 0, "fail": 0}
        assert payload["exit_code"] == 0

    def test_exit_code_1_on_fail(self):
        env = _env()
        net = NetworkState()
        backend = _bs()
        results = [CheckResult(name="x", status=Status.FAIL, message="fail")]
        payload = _to_json(env, net, backend, results)
        assert payload["exit_code"] == 1


# ─── doctor_cmd 集成 ──────────────────────────────────────────────────────────


class TestDoctorCommand:
    def _patch_all(self, env=None, network=None, backend=None, extra_results=None):
        env = env or _env(platform="linux")
        network = network or NetworkState(interfaces=[_iface("eth0", "192.168.1.100", 24)])
        backend = backend or _bs(cameras=[])
        firewall = extra_results or [CheckResult(name="防火墙", status=Status.PASS,
                                                 message="ok", section="checks")]
        return (
            patch("miloco_cli.commands.doctor.probe_environment", return_value=env),
            patch("miloco_cli.commands.doctor.probe_network", return_value=network),
            patch("miloco_cli.commands.doctor.probe_backend", return_value=backend),
            patch("miloco_cli.commands.doctor.check_firewall", return_value=firewall),
            patch("miloco_cli.commands.doctor.check_container", return_value=[]),
            patch("miloco_cli.commands.doctor.check_wsl", return_value=[]),
        )

    def test_exit_0_all_pass_no_cameras(self, runner):
        patches = self._patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "Miloco 环境诊断" in result.output
        assert "主机环境信息" in result.output
        assert "Miloco 运行状态" in result.output
        assert "检测状态" in result.output

    def test_exit_1_on_fail(self, runner):
        fail_result = [CheckResult(name="ufw UDP 入站", status=Status.FAIL,
                                   message="blocked", section="checks")]
        patches = self._patch_all(extra_results=fail_result)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 1
        assert "❌" in result.output

    def test_invalid_device_ip(self, runner):
        result = runner.invoke(cli, ["doctor", "--device-ip", "not-an-ip"])
        assert result.exit_code != 0
        assert "IPv4" in result.output or "IPv4" in str(result.exception)

    def test_device_ip_reachability_invoked(self, runner):
        patches = self._patch_all()
        called = {}

        def fake_check_reachability(env, ip, label, ifaces, t=None):
            called["ip"] = ip
            called["label"] = label
            return [CheckResult(name=f"{label} · UDP 探测",
                                status=Status.PASS, message="ok", section="checks")]

        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch("miloco_cli.commands.doctor.check_reachability",
                  side_effect=fake_check_reachability),
        ):
            result = runner.invoke(cli, ["doctor", "--device-ip", "192.168.1.55"])
        assert result.exit_code == 0
        assert called == {"ip": "192.168.1.55", "label": "--device-ip"}

    def test_auto_reachability_per_camera(self, runner):
        backend = _bs(cameras=[
            CameraSummary(did="d1", name="cam1", online=True,
                          lan_online=True, local_ip="192.168.1.55"),
            CameraSummary(did="d2", name="cam2", online=True,
                          lan_online=False, local_ip=None),
        ])
        patches = self._patch_all(backend=backend)
        seen = []

        def fake_check_reachability(env, ip, label, ifaces, t=None):
            seen.append((ip, label))
            return []

        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch("miloco_cli.commands.doctor.check_reachability",
                  side_effect=fake_check_reachability),
        ):
            runner.invoke(cli, ["doctor"])
        assert seen == [("192.168.1.55", '摄像头 "cam1"')]

    def test_json_output(self, runner):
        backend = _bs()
        patches = self._patch_all(backend=backend)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5],
            patch("miloco_cli.commands.doctor.check_reachability", return_value=[]),
        ):
            result = runner.invoke(cli, ["doctor", "--json"])
        assert result.exit_code == 0
        # 应只有一行 JSON
        stdout = result.output.strip()
        assert stdout.startswith("{") and stdout.endswith("}")
        payload = json.loads(stdout)
        assert payload["schema_version"] == 1
        assert payload["miloco"]["account"]["uid"] == "12345"

    def test_json_output_with_fail_exit1(self, runner):
        fail_result = [CheckResult(name="ufw UDP 入站", status=Status.FAIL,
                                   message="blocked", section="checks")]
        patches = self._patch_all(extra_results=fail_result)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli, ["doctor", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output.strip())
        assert payload["exit_code"] == 1
        assert payload["summary"]["fail"] == 1


# ─── i18n (--lang) ────────────────────────────────────────────────────────────


class TestI18n:
    def test_translator_zh_default(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        t = make_translator("zh")
        assert t("nic_empty.name") == "IPv4 网卡"
        assert "网段匹配" in t("reach.subnet.match.name")

    def test_translator_en(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        t = make_translator("en")
        assert t("nic_empty.name") == "IPv4 NICs"
        assert t("reach.subnet.match.name") == "Subnet match"
        assert t("cameras.no_lan_ip") == "no LAN IP"

    def test_translator_unknown_lang_falls_back_to_zh(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        t = make_translator("fr")
        assert t("nic_empty.name") == "IPv4 网卡"

    def test_translator_missing_key_returns_key(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        t = make_translator("zh")
        assert t("no.such.key.exists") == "no.such.key.exists"

    def test_translator_format_params(self):
        from miloco_cli.commands.doctor_i18n import make_translator
        t_zh = make_translator("zh")
        t_en = make_translator("en")
        assert "192.168.1.5" in t_zh("entry.invalid_ip", ip="192.168.1.5")
        assert "192.168.1.5" in t_en("entry.invalid_ip", ip="192.168.1.5")

    def test_assess_ufw_en(self):
        state = UfwState(installed=True, enabled_via_conf=True,
                         rules_readable=True, default_deny_incoming=True,
                         has_udp_allow=False)
        from miloco_cli.commands.doctor_i18n import make_translator
        t_en = make_translator("en")
        results = assess_ufw(state, t_en)
        assert len(results) == 1
        assert "UDP" in results[0].name
        assert "PPCS" in results[0].message
        assert "sudo ufw allow" in results[0].fix_hint

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def _patch_all(self):
        env = Environment(platform="linux", is_container=False,
                          container_net=None, distro="Test",
                          kernel="Linux 6.0.0 x86_64")
        net = NetworkState(interfaces=[
            NetworkInterface(name="eth0", ip="192.168.1.10", prefix=24, is_virtual=False),
        ])
        backend = BackendState(url="http://127.0.0.1:1810", reachable=True,
                               error=None, account_bound=True, account_uid="1",
                               home_enabled=True, home_id="h", home_name="Home",
                               cameras=[])
        return (
            patch("miloco_cli.commands.doctor.probe_environment", return_value=env),
            patch("miloco_cli.commands.doctor.probe_network", return_value=net),
            patch("miloco_cli.commands.doctor.probe_backend", return_value=backend),
            patch("miloco_cli.commands.doctor.check_firewall", return_value=[]),
            patch("miloco_cli.commands.doctor.check_container", return_value=[]),
            patch("miloco_cli.commands.doctor.check_wsl", return_value=[]),
        )

    def test_cli_lang_en_output(self, runner):
        patches = self._patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli, ["doctor", "--lang", "en"])
        assert result.exit_code == 0
        assert "Miloco doctor" in result.output
        assert "Host environment" in result.output
        assert "Diagnostics" in result.output

    def test_cli_lang_zh_default(self, runner):
        patches = self._patch_all()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "主机环境信息" in result.output
        assert "检测状态" in result.output

    def test_cli_lang_invalid(self, runner):
        result = runner.invoke(cli, ["doctor", "--lang", "fr"])
        assert result.exit_code != 0
        assert "fr" in result.output.lower() or "invalid" in result.output.lower()

    def test_cli_invalid_ip_en(self, runner):
        result = runner.invoke(cli, ["doctor", "--lang", "en", "--device-ip", "not-an-ip"])
        assert result.exit_code != 0
        assert "not a valid IPv4" in result.output
