"""
BasePerceptionEngine — abstract interface for multimodal perception inference.
"""

from abc import ABC, abstractmethod

from miloco.perception.types import (
    BatchedSnapshot,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
)


class BasePerceptionEngine(ABC):
    """Abstract base class for perception engine."""

    @abstractmethod
    async def realtime_perceive(self, batch: BatchedSnapshot, rules: list[dict]) -> RealtimePerceptionResult | None:
        """Realtime perception — batch inference across all devices in one cycle.
        Receives a BatchedSnapshot containing multimodal data from all active
        devices collected within the same cycle window. The implementation can
        reason across devices simultaneously for cross-device scene understanding.
        Args:
            batch: All devices' data grouped by did for this perception cycle.
        Returns:
            RealtimePerceptionResult containing environment descriptions, matched rules, speeches, and suggestions.
        """

    @abstractmethod
    async def on_demand_perceive(self, batch: BatchedSnapshot, query: str) -> OnDemandPerceptionResult | None:
        """Active perception — answer a query using multi-device multimodal data.
        Receives a BatchedSnapshot (one or more devices) plus a natural language
        query. Supports multi-device fusion inference — the implementation can
        reason across all devices in the batch simultaneously.
        Args:
            batch: Devices' multimodal data grouped by did.
            query: Natural language question to answer.
        Returns:
            OnDemandPerceptionResult containing only the answer.
        """

    # ── 生命周期 / 运行期挂载点 ──────────────────────────────────────────
    #
    # 以下方法由 PerceptionEngineProxy 与 PipelineProcessor **无条件**调用
    # (见 client.py 的 set_tierc_frame_provider / apply_omni_fps / set_main_loop
    # 与 processor.py 取帧率处)。此前它们只存在于云端实现上,ABC 没声明 ——
    # 任何第二个实现只要漏掉其中一个,后端就会在启动或首次推理时 AttributeError,
    # 而这在只直接调用两个抽象方法的单测里完全看不出来。
    # 在此给出无害默认实现:需要的实现覆盖它,不需要的(如无身份识别、无 omni
    # 帧率概念的本地通路)直接继承。

    async def close(self) -> None:
        """释放引擎资源。无常驻资源的实现无需覆盖。"""
        return None

    def set_main_loop(self, loop) -> None:  # noqa: ANN001
        """告知引擎主事件循环,供跨线程回调用。无跨线程需求则无需覆盖。"""
        return None

    def set_tierc_frame_provider(self, provider) -> None:  # noqa: ANN001
        """挂载「按 did 取最近一帧」回调(tier_c 身份清理用)。无身份能力则无需覆盖。"""
        return None

    def apply_omni_fps(self, omni_fps: int) -> None:
        """热更新 omni 抽帧率。不消费该配置的实现无需覆盖。"""
        return None

    def get_input_config(self):
        """返回带 ``fps`` / ``omni_fps`` 的输入配置,供面板显示各层帧率。

        返回 None 表示"没有可展示的帧率",调用方(processor)已裹 try/except
        并回落到 0。
        """
        return None
