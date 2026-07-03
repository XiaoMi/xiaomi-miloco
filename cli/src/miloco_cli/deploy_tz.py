"""CLI 侧「部署时区」解析——与 backend ``miloco.utils.time_utils.deploy_timezone`` 同序同兜底。

优先级：
1. 显式配置：``MILOCO_TIMEZONE`` env > ``$MILOCO_HOME/config.json`` 顶层 ``timezone``
   （backend ``settings.timezone`` 的同一落盘来源；经 ``load_config`` 合并，env 优先）
2. 系统 IANA 反查（``TZ`` env / ``/etc/timezone`` / ``/etc/localtime`` symlink /
   ``/etc/localtime`` 内容反查——四路与 backend 对齐）
3. 兜底 ``Asia/Shanghai`` + 一次性 warning（维护者裁定，与 backend 同款）：内容反查层
   已把时区配置正确的宿主基本兜住，真落到这里的多是从未配置时区的裸环境——猜沪对
   CN 主体用户群大概率正确；UTC 宿主上 OS 本地偏移 ≈ +0，不猜也无增益。

CLI 不能 import backend utils，此处为对齐副本、且是 CLI 侧唯一真源：time_compute 的
时间锚点与 service 的 supervisord 时区注入都从这里取。第 1 步的 config.json 是关键——
openclaw 网关 spawn 的 CLI 进程 env 里常无 TZ / MILOCO_TIMEZONE 而宿主系统是 Etc/UTC，
不读 config 会把北京家庭的 at 类任务锚点解析成 UTC（#383 遗留的活 bug）。

第 2 步必须拿 IANA 名（而非固定 offset），``ZoneInfo`` 内建 DST 规则才生效；第 3 步
仅在宿主完全不暴露 IANA 身份（四条反查路全失败）时到达。
"""

from __future__ import annotations

import functools
import logging
import os
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_logger = logging.getLogger(__name__)

# 四条系统反查路全失败时的最后兜底（与 backend time_utils 同款、同维护者裁定）：
# 内容反查层已把时区配置正确的宿主基本兜住，此值实际只服务"从未配置时区"的裸环境。
_FALLBACK_TZ = ZoneInfo("Asia/Shanghai")
_warned_no_iana = False

# tzdata 库目录里的非时区文件（内容反查时跳过）；与 backend time_utils 同源。
_TZDB_NON_ZONE_FILES = frozenset({
    "posixrules", "localtime", "leapseconds", "leap-seconds.list",
    "tzdata.zi", "zone.tab", "zone1970.tab", "iso3166.tab", "SECURITY",
})


def _localtime_content_lookup(localtime: Path = Path("/etc/localtime")) -> ZoneInfo | None:
    """``/etc/localtime`` 为普通文件(非 symlink)时,按字节内容反查 zoneinfo 数据库。

    docker bind-mount / ``cp`` 出来的 ``/etc/localtime`` 没有 symlink 目标可读,
    tzlocal 同款思路:与数据库逐一比对(先 size 预筛再比字节)。命中多个别名时取排序后
    优先带 "/" 的规范名(如 Asia/Shanghai 优先于顶层别名 PRC),保证确定性。
    只在 ``_system_iana_tz`` 内调用,结果随其 lru_cache 缓存,全库扫描仅一次。
    （backend time_utils 的逐字副本——CLI 不能 import backend。）
    """
    import zoneinfo

    try:
        if localtime.is_symlink() or not localtime.is_file():
            return None
        data = localtime.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    matches: list[str] = []
    for base in zoneinfo.TZPATH:
        root = Path(base)
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            try:
                if not f.is_file() or f.stat().st_size != len(data):
                    continue
                rel = f.relative_to(root).as_posix()
                # posix/ right/ 是 leap-second 变体目录,不是规范 IANA 名
                if rel.startswith(("posix/", "right/")) or rel in _TZDB_NON_ZONE_FILES:
                    continue
                if f.read_bytes() == data:
                    matches.append(rel)
            except OSError:
                continue
        if matches:
            break
    for name in sorted(matches, key=lambda n: ("/" not in n, n)):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            continue
    return None


@functools.lru_cache(maxsize=1)
def _system_iana_tz() -> ZoneInfo | None:
    """读 ``TZ`` env / ``/etc/timezone`` / ``/etc/localtime`` symlink / 内容反查 → ``ZoneInfo``。

    进程级缓存。返回 ``ZoneInfo`` 而非固定偏移，DST 规则内建生效。全失败返回 ``None``。
    四路顺序与 backend ``time_utils._system_iana_tz`` 对齐。
    """
    if name := os.environ.get("TZ"):
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    p = Path("/etc/timezone")
    if p.is_file():
        try:
            return ZoneInfo(p.read_text().strip())
        except (ZoneInfoNotFoundError, OSError):
            pass
    p = Path("/etc/localtime")
    if p.is_symlink():
        try:
            target = os.readlink(p)
            # rfind:防止 target 路径中其他位置出现 "zoneinfo" 子串切错位置。
            idx = target.rfind("zoneinfo/")
            if idx >= 0:
                return ZoneInfo(target[idx + len("zoneinfo/") :])
        except (ZoneInfoNotFoundError, OSError):
            pass
    # symlink 路读不到(普通文件拷贝,docker 常见)→ 按内容反查兜住
    return _localtime_content_lookup()


def explicit_timezone_name() -> str | None:
    """显式配置的部署时区 IANA 名：``MILOCO_TIMEZONE`` env > config.json ``timezone``。

    两者都没配（或非法）→ ``None``——**不做系统反查、不猜默认**：本函数给「只要显式
    配置」的调用方用（如 service 的 supervisord ``environment=`` 注入——拿不到就不塞，
    让子进程继承宿主 TZ、backend 自身再走系统反查兜底）。``deploy_timezone`` 在此之上
    叠加系统反查与 Asia/Shanghai 兜底。

    容错：config 读失败时退回裸 env；名字非法（非 IANA）warning 后按未配置处理——CLI
    侧宽容降级（backend settings 启动期会对同一字段强校验报错）。
    """
    name: str | None
    try:
        from miloco_cli.config import load_config

        # load_config 已做 env > config.json > 默认值 合并（MILOCO_TIMEZONE → timezone）
        raw = load_config().get("timezone")
        name = raw if isinstance(raw, str) and raw else None
    except Exception:  # noqa: BLE001 —— config 损坏不应拖垮时区解析
        name = os.environ.get("MILOCO_TIMEZONE") or None
    if not name:
        return None
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _logger.warning(
            "configured timezone %r is not a valid IANA name, ignoring", name
        )
        return None
    return name


def deploy_timezone() -> tzinfo:
    """部署时区。优先级:

    1. 显式配置（``MILOCO_TIMEZONE`` env > config.json ``timezone``，与 backend 同源）
    2. 系统 IANA 反查（``TZ`` / ``/etc/timezone`` / ``/etc/localtime`` symlink / 内容反查）
    3. 兜底 ``Asia/Shanghai`` + 一次性 warning（与 backend 同款；内容反查层使此步
       对时区配置正确的宿主基本不可达）
    """
    if name := explicit_timezone_name():
        return ZoneInfo(name)
    if iana := _system_iana_tz():
        return iana
    global _warned_no_iana
    if not _warned_no_iana:
        _logger.warning(
            "Could not detect system IANA timezone; falling back to Asia/Shanghai. "
            "If running outside China, set MILOCO_TIMEZONE or config.json "
            "`timezone` to your IANA zone name (e.g. America/Los_Angeles, "
            "Europe/London)."
        )
        _warned_no_iana = True
    return _FALLBACK_TZ
