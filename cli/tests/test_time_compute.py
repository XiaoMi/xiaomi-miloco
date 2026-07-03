"""``time-compute`` 单测。

# 测试分层

- **算法快照**(``TestEndOf`` / ``TestTodayTomorrowAt`` / ``TestNextWeekday`` /
  ``TestAdd`` / ``TestDate`` / ``TestDateFull`` / ``TestEdgeCases``):
  module 级 autouse fixture 锁 ``MILOCO_TIMEZONE=Asia/Shanghai``,
  断言完整 ISO 字符串。anchor 计算的"今日/本周/本月"语义本来就 timezone-dependent,
  必须有一个固定基准。
- **跨时区不变量**(``TestCrossTimezone``):显式切 env 验证
  (1) ``add`` 类相对运算 → 不同 tz 下 ms 相等(算法 invariant),后缀不同(显示 varying);
  (2) ``end_of_day`` 类按日运算 → 不同 tz 下"日"定义不同,iso 自然不同。
- **优先级**(``TestDeployTimezone``):只测能可靠断言的——env 显式设置时优先级,
  以及 aware 输入不受 env 影响绝对时刻。系统时区 fallback 依赖 stdlib,不测。
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from miloco_cli.commands.time_compute import compute_anchor, deploy_timezone
from miloco_cli.main import cli


@pytest.fixture(autouse=True)
def _lock_deploy_tz(monkeypatch):
    """所有算法快照测试锁 Asia/Shanghai,定死"日界/月界/周界"语义。"""
    monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")


@pytest.fixture
def runner():
    return CliRunner()


_NOW_2026_06_10 = "2026-06-10T14:30:00+08:00"  # Wednesday


def _iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


# ── compute_anchor 纯函数 ────────────────────────────────────────────────────


class TestEndOf:
    def test_end_of_day(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "end_of_day"})
        assert r == {"ok": True, "iso": "2026-06-10T23:59:59+08:00"}

    def test_end_of_week(self):
        # 2026-06-10 是周三 → 周日 = 06-14
        r = compute_anchor(_NOW_2026_06_10, {"kind": "end_of_week"})
        assert r["iso"] == "2026-06-14T23:59:59+08:00"

    def test_end_of_month(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "end_of_month"})
        assert r["iso"] == "2026-06-30T23:59:59+08:00"


class TestTodayTomorrowAt:
    def test_today_at(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "today_at", "time": "21:00:00"}
        )
        assert r["iso"] == "2026-06-10T21:00:00+08:00"

    def test_tomorrow_at(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "tomorrow_at", "time": "08:30:00"}
        )
        assert r["iso"] == "2026-06-11T08:30:00+08:00"

    def test_invalid_time(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "today_at", "time": "25:00:00"})
        assert r["ok"] is False
        assert r["error"] == "invalid_time"


class TestNextWeekday:
    def test_next_weekday_future(self):
        # 周三 → 下周一 = 06-15
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "next_weekday", "weekday": "monday"}
        )
        assert r["iso"] == "2026-06-15T23:59:59+08:00"

    def test_next_weekday_same_day_goes_next_week(self):
        # 周三 → 下周三(同 weekday → 7 天后)
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "next_weekday", "weekday": "wednesday"}
        )
        assert r["iso"] == "2026-06-17T23:59:59+08:00"

    def test_next_weekday_with_time(self):
        r = compute_anchor(
            _NOW_2026_06_10,
            {"kind": "next_weekday", "weekday": "friday", "time": "10:00:00"},
        )
        assert r["iso"] == "2026-06-12T10:00:00+08:00"

    def test_invalid_weekday(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "next_weekday", "weekday": "funday"}
        )
        assert r["error"] == "invalid_weekday"


class TestAdd:
    def test_add_minutes(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "add", "amount": 30, "unit": "minutes"}
        )
        assert r["iso"] == "2026-06-10T15:00:00+08:00"

    def test_add_hours(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "add", "amount": 5, "unit": "hours"}
        )
        assert r["iso"] == "2026-06-10T19:30:00+08:00"

    def test_add_days(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "add", "amount": 7, "unit": "days"}
        )
        assert r["iso"] == "2026-06-17T14:30:00+08:00"

    def test_add_months_with_clamp(self):
        # 2026-01-31 + 1 month → 2026-02-28(非闰年截断)
        r = compute_anchor(
            "2026-01-31T10:00:00+08:00",
            {"kind": "add", "amount": 1, "unit": "months"},
        )
        assert r["iso"] == "2026-02-28T10:00:00+08:00"

    def test_add_months_leap_year(self):
        # 2024-02-29 + 12 months → 2025-02-28(2025 非闰年)
        r = compute_anchor(
            "2024-02-29T10:00:00+08:00",
            {"kind": "add", "amount": 12, "unit": "months"},
        )
        assert r["iso"] == "2025-02-28T10:00:00+08:00"

    def test_add_invalid_unit(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "add", "amount": 1, "unit": "decades"}
        )
        assert r["error"] == "invalid_unit"

    def test_add_invalid_amount(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "add", "amount": "not_a_number", "unit": "days"}
        )
        assert r["error"] == "invalid_amount"


class TestDate:
    def test_date_future_this_year(self):
        # now 2026-06-10,5/1 已过 → 明年;MM=08 未过 → 今年
        r = compute_anchor(_NOW_2026_06_10, {"kind": "date", "month_day": "08-15"})
        assert r["iso"] == "2026-08-15T23:59:59+08:00"

    def test_date_past_rolls_to_next_year(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "date", "month_day": "01-15"})
        assert r["iso"] == "2027-01-15T23:59:59+08:00"

    def test_date_feb_29_non_leap(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "date", "month_day": "02-29"})
        assert r["iso"].startswith("2027-02-28")

    def test_date_invalid_month_day(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "date", "month_day": "13-01"})
        assert r["error"] == "invalid_month_day"


class TestDateFull:
    def test_date_full(self):
        r = compute_anchor(
            _NOW_2026_06_10,
            {"kind": "date_full", "date": "2027-03-15", "time": "09:00:00"},
        )
        assert r["iso"] == "2027-03-15T09:00:00+08:00"

    def test_date_full_default_end_of_day(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "date_full", "date": "2027-03-15"}
        )
        assert r["iso"] == "2027-03-15T23:59:59+08:00"

    def test_date_full_invalid(self):
        r = compute_anchor(
            _NOW_2026_06_10, {"kind": "date_full", "date": "2027-13-01"}
        )
        assert r["error"] == "invalid_date"


class TestEdgeCases:
    def test_invalid_now(self):
        r = compute_anchor("garbage", {"kind": "end_of_day"})
        assert r["error"] == "invalid_now_iso"

    def test_unknown_kind(self):
        r = compute_anchor(_NOW_2026_06_10, {"kind": "unknown_kind"})
        assert r["error"] == "invalid_anchor"

    def test_naive_now_treated_as_deploy_tz(self):
        """naive now 无时区后缀 → 按 ``deploy_timezone()`` 解读(本 fixture 下 Asia/Shanghai)。"""
        r = compute_anchor("2026-06-10T14:30:00", {"kind": "end_of_day"})
        assert r["iso"] == "2026-06-10T23:59:59+08:00"


# ── CLI 子命令 ───────────────────────────────────────────────────────────────


class TestCli:
    def test_cli_basic(self, runner):
        result = runner.invoke(
            cli,
            [
                "time-compute",
                "--now",
                "2026-06-10T14:30:00+08:00",
                "--anchor",
                '{"kind":"end_of_day"}',
            ],
        )
        assert result.exit_code == 0
        body = json.loads(result.output)
        assert body == {"ok": True, "iso": "2026-06-10T23:59:59+08:00"}

    def test_cli_error_exit_code(self, runner):
        result = runner.invoke(
            cli,
            [
                "time-compute",
                "--now",
                "garbage",
                "--anchor",
                '{"kind":"end_of_day"}',
            ],
        )
        assert result.exit_code == 1

    def test_cli_anchor_invalid_json(self, runner):
        result = runner.invoke(
            cli,
            [
                "time-compute",
                "--now",
                "2026-06-10T14:30:00+08:00",
                "--anchor",
                "{bad",
            ],
        )
        assert result.exit_code == 1


# ── 跨时区不变量 ─────────────────────────────────────────────────────────────


class TestCrossTimezone:
    """同一 aware now,切换 ``MILOCO_TIMEZONE`` 验证算法/显示分层。"""

    def test_add_invariant_across_tz(self, monkeypatch):
        """``add`` 是相对运算,与 deploy_timezone 无关。

        同一 aware now + 同一 add → 不同 tz 下指向同一绝对时刻(ms 相等),
        只是 iso 后缀按 deploy_timezone 渲染。
        """
        anchor = {"kind": "add", "amount": 5, "unit": "hours"}

        monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
        r_sh = compute_anchor(_NOW_2026_06_10, anchor)

        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        r_utc = compute_anchor(_NOW_2026_06_10, anchor)

        monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
        r_la = compute_anchor(_NOW_2026_06_10, anchor)

        assert _iso_to_ms(r_sh["iso"]) == _iso_to_ms(r_utc["iso"]) == _iso_to_ms(r_la["iso"])
        assert r_sh["iso"].endswith("+08:00")
        assert r_utc["iso"].endswith("+00:00")
        assert r_la["iso"].endswith("-07:00")  # 2026-06 LA PDT

    def test_end_of_day_depends_on_tz(self, monkeypatch):
        """``end_of_day`` 的"日"取决于 deploy_timezone。

        ``2026-06-10T14:30:00+08:00`` ≡ ``2026-06-10T06:30:00Z``
        - Asia/Shanghai 视角:今日=06-10 → end = 06-10T23:59:59+08:00
        - UTC 视角:今日=06-10(06:30Z 仍在 06-10) → end = 06-10T23:59:59+00:00
        - America/Los_Angeles 视角(PDT -07:00):06:30Z = 06-09T23:30 PDT,
          今日=06-09 → end = 06-09T23:59:59-07:00
        """
        anchor = {"kind": "end_of_day"}

        monkeypatch.setenv("MILOCO_TIMEZONE", "Asia/Shanghai")
        assert compute_anchor(_NOW_2026_06_10, anchor)["iso"] == "2026-06-10T23:59:59+08:00"

        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        assert compute_anchor(_NOW_2026_06_10, anchor)["iso"] == "2026-06-10T23:59:59+00:00"

        monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
        assert compute_anchor(_NOW_2026_06_10, anchor)["iso"] == "2026-06-09T23:59:59-07:00"


# ── deploy_timezone() 优先级 ─────────────────────────────────────────────────


class TestDeployTimezone:
    """优先级:显式配置（``MILOCO_TIMEZONE`` env > config.json ``timezone``）> 系统 IANA
    反查 > Asia/Shanghai 兜底（维护者裁定,与 backend 同款——内容反查层使兜底对时区
    配置正确的宿主基本不可达）。实现已迁至共享 ``miloco_cli.deploy_tz``
    （time_compute re-export）,config.json 步骤是升级新增——与 backend settings 同源:
    agent exec 环境常无 MILOCO_TIMEZONE 而宿主系统是 Etc/UTC,不读 config 会把北京家庭的
    at 类任务锚点解析成 UTC（#383 遗留活 bug）。

    系统反查必须拿 IANA 名(不是固定 offset),DST 区才不会跨切换日偏 1 小时。
    """

    def _reset_iana_cache(self):
        from miloco_cli import deploy_tz

        deploy_tz._system_iana_tz.cache_clear()
        # warn-once 已改 lru_cache 无参函数,cache_clear 复位
        deploy_tz._warn_no_iana_once.cache_clear()

    def _isolate_home(self, monkeypatch, tmp_path):
        """把 MILOCO_HOME 指到空 tmp,隔离真实 config.json 的 timezone 泄入。"""
        monkeypatch.setenv("MILOCO_HOME", str(tmp_path / "miloco-home"))

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        assert deploy_timezone() == ZoneInfo("UTC")

    def test_env_la(self, monkeypatch):
        monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
        assert deploy_timezone() == ZoneInfo("America/Los_Angeles")

    def test_config_json_timezone_used_when_no_env(self, monkeypatch, tmp_path):
        """env 未设 → 读 $MILOCO_HOME/config.json 顶层 timezone(backend 同源)。

        #383 活 bug 复现面:无 MILOCO_TIMEZONE、宿主 Etc/UTC 的 agent exec 环境下,
        config.json 的 timezone 必须生效,at 类锚点才不会解析成 UTC。
        """
        import json as _json

        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        home = tmp_path / "miloco-home"
        home.mkdir(parents=True)
        (home / "config.json").write_text(
            _json.dumps({"timezone": "Pacific/Marquesas"}), encoding="utf-8"
        )
        monkeypatch.setenv("MILOCO_HOME", str(home))
        self._reset_iana_cache()
        assert deploy_timezone() == ZoneInfo("Pacific/Marquesas")

    def test_env_beats_config_json(self, monkeypatch, tmp_path):
        """MILOCO_TIMEZONE env 优先于 config.json(与 backend pydantic 优先级一致)。"""
        import json as _json

        home = tmp_path / "miloco-home"
        home.mkdir(parents=True)
        (home / "config.json").write_text(
            _json.dumps({"timezone": "Pacific/Marquesas"}), encoding="utf-8"
        )
        monkeypatch.setenv("MILOCO_HOME", str(home))
        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        assert deploy_timezone() == ZoneInfo("UTC")

    def test_invalid_config_timezone_falls_through(self, monkeypatch, tmp_path):
        """config.json timezone 非法 IANA 名 → warning 后按未配置继续(宽容降级),
        绝不把非法名当时区用。"""
        import json as _json

        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        home = tmp_path / "miloco-home"
        home.mkdir(parents=True)
        (home / "config.json").write_text(
            _json.dumps({"timezone": "Mars/Olympus"}), encoding="utf-8"
        )
        monkeypatch.setenv("MILOCO_HOME", str(home))
        self._reset_iana_cache()
        from miloco_cli.deploy_tz import explicit_timezone_name

        assert explicit_timezone_name() is None
        tz = deploy_timezone()  # 落到系统反查/兜底,不抛
        assert tz is not None

    def test_no_env_uses_system_iana_or_fallback(self, monkeypatch, tmp_path):
        """env / config 均无 → 系统 IANA 反查,失败兜底 Asia/Shanghai。结果总是可用 tzinfo。"""
        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        self._isolate_home(monkeypatch, tmp_path)
        self._reset_iana_cache()
        tz = deploy_timezone()
        assert tz is not None

    def test_fallback_to_asia_shanghai_when_no_iana(self, monkeypatch, caplog, tmp_path):
        """env/config 无 + 系统 IANA 反查返回 None → 兜底 Asia/Shanghai + warning。

        维护者裁定(与 backend 同款):宿主完全不可检测时猜沪——「时区配置正确的宿主
        不被掰成错城市」的实际保证由 /etc/localtime 内容反查层承担(能反查出真实
        IANA 名,使本兜底对这类宿主基本不可达);真落到这里的多是从未配置时区的
        裸环境,猜沪对 CN 主体用户群大概率正确。
        """
        import logging

        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        self._isolate_home(monkeypatch, tmp_path)
        self._reset_iana_cache()

        from miloco_cli import deploy_tz

        monkeypatch.setattr(deploy_tz, "_system_iana_tz", lambda: None)

        with caplog.at_level(logging.WARNING, logger=deploy_tz._logger.name):
            tz = deploy_tz.deploy_timezone()

        assert tz == ZoneInfo("Asia/Shanghai")
        assert any("falling back to Asia/Shanghai" in r.message for r in caplog.records)

    def test_fallback_warning_only_once(self, monkeypatch, caplog, tmp_path):
        import logging

        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        self._isolate_home(monkeypatch, tmp_path)
        self._reset_iana_cache()

        from miloco_cli import deploy_tz

        monkeypatch.setattr(deploy_tz, "_system_iana_tz", lambda: None)

        with caplog.at_level(logging.WARNING, logger=deploy_tz._logger.name):
            deploy_tz.deploy_timezone()
            deploy_tz.deploy_timezone()
            deploy_tz.deploy_timezone()

        warn_count = sum(
            1 for r in caplog.records if "falling back to Asia/Shanghai" in r.message
        )
        assert warn_count == 1, f"warning 应只打 1 次,实际 {warn_count} 次"

    def test_system_iana_reads_tz_env(self, monkeypatch):
        """_system_iana_tz 优先读 TZ env。注意 MILOCO_TIMEZONE 与 TZ 是两个不同的 env。"""
        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        self._reset_iana_cache()

        from miloco_cli.deploy_tz import _system_iana_tz

        assert _system_iana_tz() == ZoneInfo("America/Los_Angeles")

    def test_service_resolve_timezone_delegates(self, monkeypatch, tmp_path):
        """service._resolve_timezone 委托 explicit_timezone_name:config 值注入,
        未配置返回 None(不强塞),非法名不注入。"""
        import json as _json

        from miloco_cli.commands.service import _resolve_timezone

        home = tmp_path / "miloco-home"
        home.mkdir(parents=True)
        monkeypatch.setenv("MILOCO_HOME", str(home))
        monkeypatch.delenv("MILOCO_TIMEZONE", raising=False)
        # 未配置 → None
        assert _resolve_timezone() is None
        # config 值 → 注入该名
        (home / "config.json").write_text(
            _json.dumps({"timezone": "Asia/Shanghai"}), encoding="utf-8"
        )
        assert _resolve_timezone() == "Asia/Shanghai"
        # 非法名 → None(比旧实现多一道 IANA 校验)
        (home / "config.json").write_text(
            _json.dumps({"timezone": "Mars/Olympus"}), encoding="utf-8"
        )
        assert _resolve_timezone() is None
        # env 优先
        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        assert _resolve_timezone() == "UTC"

    def test_dst_zone_correctly_handled_via_iana(self, monkeypatch):
        """关键回归:LA 在 1 月应 PST -08:00,7 月应 PDT -07:00。旧固定 offset 实现做不到。"""
        monkeypatch.setenv("MILOCO_TIMEZONE", "America/Los_Angeles")
        # add 1 day 后跨过日界:6 月 17 → 6 月 18,后缀仍是 -07:00 (PDT)
        r_jun = compute_anchor(
            "2026-06-17T12:00:00+00:00",
            {"kind": "add", "amount": 1, "unit": "days"},
        )
        assert r_jun["ok"] and r_jun["iso"].endswith("-07:00"), r_jun

        # 1 月时刻应是 PST -08:00,而非旧实现的固定偏移
        r_jan = compute_anchor(
            "2026-01-01T12:00:00+00:00",
            {"kind": "add", "amount": 1, "unit": "days"},
        )
        assert r_jan["ok"] and r_jan["iso"].endswith("-08:00"), r_jan

    def test_aware_input_ignores_env_for_moment(self, monkeypatch):
        """aware ISO 自带偏移,绝对时刻不受 deploy_timezone 影响,只影响输出后缀。

        ``2026-06-10T14:30:00+08:00`` ≡ UTC 06:30 → UTC 视角下今日仍是 06-10。
        """
        monkeypatch.setenv("MILOCO_TIMEZONE", "UTC")
        r = compute_anchor("2026-06-10T14:30:00+08:00", {"kind": "end_of_day"})
        assert r == {"ok": True, "iso": "2026-06-10T23:59:59+00:00"}
