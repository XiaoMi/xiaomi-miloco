"""跨模块参数关系的集中校验。

这里放的是同一类问题：**每个值单独看都合理，错的是它们之间的关系**。两个数分别定义在
不同模块、各自的注释也都自洽，但中间隐含一个不等式；破了不会崩、只会静默换一种行为，
而按文件、按 diff 做的 review 结构上碰不到这种关系。

为什么只 warning 不抛错：这些关系破掉的后果是行为退化(召回变化、状态残留)，不是数据
损坏。把线上拉挂比让人看到一条告警更糟。

为什么同时有用例：告警在**部署现场**响(那里才有真实的 config.json)，用例钉**随包配置**
那一层(确定性，CI 里有意义)。两者共用下面这一个函数，所以判据不可能各自漂移。

新增一个跨模块阈值时，在 ``check_cross_module_config`` 里补一条，并写清三件事：关系是
什么、破了会怎样、两个数各在哪。清单本身比任何一条检查都重要 —— 没有它，下一个加阈值
的人不知道该检查什么。

已覆盖的关系：

1. **单窗帧数 > track 存活上限**（``perception.collect.window_size`` × ``input.fps``
   ↔ ``identity_engine.deep_sort.max_age_sec``）。破了之后，跟丢的 track 可能整窗没有
   新特征入队，漂移自检的证据指纹判据开始真实生效，身份撤回时延随之改变。
2. **单窗帧数 > fast 模式 ReID 重抽间隔**（同上 ↔ ``deep_sort.human_reid_skip_windows``
   等三个旋钮的乘积）。破了之后静止 track 可能整窗复用缓存特征，后果同上。
   两点注意：这一条**随帧率翻转**（重抽间隔是固定帧数、不跟着 ``input.fps`` 缩放）；
   且**只在 ``deep_sort.mode == "fast"`` 时成立**——normal 档每帧真抽 ReID，没有复用
   缓存这个机制，此时不判。
3. **visual 滞回时长 ≥ track 存活上限**（``gate.hold_duration_sec`` ↔ 同上
   ``max_age_sec``）。跟踪器的删除判定只在处理 gate packet 时被求值；滞回时长短于存活
   上限(尤其取 0 = 关闭)时，纯静默窗不再产生 packet，跟丢的 track 不再被回收、连同
   其身份状态一起冻结存活，``max_age_sec`` 失去字面含义。

尚未纳入本函数、但属同一类的关系（**检查前先读这里**）：

- **检测器置信阈值 ≥ 各质量门下限**。两侧都是代码常量而非配置，所以由用例直接钉
  （见 ``tests/perception/engine/identity/test_config_relationships.py``），不在运行期
  重复判。要注意两条路径余量不同：主流程检测器吃类默认值、下限有余量；主动注册那条路
  显式传了与门限**相等**的阈值，是贴着边界成立的，任一侧再动一点就会开始静默拒图。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import GateConfig, IdentityEngineConfig
from .identity._fps_utils import frames_per_window, sec_to_frames

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigWarning:
    """一条被破坏的关系。``key`` 用于测试断言，避免依赖文案。"""

    key: str
    message: str


def check_cross_module_config(
    *,
    fps: int,
    collect_window_sec: float,
    identity_engine: IdentityEngineConfig,
    gate: GateConfig,
) -> list[ConfigWarning]:
    """按上面清单逐条判，返回被破坏的关系（全部成立时返回空列表）。

    纯函数、不打日志，便于用例直接断言；打日志的是 ``warn_cross_module_config``。
    """
    out: list[ConfigWarning] = []

    window_frames = frames_per_window(fps, collect_window_sec)
    max_age_frames = sec_to_frames(identity_engine.deep_sort.max_age_sec, fps)

    if max_age_frames >= window_frames:
        out.append(
            ConfigWarning(
                key="max_age_vs_window",
                message=(
                    f"track 存活上限({max_age_frames} 帧)已不短于单窗帧数"
                    f"({window_frames:.0f} 帧 = fps {fps} × 窗长 {collect_window_sec}s)："
                    "跟丢的 track 可能整窗没有新特征入队，漂移自检的证据指纹判据将真实"
                    "生效，身份撤回时延随之改变。涉及 deep_sort.max_age_sec 与 "
                    "perception.collect.window_size。"
                ),
            )
        )

    # 这一条只在 fast 档才有意义:静止 track 复用缓存特征是 fast 档独有的机制,
    # normal 档每帧都真抽 ReID,报了也是让人去查一个根本跑不到的东西。
    reid_interval = _reid_interval_frames(identity_engine)
    if identity_engine.deep_sort.mode == "fast" and reid_interval >= window_frames:
        out.append(
            ConfigWarning(
                key="reid_interval_vs_window",
                message=(
                    f"fast 模式 ReID 重抽间隔({reid_interval} 帧)已不短于单窗帧数"
                    f"({window_frames:.0f} 帧)：静止 track 可能整窗复用缓存特征，"
                    "后果同上。注意该间隔是固定帧数、不随 input.fps 缩放，调低帧率也会"
                    "触发本条。涉及 deep_sort.human_reid_skip_windows 与 "
                    "perception.collect.window_size。"
                ),
            )
        )

    if gate.hold_duration_sec < identity_engine.deep_sort.max_age_sec:
        out.append(
            ConfigWarning(
                key="hold_vs_max_age",
                message=(
                    f"visual 滞回时长({gate.hold_duration_sec}s)短于 track 存活上限"
                    f"({identity_engine.deep_sort.max_age_sec}s)"
                    + ("（0 = 滞回关闭）" if gate.hold_duration_sec == 0 else "")
                    + "：跟踪器的删除判定只在处理 gate packet 时求值，纯静默窗不再产生"
                    "packet 时，跟丢的 track 与其身份状态会冻结存活、不再被回收，"
                    "max_age_sec 失去字面含义。涉及 gate.hold_duration_sec 与 "
                    "deep_sort.max_age_sec。"
                ),
            )
        )

    return out


def warn_cross_module_config(
    *,
    fps: int,
    collect_window_sec: float,
    identity_engine: IdentityEngineConfig,
    gate: GateConfig,
) -> list[ConfigWarning]:
    """跑一遍检查并把结果打成 warning 日志。建引擎时调用一次。

    本函数**保证不向调用方抛异常**：它是诊断设施，任何自身故障都不该让引擎建不起来
    （见模块说明里「把线上拉挂比让人看到告警更糟」那条）。检查逻辑里用到了跨模块的内部
    实现（下面那个 ReID 间隔要借跟踪器的私有方法算），那边一次与本模块无关的改动就可能
    让这里抛异常 —— 而改跟踪器的人根本不会想到自己动了一个配置校验模块。

    签名写全、不用 ``**kwargs`` 透传：拼错关键字名要在静态检查阶段就暴露，不能拖到运行
    期抛 ``TypeError``，那样又落回同一条不设防的路径上。

    ``check_cross_module_config`` 保持不吞异常 —— 用例走的是它，检查逻辑坏了必须在持续
    集成里红。真正需要自保的只有跑在生产路径上的这一层。
    """
    try:
        warnings = check_cross_module_config(
            fps=fps,
            collect_window_sec=collect_window_sec,
            identity_engine=identity_engine,
            gate=gate,
        )
    except Exception:  # noqa: BLE001 —— 诊断设施不得影响主流程
        logger.exception("[Perception/config] 跨模块参数关系检查自身出错，已跳过")
        return []
    for w in warnings:
        logger.warning("[Perception/config] %s", w.message)
    return warnings


def _reid_interval_frames(identity_engine: IdentityEngineConfig) -> int:
    """按生产口径算 fast 模式的 ReID 重抽间隔。

    不复制公式：构造一份跟踪器配置、调它自己那个方法算。复制一份公式正是「两处声称
    同口径」那类注释腐烂的起点。该方法只读配置，给个桩就能调。
    """
    from types import SimpleNamespace

    from .identity.tracker.config import TrackerConfig
    from .identity.tracker.tracker import MultiObjectTracker

    cfg = TrackerConfig(
        human_reid_skip_windows=identity_engine.deep_sort.human_reid_skip_windows
    )
    return MultiObjectTracker._get_reid_interval(SimpleNamespace(config=cfg))
