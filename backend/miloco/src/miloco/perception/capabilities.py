"""当前感知通路的能力查询。

只有一个用途:让**不属于感知模块**的代码(规则执行、admin 端点、前端payload)
能问一句"现在这条感知通路会直接控制设备吗",而不必各自去硬编码一份"本地通路
不执行动作"的事实。事实只写在引擎的类属性上,这里负责把它翻译成一个问题的答案。
"""

from __future__ import annotations

import logging

from miloco.config import get_settings

logger = logging.getLogger(__name__)


def _active_engine():
    """取当前正在跑的感知引擎;取不到返回 None(此时回落到读配置)。

    刻意宽容:manager 还没建好、感知服务还没起来都是正常状态,不该让这条查询抛错。
    """
    try:
        from miloco.manager import get_manager

        svc = get_manager().perception_service
        return svc._pipeline._perception_engine_proxy.perception_engine
    except Exception:  # noqa: BLE001
        return None


def perception_executes_device_actions() -> bool:
    """感知层命中规则后是否会**直接**执行设备动作。

    云端通路会(这是既有行为);本地视觉通路不会 —— 它是纯视觉的,没有音频佐证
    也没有身份识别,不该让它自己去关燃气阀。

    注意这**不是**"改由 agent 去执行同一个动作":规则配置好的那些设备动作会被
    直接拒绝执行(记为一次失败触发)。agent 收到的只是"这条规则命中了"这个观察
    结论,不含任何动作 —— 要不要做、做什么,由它自己判断。

    读配置而不是读活引擎:引擎可能正处在重建/等待前置条件的中间态,而这个问题
    在那些时刻同样要有确定的答案。配置就是用户选定的那条通路。
    """
    try:
        # 先问**正在跑的那个引擎**。只读配置的话有一个真实窗口:切回云端时
        # update_shared_config 先落盘,而随后的软停是 best-effort(失败仅告警)——
        # 那段时间里配置说 cloud、实际仍是本地引擎在感知,而闸会放行设备动作。
        engine = _active_engine()
        if engine is not None:
            return bool(getattr(engine, "STATIC_RULE_EXECUTION", True))

        if get_settings().perception.engine_backend != "local":
            return True
        from miloco.perception.local_vision.engine import LocalVisionEngine

        return LocalVisionEngine.STATIC_RULE_EXECUTION
    except Exception:  # noqa: BLE001
        # 读不到配置、或本地视觉模块导入失败时,按既有行为(云端)处理。
        # import 也要盖在里面:它冒出去会顺着 _fire 变成触发端点的 500。
        logger.warning("感知能力判定失败,按云端通路处理", exc_info=True)
        return True
