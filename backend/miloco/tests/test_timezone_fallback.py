# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""deploy_timezone() 优先级 + IANA 校验测试。

优先级:settings.timezone (显式配置) > 系统 IANA 反查 (TZ env / /etc/timezone /
/etc/localtime) > 兜底 Asia/Shanghai。第 2 步必须拿 IANA 名,这样 ZoneInfo
内建的 DST 规则才能在跨切换日时生效。
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest


def _reset_settings():
    from miloco.config import reset_settings

    reset_settings()


def _reset_iana_cache():
    """lru_cache 在测试间会污染;每个用例前后清空。

    getattr 防御:fixture 依赖 monkeypatch 后 teardown 先于 monkeypatch 还原执行,
    此刻 _system_iana_tz 可能还是测试替换的裸 lambda(无 cache_clear)。
    """
    from miloco.utils import time_utils

    cache_clear = getattr(time_utils._system_iana_tz, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()
    time_utils._warned_no_iana = False


@pytest.fixture(autouse=True)
def reset_around_each(monkeypatch, tmp_path):
    # 隔离 MILOCO_HOME + 清 MILOCO_TIMEZONE:否则会读到本机真实
    # ~/.openclaw/miloco/config.json 里的 timezone,"未配置"分支的用例在已配置
    # 时区的机器上必红。指向空 tmpdir 保证 hermetic。
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco-home"))
    monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
    _reset_settings()
    _reset_iana_cache()
    yield
    _reset_settings()
    _reset_iana_cache()


def test_default_falls_back_to_system_iana(monkeypatch):
    """settings.timezone 未配 → 走 _system_iana_tz 反查;反查到则用,反查失败兜底。"""
    monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
    _reset_settings()
    _reset_iana_cache()

    from miloco.utils.time_utils import deploy_timezone

    tz = deploy_timezone()
    # CI 容器一般有 /etc/timezone 或 /etc/localtime,_system_iana_tz 拿得到 IANA;
    # 拿不到则兜底 Asia/Shanghai。两种结果都是 ZoneInfo 对象。
    assert isinstance(tz, ZoneInfo)
    from datetime import datetime

    assert datetime.now(tz).utcoffset() is not None


def test_system_iana_reads_tz_env(monkeypatch):
    """_system_iana_tz 优先读 TZ env。"""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    _reset_iana_cache()

    from miloco.utils.time_utils import _system_iana_tz

    tz = _system_iana_tz()
    assert tz == ZoneInfo("America/Los_Angeles")


def test_system_iana_skips_invalid_tz_env(monkeypatch, tmp_path):
    """TZ env 非法时跳到下一级,不抛错。"""
    monkeypatch.setenv("TZ", "Mars/Olympus")
    _reset_iana_cache()

    from miloco.utils.time_utils import _system_iana_tz

    # 非法 TZ 不该抛;能拿到下一级(/etc/timezone)的值或 None
    tz = _system_iana_tz()
    assert tz is None or isinstance(tz, ZoneInfo)
    # 关键:不是 Mars/Olympus
    if tz is not None:
        assert str(tz) != "Mars/Olympus"


def test_fallback_to_os_local_when_no_iana(monkeypatch, caplog):
    """settings.timezone 无 + _system_iana_tz 返回 None → 兜底 OS 本地偏移 + warning。

    (f) 条款回归:旧兜底猜 Asia/Shanghai,把"OS 时钟正确、只是反查不出 IANA 名"的
    非中国宿主强行掰成北京时间;展示路径上 OS 本地偏移严格好于错城市。
    """
    import logging
    from datetime import datetime

    from miloco.utils import time_utils

    monkeypatch.setattr(time_utils, "_system_iana_tz", lambda: None)

    with caplog.at_level(logging.WARNING, logger=time_utils._logger.name):
        tz = time_utils.deploy_timezone()

    # 兜底 == 此刻的 OS 本地偏移(非 Asia/Shanghai 猜测)
    assert datetime.now(tz).utcoffset() == datetime.now().astimezone().utcoffset()
    assert any("OS-local" in r.message for r in caplog.records)


def test_fallback_warning_only_once(monkeypatch, caplog):
    """兜底 warning 在进程内只打一次。"""
    import logging

    from miloco.utils import time_utils

    monkeypatch.setattr(time_utils, "_system_iana_tz", lambda: None)

    with caplog.at_level(logging.WARNING, logger=time_utils._logger.name):
        time_utils.deploy_timezone()
        time_utils.deploy_timezone()
        time_utils.deploy_timezone()

    warn_count = sum(1 for r in caplog.records if "OS-local" in r.message)
    assert warn_count == 1, f"warning 应只打 1 次,实际 {warn_count} 次"


def test_localtime_content_lookup_regular_file(tmp_path):
    """/etc/localtime 为普通文件(docker cp / bind-mount)时按字节反查出 IANA 名。"""
    import zoneinfo
    from pathlib import Path

    from miloco.utils.time_utils import _localtime_content_lookup

    src = None
    for base in zoneinfo.TZPATH:
        cand = Path(base) / "America" / "New_York"
        if cand.is_file():
            src = cand
            break
    if src is None:
        pytest.skip("本机无 zoneinfo 数据库文件,无法构造反查样本")

    fake = tmp_path / "localtime"
    fake.write_bytes(src.read_bytes())
    tz = _localtime_content_lookup(fake)
    assert tz is not None
    # 命中多个别名时优先带 "/" 的规范名 + 字典序,America/New_York 确定胜出
    assert str(tz) == "America/New_York"


def test_localtime_content_lookup_garbage_returns_none(tmp_path):
    """内容不匹配数据库任何 zone → None(继续走下一级兜底,不误报)。"""
    from miloco.utils.time_utils import _localtime_content_lookup

    fake = tmp_path / "localtime"
    fake.write_bytes(b"TZif-not-a-real-zone" * 7)
    assert _localtime_content_lookup(fake) is None


def test_dst_zone_correctly_handled_via_iana(monkeypatch):
    """关键回归:DST 区跨切换日时 ZoneInfo 返回正确偏移,旧固定 offset 实现做不到。"""
    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    _reset_settings()

    from datetime import datetime

    from miloco.utils.time_utils import ms_to_iso_local

    # 6 月 17 日 12:00 UTC → LA PDT -07:00 → 05:00
    ms_jun = int(datetime(2026, 6, 17, 12, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
    # 1 月 1 日 12:00 UTC → LA PST -08:00 → 04:00 (不是 05:00 -07:00,那是 bug)
    ms_jan = int(datetime(2026, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)

    assert ms_to_iso_local(ms_jun).endswith("-07:00"), "6 月应 PDT -07:00"
    assert ms_to_iso_local(ms_jan).endswith("-08:00"), "1 月应 PST -08:00"


def test_settings_timezone_overrides_system(monkeypatch):
    """显式配 settings.timezone=America/Los_Angeles → 返回该 IANA 时区。"""
    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    _reset_settings()

    from miloco.utils.time_utils import deploy_timezone

    tz = deploy_timezone()
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "America/Los_Angeles"


def test_settings_timezone_asia_shanghai(monkeypatch):
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    _reset_settings()

    from miloco.utils.time_utils import deploy_timezone

    tz = deploy_timezone()
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "Asia/Shanghai"

    from datetime import datetime

    # +08:00 offset
    assert datetime(2026, 6, 16, 12, 0, tzinfo=tz).utcoffset().total_seconds() == 8 * 3600


def test_settings_timezone_utc(monkeypatch):
    monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
    _reset_settings()

    from miloco.utils.time_utils import deploy_timezone

    tz = deploy_timezone()
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "UTC"


def test_invalid_iana_name_raises(monkeypatch):
    """非法 IANA 名 → settings 加载时 ValidationError。"""
    monkeypatch.setenv("MILOCO_TIMEZONE", "Mars/Olympus")
    _reset_settings()

    from miloco.config import get_settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        get_settings()


def test_iso_to_ms_naive_uses_deploy_timezone(monkeypatch):
    """naive ISO 字符串按 deploy_timezone() 解读,跨时区表现不同。"""
    from miloco.utils.time_utils import iso_to_ms

    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    _reset_settings()
    # 12:00 in Shanghai = 04:00 UTC
    ms_shanghai = iso_to_ms("2026-06-16T12:00:00")

    monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
    _reset_settings()
    # 12:00 in UTC = 12:00 UTC
    ms_utc = iso_to_ms("2026-06-16T12:00:00")

    assert ms_utc - ms_shanghai == 8 * 3600 * 1000, (
        f"deploy_timezone 切换后 naive 解读偏移应为 8h,实际 {ms_utc - ms_shanghai}ms"
    )


def test_iso_to_ms_aware_string_ignores_deploy_timezone(monkeypatch):
    """aware 字符串带显式时区,deploy_timezone 不影响解析结果。"""
    from miloco.utils.time_utils import iso_to_ms

    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    _reset_settings()
    ms_a = iso_to_ms("2026-06-16T12:00:00+08:00")
    ms_b_z = iso_to_ms("2026-06-16T04:00:00Z")
    assert ms_a == ms_b_z

    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    _reset_settings()
    ms_a2 = iso_to_ms("2026-06-16T12:00:00+08:00")
    assert ms_a == ms_a2


def test_now_iso_returns_local_offset_suffix(monkeypatch):
    """now_iso() 返回部署时区带偏移 ISO,后缀随 deploy_timezone 变化。"""
    import re

    from miloco.utils.time_utils import now_iso

    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
    _reset_settings()
    s = now_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", s), (
        f"Asia/Shanghai 下应返 +08:00 后缀,实际 {s!r}"
    )

    monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
    _reset_settings()
    s2 = now_iso()
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}-0[78]:00$", s2), (
        f"America/Los_Angeles 下应返 -07:00 (PDT) / -08:00 (PST) 后缀,实际 {s2!r}"
    )
