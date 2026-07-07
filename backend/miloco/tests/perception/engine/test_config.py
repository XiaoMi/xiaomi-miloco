"""GateConfig hold_duration_sec 默认值与 0 关闭语义。"""

from miloco.perception.engine.config import GateConfig


def test_gate_config_default_hold_duration_sec():
    assert GateConfig().hold_duration_sec == 90.0


def test_gate_config_hold_duration_sec_zero_allowed():
    cfg = GateConfig(hold_duration_sec=0.0)
    assert cfg.hold_duration_sec == 0.0


def test_gate_config_hold_duration_sec_custom():
    cfg = GateConfig(hold_duration_sec=120.0)
    assert cfg.hold_duration_sec == 120.0
