"""doctor 命令的文案表 (zh/en)。

约定:
  - key 使用 `.` 分层, 形如 `<section>.<case>.<field>`
  - 变量用 `{name}` 占位, 由 make_translator(lang) 返回的 t(...) 展开
  - 缺失 key 或语言时回退到 zh; 仍缺失则返回 key 本身
"""

from __future__ import annotations

from typing import Callable, Literal

Lang = Literal["zh", "en"]

SUPPORTED_LANGS: tuple[Lang, ...] = ("zh", "en")


_ZH: dict[str, str] = {
    # runtime tags
    "runtime.native": "Native",

    # network empty
    "nic_empty.name": "IPv4 网卡",
    "nic_empty.message": "未检测到可用 IPv4 网卡, 网络未配置或断网",

    # container
    "container.host.name": "容器网络",
    "container.host.message": "容器使用 host 网络, UDP 直通宿主机网卡",
    "container.bridge.name": "容器网络",
    "container.bridge.message": "容器运行在 bridge 网络, 无法接收局域网 UDP 打洞包",
    "container.bridge.fix": (
        "以 host 网络模式重启容器:\n"
        "  docker run --network=host <image>\n"
        "\n"
        "或 docker-compose:\n"
        "  network_mode: host"
    ),
    "container.other.name": "容器网络",
    "container.other.message": "容器网络模式未知, 局域网 UDP 通路不确定",
    "container.other.fix": (
        "推荐改用 host 网络:\n"
        "  docker run --network=host <image>"
    ),

    # ufw
    "ufw.disabled.name": "ufw 状态",
    "ufw.disabled.message": "ufw 未启用 (/etc/ufw/ufw.conf ENABLED=no), 不阻断流量",
    "ufw.unreadable.name": "ufw 状态",
    "ufw.unreadable.message": (
        "ufw 已启用, 但无法读取规则详情。"
        "如需查看详情请以 sudo 运行: sudo ufw status verbose"
    ),
    "ufw.deny.name": "ufw UDP 入站",
    "ufw.deny.message": "ufw 默认拒绝入站流量, PPCS UDP 包会被丢弃",
    "ufw.deny.fix": (
        "允许局域网 UDP 入站 (推荐, 限定子网):\n"
        "  sudo ufw allow from 192.168.0.0/16 proto udp\n"
        "\n"
        "或允许所有 UDP 入站 (宽松):\n"
        "  sudo ufw allow proto udp from any"
    ),
    "ufw.allow.name": "ufw UDP 入站",
    "ufw.allow.message": "ufw 已启用但允许 UDP 入站",

    # firewalld
    "firewalld.state_unreadable.name": "firewalld 状态",
    "firewalld.state_unreadable.message": (
        "firewalld 已安装但无法读取状态。"
        "如需查看请以 sudo 运行: sudo firewall-cmd --state"
    ),
    "firewalld.zone_unreadable.name": "firewalld 状态",
    "firewalld.zone_unreadable.message": (
        "firewalld 运行中, 但无法读取 zone 规则详情。"
        "如需查看请以 sudo 运行: sudo firewall-cmd --list-all"
    ),
    "firewalld.blocked.name": "firewalld UDP 入站",
    "firewalld.blocked.message": "firewalld zone '{zone}' 目标为 {target}, UDP 入站被丢弃",
    "firewalld.accept.name": "firewalld UDP 入站",
    "firewalld.accept.message": "firewalld zone '{zone}' 允许 UDP 流量",
    "firewalld.port_udp_only.name": "firewalld UDP 入站",
    "firewalld.port_udp_only.message": (
        "firewalld zone '{zone}' 仅放行特定端口的 UDP, "
        "PPCS 使用随机高位端口可能被阻断"
    ),
    "firewalld.unclear.name": "firewalld UDP 入站",
    "firewalld.unclear.message": (
        "firewalld zone '{zone}' target 为 default, 未找到显式 UDP 放行规则, "
        "可能阻断 PPCS UDP"
    ),
    "firewalld.fix": (
        "允许局域网 UDP:\n"
        "  sudo firewall-cmd --zone={zone} --add-rich-rule="
        "'rule family=ipv4 source address=192.168.0.0/16 protocol value=udp accept' --permanent\n"
        "  sudo firewall-cmd --reload"
    ),

    # iptables
    "iptables.unreadable.name": "iptables 状态",
    "iptables.unreadable.message": (
        "iptables 已安装但无法读取规则。"
        "如需查看请以 sudo 运行: sudo iptables -L INPUT -n"
    ),
    "iptables.conflict.name": "iptables UDP 入站",
    "iptables.conflict.message": (
        "iptables INPUT 链同时存在 UDP ACCEPT 与 UDP DROP/REJECT 规则, "
        "实际行为取决于规则顺序, 请人工核对"
    ),
    "iptables.conflict.fix": (
        "查看带行号的完整规则:\n"
        "  sudo iptables -L INPUT -nv --line-numbers"
    ),
    "iptables.policy_drop_reason": "iptables INPUT 链默认策略为 DROP 且无 UDP ACCEPT 规则",
    "iptables.explicit_drop_reason": "iptables INPUT 链存在 DROP/REJECT UDP 规则",
    "iptables.blocked.name": "iptables UDP 入站",
    "iptables.blocked.message": "{reason}, PPCS UDP 包会被丢弃",
    "iptables.blocked.fix": (
        "允许局域网 UDP 入站:\n"
        "  sudo iptables -I INPUT -p udp -s 192.168.0.0/16 -j ACCEPT\n"
        "\n"
        "持久化 (Ubuntu/Debian):\n"
        "  sudo apt install iptables-persistent && sudo netfilter-persistent save"
    ),
    "iptables.port_limited.name": "iptables UDP 入站",
    "iptables.port_limited.message": "iptables 仅放行特定端口的 UDP, PPCS 使用随机高位端口可能被阻断",
    "iptables.pass.name": "iptables UDP 入站",
    "iptables.pass.message": "iptables INPUT 链未阻断 UDP 入站",

    # firewall (macos / none)
    "firewall.macos.name": "防火墙 (macOS)",
    "firewall.macos.message": "macOS 默认不阻断 UDP 入站回包, 通常无需配置",
    "firewall.none.name": "防火墙",
    "firewall.none.message": "未检测到 ufw、firewalld 或 iptables, UDP 入站不受防火墙限制",

    # WSL
    "wsl.no_path.name": "WSL 网络模式",
    "wsl.no_path.message": (
        "无法定位 .wslconfig (Windows 用户目录检测失败)。"
        "请手动确认 %USERPROFILE%\\.wslconfig 含 [wsl2] networkingMode=mirrored"
    ),
    "wsl.no_config.name": "WSL 网络模式",
    "wsl.no_config.message": ".wslconfig 不存在 ({path}), 默认 NAT 模式无法接收局域网 UDP",
    "wsl.no_config.fix": (
        "创建 %USERPROFILE%\\.wslconfig:\n"
        "  [wsl2]\n"
        "  networkingMode=mirrored\n"
        "\n"
        "保存后执行: wsl --shutdown && wsl"
    ),
    "wsl.mirrored.name": "WSL 网络模式",
    "wsl.mirrored.message": "已启用镜像网络模式 (networkingMode=mirrored)",
    "wsl.nat.name": "WSL 网络模式",
    "wsl.nat.message": "未启用镜像网络模式, WSL 无法接收宿主机局域网 UDP 包",
    "wsl.nat.fix": (
        "在 Windows 侧编辑 %USERPROFILE%\\.wslconfig:\n"
        "  [wsl2]\n"
        "  networkingMode=mirrored\n"
        "\n"
        "保存后执行: wsl --shutdown && wsl"
    ),
    "hyperv.allow.name": "Hyper-V 防火墙",
    "hyperv.allow.message": "Hyper-V 防火墙 DefaultInboundAction=Allow, UDP 入站已放行",
    "hyperv.block.name": "Hyper-V 防火墙",
    "hyperv.block.message": "Hyper-V 防火墙 DefaultInboundAction=Block, UDP 入站被阻断",
    "hyperv.block.fix": (
        "在 Windows PowerShell (管理员) 执行:\n"
        "  Set-NetFirewallHyperVVMSetting -Name "
        "'{{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}}' "
        "-DefaultInboundAction Allow"
    ),
    "hyperv.unknown.name": "Hyper-V 防火墙",
    "hyperv.unknown.message": (
        "无法检测 Hyper-V 防火墙 (powershell.exe 不可用/无权限/超时)。"
        "如首次运行较慢可重试; 或在 Windows PowerShell (管理员) 手动检查 "
        "Get-NetFirewallHyperVVMSetting 的 DefaultInboundAction"
    ),

    # backend
    "backend.unreachable.name": "backend 运行状态",
    "backend.unreachable.message": "无法连接 backend ({url}): {error}",
    "backend.unreachable.fix": (
        "启动 backend:\n"
        "  miloco-cli service start\n"
        "\n"
        "若已启动仍无法连接, 检查 server.url 配置:\n"
        "  miloco-cli config get server.url"
    ),
    "backend.error.name": "backend 运行状态",
    "backend.error.message": "backend 可达但接口异常 ({url}): {error}",
    "backend.ok.name": "backend 运行状态",
    "backend.ok.message": "backend HTTP 服务运行中 ({url})",
    "version.name": "版本",
    "version.pkg_line": "v{version}",
    "version.git_prefix": "git: ",
    "version.dirty": "有未提交修改",
    "version.clean": "干净",
    "version.commit_time_line": "提交时间: {time}",
    "account.unbound.name": "小米账号绑定",
    "account.unbound.message": "尚未绑定 Xiaomi 账号",
    "account.unbound.fix": "miloco-cli account login",
    "account.bound.name": "小米账号绑定",
    "account.bound.message": "已绑定 Xiaomi 账号 (uid: {uid})",
    "home.none.name": "家庭配置",
    "home.none.message": "账号下无启用的家庭",
    "home.none.fix": (
        "列出并切换家庭:\n"
        "  miloco-cli scope home list\n"
        "  miloco-cli scope home switch <home_id>"
    ),
    "home.ok.name": "家庭配置",
    "home.ok.message": "已启用家庭: {home}",
    "cameras.none.name": "摄像头列表",
    "cameras.none.message": "当前家庭未发现摄像头设备",
    "cameras.no_lan_ip": "未发现 LAN IP",
    "cameras.all_ip.name": "摄像头列表",
    "cameras.all_ip.message": "检测到 {count} 台摄像头:\n{lines}",
    "cameras.all_missing.name": "摄像头列表",
    "cameras.all_missing.message": "发现 {count} 台摄像头但均未获得 LAN IP:\n{lines}",
    "cameras.all_missing.fix": (
        "确认摄像头与本机在同一局域网; "
        "重启 backend 触发 LAN 发现: miloco-cli service restart"
    ),
    "cameras.partial.name": "摄像头列表",
    "cameras.partial.message": "发现 {count} 台摄像头, 部分未获得 LAN IP:\n{lines}",
    "camera.label": '摄像头 "{name}"',

    # reachability
    "reach.name_prefix": "{label} · ",
    "reach.subnet.match.name": "网段匹配",
    "reach.subnet.match.message": "目标 IP {ip} 与本机 {iface} 同网段",
    "reach.subnet.mismatch.name": "网段匹配",
    "reach.subnet.mismatch.message": "目标 IP {ip} 与本机任一网卡均不同网段",
    "reach.subnet.mismatch.fix": (
        "PPCS 打洞跨网段成功率低。若确需跨网段, 请确认:\n"
        "  1. 两个网段之间存在三层可达\n"
        "  2. 路由器/网关允许 UDP 双向转发\n"
        "  3. 摄像头/主机均无静态 ACL 拦截"
    ),
    "reach.route.none.name": "路由出接口",
    "reach.route.none.message": "路由表无法给出到目标的出接口",
    "reach.route.mismatch.name": "路由出接口",
    "reach.route.mismatch.message": (
        "路由走 {route_iface} 但目标与 {subnet_iface} 同网段, 多网卡场景请核对"
    ),
    "reach.route.ok.name": "路由出接口",
    "reach.route.ok.message": "路由走接口 {route_iface}{src_suffix}",
    "reach.route.src_suffix": " (src {src})",
    "reach.l3.ping_ok.name": "L3 可达",
    "reach.l3.ping_ok.message": "ping 成功{rtt_suffix}",
    "reach.l3.ping_ok.rtt_suffix": ", RTT {rtt} ms",
    "reach.l3.arp_ok.name": "L3 可达",
    "reach.l3.arp_ok.message": "ping 未收到回包, 但 ARP 表状态为 {state}, 对端可能仅拦 ICMP",
    "reach.l3.fail.name": "L3 可达",
    "reach.l3.fail.message": "ping 失败, ARP 表状态: {state}",
    "reach.l3.fail.unknown": "未知",
    "reach.udp.iface_suffix_full": " (出接口 {iface}, src {src})",
    "reach.udp.iface_suffix_ip_only": " (src {src})",
    "reach.udp.blocked.name": "UDP 探测",
    "reach.udp.blocked.message": "UDP 无法发出: {error}",
    "reach.udp.blocked.fix": (
        "UDP 出站被本机策略拦截, 请检查:\n"
        "  1. iptables OUTPUT 链: sudo iptables -L OUTPUT -n\n"
        "  2. 容器 seccomp / AppArmor 策略\n"
        "  3. 若 errno=101 (Network unreachable), 说明无路由到目标网段"
    ),
    "reach.udp.port_unreach.name": "UDP 探测",
    "reach.udp.port_unreach.message": "UDP 到达目标主机 (收到 ICMP Port Unreachable, 端口无监听属正常)",
    "reach.udp.pass.name": "UDP 探测",
    "reach.udp.pass.message": "UDP 出站正常, L3/L2 综合可达 (UDP 无 ACK, 无法 100% 确认送达)",
    "reach.udp.warn.name": "UDP 探测",
    "reach.udp.warn.message": (
        "UDP 出站正常, 但 L3/L2 证据不足, 无法确认对端收到 "
        "(UDP 协议限制, 无 delivery confirmation)"
    ),

    # render
    "render.host.title": "主机环境信息",
    "render.host.runtime": "运行时:",
    "render.host.nic": "网卡:",
    "render.host.nic_empty": "(无可用 IPv4 网卡)",
    "render.miloco.title": "Miloco 运行状态",
    "render.checks.title": "检测状态",
    "render.section.empty": "(无输出)",
    "render.fix_hint": "\U0001f4a1 修复建议:",
    "render.summary.empty": "(无检测项)",
    "render.banner": "\U0001fa7a Miloco 环境诊断",

    # entry
    "entry.invalid_ip": "'{ip}' 不是合法的 IPv4 地址",
}


