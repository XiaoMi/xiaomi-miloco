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
from miloco.config.settings import (
    _SETTINGS_YAML,
    PerceptionCollectSettings,
    _load_yaml_dict,
)
from miloco.perception.engine.config import GateConfig, IdentityConfig, InputConfig
from miloco.perception.engine.config_checks import check_cross_module_config
from miloco.perception.engine.identity.config_loader import load_identity_engine_config
from miloco.perception.engine.omni.provider import adjust_fps_for_omni


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
    inp = InputConfig(**(engine_cfg.get("input", {}) or {}))
    identity_cfg = engine_cfg.get("identity", {}) or {}
    return {
        # 帧率按引擎实际会跑的那一个算：构造引擎时它会被顶成 omni_fps 的整数倍，
        # 用同一个换算函数，别在这里自己推一遍。
        "fps": adjust_fps_for_omni(inp.fps, inp.omni_fps),
        # yaml 里没写这一项时的缺省值**从生产那个模型现读**，不抄一份进来 ——
        # 抄的话它就成了第三处独立取值，而这个文件通篇在反对这件事。
        "collect_window_sec": collect_cfg.get(
            "window_size", PerceptionCollectSettings().window_size
        ),
        "identity_engine": load_identity_engine_config(
            override=engine_cfg.get("identity_engine")
        ),
        "gate": GateConfig(**(engine_cfg.get("gate", {}) or {})),
        "tracking_service_mode": identity_cfg.get(
            "tracking_service_mode", IdentityConfig().tracking_service_mode
        ),
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

    def test_active_tracker_decides_which_max_age_is_read(self):
        """存活上限要读**当前活的那个跟踪器**那一份，两份默认值不同。

        纯 SORT 与 DeepSORT 各有一份 ``max_age_sec``；读错那一份两个方向都会出错——
        该报的不报（纯 SORT 把自己那份调大到窗长以上，却按 DeepSORT 那份算出没事），
        不该报的报（让人去查一个当前根本没生效的旋钮）。
        """
        kwargs = _shipped_inputs()
        # 把纯 SORT 那份调到窗长以上，DeepSORT 那份保持出厂
        kwargs["identity_engine"].sort.max_age_sec = 99.0

        as_deep_sort = [w.key for w in check_cross_module_config(
            **{**kwargs, "tracking_service_mode": "deep_sort"})]
        as_plain_sort = [w.key for w in check_cross_module_config(
            **{**kwargs, "tracking_service_mode": "real"})]

        assert "max_age_vs_window" not in as_deep_sort, "DeepSORT 档不该读 sort 段"
        assert "max_age_vs_window" in as_plain_sort, "纯 SORT 档必须读 sort 段"

    def test_mock_tracker_skips_all_relationships(self):
        """mock 档没有真跟踪器，三条关系都无从谈起，一条都不该报。"""
        kwargs = _shipped_inputs()
        kwargs["identity_engine"].deep_sort.max_age_sec = 99.0
        kwargs["gate"].hold_duration_sec = 0.0
        assert check_cross_module_config(
            **{**kwargs, "tracking_service_mode": "mock"}) == []

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

        两侧都从生产代码现读，**不把值抄进用例**：抄一份的话，有人把离线那条路的阈值
        调走时这里照绿，而它声称固定下来的事实已经破了 —— 那正是本 PR 在别处反复清理的
        「两处声称同口径、实际早已分裂」。
        """
        from miloco.perception.engine.identity.extractor import _GATE_DET_CONF_MIN
        from miloco.perception.engine.identity.tracker.detector import OFFLINE_DET_CONF

        assert OFFLINE_DET_CONF == _GATE_DET_CONF_MIN, (
            "主动注册路径的检测器阈值与离线抽样质量门不再相等：两者此前贴边成立，"
            "现在要么开始拒图、要么出现了余量，两种情况都该更新 config_checks 里的说明"
        )

    def test_offline_call_sites_use_the_named_constant(self):
        """两个离线调用点必须用那个具名常量，不能写任何字面量。

        它们此前是两处独立的 ``0.4``；只要还有一处写死，上一条用例就只是在跟另一个常量
        自我对照、对真实调用点的改动免疫。

        判据是「有没有传字面量」而不是「有没有出现 0.4」：盯住某个具体取值的话，把它改成
        0.3 照样绿 —— 那种护栏守的是字面量、不是不变式，正是本 PR 一路在清理的形状。

        已知盲区：两条断言是「不许有字面量」+「至少有一处用了那个常量」，所以同一模块里
        再出现一个 ``conf_threshold=另一个常量`` 的调用点仍会全绿。堵死要按调用点计数，
        当前两处调用点的规模下不值得。
        """
        import inspect
        import re

        from miloco.person import router
        from miloco.pet import observe

        # 前置否定环视挡住 detector_conf_threshold 这类同形后缀 —— 那是跟踪器配置字段、
        # 与本用例无关，误伤时报出的文案会指向一个并不存在的调用点，把人带偏。
        numeric_arg = re.compile(r"(?<![\w.])conf_threshold\s*=\s*[0-9.]")
        for mod in (router, observe):
            src = inspect.getsource(mod)
            # 只看真正的实参：注释里出现同形文本（例如「历史上这里是 conf_threshold=0.4」）
            # 不算传字面量。
            code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
            assert not numeric_arg.search(code), (
                f"{mod.__name__} 里给 conf_threshold 传了字面量，请改用 OFFLINE_DET_CONF —— "
                "两处离线调用点必须同源，否则「余量为零」那条用例会对它们的改动免疫"
            )
            assert "conf_threshold=OFFLINE_DET_CONF" in src, (
                f"{mod.__name__} 没有在用 OFFLINE_DET_CONF；若这里改了参数写法，"
                "请同步更新本用例的判据"
            )
