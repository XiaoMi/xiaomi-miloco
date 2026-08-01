"""当前感知通路的能力查询。

只有一个用途:让**不属于感知模块**的代码(规则执行、admin 端点、前端payload)
能问一句"现在这条感知通路会直接控制设备吗",而不必各自去硬编码一份"本地通路
不执行动作"的事实。事实只写在引擎的类属性上,这里负责把它翻译成一个问题的答案。
"""

from __future__ import annotations

from miloco.config import get_settings


def perception_executes_device_actions() -> bool:
    """感知层命中规则后是否会**直接**执行设备动作。

    云端通路会(这是既有行为);本地视觉通路不会 —— 它是纯视觉的,没有音频佐证
    也没有身份识别,不该让它自己去关燃气阀,判定一律上报给 agent 决策。

    读配置而不是读活引擎:引擎可能正处在重建/等待前置条件的中间态,而这个问题
    在那些时刻同样要有确定的答案。配置就是用户选定的那条通路。
    """
    try:
        if get_settings().perception.engine_backend != "local":
            return True
    except Exception:  # noqa: BLE001 —— 读不到配置时按既有行为(云端)处理
        return True

    from miloco.perception.local_vision.engine import LocalVisionEngine

    return LocalVisionEngine.STATIC_RULE_EXECUTION