_EN: dict[str, str] = {
    "runtime.native": "Native",

    "nic_empty.name": "IPv4 NICs",
    "nic_empty.message": "No usable IPv4 NIC detected; network not configured or offline",

    "container.host.name": "Container network",
    "container.host.message": "Container uses host network; UDP passes through host NIC directly",
    "container.bridge.name": "Container network",
    "container.bridge.message": "Container runs in bridge network; cannot receive LAN UDP hole-punch packets",
    "container.bridge.fix": (
        "Restart the container in host network mode:\n"
        "  docker run --network=host <image>\n"
        "\n"
        "Or docker-compose:\n"
        "  network_mode: host"
    ),
    "container.other.name": "Container network",
    "container.other.message": "Container network mode unknown; LAN UDP path is uncertain",
    "container.other.fix": (
        "Switching to host network is recommended:\n"
        "  docker run --network=host <image>"
    ),

    "ufw.disabled.name": "ufw status",
    "ufw.disabled.message": "ufw is not enabled (/etc/ufw/ufw.conf ENABLED=no); traffic is not blocked",
    "ufw.unreadable.name": "ufw status",
    "ufw.unreadable.message": (
        "ufw is enabled but rule details are not readable. "
        "Run with sudo to inspect: sudo ufw status verbose"
    ),
    "ufw.deny.name": "ufw UDP inbound",
    "ufw.deny.message": "ufw denies inbound traffic by default; PPCS UDP packets will be dropped",
    "ufw.deny.fix": (
        "Allow LAN UDP inbound (recommended, subnet-scoped):\n"
        "  sudo ufw allow from 192.168.0.0/16 proto udp\n"
        "\n"
        "Or allow all UDP inbound (permissive):\n"
        "  sudo ufw allow proto udp from any"
    ),
    "ufw.allow.name": "ufw UDP inbound",
    "ufw.allow.message": "ufw is enabled but UDP inbound is allowed",

    "firewalld.state_unreadable.name": "firewalld status",
    "firewalld.state_unreadable.message": (
        "firewalld is installed but its status is not readable. "
        "Run with sudo to inspect: sudo firewall-cmd --state"
    ),
    "firewalld.zone_unreadable.name": "firewalld status",
    "firewalld.zone_unreadable.message": (
        "firewalld is running but zone rules are not readable. "
        "Run with sudo to inspect: sudo firewall-cmd --list-all"
    ),
    "firewalld.blocked.name": "firewalld UDP inbound",
    "firewalld.blocked.message": "firewalld zone '{zone}' target is {target}; UDP inbound is dropped",
    "firewalld.accept.name": "firewalld UDP inbound",
    "firewalld.accept.message": "firewalld zone '{zone}' allows UDP traffic",
    "firewalld.port_udp_only.name": "firewalld UDP inbound",
    "firewalld.port_udp_only.message": (
        "firewalld zone '{zone}' only allows UDP on specific ports; "
        "PPCS uses random high ports and may be blocked"
    ),
    "firewalld.unclear.name": "firewalld UDP inbound",
    "firewalld.unclear.message": (
        "firewalld zone '{zone}' target is default with no explicit UDP allow rule; "
        "PPCS UDP may be blocked"
    ),
    "firewalld.fix": (
        "Allow LAN UDP:\n"
        "  sudo firewall-cmd --zone={zone} --add-rich-rule="
        "'rule family=ipv4 source address=192.168.0.0/16 protocol value=udp accept' --permanent\n"
        "  sudo firewall-cmd --reload"
    ),

    "iptables.unreadable.name": "iptables status",
    "iptables.unreadable.message": (
        "iptables is installed but rules are not readable. "
        "Run with sudo to inspect: sudo iptables -L INPUT -n"
    ),
    "iptables.conflict.name": "iptables UDP inbound",
    "iptables.conflict.message": (
        "iptables INPUT chain has both UDP ACCEPT and UDP DROP/REJECT rules; "
        "effective behavior depends on rule order, please verify manually"
    ),
    "iptables.conflict.fix": (
        "Show numbered rules:\n"
        "  sudo iptables -L INPUT -nv --line-numbers"
    ),
    "iptables.policy_drop_reason": "iptables INPUT chain default policy is DROP with no UDP ACCEPT rule",
    "iptables.explicit_drop_reason": "iptables INPUT chain has UDP DROP/REJECT rules",
    "iptables.blocked.name": "iptables UDP inbound",
    "iptables.blocked.message": "{reason}; PPCS UDP packets will be dropped",
    "iptables.blocked.fix": (
        "Allow LAN UDP inbound:\n"
        "  sudo iptables -I INPUT -p udp -s 192.168.0.0/16 -j ACCEPT\n"
        "\n"
        "Persist (Ubuntu/Debian):\n"
        "  sudo apt install iptables-persistent && sudo netfilter-persistent save"
    ),
    "iptables.port_limited.name": "iptables UDP inbound",
    "iptables.port_limited.message": "iptables only allows UDP on specific ports; PPCS random high ports may be blocked",
    "iptables.pass.name": "iptables UDP inbound",
    "iptables.pass.message": "iptables INPUT chain does not block UDP inbound",

    "firewall.macos.name": "Firewall (macOS)",
    "firewall.macos.message": "macOS does not block UDP inbound replies by default; usually no configuration needed",
    "firewall.none.name": "Firewall",
    "firewall.none.message": "No ufw, firewalld or iptables detected; UDP inbound is not restricted by firewall",

    "wsl.no_path.name": "WSL network mode",
    "wsl.no_path.message": (
        "Cannot locate .wslconfig (Windows user profile detection failed). "
        "Please verify %USERPROFILE%\\.wslconfig contains [wsl2] networkingMode=mirrored"
    ),
    "wsl.no_config.name": "WSL network mode",
    "wsl.no_config.message": ".wslconfig does not exist ({path}); default NAT mode cannot receive LAN UDP",
    "wsl.no_config.fix": (
        "Create %USERPROFILE%\\.wslconfig:\n"
        "  [wsl2]\n"
        "  networkingMode=mirrored\n"
        "\n"
        "Then run: wsl --shutdown && wsl"
    ),
    "wsl.mirrored.name": "WSL network mode",
    "wsl.mirrored.message": "Mirrored networking mode enabled (networkingMode=mirrored)",
    "wsl.nat.name": "WSL network mode",
    "wsl.nat.message": "Mirrored networking mode not enabled; WSL cannot receive host LAN UDP packets",
    "wsl.nat.fix": (
        "On Windows side, edit %USERPROFILE%\\.wslconfig:\n"
        "  [wsl2]\n"
        "  networkingMode=mirrored\n"
        "\n"
        "Then run: wsl --shutdown && wsl"
    ),
    "hyperv.allow.name": "Hyper-V firewall",
    "hyperv.allow.message": "Hyper-V firewall DefaultInboundAction=Allow; UDP inbound is allowed",
    "hyperv.block.name": "Hyper-V firewall",
    "hyperv.block.message": "Hyper-V firewall DefaultInboundAction=Block; UDP inbound is blocked",
    "hyperv.block.fix": (
        "Run in Windows PowerShell (Administrator):\n"
        "  Set-NetFirewallHyperVVMSetting -Name "
        "'{{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}}' "
        "-DefaultInboundAction Allow"
    ),
    "hyperv.unknown.name": "Hyper-V firewall",
    "hyperv.unknown.message": (
        "Cannot detect Hyper-V firewall (powershell.exe unavailable / no permission / timed out). "
        "Retry if first run is slow; or check DefaultInboundAction of "
        "Get-NetFirewallHyperVVMSetting manually in Windows PowerShell (Administrator)"
    ),

    "backend.unreachable.name": "backend service",
    "backend.unreachable.message": "Cannot connect to backend ({url}): {error}",
    "backend.unreachable.fix": (
        "Start backend:\n"
        "  miloco-cli service start\n"
        "\n"
        "If already started but still unreachable, check server.url:\n"
        "  miloco-cli config get server.url"
    ),
    "backend.error.name": "backend service",
    "backend.error.message": "backend is reachable but API error ({url}): {error}",
    "backend.ok.name": "backend service",
    "backend.ok.message": "backend HTTP service is running ({url})",
    "version.name": "Version",
    "version.pkg_line": "v{version}",
    "version.git_prefix": "git: ",
    "version.dirty": "dirty",
    "version.clean": "clean",
    "version.commit_time_line": "commit time: {time}",
    "account.unbound.name": "Xiaomi account binding",
    "account.unbound.message": "No Xiaomi account bound yet",
    "account.unbound.fix": "miloco-cli account login",
    "account.bound.name": "Xiaomi account binding",
    "account.bound.message": "Xiaomi account bound (uid: {uid})",
    "home.none.name": "Home configuration",
    "home.none.message": "No active home under this account",
    "home.none.fix": (
        "List and switch home:\n"
        "  miloco-cli scope home list\n"
        "  miloco-cli scope home switch <home_id>"
    ),
    "home.ok.name": "Home configuration",
    "home.ok.message": "Active home: {home}",
    "cameras.none.name": "Camera list",
    "cameras.none.message": "No camera device found in the current home",
    "cameras.no_lan_ip": "no LAN IP",
    "cameras.all_ip.name": "Camera list",
    "cameras.all_ip.message": "Found {count} camera(s):\n{lines}",
    "cameras.all_missing.name": "Camera list",
    "cameras.all_missing.message": "Found {count} camera(s) but none has a LAN IP:\n{lines}",
    "cameras.all_missing.fix": (
        "Ensure cameras are on the same LAN as this host; "
        "restart backend to trigger LAN discovery: miloco-cli service restart"
    ),
    "cameras.partial.name": "Camera list",
    "cameras.partial.message": "Found {count} camera(s), some without a LAN IP:\n{lines}",
    "camera.label": 'camera "{name}"',

    "reach.name_prefix": "{label} · ",
    "reach.subnet.match.name": "Subnet match",
    "reach.subnet.match.message": "Target IP {ip} is on the same subnet as local NIC {iface}",
    "reach.subnet.mismatch.name": "Subnet match",
    "reach.subnet.mismatch.message": "Target IP {ip} is on a different subnet than any local NIC",
    "reach.subnet.mismatch.fix": (
        "PPCS hole punching across subnets has low success rate. If cross-subnet is required, verify:\n"
        "  1. L3 reachability exists between the two subnets\n"
        "  2. Router/gateway allows bidirectional UDP forwarding\n"
        "  3. Neither camera nor host has a static ACL blocking traffic"
    ),
    "reach.route.none.name": "Route egress interface",
    "reach.route.none.message": "Routing table cannot provide an egress interface to the target",
    "reach.route.mismatch.name": "Route egress interface",
    "reach.route.mismatch.message": (
        "Route uses {route_iface} but the target is on the same subnet as {subnet_iface}; "
        "please verify multi-NIC configuration"
    ),
    "reach.route.ok.name": "Route egress interface",
    "reach.route.ok.message": "Route via interface {route_iface}{src_suffix}",
    "reach.route.src_suffix": " (src {src})",
    "reach.l3.ping_ok.name": "L3 reachability",
    "reach.l3.ping_ok.message": "ping succeeded{rtt_suffix}",
    "reach.l3.ping_ok.rtt_suffix": ", RTT {rtt} ms",
    "reach.l3.arp_ok.name": "L3 reachability",
    "reach.l3.arp_ok.message": "ping got no reply, but ARP state is {state}; peer may only filter ICMP",
    "reach.l3.fail.name": "L3 reachability",
    "reach.l3.fail.message": "ping failed, ARP state: {state}",
    "reach.l3.fail.unknown": "unknown",
    "reach.udp.iface_suffix_full": " (via {iface}, src {src})",
    "reach.udp.iface_suffix_ip_only": " (src {src})",
    "reach.udp.blocked.name": "UDP probe",
    "reach.udp.blocked.message": "UDP could not be sent: {error}",
    "reach.udp.blocked.fix": (
        "UDP outbound is blocked by local policy. Check:\n"
        "  1. iptables OUTPUT chain: sudo iptables -L OUTPUT -n\n"
        "  2. Container seccomp / AppArmor policy\n"
        "  3. If errno=101 (Network unreachable), no route to target subnet"
    ),
    "reach.udp.port_unreach.name": "UDP probe",
    "reach.udp.port_unreach.message": "UDP reached target host (ICMP Port Unreachable received; port not listening is normal)",
    "reach.udp.pass.name": "UDP probe",
    "reach.udp.pass.message": "UDP outbound OK; combined L3/L2 reachable (UDP has no ACK, delivery cannot be 100% confirmed)",
    "reach.udp.warn.name": "UDP probe",
    "reach.udp.warn.message": (
        "UDP outbound OK, but L3/L2 evidence is insufficient to confirm delivery to peer "
        "(UDP protocol limitation, no delivery confirmation)"
    ),

    "render.host.title": "Host environment",
    "render.host.runtime": "Runtime:",
    "render.host.nic": "NICs:",
    "render.host.nic_empty": "(no usable IPv4 NIC)",
    "render.miloco.title": "Miloco status",
    "render.checks.title": "Diagnostics",
    "render.section.empty": "(no output)",
    "render.fix_hint": "\U0001f4a1 Suggested fix:",
    "render.summary.empty": "(no checks)",
    "render.banner": "\U0001fa7a Miloco doctor",

    "entry.invalid_ip": "'{ip}' is not a valid IPv4 address",
}


_TABLES: dict[Lang, dict[str, str]] = {"zh": _ZH, "en": _EN}

Translator = Callable[..., str]


def make_translator(lang: str) -> Translator:
    table = _TABLES.get(lang, _ZH) if lang in _TABLES else _ZH  # type: ignore[arg-type]

    def t(key: str, /, **params: object) -> str:
        # 注意: 文案始终过 str.format, 表里任何字面花括号必须写成 {{ }} 转义
        # (如 hyperv.block.fix 的 PowerShell GUID), 占位符仍用单花括号 {name}。
        # 不遵守约定会在该文案被取用时抛 KeyError / ValueError。
        text = table.get(key)
        if text is None:
            text = _ZH.get(key, key)
        return text.format(**params)

    return t
