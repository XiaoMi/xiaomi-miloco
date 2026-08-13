"""跨模块参数关系的护栏。

这些约束的共性是**每个值单独看都合理、错的是关系**：两个数分别定义在不同模块、各自
注释也自洽，中间却隐含一个不等式。破了不崩、只静默换一种行为，而按文件按 diff 做的
review 结构上碰不到。

分两层各管一段，判据共用 ``config_checks.check_cross_module_config``，不会各自漂移：

- 本文件钉的是**随包那一层**（仓里的 ``settings.yaml`` 叠代码默认值），**刻意不读本机
  ``config.json``**。理由是跑测试的人和改配置的人往往不是同一个：读部署配置的话，CI 上
  没有 ``config.json`` 永远取出厂值、这条断言基本恒绿，它想捕捉的"部署与出厂脱钩"恰恰
  在 CI 里不会发生；而在调过参的机器上它会红，且红的原因与那人这次的改动无关。
- 部署现场那一层由建引擎时的告警负责（``client.py`` 调 ``warn_cross_module_config``），
  在真有那份配置的机器上、配置生效的时刻响。
"""

from __future__ import annotations

from typing import Any

import pytest
from miloco.config.settings import _SETTINGS_YAML, _load_yaml_dict
from miloco.perception.engine.config import GateConfig, InputConfig
from miloco.perception.engine.config_checks import check_cross_module_config
from miloco.perception.engine.identity.config_loader import load_identity_engine_config


def _shipped() -> dict[str, Any]:
    """仓里 ``settings.yaml`` 的原始内容 —— 不叠 ``config.json``、不读环境变量。

    直接读那份 yaml 而不是走 ``get_settings()``：后者会合并本机部署配置，见模块说明。
    """
    return _load_yaml_dict(_SETTINGS_YAML)


def _shipped_inputs() -> dict[str, Any]:
    data = _shipped()
    perception = data.get("perception", {}) or {}
    engine_cfg = perception.get("engine", {}) or {}
    collect_cfg = perception.get("collect", {}) or {}
    return {
        "fps": InputConfig(**(engine_cfg.get("input", {}) or {})).fps,
        # 缺省值与 CollectConfig.window_size 的字段默认一致；yaml 里没写这一项时走它。
        "collect_window_sec": collect_cfg.get("window_size", 4),
        "identity_engine": load_identity_engine_config(
            override=engine_cfg.get("identity_engine")
        ),
        "gate": GateConfig(**(engine_cfg.get("gate", {}) or {})),
    }


def _raise_max_age(kwargs: dict[str, Any]) -> None:
    kwargs["identity_engine"].deep_sort.max_age_sec = 99.0


def _widen_reid_skip(kwargs: dict[str, Any]) -> None:
    kwargs["identity_engine"].deep_sort.human_reid_skip_windows = 99


def _disable_hold(kwargs: dict[str, Any]) -> None:
    kwargs["gate"].hold_duration_sec = 0.0


class TestShippedConfigRelationships:
    def test_shipped_config_satisfies_all_relationships(self):
        """随包配置下三条关系全部成立。

        任一条被破坏时这里会红，报出的就是 ``config_checks`` 那份清单里的原话 ——
        包含「破了会怎样」和「两个数各在哪」，不用再去翻代码。
        """
        warnings = check_cross_module_config(**_shipped_inputs())
        assert warnings == [], "随包配置破坏了跨模块关系：\n" + "\n".join(
            f"  · [{w.key}] {w.message}" for w in warnings
        )

    @pytest.mark.parametrize(
        ("mutate", "expected_key"),
        [
            pytest.param(_raise_max_age, "max_age_vs_window", id="存活上限提到窗长以上"),
            pytest.param(_widen_reid_skip, "reid_interval_vs_window", id="ReID 跳窗调大"),
            pytest.param(_disable_hold, "hold_vs_max_age", id="关掉 visual 滞回"),
        ],
    )
    def test_each_relationship_actually_fires(self, mutate, expected_key):
        """逐条验非空转：破坏哪一条，就必须报哪一条。

        没有这组用例的话，上面那条断言在检查函数写错、恒返回空列表时照样绿。
        """
        kwargs = _shipped_inputs()
        mutate(kwargs)
        keys = [w.key for w in check_cross_module_config(**kwargs)]
        assert expected_key in keys, f"破坏了 {expected_key} 却没报，实得 {keys}"

    def test_window_relationship_follows_collect_knob(self):
        """窗长要跟着**采集侧**那个旋钮走。

        引擎侧 ``InputConfig.period_sec`` 既不在 settings.yaml 也不在设置接口里、
        任何 override 都动不了它，取它等于把答案钉回一个常量。真正能改窗长的是
        ``perception.collect.window_size``（设置页那个「时间窗口大小」，采集循环也拿它
        当 tick 周期）。
        """
        kwargs = _shipped_inputs()
        kwargs["collect_window_sec"] = 1  # 设置页可调到的值
        keys = [w.key for w in check_cross_module_config(**kwargs)]
        assert "max_age_vs_window" in keys


class TestDetectorThresholdVsQualityGates:
    """检测器置信阈值 ≥ 各质量门下限。

    两侧都是**代码常量而非配置**，所以不进运行期检查、由这里直接钉。
    """

    def test_main_path_detector_threshold_covers_pool_gate(self):
        import inspect

        from miloco.perception.engine.identity.tier_u import TierUConfig
        from miloco.perception.engine.identity.tracker.detector import Detector

        # 主流程建 Detector 时不传该参数，吃的就是签名上的默认值。
        detector_default = inspect.signature(Detector.__init__).parameters[
            "conf_threshold"
        ].default
        assert detector_default >= TierUConfig().detector_conf_min, (
            "主流程检测器阈值已低于陌生人池的质量门下限，池内会开始静默拒图；"
            "查 tracker/detector.py 的 conf_threshold 默认值与 tier_u.py 的 "
            "detector_conf_min"
        )

    def test_offline_registration_path_has_zero_margin(self):
        """主动注册那条路是**贴着边界**成立的，任一侧再动一点就开始拒图。

        这条用例的作用不是拦住谁，而是把「余量为零」这个事实固定下来 —— 它和主流程
        那条不同，读注释的人很容易以为两条路径一样宽。
        """
        from miloco.perception.engine.identity.extractor import _GATE_DET_CONF_MIN

        offline_detector_conf = 0.4  # person/router.py 建 Detector 时显式传入
        assert offline_detector_conf == _GATE_DET_CONF_MIN, (
            "主动注册路径的检测器阈值与离线抽样质量门不再相等：两者此前贴边成立，"
            "现在要么开始拒图、要么出现了余量，两种情况都该更新 config_checks 里的说明"
        )
