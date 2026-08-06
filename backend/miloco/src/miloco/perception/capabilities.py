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


def active_window_size_sec() -> int:
    """当前生效那条感知通路的窗口长度(秒)。

    两条通路同一时刻只会开一条,所以窗口不需要各存一份再手动对齐 —— 直接跟着
    当前那条走:切到本地是 12 秒(喂饱 codec 的画布),切回云端自动变回 4 秒
    (它每帧都要付 token 钱,窗口长了直接变贵)。用户不用记得手动改回来。
    """
    try:
        s = get_settings().perception
        if s.engine_backend == "local":
            return int(s.local_vision.window_size)
        return int(s.collect.window_size)
    except Exception:  # noqa: BLE001 —— 读不到就按云端既有默认
        logger.warning("窗口长度解析失败,回落到云端设置", exc_info=True)
        return 4


def perception_executes_device_actions() -> bool:
    """感知层命中规则后是否会**直接**执行设备动作。

    云端通路会(这是既有行为);本地视觉通路不会 —— 它是纯视觉的,没有音频佐证,
    身份也只来自本地 ReID 的相似度比对(阈值从未拿人标定、身份库过期会高置信度
    认错),不该让它自己去关燃气阀。

    注意这**不是**"改由 agent 去执行同一个动作":规则配置好的那些设备动作会被
    直接拒绝执行(记为一次失败触发)。agent 收到的只是"这条规则命中了"这个观察
    结论,不含任何动作 —— 要不要做、做什么,由它自己判断。

    **先问正在跑的那个引擎,配置只作兜底。** 只读配置会留一个真实窗口:切回云端时
    配置先落盘,而随后的软停是 best-effort(失败仅告警)—— 那段时间里配置说 cloud、
    实际仍是本地引擎在感知,闸会放行设备动作,而这正是它存在的理由。引擎取不到
    (manager 还没建好、正在重建)时才回落到配置,那时配置就是用户选定的那条通路。
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
        # **按安全方向回落:不执行设备动作。**
        #
        # 之前这里回落到 True(执行),理由是"按既有行为(云端)处理"。方向是错的:
        # 这是一条**安全不变量**的兜底,而不变量的兜底只能朝拒绝的方向倒。两种误判
        # 的代价完全不对称 —— 误拒只是一次规则没响(而且会记成一次 RULE_TRIGGER_FAILURE,
        # 看得见);误放是让一个纯视觉模型去关燃气阀、开门锁。
        #
        # 这个窗口需要两个独立异常同时发生(引擎属性链读不到 **且** settings 也读不到),
        # 极窄 —— 但"窄"不是把方向定反的理由,而且属性链是五层私有属性,任何一次重构
        # 改了中间名字都会让第一层静默失效。
        #
        # import 也要盖在里面:它冒出去会顺着 _fire 变成触发端点的 500。
        logger.warning("感知能力判定失败,按安全方向处理(不执行设备动作)", exc_info=True)
        return False
